# 边缘节点开发计划（motrix_edge）

## 摘要

基于[架构设计](../design/motrix_edge_architecture.md)的落地计划，反映当前已合并的代码状态与后续待办。功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) / [!5（任务运行时）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/5) 落地，在本 MR 内均为 `[ ]`。

## 已完成

-   [ ] 单一 src-layout 包 `motrix_edge`，console script `motrix-edge`，支持 `uv run motrix-edge --base_cfg test`
-   [ ] EdgeNode 节点生命周期状态机（IDLE / ACTIVE / ERROR）
-   [ ] 会话框架（`BaseSession` / `CaptureSession` / `InferSession` 骨架）+ `get_session` 工厂
-   [ ] 信号驱动流程（CLI 命令 → 信号，`COMMAND_TO_SIG` / `command_source` 可注入）
-   [ ] 硬件适配层（`RobotAdapter` HAL + entry point 注册工厂）
-   [ ] 推理策略客户端框架（`policy` 子包：BasePolicyClient / 通用 msgpack 传输 / 格式契约 / ActionChunkBroker / openpi 策略 + `get_policy` 工厂；格式契约 openpi 兼容，服务端无需改动）
-   [ ] 数据布局下沉到适配器 `capabilities`（动作维度 / 观测布局由 `RobotAdapter` 声明，核心不再持有 profile）
-   [ ] 机器人统一控制接口（通用 `cmd` + 控制接口 `enable/disable_teleop` + 每帧推进 `step()`；`reset` 非阻塞、与遥操作共用统一限速 `step_rad`，频率由 CaptureSession 统一控制，adapter 不再 `reset_interval` 自定时）
-   [ ] 通用控制接口（`RobotAdapter.cmd` 唯一指令来源 + `enable_teleop/disable_teleop` + `teleop`（原 sync）+ 通用 `reset` 程序控制复位；采集回合自动启用 / 结束关闭遥操作）
-   [ ] 机器人安全停止（`RobotAdapter.safe_stop()` 幂等、失败安全契约；ESTOP 与任意任务异常路径先 `safe_stop` 再进 ERROR；覆盖 ready 前与推理/录制中异常分支）
-   [ ] 采集回合生命周期（adapter `start_capture / stop_capture` 自含录制；中断/急停丢弃未提交帧，防污染下一回合）
-   [ ] 推理主循环测试（InferSession 用 fake policy + fake signal source 覆盖 obs → infer → action 一轮与急停/退出分支；复位非阻塞，无专门等待）
-   [ ] 设备身份声明（`identity` 子包：`Identity` + `headers()` 预留发送接口 + `load_identity` + correlation_id / idempotency_key 生成器；配置 `identity` 段）
-   [ ] MotrixEdge HTTP 服务骨架（`server` 子包：FastAPI `GET /v1/health` + `POST /v1/commands`（accepted）+ correlation 中间件；默认运行 node 主线程 + web 独立线程；host/port 走配置 `server` 段）
-   [ ] 数据采集 HTTP 接口（`/v1/captures/*`：precheck / start / stop / interrupt / resume / close / status；web 线程经共享 `SignalBus` 驱动 EdgeNode，`CaptureService(node, bus)` 绑定运行中 node，不持有 node）
-   [ ] 单元测试（节点状态机 / 两条会话主路径 / policy 契约与 broker / adapter 采集回合生命周期，全部无硬件可跑）
-   [ ] 硬件抽象收敛（Robot / Sensor / Controller / Collector 统一收敛到 `adapter/`：`RobotAdapter` HAL，具体实现移出核心包）

## 待办

-   [ ] OpenPI 服务端实机联调（host/port/api_key/契约逐项验证）
-   [ ] piper 实机验证（adapter 的 execute 拆分到 slave 臂、qpos/action 维度与模型一致）
-   [ ] 传输层断线重连 / 异常恢复（request 阶段连接中断时的策略）
-   [ ] 信号源网络化：支持 MotrixConsole 控制面经授权协议驱动任务生命周期（推理节点不进入 command_source）
-   [ ] HTTP 服务接入真实命令执行（`/v1/commands` 接 lease / capability / 现场在场校验并下发机器人）
-   [ ] Outbound HTTP 客户端（用 `identity.headers()` 真正发起对外请求：InferenceAccessGrant / 上传）
-   [ ] 真机强化学习接口（observation 上传 / 动作下发）
-   [ ] 实机联调 RobotAdapter 具体实现
-   [ ] 引入 Docker 化部署（镜像构建 / 容器编排）

## 备注

-   检查：`uv run ruff check .`、`uv run pytest`
-   提交前：`npm run format` → `uv run ruff check .` → `uv run pytest`
