# 推理策略客户端（policy）

## 摘要

`policy/` 提供「网络推理客户端」抽象：边缘节点把 observation 经它发给推理节点并取回动作。
**传输层独立成包（`motrix_edge/transport`）、与具体策略 / lerobot 解耦**；`policy/` 只保留
格式契约与策略特有行为（openpi / act）。进入推理会话时显式选择注册表中的 `policy_type`，
由 `get_policy(base_cfg, policy_type=...)` **懒加载**实例化（避免导入 `motrix_edge` 时因缺
第三方依赖报错）。

策略与 wire 形态（**策略只负责「取一次推理的原始动作块」**；块缓存 / 三元切分 / 时序平滑 /
预取时机统一由 [实时动作块（rtc）](./motrix_edge_rtc.md) 负责）：

| 类型   | 传输         | 消息格式          | 推理结果                          |
| ------ | ------------ | ----------------- | --------------------------------- |
| openpi | WebSocket    | msgpack（契约）   | `[horizon, dim]` 原始动作块       |
| act    | lerobot gRPC | pickle（lerobot） | `TimedAction` 整块（含 timestep） |

## 目标与原则

-   生命周期：连接状态与时机**内聚到 policy**（策略自行管理，不硬编码在会话层）。
    `connect()` 幂等可重连；`ensure_connected()` 惰性（未连则单次限时连接，供 rollout 自动触发）；
    `prepare(obs)` 可选**预热**（act：提前下发策略指令 / 服务端加载模型；openpi：no-op）；
    `session_finish` 时 `disconnect`。进入推理会话不自动连接（首个 rollout 惰性自连）。
-   `infer_chunk(obs, index)` 输入观测返回**一次推理的原始动作块**（`ActionChunk` / ndarray）；
    异常 / 空块返回 `None` 供上层跳过；`index` = 当前绝对步号（流式策略 act 用作
    `TimedObservation.timestep`，openpi 仅回填 `start_index`）。**块缓存 / 三元切分 /
    时序平滑 / 预取时机不属策略职责**（统一在 `motrix_edge.rtc`，见
    [实时动作块（rtc）](./motrix_edge_rtc.md)）。
-   注册式懒加载：`POLICY_REGISTRY` 登记类型，`get_policy()` 选中时才 `import`。

## 包结构

```
src/motrix_edge/
├── transport/          # 通用传输层（与 lerobot/具体策略解耦）
│   ├── __init__.py     # BaseTransport / WsTransport / MsgpackTransport(别名) + get_transport(kind,cfg)
│   ├── base.py         # BaseTransport：connect / close / server_metadata
│   ├── ws.py           # WsTransport：msgpack-over-websocket（一问一答 request）——openpi 用
│   ├── grpc.py         # AsyncInferenceGrpcTransport：channel + stub 封装（Ready 后组合 wire）——act 用
│   └── msgpack_numpy.py# numpy 安全 msgpack 序列化
└── policy/
    ├── __init__.py     # POLICY_REGISTRY + get_policy 工厂 + policy_adapters()
    ├── base.py         # BasePolicyClient 抽象（connect / infer_chunk / reset / disconnect）
    ├── contract.py     # 格式契约（openpi wire）：key 常量 + build_observation/extract_action/图像编码
    ├── openpi/         # OpenPIClient（ws + msgpack：请求一次返回原始动作块）
    └── act/            # ACTClient（lerobot gRPC 流式：按绝对步号取回整块）
```

lerobot 仅作为 **vendored 内置依赖**（`src/lerobot`，Apache-2.0 头保留）提供 wire 最小件：
`transport/`（proto 生成物 + 分块 / pickle 工具）、`async_inference/helpers.py`（wire 数据类
`TimedObservation` / `TimedAction` / `RemotePolicyConfig`）。edge **不引入 `pip lerobot`**，
仅 act 依赖 CPU torch 解析 `torch.Tensor` 动作。

## 传输层（motrix_edge/transport）

按「传输方式」承载、不关心消息格式（序列化契约与策略语义在上层）：

-   `WsTransport`：msgpack-over-websocket。`connect()` 建连并收服务端首条 metadata；`request(payload)`
    发收一问一答；可选 `api_key`。openpi 使用。
-   `AsyncInferenceGrpcTransport`：lerobot AsyncInference 的 channel + stub 封装（insecure channel、
    connect_timeout、幂等 close）。**只做连接管理**；`Ready` / `SendPolicyInstructions` /
    `SendObservations` / `GetActions` 的 **wire 语义由 act 客户端组合**。grpc / pb2 延迟导入。

## BasePolicyClient

最小接口：`connect()`（幂等/可重连：初始化传输、读取服务端 metadata）、`connected`（只读：是否已连）、
`ensure_connected()`（未连则 `connect()`，已连 no-op——**惰性自连**入口）、`prepare(observation=None)`
（可选预热，默认 no-op；act 覆盖为首次下发策略指令/触发服务端加载）、
`infer_chunk(observation, index=None)`（返回**原始动作块**；策略唯一职责）、`bind_adapter(...)`
（绑定 adapter 启用布局：相机名 / qpos 维数）、`reset()`（清策略状态）、`disconnect()`。
基类无连接判断（`connected` 默认 False，子类覆盖）。**块缓存 / 切分 / 平滑不属基类**——见 rtc。

> 连接语义（旧版由 `InferSession` 维护 `_connected` + 强制先 `infer connect`，act 引入后
> connect 只轻握手、真正就绪=服务端加载模型 → 该硬编码已删除）：策略自行管理连接状态；
> 会话只做编排（rollout 前 `ensure_connected()`；`infer connect` 可选显式预连 + `prepare` 预热）。

## 格式契约（contract.py，openpi wire）

仅 openpi 使用（act 走 lerobot wire，见 act 节）。消息 schema（msgpack）**单点定义**：

| 方向                        | 消息                                                                             | 说明                                                                                                                     |
| --------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| 客户端 → 服务端（每步一次） | `{"observations/qpos": ndarray, "observations/images/<name>": ndarray \| bytes}` | 图像统一解码 → `resize_with_pad` 等比缩放补零到 `policy.image_size`（默认 224×224）→ 按 `image_format` 编码（jpeg 默认） |
| 服务端 → 客户端             | `{"action": ndarray}`                                                            | `[horizon, dim]` 动作块或 `[dim]` 单步；含 `error` 键视为异常                                                            |

`build_observation` / `extract_action` / `resize_with_pad`（复刻 openpi `tf.image.resize_with_pad`）。

## OpenPIClient（ws + msgpack）

-   `connect()`：建 ws 连接，收 metadata（含 `action_horizon`）→ `server_metadata`；失败清理半开连接。
-   返回**原始动作块**（`[horizon, dim]` 或单步 `[dim]`）；**不做缓存 / 切片**——块消费由 rtc 负责。
-   `infer_chunk(obs, index)`：`build_observation` → `request` → 返回整块（每次调用都真实请求）。
-   `prepare(obs)`：预热（发一帧观测触发模型加载，丢弃结果）；`reset`：无本地状态（连接保持）。

## ACTClient（lerobot gRPC 流式）

edge = lerobot `Robot` 侧客户端，与官方 `async_inference/policy_server.py` 互通，**采用 lerobot
原生流式语义**（对照官方 `robot_client.py`）：

-   `connect()`：gRPC channel + `Ready` 握手（服务端 `_reset_server` 清状态）。策略指令
    `SendPolicyInstructions` 延后到首次 `infer_chunk`（此时才知 state 维度 / 相机）。
-   wire：观测 `pickle(TimedObservation)` **分块** `SendObservations`（`must_go=True` 强制推理）；
    服务端每 `GetActions` 对队列最新观测推理并**返回整个动作块**（不缓存）；edge `GetActions`
    轮询取回 `pickle(list[TimedAction])`，转成 `ActionChunk`（首步绝对步号）返回。服务端无动作缓存。
-   **无本地动作缓存**：`infer_chunk(obs, index)` 每次上传观测（`timestep=index`）、取回整块；
    块重叠 / 平滑由 rtc 负责（见 [实时动作块（rtc）](./motrix_edge_rtc.md)）。
-   图像：edge 侧直接 `resize_with_pad` **letterbox 到 `policy.image_size`（默认 224×224，横向图
    上下留黑边）** 后以 uint8 RGB 上传——服务端 ACT 按 `image_features(224×224)` 处理时 resize
    为 no-op、不变形。
-   服务端观测过滤：丢弃「timestep 已预测」或「与上次处理观测过于相似」的观测，除非 `must_go=True`；
    edge 恒置 `must_go=True` 规避。

### 时序平滑（移交 rtc）

动作块的**重叠预取 + 加权聚合**（重叠窗口 / 聚合函数 / 关闭开关 / 预取时机）已统一收进
[实时动作块（rtc）](./motrix_edge_rtc.md)：

-   策略侧不再自持 `{timestep: action}` 缓存、不做块内平滑；act 只负责「按绝对步号上传观测 →
    `GetActions` 取回整块」。
-   会话侧 `RTCManager` 负责块队列 / 三元切分（prefix / execution / suffix）/ 重叠加权融合 /
    预取时机；参数经 `policy.rtc` + `infer rtc` 命令 / `POST /v1/infers/rtc` 运行期可查改。

## 配置（policy 段）

```yaml
policy:
    host: 0.0.0.0 # 推理节点默认地址
    port: 8765 # 推理节点默认端口
    type: act # 默认策略类型（会话显式选择时覆盖）
    # 策略配置项（**每个策略有自己的独立配置项**；运行时给定，见「策略配置项」）
    device: cuda # act：服务端推理设备
    actions_per_chunk: 50 # act：动作块长 K（须匹配模型 chunk）
    infer_freq: 10 # 推理会话步进频率（Hz，edge 侧参数）；间隔 = 1/infer_freq
    # RTC（实时动作块：策略只返回原始块；块缓存 / 三元切分 / 时序平滑 / 预取 / prefix 跳过由 rtc 负责）
    rtc:
        enabled: true # 关闭 → 每步一次推理只取块首步（无块缓存 / 无平滑）
        action_horizon: 50 # 块长上限 H（一次推理只取策略块的前 H 步）
        prefix_len: 2 # 前置段 P（推理期间已被执行的前 P 步，跳过）
        execution_horizon: 28 # 执行段 E（缺省 = 实际块长 - P - S）
        suffix_len: 20 # 后缀段 S（与下一块的重叠窗口）
        aggregate_fn: weighted_average # 重叠聚合  # P + E + S = 50 = H
```

### 策略配置项（每个策略独立，前端按 schema 动态渲染）

**每个策略有自己的独立配置项**（prompt / 模型路径 / 设备 / 块长…），外加所有策略共有的**推理端点**
（`host` / `port`）；全部由策略包**静态声明**，CLI 与前端共用同一声明——前端**按所选策略动态渲染
表单**（端点项、会话内锁定项都由 schema 标记，不需写死输入框）：

| 分组                 | 配置项                                                                                             | 适用范围 | 会话内可改       |
| -------------------- | -------------------------------------------------------------------------------------------------- | -------- | ---------------- |
| `endpoint`（公共项） | `host`（推理节点 IP）/ `port`（端口 1-65535）                                                      | 所有策略 | 否（随会话锁定） |
| 策略项               | `openpi`：`prompt`（文本指令，必填）                                                               | openpi   | 是               |
| 策略项               | `act`：`pretrained_name_or_path`（模型路径，**运行时给定**，必填）/ `device` / `actions_per_chunk` | act      | 是               |

声明结构（`policy.POLICY_COMMON_CONFIG_ITEMS` + `policy.POLICY_CONFIG_ITEMS`）：`key` / `label` /
`type`（text / int / bool）/ `required` / `runtime`（`false` = 会话内锁定）/ `group`（`endpoint` = 推理
端点）/ `default` / `placeholder` / `help`；派生便捷标志 `requires_prompt` / `requires_model_path`，
以及 `policy_config_status()` 的 `runtime_keys`（会话内可改键清单）。

推理端点与 `infer ip` / `infer port` **同一写入实现**（`set_policy_endpoint`，非空 host / 端口范围校验），
只是现在也能从策略配置表单 / `infer config set` / `POST /v1/infers` body 的 `config` 一起改。

运行时通道（均写内存态 `policy` 段，**不写回 yaml**；下次 `session run infer` 或当前会话下一请求生效）：

| 命令                      | 位置参数 | 语义                                                       |
| ------------------------- | -------- | ---------------------------------------------------------- |
| `infer config`            | —        | 查询当前策略的配置项（schema + 当前值 + `missing` 缺失项） |
| `infer config set <json>` | `json`   | 按当前策略 schema 白名单校验并设置（可部分）               |
| `infer prompt <text>`     | `text`   | `prompt` 项的快捷命令（语言条件策略）                      |
| `infer model(set) <path>` | `path`   | `pretrained_name_or_path` 项的快捷命令（lerobot 类）       |

未知键 / 类型不符 / 必填为空 → `rejected`（400）。**策略已连接时设置端点（host / port）→
`rejected`（409）**：连接目标不能热改（否则与实际连接不一致），需断开连接 / 退出会话后再改。
端点与其它配置项均为 `runtime=True`（未连接时会话内可改，并同步到运行中的策略客户端：
`policy.set_endpoint` → 传输层重建 URI / gRPC target，下次 `infer connect` 用新地址）。
会话内设置其余项会同时应用到**正在运行的**策略客户端（下一请求生效）；会话外只影响下次进入会话。

### 文本指令（prompt，**仅语言条件策略**）

`prompt` 是**声明了 prompt 配置项的策略**（当前为 openpi）的文本指令：

-   `BasePolicyClient.prompt`（缺省 None；`requires_prompt` 类属性由策略声明）；openpi 每次 infer 请求
    动态携带（服务端每帧重新 tokenize）。
-   会话内经 `infer prompt <text>` 预置；对该类策略 **prompt 为空不能开始推理**（单步 / 持续 rollout 与
    rollout 录制开始均门控拒绝）——录制 rollout 时作为 episode 的 `task_name`（`operator=policy`）。
-   **act 不需要 prompt**（ACT 不接受文本条件）：不门控、不下发；录制 rollout 只同步 `operator=policy`。
-   多种策略共用一条通道：均经 `handle_policy_config` / `set_policy_config` 校验与写入（见上表）。

单臂任务：`policy.type` 用 `act`（通用 ACT，按启用臂数直通）；`enabled_arms` / `enabled_cameras` /
`home_qpos` 为**运行时配置**（见 [机器人适配器（adapter）](./motrix_edge_adapter.md)）。

## 运行时端点配置（infer ip / infer port）

推理节点地址（`policy.host` / `policy.port`）可由 `edge.yml` 静态配置，也可运行期经命令总线动态
设置（前端「策略配置」卡片的端点项，或进入会话时随 `config` 一并写入）：

| 命令                 | 位置参数 | 语义                                                    | 状态可用性     |
| -------------------- | -------- | ------------------------------------------------------- | -------------- |
| `infer ip`           | —        | 查询当前推理节点 IP                                     | 全局           |
| `infer ip set <ip>`  | `ip`     | 设置推理节点 IP（写入内存态 `policy.host`）             | 未连接策略即可 |
| `infer port`         | —        | 查询当前推理节点端口                                    | 全局           |
| `infer port set <p>` | `port`   | 设置推理节点端口（写入内存态 `policy.port`）            | 未连接策略即可 |
| `infer connect`      | —        | 单次尝试连接推理节点（推理会话内；成功回执含 metadata） | 会话内         |

配置为**内存态**（写入 `base_cfg["policy"]`，不写回 yaml），下次 `session run infer` 实例化策略
客户端时生效。端点是 Edge 级配置；HTTP 经 `/v1/commands` capability 走同一命令总线。

端点同时是**策略配置的公共项**（`host` / `port`，见上「策略配置项」）：`infer config` /
`infer config set`（或 `POST /v1/infers` 的 `config`）走 `set_policy_endpoint` 同一校验与写入。
端点与其它配置项**同层级**（未连接时随时可改，含会话内）：会话内改会同步到运行中的策略客户端
（`policy.set_endpoint` → 传输层重建 URI / gRPC target，下次 `infer connect` 用新地址）；
**策略已连接后改为 409**（连接目标不能热改）——断开连接 / 退出会话后再改。

## 虚拟推理端点（scripts/test_infer_point.py）

无真实推理的模拟 **openpi** 策略服务端（联调用）：运行在指定 ip / 端口，连接后先下发 metadata
（含 `action_horizon`），每个请求返回一段**有界随机游走**的 action chunk（`[horizon, dim]`），用于
验证「edge → 推理端」传输契约与 openpi 自有块缓存的逐帧消费。与 Edge 耦合仅限 wire 契约
（`contract` / `transport.msgpack_numpy`），可独立运行：:

```
uv run python scripts/test_infer_point.py --host 0.0.0.0 --port 8765 --action-dim 14 --action-horizon 16
```

act 的联调（fake gRPC 服务端 + 真实 lerobot `policy_server`）见
[act-lerobot-grpc 实施计划](../plan/motrix_edge_policy_act_grpc_plan.md)。

## 相关文档

-   推理会话（消费 policy，驱动 connect / rollout）：[会话（session）](./motrix_edge_session.md)
-   命令总线（infer ip/port 命令）：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   vendored lerobot 与 transport 包说明：见本文件「包结构」「传输层」；代码入口：
    `src/motrix_edge/policy/`、`src/motrix_edge/transport/`、`src/lerobot/`
