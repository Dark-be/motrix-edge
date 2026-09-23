# robot-pipeline 动作空间与观测契约（joint / pose / pose_delta / gripper）

## 摘要

动作空间是**各自只表达一件事**的空间：`joint`（每臂 6 关节角）/ `pose`（每臂 `xyz + rpy`，
**绝对目标**）/ `pose_delta`（每臂 `xyz + rpy`，**增量**）/ `gripper`（每臂 1 夹爪）。
夹爪不混在 `joint` / `pose` 的值里——**每个空间的值都是「每臂等长」的向量**，第 7 维
（夹爪）的隐式约定消失。

观测则是**常驻**、各自一个键（都与动作空间无关）：`observations/qpos` = 关节角、
`observations/gripper` = 夹爪、`observations/pose` = 末端位姿（由**同一帧关节角**正解）、
`observations/pose_target` = **关节段目标的正解位姿**（底层位姿目标）——数采 / VLA 拿到的
永远是关节角 + 夹爪。

`pose_delta` 的增量**叠加在机器人自己的关节段目标上**（不是实测位姿）：底层是 MIT 力矩控制，
实测关节恒落后目标一个稳态误差，拿实测当基准会把误差写进新目标、逐步累积——见
[robot-pipeline 位姿动作（求解器）](./robot_pipeline_cartesian.md#位姿增量pose_delta)。

底层控制通路（关节目标 + MIT）与逆解时机（收到位姿目标时解一次）**不变**。

## 目标与约束

-   **一个空间一件事**：`joint` 只给关节角、`pose` 只给**绝对**位姿、`pose_delta` 只给位姿
    **增量**、`gripper` 只给夹爪。没有「第 7 维默认是夹爪」这类隐含约定，调用方不必记布局。
-   **每臂等长**：各空间的值都按 `ARM_NAMES` **逐臂展开**，故维度 = 每臂维数 × 臂数
    （单臂机器人因此与双臂同构，无需特例）。
-   **值同形、语义靠空间标记**：`joint` / `pose` / `pose_delta` 的扁平维度**相同**（双臂都是
    12），只能靠 `action_space` 区分（**下发方向**）；观测则分键常驻，不做动态语义切换。
-   **增量只叠在目标上**：`pose_delta` 以**当前关节段目标**的正解位姿为基准，不是实测位姿
    （见 [位姿增量](./robot_pipeline_cartesian.md#位姿增量pose_delta)）——底层 MIT 无重力前馈，
    实测恒落后目标一个稳态误差，以实测为基准会把误差逐步累积进目标。
-   **目标位姿可观**：底层位姿目标 = `observations/pose_target`（`POSE > 0` 的机器人始终发布），
    与实测位姿同系可比——「目标 − 实测」即当前稳态误差。
-   **下发只写自己那一段**：三个空间各自只改目标的一部分，互不覆盖（只给夹爪时不会顺带
    把关节目标重置）。
-   **观测与动作同源**：`observations/pose` 与下发 `pose` 目标共用同一运动学模型 / 同一坐标系，
    「读到的」与「下发的」可直接比较（现场标定仍见 `robot-pipeline/scripts/verify_cartesian.py`）。

## 动作空间契约

| 空间         | 每臂维度 | 双臂维度 | 语义                                                     | 单位            |
| ------------ | -------- | -------- | -------------------------------------------------------- | --------------- |
| `joint`      | 6        | 12       | 关节角**绝对目标**                                       | 弧度            |
| `pose`       | 6        | 12       | 末端位姿**绝对目标**（`xyz + rpy`，法兰坐标系）          | 米 / 弧度       |
| `pose_delta` | 6        | 12       | 末端位姿**增量**（叠加在**当前关节段目标**的正解位姿上） | 米 / 弧度       |
| `gripper`    | 1        | 2        | 夹爪开合**绝对目标**                                     | 归一化 `[0, 1]` |

-   **缺省 `joint`**：不带 `action_space`（HTTP 字段 / CLI 参数）时按 `joint` 解释，
    与「只发 `<qpos>`」的旧调用方等价。
-   **声明式支持**：机器人用类常量 `ACTION_SPACES` 声明（`single_piper` 未接入位姿时为
    `("joint", "gripper")`，`dual_piper` 为 `("joint", "pose", "pose_delta", "gripper")`）；
    adapter 侧同名常量，`GET /v1/adapters` 的 `capabilities.action_spaces` 对外广告。
-   **`pose` / `pose_delta` 的解算仍归机器人**：收到位姿（增量）目标时调控制器的**静态**转换
    函数（`PiperController.pose_to_joint`）解一次 → 写关节目标；解算失败回 422 且**不改既有目标**。
-   **增量的基准是机器人自己的目标**：`target_pose = FK(关节段目标) + [Δxyz, wrap(Δrpy)]`，
    逆解起点（IK seed）同样用**当前关节段目标**——就近解、不甩关节，且不受 MIT 稳态误差污染。
-   **未声明即不支持**：`pose_delta` 与 `pose` 一样是声明式能力；旧机器人不声明时下发 →
    拒绝（**不会**被静默按实测位姿当基准处理）。

## 观测契约

| 键                          | 内容                                                                   | 维度   |
| --------------------------- | ---------------------------------------------------------------------- | ------ |
| `observations/qpos`         | **关节角**（始终是关节，与动作空间无关）                               | 6 / 臂 |
| `observations/gripper`      | 实测夹爪开合                                                           | 1 / 臂 |
| `observations/pose`         | 实测末端位姿（`xyz + rpy`，米 / 弧度，法兰系）——不提供位姿时该键不出现 | 6 / 臂 |
| `observations/pose_target`  | **目标位姿** = `FK(关节段目标)`——底层位姿目标，`POSE > 0` 时始终发布   | 6 / 臂 |
| `action`                    | **关节段目标**（底层实际控制量，始终关节语义）                         | 6 / 臂 |
| `observations/images/<cam>` | 相机帧（JPEG / raw，见运行时文档）                                     | —      |
| `timestamp`                 | 观测线程打点（见 [运行时](./robot_pipeline_runtime.md)）               | 1      |

-   **观测与动作空间无关**：发什么动作都不会改观测的键与语义——`joint` / `pose` / `pose_delta`
    / `gripper` 只是**下发方向**的区分。这样数采（`.mcap` 里的 `observations/qpos`）拿到的永远
    是关节角，不会因为“上一次发的是位姿”而变成位姿。
-   **位姿始终可读**：由**同一帧关节角**正解（机器人侧运动学，edge 不做 FK），所以读到的位姿与
    下发的 `pose` 目标同系可直接比对。
-   **目标也可读**：`observations/pose_target` 与 `observations/pose` 同一套 FK、同一拍——增量
    原语（`pose_delta`）的解算结果因此对上位可见（`settle` 不必自己攒基准），「目标 − 实测」
    还能直接量出 MIT 稳态误差。
-   **取数入口**（子类实现，同一拍取）：`get_observation_qpos()`（关节）、
    `get_observation_gripper()`（夹爪）、`get_observation_pose(qpos=None)`（实测位姿，缺省不
    提供）、`get_target_pose()`（目标位姿，缺省不提供）。
-   **`action` 是关节目标**：底层就是一条关节通路，`pose` / `pose_delta` 早已在落 target 时解算
    成关节；想看位姿就看 `observations/pose`（实测）/ `observations/pose_target`（目标）。

## 控制通路（不改：底层始终是关节控制）

| 动作空间     | 下落点                                                   | 落地后                         |
| ------------ | -------------------------------------------------------- | ------------------------------ |
| `joint`      | 关节段目标 = 给定关节角（维度校验）                      | 关节段限速插值 → `set_joint`   |
| `pose`       | 关节段目标 = **一次静态逆解**的结果                      | 同上（底层不知道它曾是个位姿） |
| `pose_delta` | 关节段目标 = **目标位姿正解 + 增量**后一次静态逆解的结果 | 同上                           |
| `gripper`    | 夹爪段目标 = 给定夹爪值（钳 `[0, 1]`）                   | 夹爪段直接跟随 → `set_gripper` |

-   **没有第二种控制模式**：`step()` 每拍只做「关节段按 `step_rad` 限速插值 + 夹爪段直接跟随」，
    然后 `_apply_action()` 拆段下发；不引入 `move_p` / 力控，`set_joint` 只给 MIT 的
    `p_des`（`kp` / `kd` / `t_ff` 全用缺省值）——**本次位姿（阻抗）控制无力矩前馈**：
    `t_ff = 0` / `v_des = 0`，既不补重力 / 摩擦，也不做力控。
-   **下发空间只是“目标怎么写”**：`joint` 直写关节段、`pose` 解算后写关节段、`gripper` 写夹爪段；
    三者落地后完全同一条通路，观测也不会因此改变。
-   **目标只有一条**：底层目标向量 = `[关节段 | 夹爪段]`（关节段在前），各空间只写自己那段，
    因此 `gripper` 命令不会把机械臂拉回旧关节目标。

## 机器人内部状态（实现约定）

控制拍仍只有一条通路，内部只维护**一条目标向量**（关节段在前 + 夹爪段在后）：

| 字段                                     | 内容                                     | 维度             |
| ---------------------------------------- | ---------------------------------------- | ---------------- |
| `action` / `target_action`               | 底层当前位置 / 目标 `[关节段 \| 夹爪段]` | `QPOS + GRIPPER` |
| `action[:QPOS]` / `target_action[:QPOS]` | 关节段（当前位置 / 目标）                | 6 / 臂           |
| `action[QPOS:]` / `target_action[QPOS:]` | 夹爪段（当前位置 / 目标）                | 1 / 臂           |

-   限速插值（`step_rad`）只作用于关节段，夹爪段每拍直接跟随目标；下发（`set_joint` +
    `set_gripper`）逐段处理，行为与原先「14 维一起插值」等价（夹爪本就不插值）。
-   各空间只写自己的段：`joint` → 关节段；`pose` → 解算后写关节段；`gripper` → 夹爪段。
    因此 `execute(gripper)` 不会让机械臂回到旧关节目标（未初始化的段以**当前实际状态**补上）。
-   遥操作：主臂读数拆成「关节段 + 夹爪段」，`absolute` / `delta` 两套映射对两段同源处理
    （见 [遥操作](./robot_pipeline_teleop.md)）。
-   配置：`init_joint`（关节）与 `init_gripper`（夹爪）分开，默认全 0 / 全 1，长度 = 12 / 2。
    配置键一律以**空间名**结尾（`init_joint` / `init_gripper`）——不再叫 `init_qpos`，
    免得被读成「当前空间的值」（`qpos` 这个名字留给**下发/观测的值参数**本身）。

## 观测共享内存布局（v5）

单块共享内存，**区域顺序**：`header | qpos | action | gripper | pose | pose_target | images`。

| header 字段                                                     | 说明                                                                                      |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `version`                                                       | **5**（v2 = 仅 qpos+action+images、v3 = 位姿区在 action 后、v4 = 无目标位姿区，均已废弃） |
| `qpos_dim` / `qpos_offset`                                      | 关节角维度（双臂 12）/ 偏移                                                               |
| `action_dim` / `action_offset`                                  | 关节段目标维度（与 `qpos_dim` 相同）/ 偏移                                                |
| `gripper_dim` / `gripper_offset`                                | 夹爪维度（双臂 2）/ 偏移                                                                  |
| `pose_dim` / `pose_offset`                                      | 位姿维度（双臂 12）/ 偏移；`0` = 本机不提供位姿，不占该区                                 |
| `pose_target_dim` / `pose_target_offset`                        | 目标位姿维度（与 `pose_dim` 同为 0 / 12）/ 偏移                                           |
| `image_*` / `running` / `capturing` / `timestamp` / `frame_seq` | 同前                                                                                      |

-   **v2 / v3 / v4 不再兼容**（区域顺序与含义都变了）：读者发现 `version != 5` → 拒绝 attach 并按
    「陈旧布局」处理（机器人进程需重启）。
-   **位姿目标区随位姿区同生共死**：`pose_dim = 0`（本机不提供位姿）时该区同样为 `0`，不占空间。
-   写者与读者共用本模块（`motrix_edge.adapter.shm_contract`），机器人侧直接复用，布局单点维护。

## Edge 侧映射

-   **能力面**：`ActionSpace` = `JOINT` / `POSE` / `POSE_DELTA` / `GRIPPER`；`ACTION_SPACES` 按适配器
    声明；维度**按空间**：`action_dims: {joint: 12, pose: 12, pose_delta: 12, gripper: 2}`
    （`http_contract` 单点，adapter / server / 前端共用）。
-   **校验**：`execute` / `rollout` 先归一化空间（不在 `ACTION_SPACES` → 拒绝），再按**该空间**
    的维度校验动作；`pose` / `pose_delta` 要求**全臂启用**（未启用臂没有「同空间 home」可填 → 位姿
    语义下会误发）；`gripper` 单臂语义与臂裁剪一致（未启用臂用夹爪 home 填充）。
-   **观测**：`observe()` 给出 `KEY_QPOS`（关节角）、`KEY_GRIPPER`、`KEY_POSE`（实测位姿，
    机器人提供时）、`KEY_POSE_TARGET`（**目标位姿** = `FK(关节段目标)`）、`KEY_ACTION`（关节段
    目标）——与动作空间无关，数采 / 策略拿到的永远是关节角 + 夹爪。
-   **状态 / 预览**：`/v1/preview` 与 WebRTC 帧的观测字段按新键名给出
    （`qpos` + `gripper` + `pose` + `pose_target` + `action`）；预览的「末端位姿」区块读 `pose`
    （实测）。
-   **RPent 转换**：桥接层保留 RPent 的「每臂 7 维 = 值 + 夹爪」外观——读侧由
    「关节段 / 位姿段 + 夹爪」拼出，写侧拆成「值」与「夹爪」两条命令下发。
-   **策略**：`bind_adapter` 的动作维度按策略声明的空间取（ACT / openpi 走 `joint`）。

## 变更影响面（破坏性）

| 项               | 旧                                                        | 新                                                                                                             |
| ---------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 动作维度（双臂） | `joint` 14 / `pose` 14                                    | `joint` 12 / `pose` 12 / `pose_delta` 12 / `gripper` 2                                                         |
| 观测键           | `observations/qpos`(14，含夹爪) + `observations/pose`(12) | `observations/qpos`(12) + `observations/gripper`(2) + `observations/pose`(12) + `observations/pose_target`(12) |
| 共享内存         | v2 / v3                                                   | **v5**（区域顺序 = qpos / action / gripper / pose / pose_target / images）                                     |
| 机器人常量       | `QPOS`=14 / `POSE`=12                                     | `QPOS`=12 / `POSE`=12 / `GRIPPER`=2                                                                            |
| 配置             | `init_qpos` = 14（含夹爪）                                | `init_joint` = 12 + `init_gripper` = 2                                                                         |

机器人进程与 Edge **必须同版本**部署（共享内存版本 + 动作维度都变了）；重启顺序仍是
**机器人进程 → Edge**，重启前清理共享内存（见 [运行时](./robot_pipeline_runtime.md)）。
