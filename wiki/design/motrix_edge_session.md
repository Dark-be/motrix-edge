# 会话（session）

## 摘要

`session/` 是**被节点启停的任务执行器**：不实现节点生命周期，只实现任务流程。`BaseSession`
定义最小接口（`session_start` / `run` / `session_finish` / `safe_stop`），`CaptureSession`、`InferSession` 和 `UploadSession` 位于同一 `session/` 包。
其中 `CaptureSession` 与 `InferSession` 是由 `get_session()` 工厂选择的机器人任务会话，
复用节点注入的 adapter；`UploadSession` 是独立的本地文件管理会话，不进入 EdgeNode
任务状态机，通过 `UploadSession` 直接实例化。

## 目标与原则

-   **会话 = 任务环境**：`session run <type>` 一步完成「选择 + 启动」→ ACTIVE；`session quit`
    退出回 READY。租约**独立于任务**（Edge 级，见 [lease](./motrix_edge_lease.md)），session 只消费。
-   **命令驱动**：会话在 `run()` 循环内消费命令（`session quit` / `robot estop` / `robot execute` 等）。
-   **adapter 注入**：`get_session(..., adapter=node.adapter)`；会话按能力校验（capture 要求
    CAPTURE，infer 要求 EXECUTE），不支持 → `ValueError`。
-   **无硬件可单测**：注入 fake `command_source` + `TestRobotAdapter`。

## BaseSession 接口

```python
session.session_start()  # 节点进入 ACTIVE 前：连接硬件 / 初始化会话
result = session.run()   # 阻塞式任务主循环，返回 RunResult
session.session_finish() # 节点释放会话时：释放资源（adapter 由节点持有，不在此释放）
session.safe_stop()      # 安全停止（幂等、失败安全；委托 adapter.safe_stop）
```

### 结束契约（RunResult）

`run()` 通过 `RunResult` 告知节点结束原因，节点据此推进自身状态：

| 值            | 含义                              | 节点行为                |
| ------------- | --------------------------------- | ----------------------- |
| `FINISHED`    | 任务正常结束（如 `session quit`） | 释放会话回 READY        |
| `ERROR`       | 硬件 / 通信异常（已安全停止）     | 先 safe_stop 再转 ERROR |
| `INTERRUPTED` | 回合被打断，会话仍可继续          | 释放会话回 READY        |
| `OK`          | 任务执行成功（会话继续运行）      | —（仅内部语义）         |

### 实时状态（SessionState）

`INIT`（已创建未连接）→ `READY`（运行中，持续观测 / 持续推理）→ `FINISHED` / `ERROR`。
`exit_command` 记录退出命令（仅 submit 通道），由节点在状态落定后补发回执。

## get_session 工厂

从 `SESSION_REGISTRY` 按 `session_type`（capture / infer，缺省用配置 `session.type`，再缺省
capture）实例化；仅 infer 会话额外消费 `policy_type`（缺省用配置 `policy.type`）。

## UploadSession（上传会话，文件会话）

`UploadSession` 扫描本地采集目录，按 episode 文件名配对 `.mcap` 与 `.json`，读取 JSON 元数据并生成文件摘要；它不占用 RobotAdapter，也不改变 EdgeNode 节点状态。详细接口见 [上传会话设计](./motrix_edge_upload_session.md)。

## UploadSession（上传会话，文件会话）

`UploadSession` 与 `CaptureSession`、`InferSession` 同属 `session/` 包，但不进入 EdgeNode 的机器人任务状态机。它扫描本地采集目录、配对 `.mcap` / `.json`、读取元数据并生成 episode 文件摘要；详细接口见 [上传会话设计](./motrix_edge_upload_session.md)。

## CaptureSession（采集会话，观测会话）

基于 `RobotAdapter` 的**观测会话（无回合流程控制）**：

-   `run()`：`adapter.reset()` → 等待就绪（期间可 `session quit` / `robot estop` / `robot reset`）
    → 持续消费命令直到 `session quit` 退出。**显示观测由节点级持续写入 `frame_manager`**，
    本会话不再 `observe` / 写 `frame_manager`——采集只负责驱动机器人进程采集（episode 起止）。
-   命令：`session quit` 退出、`robot estop` 急停、`robot execute <qpos>` 直发动作、
    `robot teleop <bool>` 遥操作开关、`capture episode start/end` 控制一轮采集。
-   采集数据由适配器 / 进程自维护，Edge 只读共享内存观测并展示。

## InferSession（推理会话）

基于 `RobotAdapter` + 推理策略客户端的**推理执行器（无回合概念，由 rollout 步进驱动）**：

-   `run()`：`adapter.reset()` + `policy.reset()` → **连接推理节点**（`_wait_connect`，可被打断，
    未就绪持续重试）→ 等待机器人就绪 → 等待 `infer rollout` 步进闭环。
-   `infer rollout [count]`：连续执行 `count` 次（缺省 1，范围 1–100）`obs = adapter.observe()` →
    `action = policy.infer(obs)` → `adapter.rollout(action)`；回执包含最后动作与动作列表。
    （`observe` 是**推理输入**；显示观测由节点级写入 `frame_manager`，会话不写。）
-   命令：`infer rollout [count]`、`session quit`（退出回 home）、`robot estop`、`robot reset`、
    `robot execute`、`robot teleop`。
-   策略连接推迟到 `run()`（`_wait_connect`），避免同步连接阻塞 `session run` 命令回执。

## 相关文档

-   节点启停会话 / 任务线程：[节点生命周期（node）](./motrix_edge_node.md)
-   推理策略客户端：[推理策略客户端（policy）](./motrix_edge_policy.md)
-   观测帧缓存：[FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   命令定义：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   代码入口：`src/motrix_edge/session/` —— 随 **feat/6**（任务运行时核心）落地
