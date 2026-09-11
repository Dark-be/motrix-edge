# robot-pipeline 控制 / 观测双线程实施计划

## 摘要

把 robot-pipeline 的 `BaseEnv` 单主循环拆成**控制线程**（`HZ`，机械臂限速步进 + 状态采样）与
**观测线程**（`OBS_HZ`，相机取帧 + 共享内存发布 + 采集落盘），使相机 / 磁盘卡顿不影响机械臂控制。
方案见 [robot-pipeline 运行时](../design/robot_pipeline_runtime.md)。

## 状态

| 项       | 内容                                                    |
| -------- | ------------------------------------------------------- |
| 范围     | `robot-pipeline/src/env`、`robot-pipeline/src/robot`    |
| 契约影响 | 无（/v1 端点、观测键、共享内存布局、health 字段均不变） |
| 验证     | 离线冒烟（相机每拍阻塞 2s）：控制 29.97Hz / 观测 0.50Hz |

## TODO

-   [x] `BaseRobot` 拆出线程入口：`sample_qpos()`（控制线程，写 `motion_state` 快照）、
        `capture_images()` 与 `build_observation()`（观测线程）；`get_observation()` 保留给单线程脚本。
-   [x] `BaseEnv` 拆两条循环：`_control_loop` / `step_control()`（HZ）与 `_observe_loop` /
        `step_observe()`（OBS_HZ），各自 sleep 补偿。
-   [x] 指令分两条队列：运动指令 → `commands`（控制线程消费），采集指令 → `capture_commands`
        （观测线程消费）。
-   [x] 频率统计与健康检查：控制 / 观测各自周期窗口，`health.ready` 覆盖两线程，
        `measured_hz` 取控制线程。
-   [x] 观测慢帧告警（`OBS_SLOW_FRAME_S` 阈值 + 每秒限流）。
-   [x] 停机：`stop()` 分别 join 控制线程（2s）与观测线程（6s）后断开机器人。
-   [x] 文档同步：`robot-pipeline/README.md`、`env` / `robot` / `server` / `collector` 模块 docstring、
        `wiki/design/robot_pipeline_runtime.md`。
-   [x] 离线验证：TestRobot + 模拟相机每拍阻塞 2s，断言控制线程频率、观测掉帧、execute 目标到位、
        episode 落盘。
-   [ ] 实机验证（dual_piper / dual_alicia_piper）：相机拔插或长阻塞下机械臂运动连续性、
        `/v1/health` 的 `measured_hz` 是否稳定在 30。
