# robot-pipeline 末端 TCP 位姿（帧开关）

## 摘要

`pose` / `pose_delta` 动作与 robot 原始观测 `pose` / `pose_action` 的**原点**可从法兰（缺省）切到
**工具末端（TCP）**：TCP 相对法兰的偏移写在机器人配置（`robot.tcp`），纯平移、可逐臂覆盖。读数与写数
**仍是同一个坐标系**（既有契约「读与写必须同系」不变），只是系名随开关从 `flange` 变成 `tcp`；帧名与
偏移量由机器人进程**上报**给 Edge（Edge 不重复配置）。开关关闭（缺省）时行为与现在完全一致。

## 目标与约束

**目标**

-   位姿（绝对目标 / 增量 / 实测 / 目标）的语义从「法兰原点」变为「**TCP** 原点」——`move_delta(x=0.05)`
    让尖端前移 5 cm，`look_at` 保持**尖端**位置不动。
-   偏移**可配**（`dual_piper.yml`），两臂可不同（左右腕相机 / 探针支架不等长是常态）。
-   开关关闭（缺省）时行为与现状**逐字节一致**。

**非目标（本期）**

-   不做**带姿态**的 TCP（第一期只允许纯平移，理由见下）；
-   不新增动作空间（`pose` / `pose_delta` 语义平移，不引入 `tcp_pose`）；
-   不搬深度反投影（见「后续」）。

**约束**

-   `PiperKinematics`（DH / FK / IK）与 `PiperController.joint_to_pose` / `pose_to_joint` **保持纯法兰、
    无配置**：`scripts/verify_cartesian.py` 用 `fk(q)` 对照 SDK 的 `get_flange_pose()`，混入 TCP 会让
    DH / 零位核对整体偏一个常量、失去判据。
-   底层仍只有关节通路（MIT），位姿解算时机（收到目标时解一次）不变。
-   `POSE_FRAME` 是既有契约（[机器人适配器](./motrix_edge_adapter.md)）：读 / 写同系，且系名必须**如实**。

## 整体原则

1.  **一处换算**：帧换算只发生在机器人基类（`BaseRobot`）的「位姿 ↔ 法兰」边界；运动学与控制器不动。
2.  **单一事实来源**：偏移只在机器人配置；Edge 通过上报获知，不另配一份。
3.  **契约不变、帧名可变**：动作空间 / 维度 / 单位 / 增量语义 / 判到位口径全部不变。

## 帧与配置

### 帧定义（单点）

`T_flange_tcp` = TCP 在**法兰系**下的位姿（本期纯平移，`[x, y, z]` 米，旋转 = 单位矩阵）：

-   基座 ← TCP：`T_base_tcp = T_base_flange · T_flange_tcp`（**读**：观测位姿）；
-   写侧反向还原：`T_base_flange = T_base_tcp · T_flange_tcp⁻¹`（**写**：解算前还原法兰）。

「沿工具方向伸出的尖端」= `[0, 0, L]`——法兰 `+z` 是工具伸出方向，与 [`look_at` 的指向轴](./motrix_edge_rpent_bridge.md)
和 `j6` 转轴一致（见 [位姿动作](./robot_pipeline_cartesian.md)）。

**为什么只允许纯平移**：TCP 的**朝向**若与法兰不同，RPent 的工具三轴（`ego_axes` 的 `forward` /
`left` / `up`）与 `look_at` 的「法兰 `+z` 指向 `target`」会同时失真——它们都是按法兰系写的。纯平移下
朝向不变，这两处**完全不受影响**；配置里出现非零旋转直接报错（不静默忽略）。

### 配置（`robot-pipeline/src/config/<机型>.yml` → `robot.tcp`）

```yaml
robot:
    tcp:
        enabled: false # false（缺省）= 法兰原点（现状）；true = 位姿读 / 写都以 TCP 为原点
        offset: [0.0, 0.0, 0.1] # TCP 在**法兰系**下的位置（米，纯平移）
        arms: {} # 可选：逐臂覆盖（键 = 控制器名 left / right），如 right: {offset: [...]}
```

-   逐臂覆盖沿用既有写法（`robot.gravity.arms.<arm>` 的「公共键 + 臂覆盖」，见
    [重力补偿与阻抗控制](./robot_pipeline_impedance.md)）；某臂未覆盖 → 用公共 `offset`。
-   校验（构造期，失败即报错）：`offset` 必须是 3 个有限数；出现旋转键（`rpy` 等）→ 报错；
    `enabled: true` 但缺 `offset` → 报错。
-   **与标定产物的关系**：`frames.json` 的 `arms.<arm>.probe_tip`（探针尖在法兰系的位置，见
    [统一坐标系与外参](./robot_pipeline_frames.md)）与本配置的 `offset` 是**同一类量**——末端装的若就是
    标定探针，两者应一致。本期只在文档里说明；一致性验收见「后续」。

## 读 / 写链路

| 环节              | 位置                                                      | 本期改动                                    |
| ----------------- | --------------------------------------------------------- | ------------------------------------------- |
| 读（实测位姿）    | `DualPiperRobot._pose_of_joints()` / `TestRobot` 同名方法 | FK 结果再乘 `T_flange_tcp`                  |
| 读（目标位姿）    | 同一换算层（`get_target_pose()` 复用它）                  | 自动跟随                                    |
| 写（绝对 / 增量） | `DualPiperRobot._solve_pose()` / `TestRobot` 同名方法     | 喂 `pose_to_joint` 前还原成法兰             |
| 增量基准          | `BaseRobot._absolute_pose_target()`                       | **不动**（基准取目标位姿，自动是 TCP 位姿） |

-   **两个观测键同生共死**：robot 原始键 `pose` 与 `pose_action` 共用同一换算层，故始终同系、可直接比较
    （「目标 − 实测」仍是 MIT 稳态误差）。
-   **`pose_delta` 语义不变**：仍是**基座系**的 `[Δxyz, wrap(Δrpy)]` chart 相加（不是 TCP 系相加），
    只是基准量从法兰位姿换成 TCP 位姿。上位按工具系给增量时的换算链（见 RPent 对接契约「增量」一节）
    不变——纯平移下工具轴与法兰轴同向，只有原点变了。
-   **完全不受影响**：限位裁切 / IK / `step_rad` 限速 / 动作空间枚举与维度 / 共享内存布局 /
    `settle` 判到位 / 采集 JSON 的 `state_dims` 维度。

换算只用 `robot/kinematics/transforms.py` 已有的 `rpy_to_matrix` / `matrix_to_rpy` / `make_transform`
（纯 numpy、无硬件依赖，可离线单测）。

## 运行期开关与上报

**机器人 → Edge**（`/v1/health` 的**运行位**字段；字段名单点在 `src/motrix_edge/adapter/http_contract.py`）：

| 字段         | 取值                 | 说明                                                    |
| ------------ | -------------------- | ------------------------------------------------------- |
| `pose_frame` | `flange` / `tcp`     | 位姿（读 + 写）所在坐标系，与 `POSE_FRAME` 同词表       |
| `tcp_offset` | `[x, y, z]` / `null` | TCP 在法兰系下的位置（米）；位姿系为 `flange` 时 `null` |

字段名刻意用 `tcp_offset` 而不是 `tcp`（避免与传输层 TCP 混淆）。

**开关**：`POST /v1/tcp`（`{enabled, offset?}`，Edge 侧命令 `robot tcp`）——`offset`（3 个数，米）是
**运行期值**，覆盖配置的公共偏移但不写回 yml（重启回落配置；要长期生效写进 `robot.tcp`）。

-   机器人侧：**直接同步切换**（HTTP 线程调 `BaseRobot.set_tcp()`）——先校验（与构造期同一套
    `resolve_tcp`）再改运行位：偏移非法 → **422** 且运行位不变（失败即零副作用）；不做单独的
    预检接口。只改两个字段（`tcp_offsets` / `pose_frame`，同一行赋值，控制线程看不到中间态），
    **不跑控制拍、也不动任何目标**。
-   只改**语义**（`pose` / `pose_delta` 与两个位姿观测键的原点），**不动机目标**：在飞目标对应的
    物理位姿不变，机械臂不会跳。
-   Edge 侧：`robot tcp <true|false> [offset=[x,y,z]]`，**离开会话才能切**——位姿系决定
    `observations/pose` 的原点与 `env_meta` 的口径，同一 episode 内前后不一致的数据没法用
    （`robot teleop` 人工接管不受此限，它是会话中的正常操作）。

**Edge 侧读回（不留影子）**

-   `pose_frame()` / `tcp_offset_by_arm()` 一律从 `health()`（`GET /v1/health`，节点本来就每 2s
    探活一次）读回，**不单独发请求、也不缓存开关状态**：切换后的真值同样走 health，`POST /v1/tcp`
    只负责下发命令。失联 → **保留上一次已知值**（宁可稍旧，也不退回「按法兰理解」，那会静默差一个
    常量）；从未成功过（旧机器人进程 / 进程不可达）→ 回退适配器声明的 `POSE_FRAME` 类常量。
-   `env.get_env_meta` 的 `pose_frame` / `tcp_offset` 因此如实——「数值 + 帧名一起给」，与
    [统一坐标系与外参](./robot_pipeline_frames.md) 同一口径。
-   `DepthService._coordinates()`（`server/depth.py`）合成腕相机外参前，把观测位姿从 TCP 原点还原成
    法兰：`T_base_flange = T_base_tcp · T_flange_tcp⁻¹`（偏移为零 / 非 TCP 时是恒等）。深度查询本身、
    内参、`xyz_camera` 都不受影响。
-   RPent 面（`server/rpent/service.py`）**不改**：`move_to` / `look_at` / `move_delta` 都按「位姿系」
    解释（世界点进、位姿目标出），换系后语义自动变成「对**尖端**操作」，而 `get_env_meta.pose_frame`
    会如实告诉 agent 现在是哪个系。
-   **既有消费者要一起改**：`robot-pipeline/scripts/verify_extrinsics.py` 把 `/v1/preview` 的
    `observation.pose` 当**法兰**位姿用（探针尖绝对精度比对）→ 按 `pose_frame` 反算，或在 TCP 开启时
    明确提示「先关 TCP 再验收」。

## 影响面小结

| 项                                  | 是否变                         |
| ----------------------------------- | ------------------------------ |
| 动作空间枚举 / 维度 / layout        | 不变                           |
| 观测键集合 / 共享内存布局版本       | 不变                           |
| `pose` 值的语义（原点）             | **变**（法兰 → TCP，开关控制） |
| `POSE_FRAME` / `env.get_env_meta`   | **变**（如实反映系名 + 偏移）  |
| 腕相机深度反投影的世界坐标          | **不变**（Edge 侧补回法兰）    |
| `settle` / RPent 判到位 / 限位 / IK | 不变                           |

## 后续（不在本期）

1.  **深度反投影搬到机器人侧**：反投影的四个输入（深度图 / 内参 / 外参 / 法兰位姿）**都在机器人进程
    里**，而帧模型与变换数学已在共享包 `motrix_edge.geometry`（robot-pipeline 本就依赖它，见
    `robot/calibration/store.py`）——搬过去后坐标链在机器人内部闭合，Edge 不再需要 `tcp_offset`。
    代价：新增跨项目端点 + 机器人侧帧缓存（保住「查询与 `/v1/preview` 同帧」语义）+ 本设计文档
    「分层与归属」重写 → 单独立项。
2.  **TCP 带姿态**：需同步定义工具三轴（`ego_axes`）与 `look_at` 指向轴在 TCP 系下的含义。
3.  **`probe_tip` ↔ `tcp_offset` 一致性验收**：标定产物与配置两条数值通道可能打架，需要一个可执行的
    核对步骤。
