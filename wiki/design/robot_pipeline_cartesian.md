# robot-pipeline 位姿动作（运动学基础）

## 摘要

Piper 的位姿能力建立在 MotrixSim 与官方 URDF 转换的 MJCF 上。正解、雅可比、逆解和软关节
限位共享同一模型；不再维护一套简化 DH。新运行时控制链路只下发关节目标，不切换到 SDK 的 `move_p` 模式（历史
`set_pose()` / `set_position()` 兼容入口除外）。本阶段只提供运动学基础，
`observations/pose` 发布、动作布局和 Edge 契约由后续 robot 栈接入；接入前保持 `pose_dim = 0`。

## 目标与约束

-   运动学内部统一使用 MotrixSim Pose7 `[x, y, z, qx, qy, qz, qw]`；
-   Edge/SDK 边界继续使用 Pose6 `[x, y, z, roll, pitch, yaw]`，只在控制器边界转换；
-   正解与逆解共用 MJCF 和软限位，避免观测模型与控制模型漂移；
-   求解器依赖 MotrixSim，但不把硬件 SDK 带入离线测试；
-   逆解失败必须显式返回原因，不得返回近似关节值供调用方误下发；
-   现场 SDK 法兰位姿只用于兼容读取和模型标定，不作为 FK / IK 的另一套运行时模型。

## 模型与限位

`robot/kinematics/piper.py` 在启动时加载 `robot-pipeline/assets/robots/piper/mjcf/arm/piper.xml`，
通过 MotrixSim `IkChain` 输出基座到法兰的 Pose7、几何雅可比和 DLS 逆解。每个调用线程持有独立
`SceneData`，模型和链定义只读共享。软限位由 Piper SDK preset 内缩 `JOINT_LIMIT_MARGIN` 得到；
值为 0 的边界不内缩，因为厂商 home 位于 J2 下界 0 / J3 上界 0，且 0 可被浮点精确表示。IK 和
`PiperController.set_joint()` 通过模块级共享模型使用同一张限位表。控制器下发前仍会再次裁切，
硬件 SDK 的限位保护作为最后一道防线。

MJCF 从 AgileX 最新官方 `agx_arm_urdf` 转换，并与官方 `piper_sdk` 固件 `1.6-6` 及之后的
关节 frame 核对。MJCF 保留 URDF 的机械
范围和最新 CAD 惯量；真机下发则使用不超过 SDK preset 的独立软限位。模型来源、固定版本、转换
规则和许可证记录在 `robot-pipeline/assets/robots/piper/README.md`。

## 逆解契约

`solve_ik()` 调用 MotrixSim `DlsSolver`：

$$
\Delta q = J^T(JJ^T + \lambda^2I)^{-1}e
$$

MotrixSim 负责迭代，`max_iters`、`step_size` 和 `damping` 直接遵循其命名和语义；为保持显式
`step_size` 生效，关闭求解器的 `adaptive_damping`。内部停止阈值由 `min(pos_tol, rot_tol)` 推导，
适配层直接用 MotrixSim Pose7 的位置差与最短四元数角距离分别复核最终误差，并在
`IkResult.residual` 中保留 MotrixSim 的组合残差。调用方提供首选 seed 和可选 fallback seeds；求解器返回
首个满足位置、姿态容差且位于软限位内的解。失败时 `q` 必须为 `None`，并通过 `reason` 区分：

-   `bad_input`：目标不是合法的 Pose7、包含非有限值或四元数未归一化；
-   `bad_seed`：所有起点维数错误或包含非有限值；
-   `bad_config`：迭代次数、容差、阻尼或步长配置无效；
-   `joint_limit`：MotrixSim 候选解超出 SDK 软限位；
-   `stalled`：MotrixSim 求解异常或返回无效结果；
-   `max_iters`：达到迭代上限仍未满足容差。

## 控制器边界

`PiperController.joint_to_pose()` 与 `pose_to_joint()` 是 Edge Pose6 兼容边界；内部立即转换为/从
MotrixSim Pose7。后端可变 `SceneData` 按线程隔离，后者只求解、不下发。
新运行时的硬件运动最终经 `set_joint()` 进入 `move_mit` 或既有 `move_j` 通路。为兼容既有调用方，
`get_position()` 继续读取 SDK `get_flange_pose()`；历史 `set_pose()` 与基类统一入口 `set_position()`
继续直接包装 SDK `move_p()`。这些兼容接口的位姿单位均为米 / 弧度，不参与上述 FK / IK 闭环。

## 验证

-   离线测试覆盖 Pose6/Pose7 边界、Pose7 / 雅可比有限差分、IK 成功与各失败原因、软限位及
    出厂 `init_qpos`；
-   现场误差超过阈值时应先核对 MJCF、关节零位和方向，不得通过放宽 IK 容差掩盖模型错误。
