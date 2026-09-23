# robot-pipeline 遥操作（增量接管）实施计划

## 摘要

按 [robot-pipeline 遥操作（绝对映射 / 增量接管）](../design/robot_pipeline_teleop.md) 落地**增量接管**：
`BaseRobot` 增加遥操作模式与锚点、`set_target_action_delta()`；`/v1/teleop` 增加 `mode` 字段；
env / contract server 透传；遥操作期间推理让位（`/v1/rollout` 409），Edge adapter / session / console
补齐接管入口（`robot teleop <true|false> [mode]`）。

## 阶段一：增量接管（robot-pipeline）

-   [x] `http_contract`：`/v1/teleop` 契约增加 `mode` 字段与取值常量（`FIELD_TELEOP_MODE` /
        `VALUE_TELEOP_MODE_ABSOLUTE` / `VALUE_TELEOP_MODE_DELTA` / `TELEOP_MODES` /
        `DEFAULT_TELEOP_MODE`）。
-   [x] `BaseRobot`：遥操作模式常量与状态（`teleop_mode` / `teleop_master_ref` /
        `teleop_slave_ref`）；`enable_teleop(mode)` 校验模式并清锚点；`disable_teleop()` 清锚点；
        `set_target_action_delta()` 在锚点上叠加增量写 target；`current_action()` 提供从臂位姿参考；
        `step()` 改为 `_refresh_teleop_target()`（absolute 直连 / delta 首拍采锚点）；`safe_stop()`
        统一走 `disable_teleop()`。
-   [x] `BaseEnv`：`robot_set_teleop(enabled, mode)` 入队 `("teleop", (enabled, mode))`，
        控制线程按模式调用 `enable_teleop` / `disable_teleop`。
-   [x] `contract_server`：`TeleopRequest` 增加 `mode`（缺省 `absolute`），非法取值 422。
-   [x] 文档：`wiki/design/robot_pipeline_teleop.md`、`robot-pipeline/README.md` 的 `/v1` 表与
        遥操作章节。
-   [x] 校验：`ruff check` + `ruff format --check` + `pytest`（容器，352 passed）+ 增量语义
        smoke（接管首拍 target == 从臂位姿、限速仍由 `step_rad` 决定、主臂回锚点则从臂回锚点）。

## 阶段二：推理让位（robot-pipeline + Edge）

-   [x] `BaseRobot.rollout()` 在**遥操作开启时一律拒绝**（不分模式：遥操作即人工接管），返回 False、
        不改 target、不退出遥操作；`execute` / `reset` / `safe_stop` 仍可抢回程控。
-   [x] `BaseEnv`：`TakeoverActiveError` + `robot_rollout` 同步拒绝（HTTP 线程），控制线程
        `_drain_commands` 再判一次（只记日志，不置 `last_error`）。
-   [x] `contract_server`：`/v1/rollout` 遥操作中 → 409 + detail。
-   [x] Edge 上行：`parse_teleop_mode` + `robot teleop <true|false> [mode]`（CommandSpec 可选位置参数）
        → node / session → `adapter.set_teleop(enabled, mode)` → `/v1/teleop {enabled, mode?}`。
-   [x] Edge 推理让位：`HttpShmAdapter.rollout()` 识 409 → 返回 False（日志限流）；InferSession
        单步回执 rejected(409)、持续推理跳过该拍，遥操作关闭后自动恢复。
-   [x] Console：命令卡片加「人工接管 Takeover（增量）」（`capability=robot/teleop` + `mode=delta`），
        入口校验非法 mode → 400。
-   [x] mock SDK（`robot-pipeline` 契约服务）：`/v1/teleop` 收 mode、遥操作中 `/v1/rollout` → 409。
-   [x] 校验：`ruff check`（src / tests / robot-pipeline / scripts）+ `ruff format --check` + `pytest`
        （容器，358 passed）+ `/v1` 冒烟（接管中 rollout 409 且 target 不被改写、execute 抢回后 rollout 恢复、
        非法 mode 422）+ 前端 `tsc --noEmit` / `npm run build`。

## 待启动（需先决策）

-   [ ] 交回模型的平滑：`teleop=false` 后首拍 `rollout` 的 target 与人工位姿的差如何处理
        （RTC 过渡 / 等模型动作与当前位姿足够接近再交回）。
-   [ ] 单臂 `QPOS = 7` 的 `step()` 布局泛化（当前硬编码 14 维切片，`SinglePiperRobot` 会 IndexError）。
-   [ ] episode 元信息标记接管区间（人工介入区间），便于数据筛选。

## 已定不做

-   推理期主臂跟随从臂：主臂**不可被程序驱动**（Piper leader 模式只读），由操作员手动把主臂摆到
    与从臂相近位姿，残余差值由锚点零化（见设计文档「增量锚点」）。
