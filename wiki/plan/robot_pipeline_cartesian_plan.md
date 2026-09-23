# robot-pipeline 位姿动作（求解器）实施计划

## 摘要

按 [robot-pipeline 位姿动作](../design/robot_pipeline_cartesian.md) 落地
`action_space=pose`：新增 `robot/kinematics`（DH / 正解 / 雅可比 / DLS 逆解），
`BaseRobot` 增加动作空间声明与位姿目标钩子，`DualPiperRobot` 接线（每臂解算 → 关节目标 → MIT），
env / contract server 透传 `action_space`，edge adapter 声明 `pose`。

> 收敛结果（经用户多轮校正）：**运动学与位姿解算全在控制器**，robot 层只有骨架；机器人类只做取数与转发；
> **阻抗 / 力矩前馈暂不实现**（下发只有关节角，`kp` / `kd` / `t_ff` 用 MIT 缺省值）。

## 阶段一：运动学与求解器（robot-pipeline/src/robot/kinematics）

-   [x] `transforms.py`：姿态与旋转工具（`rpy ↔ R`，约定 `Rz·Ry·Rx`；`log3` / `vee` / `skew`）。
-   [x] `piper.py`：Modified DH 参数（`PIPER_DH`）、关节软限位（`PIPER_JOINT_LIMITS`）、
        `fk()` / `pose()` / `pose_matrix()` / `jacobian()` / `clip_joints()`。
-   [x] `ik.py`：`IkResult` + `solve_ik()`（阻尼最小二乘、步长 / 限位裁切、多起点、容差与迭代上限）。
-   [x] `__init__.py`：对外导出（`PiperKinematics` / `solve_ik` / `IkResult`）。

> 曾实现过 `impedance.py`（Kp / Kr / Kd / Dr + 力矩限幅 + `J^T·F`），按用户决定**已删除**：
> 本阶段不做阻抗控制，下发只有关节角（配置项 `robot.cartesian.impedance` 一并移除）。

## 阶段二：职责划分与 robot 层接线

-   [x] `controller/piper_controller.py`（**只管关节 / 夹爪读写 + 限位把关**）：对外提供**静态**
        位姿转换 `joint_to_pose()`（正解）/ `pose_to_joint(pose, seed)`（解算，**只解算不下发**），
        内部用模块级共享模型（限位 = `PIPER_JOINT_LIMITS`，与下发裁切同一份）；`set_joint(joint, torque_ff=None)`
        下发前**全 6 关节**裁到软限位（+去重告警），**不自己算力矩**；**删除** SDK 位姿读写
        （`get_position` / `set_position` / `move_p`）——真机依赖只剩 `get_joint` + `move_mit`。
-   [x] `controller/test_arm_controller.py`：同形**静态** `joint_to_pose` / `pose_to_joint`（虚拟线性
        运动学 `POSE_MAP`）。
-   [x] `base_robot.py`（**只留骨架**）：`ACTION_SPACES` / `ACTION_SPACE_JOINT` / `ACTION_SPACE_POSE` /
        `CARTESIAN_DIM_PER_ARM`、`normalize_action_space()`、`CartesianActionError`；`execute()` / `rollout()`
        接受 `action_space`；目标状态机 + 逐拍限速插值 + 遥操作映射 + 观测组装；子类钩子
        `_prepare_target()`（缺省只支持关节空间）/ `get_observation_qpos()` / `get_observation_pose()`
        （缺省 `None` = 不提供位姿）/ `_apply_action()`——**基类不碰位姿语义、不碰运动学**。
-   [x] `dual_piper_robot.py` / `test_robot.py`：**编排 + 取数 + 下发**（`_prepare_target()` 里对
        `pose` 逐臂调静态 `pose_to_joint`（起点 = 当前指令位置，兜底 home）→ 拼关节 target；
        `get_observation_pose()` 用静态正解；`_apply_action()` 逐臂 `set_joint` + `set_gripper`）+
        声明与装配（类常量、控制器 / 相机、`connect` / `disconnect`、piper 的 `_get_teleop_target`）；
        `robot.cartesian.ik` 在这里解析后**直接传给解算**（无 joint_limits 配置：限位单一来源）。

## 阶段三：链路透传

-   [x] `env/base_env.py`：`robot_execute` / `robot_rollout(action, action_space=None)` 入队并透传；
        `_check_action_dim` 增加动作空间校验；`_drain_commands` 把 `ValueError`（目标不可达 / 空间不支持）
        与硬件故障分开——前者只记 WARNING，**不置 `last_error`**（不影响 health）。
-   [x] `server/contract_server.py`：`ActionRequest.action_space`（缺省 `joint`）；execute / rollout
        透传；`/` 调试端点上报 `action_spaces`。
-   [x] `src/motrix_edge/adapter/`：`ActionSpace.POSE`（`= "pose"`）+ `dual_piper_adapter.ACTION_SPACES`
        声明 `pose`；`base.RobotAdapter.execute` / `http_shm_adapter.execute` 接受并透传 `action_space`；
        新增 `_require_full_arms_for_cartesian()` 守卫（部分臂启用时位姿动作**拒绝**——未启用臂用
        `HOME_QPOS`（关节值）填充，不能当位姿下发）。
-   [x] `http_contract.py`：`/v1/execute` 与 `/v1/rollout` 的 body 文档补 `action_space`（含失败语义）。

## 阶段四：现场校验脚本与文档

-   [x] `robot-pipeline/scripts/verify_cartesian.py`：FK 与 SDK 法兰位姿对照（位置 / 姿态误差 +
        限位余量）+ IK 往返（收敛 / 耗时），默认**只读**（`--cycles` 才小幅摆动）。
-   [x] 文档：`robot-pipeline/README.md`（目录 / 类常量 / HTTP 表格 / 「位姿动作」小节）、
        `wiki/design/robot_pipeline_cartesian.md`（新建 + `design/index.md` 登记）、
        `robot_pipeline_runtime.md`（位姿小节 + 「位姿动作下发」小节改写）、`motrix_edge_adapter.md`、
        `motrix_edge_primitives.md`、`motrix_edge_rpent_bridge.md` 与 RPent 计划。

## 阶段五：测试与校验

-   [x] `tests/test_piper_kinematics.py`：rpy 往返 / 万向锁、`log3` 轴角、正解 ↔ 位姿一致、
        **雅可比 = 正解数值导数**、限位裁切、逆解可达 / 不可达 / 坏输入 / fallback 起点。
-   [x] `tests/test_piper_controller.py`：限位裁切（全 6 关节 / 就地裁切 / 告警去重 / 维数拒绝 /
        `t_ff` 透传）+ **静态**转换（正解 ↔ 解算往返、失败语义、`**ik_config` 覆盖、无 SDK 位姿读写）。
-   [x] `tests/test_dual_piper_robot.py`：位姿 = 同拍正解（含 NaN 语义）、位姿目标解算成关节目标、
        经 MIT 下发、不可达 / 维度不符报错且不改 target、遥操作期拒绝、关节动作直通。
-   [x] `tests/test_dual_piper_adapter.py`：断言「宣称 `pose` + body 透传 + 部分臂守卫」
-   [x] 全量校验：`ruff format` / `ruff check`（`src` + `tests` + `robot-pipeline/src` + `scripts`）、
        `XDG_STATE_HOME=/tmp/xdg PYTHONPATH=src .venv/bin/python -m pytest -q` = 541 passed / 1 skipped、
        prettier（wiki / README）。

## 待现场确认（离线无法判定）

-   [ ] DH 参数与关节读数符号是否与真机一致（用 `verify_cartesian.py` 对照法兰位姿）。
-   [ ] 关节限位表（`PIPER_JOINT_LIMITS`）按现场机型核对（**解算与下发共用的唯一来源**，
        如需修改直接改代码常量）。
-   [ ] 解算参数（`robot.cartesian.ik`：`pos_tol` / `rot_tol` / `max_iters` / `damping`）
        现场按实际位姿误差与耗时调优。
