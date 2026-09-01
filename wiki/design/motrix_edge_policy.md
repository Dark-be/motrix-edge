# 推理策略客户端（policy）

## 摘要

`policy/` 提供通用的「网络推理客户端」抽象：边缘节点收集 observation 后经它发给推理节点并
取回动作。**传输层与格式契约解耦**，使不同推理策略（openpi / act / 自研）可插拔；进入推理
会话时显式选择注册表中的 `policy_type`，由 `get_policy(base_cfg, policy_type=...)`
**懒加载**实例化（依赖第三方包，避免导入 `motrix_edge` 时缺依赖报错）。

## 目标与原则

-   生命周期由 `InferSession` 驱动：**显式 `infer connect` 单次触发 `connect`**，`session_finish` 时 `disconnect`；
    进入推理会话不自动连接。
-   一问一答阻塞式：`infer(obs)` 上传观测 → 返回单步动作；异常返回 `None` 供上层跳过。
-   注册式懒加载：`POLICY_REGISTRY` 登记策略类型，`get_policy()` 选中时才 `import`（连带加载依赖）。

## 包结构

```
policy/
├── __init__.py       # POLICY_REGISTRY + get_policy 工厂 + policy_adapters()
├── base.py           # BasePolicyClient 抽象（connect / infer / reset / disconnect）
├── transport.py      # MsgpackTransport：通用 msgpack-over-websocket 传输（一问一答）
├── msgpack_numpy.py  # numpy 数组安全序列化（msgpack 扩展，对象数组不回落 pickle）
├── contract.py       # 格式契约：消息 key 常量 + build_observation / extract_action / 图像编码
├── broker.py         # ActionChunkBroker：动作块逐帧下发（[horizon, dim] → 单步）
├── openpi/           # OpenPIClient：openpi 默认图像尺寸 224×224
└── act/              # ACTClient：ACT 默认图像尺寸 640×480
```

## BasePolicyClient

最小接口：`connect()`（初始化传输、读取服务端 metadata）、`infer(observation)`（输入观测返回
动作）、`reset()`（清空策略状态，如动作块缓存）、`disconnect()`。子类实现具体策略。

## 传输层（MsgpackTransport）

通用 msgpack-over-websocket 传输（借鉴 openpi-client 的 `WebsocketClientPolicy`）：

-   `connect()`：建立连接并接收服务端首条 **metadata**（单次尝试限时，重试由 session 驱动）。
-   `request(payload)`：发送 msgpack 并阻塞接收响应；服务端以文本回包表示错误。
-   `close()`：关闭连接。可选 `api_key` 鉴权头。

## 格式契约（contract.py）

消息 schema（wire 上 msgpack），**单点定义**：

| 方向                        | 消息                                                                             | 说明                                                                                                                 |
| --------------------------- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 客户端 → 服务端（每步一次） | `{"observations/qpos": ndarray, "observations/images/<name>": ndarray \| bytes}` | 图像统一解码 → 等比缩放补零到 `policy.image_size`（默认 224×224）→ 按 `image_format` 编码 jpeg bytes（默认）或 uint8 |
| 服务端 → 客户端             | `{"action": ndarray}`                                                            | `[horizon, dim]` 动作块（由 `ActionChunkBroker` 逐帧下发）或 `[dim]` 单步；含 `error` 键视为异常                     |

-   `build_observation(qpos, images, image_size, image_format)`：组装观测消息。
-   `extract_action(response)`：抽取 `"action"` 键（`error` 键 → `RuntimeError`）。
-   图像编码：`resize_with_pad` 复刻 openpi 的 `tf.image.resize_with_pad`（等比缩放 + 居中补零）。

适配器 `get_observation()` 输出的键名（`observations/qpos`、`observations/images/*`）即契约格式，
客户端直接透传并重编码图像，无需在会话侧再次组装。

## ActionChunkBroker

动作块逐帧下发，**块耗尽后才由调用方向推理端请求新块**（与 openpi-client 语义一致）：

-   `empty`：当前无可用动作块（True = 调用方需向推理端请求新块并 `feed`）。
-   `feed(chunk)`：存入新动作块（仅在 `empty` 时调用）。
-   `step()`：消耗缓存块的当前步动作（**不触发网络请求**）；单步动作（`[dim]`）透传不切片。
-   `reset()`：清空缓存。

## OpenPIClient 与 ACTClient

`OpenPIClient` 和 `ACTClient` 共享同一 WebSocket + MsgPack 传输与动作块消费流程：

-   `connect()`：websocket 连接，接收 metadata（含 `action_horizon`），保存为客户端
    `server_metadata` 并初始化 `ActionChunkBroker`；连接失败或断开后清空。
-   `infer(obs)`：**仅当动作块耗尽（`broker.empty`）时**组装观测 → `transport.request` 取新动作块；
    其余步骤直接 `broker.step()` 消耗缓存块（一个动作块支撑 horizon 步推理，期间不访问推理端）。
-   `reset()`：清空动作块缓存；`disconnect()`：关闭传输。

两者当前的协议字段相同，差异是默认图像输入尺寸：

| 客户端          | 注册类型  | 默认 `image_size`                  |
| --------------- | --------- | ---------------------------------- |
| `OpenPIClient`  | `openpi`  | `[224, 224]`                       |
| `ACTClient`     | `act`     | `[480, 640]`（图像宽 640、高 480） |

ACT 可通过 `policy.image_size` 覆盖默认值；其余观测键、响应键和 `action_horizon` 约定不变。

> **单臂任务（不再用 `act7dof`）**：原 `ACT7DofClient`（`act7dof`）把「哪条臂 / 怎么映射回
> 14 维双臂空间」的索引配置放在策略层（edge.yml `policy` 段），已删除。单臂任务的臂对应关系由
> **`RobotAdapter` 基类**的 `configure()` 在 **adapter 层**承载（`adapter.enabled_arms` /
> `enabled_cameras` / `home_qpos`）：只启用部分臂时 `action_dim = 启用臂数 × 7`，`execute` 按启用臂数
> 接收动作、未启用臂用 `HOME_QPOS` 填充，`observe()` 只返回启用臂 qpos + 启用相机；策略统一用通用
> `ACTClient`（按启用臂数直通）。详见 [机器人适配器（adapter）](./motrix_edge_adapter.md)。

## 配置（policy 段）

```yaml
policy:
    host: 0.0.0.0 # 推理节点默认地址
    port: 8765 # 推理节点默认端口
```

策略类型、图像尺寸、图像格式和 `action_horizon` 由具体策略客户端的默认值或服务端 metadata
决定；进入推理会话时必须显式选择已注册策略。`infer ip` / `infer port` 仅修改上述默认端点。

> 单臂任务：`policy.type` 用 `act`（通用 ACT，按启用臂数直通）；`enabled_arms` /
> `enabled_cameras` / `home_qpos` 为**运行时配置**（命令 `adapter config set` / 前端
> `POST /v1/adapters/config`，见 [机器人适配器（adapter）](./motrix_edge_adapter.md)）。

## 运行时端点配置（infer ip / infer port）

推理节点地址（`policy.host` / `policy.port`）既可由 `edge.yml`（包内默认 / `MOTRIX_CONFIG_DIR`）静态配置，也可在
运行期经命令总线动态设置（前端推理卡片设置推理端 ip / 端口后，edge 下次启动推理会话生效）：

| 命令                 | 位置参数 | 语义                                                    | 状态可用性 |
| -------------------- | -------- | ------------------------------------------------------- | ---------- |
| `infer ip`           | —        | 查询当前推理节点 IP                                     | 全局       |
| `infer ip set <ip>`  | `ip`     | 设置推理节点 IP（写入内存态 `policy.host`）             | 全局       |
| `infer port`         | —        | 查询当前推理节点端口                                    | 全局       |
| `infer port set <p>` | `port`   | 设置推理节点端口（写入内存态 `policy.port`）            | 全局       |
| `infer connect`      | —        | 单次尝试连接推理节点（推理会话内；成功回执含 metadata） | 会话内     |

-   配置为**内存态**（写入 `base_cfg["policy"]`，不写回 yaml），下次 `session run infer`
    实例化策略客户端时生效；推理会话进行中设置仅对下一会话生效。
-   端点是 Edge 级配置（与节点状态机解耦），任何状态（IDLE / READY / ACTIVE / ERROR）均可用。
-   HTTP 经 `/v1/commands` capability（`infer_ip` / `infer_port` / `infer_ip_set` /
    `infer_port_set`）走同一命令总线，本地 CLI 与前端行为一致；当前配置端点由
    `/v1/infers` status 的 `endpoint` 字段回读。

## 虚拟推理端点（scripts/test_infer_point.py）

无真实推理的模拟 openpi 策略服务端（联调用）：运行在指定 ip / 端口，连接后先下发 metadata
（含 `action_horizon`），每个请求返回一段**有界随机游走**的 action chunk（`[horizon, dim]`），
用于在无真实模型时验证「edge → 推理端」传输契约与 `ActionChunkBroker` 逐帧切片。与 Edge 的
耦合仅限 wire 契约（`contract` / `msgpack_numpy`），可独立运行：:

```
uv run python scripts/test_infer_point.py --host 0.0.0.0 --port 8765 --action-dim 14 --action-horizon 16
```

## 相关文档

-   推理会话（消费 policy）：[会话（session）](./motrix_edge_session.md)
-   命令总线（infer ip/port 命令）：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   代码入口：`src/motrix_edge/policy/` —— 随 **feat/6**（任务运行时核心）落地
