# robot-pipeline 动作与观测契约

## 摘要

robot 底层只控制关节。`joint` 直接写关节目标；`pose` / `pose_delta` 先按当前位姿系解释，完成
TCP→ 法兰换算和 IK 后再写同一条关节目标链。robot 原始观测以无前缀的明确物理量键发布；到
Edge 边界后，再组装成每臂 7 维 `observations/qpos`、`observations/qpos_err` 与 `action`。
落盘仍只保存 qpos / action，保持数据集 schema 稳定。

## 目标与约束

-   **关节是唯一控制落点**：任意位姿输入在写目标前完成解算，控制拍只处理关节角和 MIT 下发。
-   **robot 键名即物理量**：原始帧使用 `joint`、`gripper`、`pose` 及对应的 `*_action`，不带
    `observations/` 前缀，也不用 `qpos` 指代局部数据。
-   **采集契约稳定**：Edge 观测与落盘仍使用 `observations/qpos` / `action`，每臂固定为
    `[6 joint, 1 gripper]`，保持策略与历史数据格式不变。
-   **组合下发仍原子**：`layout=joint+gripper` 等请求可同时更新两条目标链；校验或 IK 失败时两者都不改。
-   **部分臂保持**：`arms` 未选中的关节和夹爪目标保持原值，不补 home。

## 下发空间

| 段              | 每臂维度 | 输入语义                       | 写入目标            |
| --------------- | -------- | ------------------------------ | ------------------- |
| `joint`         | 6        | 绝对关节角（rad）              | 关节目标            |
| `joint_delta`   | 6        | 相对当前关节目标的关节角增量   | 当前关节目标 + 增量 |
| `pose`          | 6        | 绝对末端位姿（`xyz + rpy`）    | TCP→ 法兰 →IK→ 关节 |
| `pose_delta`    | 6        | 相对当前关节目标 FK 的位姿增量 | 合成位姿 →IK→ 关节  |
| `gripper`       | 1        | 归一化绝对开合（`[0, 1]`）     | 夹爪目标            |
| `gripper_delta` | 1        | 相对当前夹爪目标的开合增量     | 目标 + 增量并钳位   |

`layout` 决定每臂块的段序，`arms` 决定块对应的臂。组合与作用域的完整契约见
[下发布局](./robot_pipeline_action_layouts.md)。

## robot 原始观测键

| 键               | 内容                           | 双臂维度 |
| ---------------- | ------------------------------ | -------- |
| `joint`          | 实测绝对关节角                 | 12       |
| `joint_action`   | 当前关节目标；无目标回退 joint | 12       |
| `gripper`        | 实测夹爪开合                   | 2        |
| `gripper_action` | 当前夹爪目标；无目标回退实测   | 2        |
| `joint_error`    | 关节目标 − 实测关节角          | 12       |
| `gripper_error`  | 夹爪目标 − 实测夹爪值          | 2        |
| `pose`           | 实测末端位姿                   | 12       |
| `pose_action`    | 关节目标的 FK 位姿             | 12       |

图像与深度仍使用既有的 `observations/images/<cam>` / `observations/depth/<cam>`，因为它们是
跨进程传输命名空间，而非 robot 状态向量。

## Edge 与采集观测

发布器按物理臂顺序，把 `joint` 与 `gripper` 组装为每臂 `[j1..j6, gripper]`，得到
`observations/qpos`；把 `joint_action` 与 `gripper_action` 同样组装为 `action`。双臂时二者均为
14 维。`joint_error` / `gripper_error` 同样组装成 14 维 `observations/qpos_err`。误差在
`BaseRobot.sample_state()` 内按同拍目标和实测值计算，Edge 只消费，不缓存或反推机器人目标。
位姿映射为 `observations/pose` / `observations/pose_action`。

`state_layout()` 描述的也是采集边界布局：每臂 7 维，标签为 `j1..j6, gripper`。

## robot 内部布局

机器人类只声明物理臂顺序与可变的每臂宽度：`ARM_NAMES`、`JOINT_DIM_PER_ARM`、
`POSE_DIM_PER_ARM`。整机 `joint_dim()` / `pose_dim()` 根据每臂宽度和臂数推导；夹爪契约固定每臂一个
开合值，因此 `gripper_dim()` 直接等于臂数。不再维护 `QPOS` / `POSE` / `GRIPPER` 这组三份总维度。
`POSE_DIM_PER_ARM = 0` 明确表示机型不提供位姿，因此不会误分配位姿观测区。

`BaseRobot` 分别维护：

-   `joint_command`：本控制拍实际下发的关节指令，长度 `joint_dim()`；
-   `joint_target`：最新请求的最终关节目标，长度 `joint_dim()`；
-   `gripper_command`：本控制拍实际下发的夹爪指令，长度 `gripper_dim()`；
-   `gripper_target`：最新请求的最终夹爪目标，长度 `gripper_dim()`。

`command` 表示当前控制拍，`target` 表示最终目标；二者不使用 `action` 命名。控制拍对关节目标做
`step_rad` 限速，对夹爪目标直接跟随，然后以两个显式参数交给 `_apply_commands()`。

具体机型以声明驱动装配和数据展开：`ARM_NAMES` 定义唯一物理臂顺序，`ARM_CONTROLLERS` 将物理臂
映射到执行控制器；带主手的遥操作机型另用 `MASTER_CONTROLLERS` 声明同一物理臂对应的输入控制器。

### 单臂机型复用双臂机器档案

机器档案描述整台机器的现场接线，机型只选择自己使用的硬件子集。`single_piper` 固定使用
`left_master + right`，执行臂名保持为 `right`，头部与右腕相机为 `cam_head / cam_right_wrist`。
它与 `dual_piper` 共用机器档案；左从臂、右主臂及左腕相机不构造、不连接，损坏或缺席不阻碍采集。
已知但未启用的端口、相机、TCP 与深度配置可保留；未知资源名仍须报错或告警。
逐臂遥操作偏置按执行臂 `right` 选择，左主臂与右从臂的跨侧零位差需现场核验。

单臂复用双臂的观测、动作、夹爪死区、FK/IK、TCP、遥操作与相机时间戳通路，
默认同样启用深度和采集帧头跳过。状态与 action 为右臂 7 维、pose 为 6 维；
不填充虚构的左臂数据。Edge 为 `single_piper` 注册相同布局的薄适配器。
机器档案用于现场接线及逐臂参数，机型配置负责单臂 home 和数据目录；不把双臂 episode
与单臂 episode 当成相同维度的数据集。
子类的连接、观测、遥操作读取和命令下发均按 `ARM_NAMES` 遍历，并使用
`JOINT_DIM_PER_ARM` / `POSE_DIM_PER_ARM` 切块。控制器返回的关节块不足声明宽度时必须立即报错，
不能静默拼成短向量。

原始观测中的 `joint_action` / `gripper_action` 是采集映射键，取对应 `target`（无目标时回退实测）；
它们只在 server 边界用于组装数据集 `action`，不是 robot 内部状态变量。

## 共享内存

共享内存 v8 分区：`qpos | qpos_err | action | pose | pose_action | images | depths`。三个状态向量均在
robot server 发布前完成每臂 7 维组装，不另设夹爪分区；同一个 frame sequence 保证三者同帧。

## 相关文档

-   [下发布局](./robot_pipeline_action_layouts.md)
-   [位姿动作](./robot_pipeline_cartesian.md)
-   [末端 TCP](./robot_pipeline_tcp.md)
-   [运行时](./robot_pipeline_runtime.md)
