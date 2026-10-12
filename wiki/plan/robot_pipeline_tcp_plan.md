# robot-pipeline 末端 TCP 位姿实施计划

## 摘要

按 [robot-pipeline 末端 TCP 位姿（帧开关）](../design/robot_pipeline_tcp.md) 落地：机器人侧帧换算 +
`robot.tcp` 配置、机器人 → Edge 的帧名 / 偏移上报、Edge 侧发现驱动与深度反投影补回法兰，外加测试与
文档同步。开关缺省关闭，关闭时行为与现状一致。

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

-   [x] `src/motrix_edge/adapter/http_contract.py`：新增 `FIELD_POSE_FRAME` / `FIELD_TCP_OFFSET`（契约单点）
-   [x] `robot-pipeline/src/server/contract_server.py`：discover 的 `robot` 块补这两个字段（取机器人
        `pose_frame` / `tcp_offsets`；不提供位姿 → `null`）
-   [x] `src/motrix_edge/adapter/http_shm_adapter.py`：**惰性拉取**机器人自描述（`POST /v1/discover`，
        成功才缓存）· 暴露 `pose_frame()` / `tcp_offset_by_arm()`，未上报 → 回退类常量 `flange`
        （不动 `DiscoveredRobot` / 构造器签名，旧进程与第三方适配器零影响）
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
-   [x] `tests/test_dual_piper_adapter.py`：`pose_frame()` 上报优先 / 未上报回退 / 偏移非法不采纳 /
        自描述只问一次
-   [x] `tests/test_server.py`：TCP 偏移下腕相机反投影的 `xyz_world` 与开 TCP 前一致
-   [x] `tests/test_rpent.py`：`env.get_env_meta` 的 `pose_frame` / `tcp_offset` 同源上报
-   [x] 全量 `pytest`（804 passed / 1 skipped）+ `ruff check` + `prettier`

### D. 文档同步

-   [x] `wiki/design/robot_pipeline_action_spaces.md`：`pose` 行与观测行标「位姿系 = `POSE_FRAME`」
        （法兰或 TCP，指向 TCP 设计文档）
-   [x] `wiki/design/robot_pipeline_cartesian.md`：契约表「坐标系」指向 TCP 设计文档
-   [x] `wiki/design/motrix_edge_adapter.md`：`POSE_FRAME` 段补「由机器人上报优先、未上报回退类常量」
-   [x] `wiki/design/robot_pipeline_frames.md`：`probe_tip` 与 `tcp_offset` 的关系（同一类量，应一致）
-   [x] `wiki/design/index.md` / `wiki/plan/index.md` 登记
-   [x] `src/motrix_edge/adapter/http_contract.py` 的 `/v1/discover` 段落说明这两个字段

### E. 验收

-   [x] 离线：全量 pytest（804 passed / 1 skipped）
-   [ ] 真机（运动学未被污染）：`scripts/verify_cartesian.py --port left` 仍为 mm / mrad 级
-   [ ] 真机（TCP 生效）：开 `robot.tcp` 后发 5 cm 位姿增量，量**尖端**位移 = 5 cm（尺 / 探针）
-   [ ] 真机（深度不变）：腕相机 `GET /v1/depth` 的 `xyz_world` 在 TCP 开关前后一致
-   [ ] 计划完整落地后删除本文档并更新 `wiki/plan/index.md`（真机三项待现场）
