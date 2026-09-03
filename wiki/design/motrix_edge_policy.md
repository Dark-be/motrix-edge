# 推理策略客户端（policy）

## 摘要

`policy/` 提供「网络推理客户端」抽象：边缘节点把 observation 经它发给推理节点并取回动作。
**传输层独立成包（`motrix_edge/transport`）、与具体策略 / lerobot 解耦**；`policy/` 只保留
格式契约与策略特有行为（openpi / act）。进入推理会话时显式选择注册表中的 `policy_type`，
由 `get_policy(base_cfg, policy_type=...)` **懒加载**实例化（避免导入 `motrix_edge` 时因缺
第三方依赖报错）。

策略与 wire 形态（各策略自持动作块缓存，**无通用 broker**）：

| 类型  | 传输            | 消息格式         | 动作语义                          | 动作缓存              |
| ----- | --------------- | ---------------- | --------------------------------- | --------------------- |
| openpi | WebSocket       | msgpack（契约）  | `[horizon, dim]` 动作块逐帧消费   | openpi 自有（块切片） |
| act   | lerobot gRPC    | pickle（lerobot）| 流式 `TimedAction` 按 timestep 消费 | act 自有（timestep→动作） |

## 目标与原则

-   生命周期：连接状态与时机**内聚到 policy**（策略自行管理，不硬编码在会话层）。
    `connect()` 幂等可重连；`ensure_connected()` 惰性（未连则单次限时连接，供 rollout 自动触发）；
    `prepare(obs)` 可选**预热**（act：提前下发策略指令 / 服务端加载模型；openpi：no-op）；
    `session_finish` 时 `disconnect`。进入推理会话不自动连接（首个 rollout 惰性自连）。
-   `infer(obs)` 输入观测返回单步动作；异常返回 `None` 供上层跳过。动作块缓存**策略自有**
    （openpi 块切片 / act timestep 键控），不共享通用缓存器——不同策略的块语义与平滑需求不同。
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
    ├── base.py         # BasePolicyClient 抽象（connect / infer / drain / reset / disconnect）
    ├── contract.py     # 格式契约（openpi wire）：key 常量 + build_observation/extract_action/图像编码
    ├── openpi/         # OpenPIClient（ws + msgpack + 自有块切片缓存）
    └── act/            # ACTClient（lerobot gRPC 流式 + 自有 timestep 缓存 + 时序平滑）
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
（可选预热，默认 no-op；act 覆盖为首次下发策略指令/触发服务端加载）、`infer(observation)`（返回单步动作）、
`drain(observation=None)`（只消费缓存动作块，不发新推理请求；无缓存返回 `None`）、`reset()`（清策略
状态）、`disconnect()`。基类默认无缓存消费逻辑、无连接判断（`connected` 默认 False，子类覆盖）。
动作块缓存为**各策略自有**（见下）。

> 连接语义（旧版由 `InferSession` 维护 `_connected` + 强制先 `infer connect`，act 引入后
> connect 只轻握手、真正就绪=服务端加载模型 → 该硬编码已删除）：策略自行管理连接状态；
> 会话只做编排（rollout 前 `ensure_connected()`；`infer connect` 可选显式预连 + `prepare` 预热）。

## 格式契约（contract.py，openpi wire）

仅 openpi 使用（act 走 lerobot wire，见 act 节）。消息 schema（msgpack）**单点定义**：

| 方向                        | 消息                                                                             | 说明                                                                                                                 |
| --------------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 客户端 → 服务端（每步一次） | `{"observations/qpos": ndarray, "observations/images/<name>": ndarray \| bytes}` | 图像统一解码 → `resize_with_pad` 等比缩放补零到 `policy.image_size`（默认 224×224）→ 按 `image_format` 编码（jpeg 默认） |
| 服务端 → 客户端             | `{"action": ndarray}`                                                            | `[horizon, dim]` 动作块或 `[dim]` 单步；含 `error` 键视为异常                                                         |

`build_observation` / `extract_action` / `resize_with_pad`（复刻 openpi `tf.image.resize_with_pad`）。

## OpenPIClient（ws + msgpack）

-   `connect()`：建 ws 连接，收 metadata（含 `action_horizon`）→ `server_metadata`；失败清理半开连接。
-   **动作块缓存为 openpi 自有**（`_chunk` + `_cursor`）：`[horizon, dim]` 块逐帧切片消费，单步
    `[dim]` 透传；短/长块按实际块长耗尽，不越界、不静默丢弃。
-   `infer(obs)`：**仅当缓存块耗尽时** `build_observation` → `request` 取新块；其余步骤直接消费缓存。
-   `drain`：只消费缓存；`reset`：清块缓存。

## ACTClient（lerobot gRPC 流式）

edge = lerobot `Robot` 侧客户端，与官方 `async_inference/policy_server.py` 互通，**采用 lerobot
原生流式语义**（对照官方 `robot_client.py`）：

-   `connect()`：gRPC channel + `Ready` 握手（服务端 `_reset_server` 清状态）。策略指令
    `SendPolicyInstructions` 延后到首次 `infer`（此时才知 state 维度 / 相机）。
-   wire：观测 `pickle(TimedObservation)` **分块** `SendObservations`（`must_go=True` 强制推理）；
    服务端每 `GetActions` 对队列最新观测推理并**返回整个动作块**（不缓存）；edge `GetActions`
    轮询取回 `pickle(list[TimedAction])` 落入本地缓存按 timestep 消费。服务端无动作缓存
    （详见 act 时序平滑节）。
-   **动作缓存为 act 自有**（`_actions: {timestep: action}`，`_next_timestep` 单调递增、reset 不回退，
    避免服务端按「timestep 已预测」过滤新观测）。
-   图像：edge 侧直接 `resize_with_pad` **letterbox 到 `policy.image_size`（默认 224×224，横向图
    上下留黑边）** 后以 uint8 RGB 上传——服务端 ACT 按 `image_features(224×224)` 处理时 resize
    为 no-op、不变形。
-   `drain`：只消费缓存（`pop(_next_timestep)`）；`infer`：缓存耗尽才请求新块（块内不重复上传）。
-   服务端观测过滤：丢弃「timestep 已预测」或「与上次处理观测过于相似」的观测，除非 `must_go=True`；
    edge 恒置 `must_go=True` 规避。

### ACT 时序平滑（edge 同步重叠 + 加权聚合）

**问题与目标**：edge 同步按需下动作块**不重叠**——块边界处直接从旧块末步跳到新块首步，可能跳变
（机械冲击）。lerobot 官方在**客户端**做时序平滑（服务端每次 `GetActions` 只对最新观测推理并
返回整块、**无动作缓存**，见上「ACTClient」wire；平滑职责在客户端
`robot_client._aggregate_action_queues`），机制 = 让相邻动作块在 timestep 上**重叠**，对重叠步做
**加权平均**（默认 `weighted_average = 0.3*old + 0.7*new`）。edge 在**保持同步按需（无后台线程）**
的前提下实现同一语义。

**原理**：块重叠来自「提前触发推理」。旧块还剩余 $o$ 步未消费时，用当前观测请求下一块
（服务端从当前步预测未来 $K$ 步），新块与旧块在 $[cur, cur+o)$ 重叠 $o$ 步——对这 $o$ 步做
聚合即可抹平边界。$K$=动作块长，$o$=重叠窗口。

**机制（edge 同步版）**：ACTClient 每次 `infer(obs)` 按缓存剩余步决策（剩余 =
`max(_actions)+1 - _next_timestep`，缓存空为 0）：

1.  剩余 **0**（块耗尽）→ 常规请求新块（现状，无重叠）。
2.  剩余 $\in (0, o]$ 且未触发本轮预取 → **同步重叠预取**：上传当前 obs
    （`timestep=cur`、`must_go=True`）→ `GetActions` 取新块 `[cur, cur+K)` → 落缓存时对与已缓存
    重叠的 timestep 做 `aggregate(old, new)` 加权更新（`_store_action_chunk` 升级点）。
3.  剩余 $> o$ → 直接消费 `pop(cur)`。

-   `drain` 只消费缓存（含已平滑的重叠区），**不触发推理**；`reset` 清缓存与预取态，timestep 不回退。
-   **平滑质量**：每 $o$ 步触发一次新决策，重叠窗口 $o$ 步内做 $0.3\cdot\text{old}+0.7\cdot\text{new}$
    式融合；$o$ 越大平滑越强、推理越频繁（服务端推理周期 $=K-o$ 步）。
-   **时序可行性**：预取同步阻塞在触发步至多「一次推理延迟」，因提前 $o$ 步发起，缓存不断流；
    与现「块耗尽时请求」相比单次等待相同、频率更高（$K-o$ 步一次）。若需零卡顿，可把预取等待挪到
    步进间隙或后台线程（可选项，默认同步）。
-   **与服务端交互**：预取观测恒 `must_go=True`，不受 predicted-timestep / 相似过滤丢弃；观测历史
    窗口由服务端策略拼装，edge 低频稀疏上传属既有部署约束。
-   **聚合函数对齐 lerobot** `AGGREGATE_FUNCTIONS`：`weighted_average`(0.3/0.7，默认) /
    `latest_only`(取新) / `average`(0.5/0.5) / `conservative`(0.7/0.3)。
-   平滑关闭：`smooth_overlap=0` → 退化为现状（仅块耗尽才推理，无重叠）。

## 配置（policy 段）

```yaml
policy:
    host: 0.0.0.0 # 推理节点默认地址
    port: 8765 # 推理节点默认端口
    # openpi 专用
    image_size: [224, 224]
    image_format: jpeg
    # act（lerobot gRPC）专用
    pretrained_name_or_path: <ACT checkpoint> # 必填：服务端据此加载策略
    actions_per_chunk: 50 # 动作块长 K
    fps: 30 # 训练/环境频率（动作块时间标定）
    task: "" # 指令（任务描述）随观测上传
    rename_cameras: {} # edge 相机名 → 策略图像特征名重命名
    image_cameras: null # 策略输入相机子集（edge 观测图像名）；缺省全部。多余相机（策略
                        # image_features 没有的）不下发，避免服务端 KeyError
    smooth_overlap: 10 # act 时序平滑重叠窗口（步）；0 = 关闭（edge 侧参数，默认开启）
    aggregate_fn: weighted_average # 重叠聚合：weighted_average/latest_only/average/conservative
```

单臂任务：`policy.type` 用 `act`（通用 ACT，按启用臂数直通）；`enabled_arms` / `enabled_cameras` /
`home_qpos` 为**运行时配置**（见 [机器人适配器（adapter）](./motrix_edge_adapter.md)）。

## 运行时端点配置（infer ip / infer port）

推理节点地址（`policy.host` / `policy.port`）可由 `edge.yml` 静态配置，也可运行期经命令总线动态
设置（前端推理卡片设置后，edge 下次启动推理会话生效）：

| 命令                 | 位置参数 | 语义                                                    | 状态可用性 |
| -------------------- | -------- | ------------------------------------------------------- | ---------- |
| `infer ip`           | —        | 查询当前推理节点 IP                                     | 全局       |
| `infer ip set <ip>`  | `ip`     | 设置推理节点 IP（写入内存态 `policy.host`）             | 全局       |
| `infer port`         | —        | 查询当前推理节点端口                                    | 全局       |
| `infer port set <p>` | `port`   | 设置推理节点端口（写入内存态 `policy.port`）            | 全局       |
| `infer connect`      | —        | 单次尝试连接推理节点（推理会话内；成功回执含 metadata） | 会话内     |

配置为**内存态**（写入 `base_cfg["policy"]`，不写回 yaml），下次 `session run infer` 实例化策略
客户端时生效。端点是 Edge 级配置，任何状态可用；HTTP 经 `/v1/commands` capability 走同一命令总线。

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

-   推理会话（消费 policy，驱动 connect / rollout / drain）：[会话（session）](./motrix_edge_session.md)
-   命令总线（infer ip/port 命令）：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   vendored lerobot 与 transport 包说明：见本文件「包结构」「传输层」；代码入口：
    `src/motrix_edge/policy/`、`src/motrix_edge/transport/`、`src/lerobot/`
