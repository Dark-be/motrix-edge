# HTTP 控制面（server）

## 摘要

`server/` 用 FastAPI 暴露 MotrixEdge HTTP 控制面（`/v1/*`）。**web 是 node 进程内的独立线程**：
`CaptureService` / `InferService` / `CommandService` 只绑定「正在运行的 node 实例 + 共享
`CommandBus`」，经 `CommandBus.submit` 提交命令并**同步等待回执**驱动 EdgeNode，**不持有 / 不创建 /
不运行 node**（观测语义唯一实现在 session，web 只做命令提交与状态读取）。

## 目标与原则

-   **单点定义**：任务语义唯一实现在 session / node；server 只做「HTTP 动作 → 命令」翻译 + 状态读取。
-   **命令化驱动**：HTTP 动作经 `submit` 同步等回执（`session run` / `session quit` / `infer rollout`），
    无需轮询节点状态伪造同步。
-   **租约校验**：受控操作（enter / exit / preview / commands / webrtc）须持有 Edge 级活跃租约
    （`X-Lease-Id`，经 `LeaseManager` 校验）；只读操作（status / precheck / health / leases）免租约。
-   **状态校验在 HTTP 层**：提交命令前校验当前状态，非法转移返回 `409`，不污染命令队列。
-   **服务未注入 → 501**：`create_app` 未注入对应 Service 时端点返回 501。

## 端点总览

| 方法            | 路径                                               | 说明                                           | 服务           |
| --------------- | -------------------------------------------------- | ---------------------------------------------- | -------------- |
| GET             | `/v1/health`                                       | 版本 / identity / 已绑定 adapter / 磁盘 / 时钟 | —（内建）      |
| GET             | `/v1/adapters`                                     | 静态列出全部注册适配器（不 discover / 不探活） | —（内建）      |
| POST            | `/v1/commands`                                     | 受控命令（capability 映射，须租约）            | CommandService |
| POST/GET        | `/v1/leases/activate·renew·release` / `/v1/leases` | Edge 级租约                                    | LeaseManager   |
| GET/POST/DELETE | `/v1/captures` + `/v1/captures/precheck`           | 采集会话控制                                   | CaptureService |
| GET/POST/DELETE | `/v1/infers` + `/v1/infers/rollout`                | 推理会话控制                                   | InferService   |
| GET             | `/v1/preview`                                      | 最新观测预览（须租约）                         | CaptureService |
| POST            | `/v1/webrtc/offer`                                 | WebRTC 推流信令（须租约）                      | WebRTCService  |

correlation 中间件：`X-Correlation-Id` 贯穿请求与响应（缺省自动生成）。

## /v1/commands（受控命令）

`POST /v1/commands` 请求体：`command_id` / `lease_id` / `capability` / `params` /
`idempotency_key`。`CommandService.execute` 先校验租约，再按 capability 映射为总线命令：

| capability                  | 总线命令                            | 说明                                |
| --------------------------- | ----------------------------------- | ----------------------------------- |
| `estop`                     | `robot estop`（push）               | 全局急停：安全停止 + 节点转 ERROR   |
| `reset`                     | `node reset`（push）                | 节点复位（ERROR → IDLE）            |
| `robot_reset`               | `robot reset`（push）               | 机器人复位（adapter.reset）         |
| `robot_execute`             | `robot execute`（submit）           | 直接下发 raw 动作（qpos），回执透传 |
| `robot_teleop`              | `robot teleop`（push）              | 遥操作开关（enabled=true/false）    |
| `capture_episode_start/end` | `capture episode start/end`（push） | 开始 / 结束一轮采集                 |
| 其他                        | —（骨架）                           | 预留 Capability 校验 / 具体下发     |

## /v1/captures（采集会话控制）

采集为**观测会话（无回合流程控制）**，端点经 `CaptureService` 桥接：

| 方法   | 路径                     | 租约          | 说明                                                                               |
| ------ | ------------------------ | ------------- | ---------------------------------------------------------------------------------- |
| POST   | `/v1/captures`           | 必需          | `enter`：`session run capture`（READY → ACTIVE，选择 + 启动一步）                  |
| GET    | `/v1/captures`           | 无            | 状态快照：node_state / session / adapter / save_dir / data_files / disk / lease_id |
| GET    | `/v1/captures/precheck`  | 无            | 只读预检：节点 / 会话 / 机器人就绪 + 磁盘 + lease_id / leasable                    |
| DELETE | `/v1/captures?lease_id=` | 必需（query） | `exit`：`session quit`（ACTIVE → READY；**租约不随退出销毁**）                     |
| GET    | `/v1/preview`            | 必需          | 最新观测预览（见 [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)）    |

`POST /v1/captures` 响应：`{status: "accepted", state, lease_id, adapter}`（无请求体，单 adapter 包）。

## /v1/infers（推理会话控制）

推理会话**无回合概念**（enter → 持续推理 → exit，`infer rollout` 步进），端点经 `InferService` 桥接：

| 方法   | 路径                   | 租约          | 说明                                                                |
| ------ | ---------------------- | ------------- | ------------------------------------------------------------------- |
| POST   | `/v1/infers`           | 必需          | `enter`：`session run infer`（可选 body `policy_type`，缺省用配置） |
| GET    | `/v1/infers`           | 无            | 状态快照：node_state / session / adapter / policy / lease_id        |
| POST   | `/v1/infers/rollout`   | 必需          | `infer rollout`：上传观测 → 推理 → 下发动作，返回 action 回执       |
| DELETE | `/v1/infers?lease_id=` | 必需（query） | `exit`：`session quit`（ACTIVE → READY）                            |

## 错误语义

| 状态码 | 含义                                                                       |
| ------ | -------------------------------------------------------------------------- |
| `409`  | 非法状态转移（已在会话再 enter / 未在会话 exit / 节点未就绪 / 无活跃租约） |
| `403`  | `X-Lease-Id` 缺失或不匹配（异租约）                                        |
| `410`  | 租约已过期                                                                 |
| `501`  | 服务未注入（create_app 未启用对应模块）                                    |
| `500`  | 内部异常                                                                   |

## 相关文档

-   命令模型与传输：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   会话语义：[会话（session）](./motrix_edge_session.md)
-   租约：[Edge 级租约（lease）](./motrix_edge_lease.md)
-   WebRTC 推流：[FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   身份上报：[设备身份（identity）](./motrix_edge_identity.md)
-   代码入口：`src/motrix_edge/server/` —— 随 **feat/3**（HTTP 控制面）落地
