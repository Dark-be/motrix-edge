# 会话（session）

## 摘要

`session/` 是**被节点启停的任务执行器**：不实现节点生命周期，只实现任务流程。`BaseSession`
定义最小接口（`session_start` / `run` / `session_finish` / `safe_stop`），`CaptureSession`、`InferSession` 和 `UploadSession` 位于同一 `session/` 包。
其中 `CaptureSession` 与 `InferSession` 是由 `get_session()` 工厂选择的机器人任务会话，
复用节点注入的 adapter；`UploadSession` 是独立的本地文件管理会话，不进入 EdgeNode
任务状态机，通过 `UploadSession` 直接实例化。

## 目标与原则

-   **会话 = 任务环境**：`session run <type>` 一步完成「选择 + 启动」→ ACTIVE；`session quit`
    退出回 READY。租约**独立于任务**（Edge 级，随 feat/3 落地），session 只消费。
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

除会话类型外，节点会注入 `frame_manager` / `adapter` / `capture_meta_store`（采集元信息选项
存储，与节点命令共用同一实例；不注入时会话自行按需创建）。

从 `SESSION_REGISTRY` 按 `session_type`（capture / infer，缺省用配置 `session.type`，再缺省
capture）实例化；仅 infer 会话额外消费 `policy_type`（缺省用配置 `policy.type`）。

## CaptureSession（采集会话）

基于 `RobotAdapter` 的**采集执行器（无回合流程控制）**：

-   `run()`：`adapter.reset()` → 等待就绪 → 持续消费命令直到 `session quit` 退出。
    **显示观测由节点级持续写入 `frame_manager`**，本会话不再 `observe` / 写 `frame_manager`。
-   命令：`session quit` 退出、`robot estop` 急停、`robot execute <qpos>` 直发动作、
    `robot teleop <bool> [mode]` 遥操作 / 人工接管（`mode=delta` = 增量接管）、`capture episode start/end` 控制一轮采集、
    `capture sync --meta <json>` 把采集元信息（采集员 / 任务名等）同步到机器人进程（进程保存数据时附加）；`capture meta list/add/edit/delete/delete-key` 管理元信息选项（配置级命令，任务态同样可用，读写 `capture.yml`）。
-   采集数据由适配器 / 进程自维护；采集会话期间周期查询 `adapter.capture_status()`（node 刷新缓存）上报元信息。

## InferSession（推理会话）

基于 `RobotAdapter` + 推理策略客户端 + `RTCManager` 的**推理执行器（无回合概念，由 rollout 步进驱动）**：

-   `run()`：`adapter.reset()` + `rtc.reset()` → 等待机器人就绪 → 等待 `infer rollout` 步进闭环。
-   **预热门控**：`infer connect` = 连接 + **预热**（`prepare` + 取一块丢弃，**不下发动作**）；它在
    **工作线程**里跑且**命令立即回执**（`started` / `warming`；状态见 `warmed_up` / `warmup_error`，
    重复调用幂等）。公共配置项 `warmup_required`（缺省 true）时未预热就 `infer rollout` → rejected 409
    （不惰性自连）——“连接 + 加载模型”可能几十秒到几分钟，压在动作命令上会变成「调用方超时判失败、
    真机却动了」；置 false 则保留 rollout 惰性自连（脚本 / 联调）。
-   **预热可中断**：`robot estop`（命令总线旁路，任何状态即时生效）与 `session quit` 都会取消预热
    （取消标志 + 关传输打断在飞调用）；否则一条长操作就把急停一起挡住。
-   **预热闩锁随连接失效**：`warmed_up` 是「本连接已预热」——连接丢失（推理服务端重启 / 断连）→
    自动复位并记 `warmup_error=connection lost`，`infer connect` 可重新预热（不再被幂等短路），
    未重新预热前 `infer rollout` 仍 409（不退回惰性重连 + 首块内联等模型加载）。
-   **预热进行中一律拦住 rollout**（`warmup_required` 是 false 也一样）：策略客户端同一时刻只能有
    一个在飞请求（见 [policy 设计](./motrix_edge_policy.md) 的预热门控）。
-   **回执有效期**：`infer rollout` 单步在下发动作前自查 `deadline_exceeded`（提交方已超时放弃）
    → **丢弃动作**（真机不动）并回执 504，丢弃步数计入 `dropped_actions`。
-   `infer rollout [mode]`：推理闭环步进，两种模式（参数缺省 = `single`）：
    -   `single`（缺省）：单步推理 —— `obs = adapter.observe()` → `action = rtc.infer(obs)`
        → `adapter.rollout(action)`；回执含 `count=1` / `action` / `actions`。
    -   `continuous`（`infer rollout continuous`）：**持续推理** —— 启动即回执 `started`，
        然后持续执行推理闭环，直到 `session quit` / `robot estop` 停止（持续期间每步轮询
        命令响应退出 / 复位 / 急停；重复 `infer rollout` → rejected）。
    -   多步（`count`）与 `drain` 模式**已取消**：动作块只作策略内部缓存、由 rtc 统一管理；
        录制 rollout 走 `capture episode start/end`（多余的 `count` 字段被忽略）。
-   命令：`infer connect`、`infer rollout [mode]`、`infer prompt` / `infer config` / `infer model`、
    `infer rtc`、`capture episode start` / `end`（rollout 录制）、`capture sync`、`session quit`
    （退出回 home）、`robot estop`、`robot reset`、`robot execute`、`robot teleop`；
    `capture meta list/add/edit/delete/delete-key`（配置级命令，任务态同样可用）。
-   单步主循环与持续推理循环**共用**一批命令（策略配置 / RTC / 录制 / 同步），
    实现在 `InferSession._handle_shared_cmd`（一处维护，避免两个循环各写一遍导致漂移）。

## 相关文档

-   节点启停会话 / 任务线程：[节点生命周期（node）](./motrix_edge_node.md)
-   推理策略客户端：[推理策略客户端（policy）](./motrix_edge_policy.md)
-   观测帧缓存：[FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   命令定义：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   代码入口：`src/motrix_edge/session/` —— 随 **feat/6**（任务运行时核心）落地
