# robot-pipeline 末端 TCP 位姿实施计划

## 摘要

按 [robot-pipeline 末端 TCP 位姿（帧开关）](../design/robot_pipeline_tcp.md) 落地：机器人侧帧换算 +
`robot.tcp` 配置、**运行期开关**（`robot tcp` / `robot gravity`，离开会话才能切）、Edge 侧**从
`/v1/health` 读回运行位**与深度反投影补回法兰，外加测试与文档同步。开关缺省关闭，关闭时行为与现状一致。

## TODO

### A. 机器人侧（robot-pipeline）

-   [x] 新增无硬件依赖的纯函数（`robot/kinematics/tcp.py`）：`resolve_tcp()`（配置解析 + 校验）+
        `to_tcp()` / `from_tcp()`（扁平「每臂 xyz + rpy」进出一致，逐臂偏移）+ 单臂 `to_tcp_block()` /
        `from_tcp_block()`
-   [x] `BaseRobot`：解析 `robot.tcp`（`enabled` / `offset` / `arms.<arm>` 覆盖），构造期校验
        （3 个有限数、拒绝旋转键、`enabled` 缺 `offset` 报错），暴露 `pose_frame` / `tcp_offsets` /
        `pose_to_tcp()` / `tcp_to_flange()`；开启却不生效（无位姿 / 无臂名）→ 一条 WARNING
-   [x] `DualPiperRobot._pose_of_joints()`（读）与 `_solve_pose()`（写）各加一处换算
-   [x] `TestRobot` 同两处（虚拟机器人同套逻辑，便于前端联调与离线验收）
-   [x] `robot-pipeline/src/config/dual_piper.yml` 新增 `robot.tcp`（缺省 `enabled: false` + 注释示例）
-   [x] `robot-pipeline/README.md`：新增「末端 TCP（可选）」小节 + `robot` 段配置项清单补 `tcp`

### B. 契约与 Edge

-   [x] `src/motrix_edge/adapter/http_contract.py`：新增 `FIELD_POSE_FRAME` / `FIELD_TCP_OFFSET` /
        `FIELD_GRAVITY` 与 `PATH_TCP` / `PATH_GRAVITY`（契约单点）
-   [x] `robot-pipeline/src/server/contract_server.py`：`/v1/tcp` / `/v1/gravity` 端点（`ValueError` →
        422）+ health 的运行位段（`pose_frame` / `tcp_offset` / `gravity`；discover 不再带这两个字段）
-   [x] `src/motrix_edge/adapter/http_shm_adapter.py`：运行位**从 `health()` 读回**（节点本来就每 2s
        探活一次，读回就是随时新鲜的；删除自描述请求 `_declare()` 与 `DECLARATION_TTL_S`）· 暴露
        `pose_frame()` / `tcp_offset_by_arm()`，失联保留上次已知值、从未成功过 → 回退类常量 `flange`
-   [x] `src/motrix_edge/server/depth.py`：腕相机外参合成前还原法兰
        （`T_base_flange = T_base_tcp · T_flange_tcp⁻¹`；该臂没有偏移可查 → 不给 `xyz_world`）
-   [x] `src/motrix_edge/server/rpent/node_view.py` + `service.py`：`get_env_meta` 增 `tcp_offset`
        （`pose_frame` 改为「上报优先 → 类常量」）
-   [x] `robot-pipeline/scripts/verify_extrinsics.py`：向机器人问 `pose_frame` / `tcp_offset` 后**反算**
        法兰再比对探针尖（并打印一行说明）

### C. 测试

-   [x] `tests/test_robot_tcp.py`（17 例）：配置解析（关闭恒等 / 逐臂覆盖 / 7 类非法配置）+ 换算
        （沿法兰 z 平移 / 偏移随姿态转 / 往返精确 / 逐臂 / 尖端语义）+ 基类钩子（关闭恒等 / 开启互逆 /
        无臂名回退）；沿用 `tests/test_piper_kinematics.py` 的「插入 robot-pipeline/src 后导入纯 numpy
        模块」手法
-   [x] `tests/test_robot_pipeline_configs.py`：用**同一个解析器**把出厂配置过一遍 + 出厂默认关闭
-   [x] `tests/test_dual_piper_adapter.py`：`pose_frame()` 从 health 读回（不单独发请求）/ 未上报
        回退类常量 / 偏移非法不采纳 / 失联保留上次已知值 / `set_tcp`·`set_gravity` 请求体 + 4xx →
        `ValueError`
-   [x] `tests/test_server.py`：TCP 偏移下腕相机反投影的 `xyz_world` 与开 TCP 前一致
-   [x] `tests/test_rpent.py`：`env.get_env_meta` 的 `pose_frame` / `tcp_offset` 同源上报
-   [x] 全量 `pytest`（818 passed / 1 skipped）+ `ruff check` + `prettier`

### D. 运行期开关（`robot tcp` / `robot gravity`）

-   [x] 机器人：`set_tcp()` / `set_gravity()` 都**直接同步切换**（HTTP 线程执行、不入队，否则
        「偏移非法 / 机型不支持」回不到调用方）；先校验后动状态——`ValueError` → 422，失败零副作用；
        gravity 切换后立即重注入 `control_layout()`（否则后续 episode 的 `control_mode` 漏报）
-   [x] Edge 侧会话限制：`robot tcp` / `robot gravity` **只在没进入会话时可切**（会话中 409；
        `robot teleop` 不受限——人工接管是会话中的正常操作）
-   [x] Edge 适配器：`set_tcp()` / `set_gravity()` 转发 + **回执检查**（4xx → `ValueError`，不静默
        当成功）；`health()` 带回运行位
-   [x] Edge 命令层：`robot tcp <true|false> [offset=[x,y,z]]` / `robot gravity <true|false> [alpha=<a>]`
        （`command/params.py` 解析 + `naming.py` 常量 + 会话限制 `SWITCH_COMMANDS`；**`robot teleop`
        不受限**——人工接管是会话中的正常操作）
-   [x] `/v1/status`（capture / infer 快照的 `adapter` 段）暴露 `pose_frame` / `tcp_offset` / `gravity`
-   [x] 测试：机器人侧 `set_tcp` / `set_gravity`（成功 / 非法或不支持 / 零副作用）、env 层「两者都不
        入队（直接生效）」+ 重注入控制模式、node 层「会话中拒绝（409）· 空闲放行」、会话层拒绝
        （capture 路径）、适配器 `_post_switch` 4xx

### E. 文档同步

-   [x] `wiki/design/robot_pipeline_action_spaces.md`：`pose` 行与观测行标「位姿系 = `POSE_FRAME`」
        （法兰或 TCP，指向 TCP 设计文档）
-   [x] `wiki/design/robot_pipeline_cartesian.md`：契约表「坐标系」指向 TCP 设计文档
-   [x] `wiki/design/motrix_edge_adapter.md`：`POSE_FRAME` 段补「**从 `/v1/health` 读回**（运行期可
        切）、失联保留上次已知值、从未成功过回退类常量」+ 契约表补 `/v1/tcp` / `/v1/gravity`
-   [x] `wiki/design/robot_pipeline_frames.md`：`probe_tip` 与 `tcp_offset` 的关系（同一类量，应一致）
-   [x] `wiki/design/robot_pipeline_tcp.md`：「运行期开关与上报」（health 读回 + 会话限制）
-   [x] `wiki/design/motrix_edge_command_bus.md`：命令表补两条 + 会话限制说明
-   [x] `wiki/design/robot_pipeline_impedance.md`：「可观测」段补 `/v1/health` 的 `gravity` + 运行期开关
-   [x] `wiki/design/index.md` / `wiki/plan/index.md` 登记
-   [x] `src/motrix_edge/adapter/http_contract.py` 的 `/v1/health` 段落说明运行位三字段

### F. 验收

-   [x] 离线：全量 pytest（818 passed / 1 skipped）
-   [ ] 真机（运动学未被污染）：`scripts/verify_cartesian.py --port left` 仍为 mm / mrad 级
-   [ ] 真机（TCP 生效）：`robot tcp true offset=[0,0,<L>]` 后发 5 cm 位姿增量，量**尖端**位移 = 5 cm
        （尺 / 探针）
-   [ ] 真机（重力开关）：`robot gravity true` → health 的 `gravity.control_mode` 变 `mit+gravity`，
        `t_ff` 不为 0；对无重力前馈的机型 → 回执 `rejected`（422 not supported）
-   [ ] 真机（深度不变）：腕相机 `GET /v1/depth` 的 `xyz_world` 在 TCP 开关前后一致
-   [ ] 计划完整落地后删除本文档并更新 `wiki/plan/index.md`（真机项待现场）
