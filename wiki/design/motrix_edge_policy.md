# 推理策略客户端（policy）

## 摘要

`policy/` 提供通用的「网络推理客户端」抽象：边缘节点收集 observation 后经它发给推理节点并
取回动作。**传输层与格式契约解耦**，使不同推理策略（openpi / act / 自研）可插拔；由
`get_policy(base_cfg)` 按配置 `policy.type` **懒加载**实例化（依赖第三方包，避免导入
`motrix_edge` 时缺依赖报错）。

## 目标与原则

-   生命周期由 `InferSession` 驱动：`session_start` 时 `connect`，`session_finish` 时 `disconnect`。
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
└── openpi/           # OpenPIClient：按 openpi 契约实现（wire 与服务端完全兼容）
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

动作块逐帧下发：`step(chunk)` 每次只取当前步动作，块耗尽后再请求服务端；单步动作（`[dim]`）
透传不切片；`reset()` 清空缓存。

## OpenPIClient

按 openpi 契约实现，流程与服务端完全兼容（服务端无需改动）：

-   `connect()`：websocket 连接，接收 metadata（含 `action_horizon`）→ 初始化 `ActionChunkBroker`。
-   `infer(obs)`：组装观测 → `transport.request` → 从动作块切片返回单步动作。
-   `reset()`：清空动作块缓存；`disconnect()`：关闭传输。

## 配置（policy 段）

```yaml
policy:
    type: openpi # 策略类型（注册表键；缺省 openpi）
    host: 0.0.0.0 # 推理节点地址
    port: 8765 # 推理节点端口
    api_key: <可选鉴权>
    image_size: [224, 224] # 约定图像尺寸
    image_format: jpeg # jpeg（默认） | uint8
    action_horizon: <可选，服务端 metadata 未提供时使用>
```

## 相关文档

-   推理会话（消费 policy）：[会话（session）](./motrix_edge_session.md)
-   代码入口：`src/motrix_edge/policy/` —— 随 **feat/6**（任务运行时核心）落地
