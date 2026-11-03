# robot-pipeline 遥操作（绝对映射 / 增量接管）

## 摘要

robot-pipeline 的遥操作由三个正交维度组成：**跟随空间**（`joint` / `pose`）、**映射模式**
（`absolute` / `delta`）和位姿跟随时的**轴白名单**（`follow_axes`）。关节跟随保留既有行为；位姿跟随
把主臂关节先正解为 `xyz + rpy`，按映射模式与轴白名单合成从臂目标，再逆解成关节目标。两者最终
共用一条链路——写 `joint_target` → `step_rad` 限速生成 `joint_command` → MIT 下发。

该能力只属于 robot-pipeline 的 teleop 映射：Edge 的 `rollout` / `execute`、动作 `layout` 与观测
契约均不改变。配置缺省 `follow_space: joint`，升级不改变既有真机行为。

目标操作顺序：通过关节遥操作建立初始姿态并等待跟踪到位；**先关闭，再重新映射设置新的锚点**。
关闭并确认状态后选择末端跟随（`follow_space=pose`）、增量模式（`mode=delta`）和轴白名单，
重新启用，在首个有效读数拍采集新锚点。xyz 位置比例同样在关闭期间配置。主臂与从臂
分别以启用后首个有效拍的主臂读数和从臂执行命令参考作为锚点，从臂从自己的初始位姿出发。两侧锚点不要求数值相同：
`delta` 映射的是主臂从其锚点移动了多少，不是把主臂绝对位置复制给从臂。

> **实现状态**：本地后端已接通位置比例、组合校验、状态与入队配置回显；独立控制台源码不在本仓库，
> 比例输入与逐臂编辑需在控制台仓库配套落地。本次不调整失败保持、RTC 或重复启用行为。

## 目标与约束

-   **接管不突变**：增量接管后的首次有效映射拍，从臂 target 应对应从臂**当前执行命令参考**——此前残留的
    （可能已发散的）推理 target 应被丢弃。当前实现仅在首次成功采锚点并完成映射后替换 target；
    首拍读数或映射失败仍继续追踪旧 target，失败拍的行为仍需单独定义；目标跳变会影响动作轨迹和数据质量。
-   **限速唯一出处**：增量链路只决定 `joint_target`，逐拍逼近幅度仍由 `step_rad` /
    `_step_toward()` 唯一负责，增量本身**不额外限幅**；该限速不提供碰撞、工作空间或失联停止保证。
-   **控制回路单写者**：主臂读取与 target 计算都在控制线程（`step()` 内），HTTP 线程只入队。
-   **不新增 edge 侧同义契约**：`mode` 字段与取值单点定义在
    `motrix_edge.adapter.http_contract`，robot server 复用；robot 层（`robot/`）保持契约无关。
-   **遥操作期间推理让位**：只要遥操作开着（**不分模式**：遥操作即人工接管），`rollout` 就被拒。
-   **锁定只作用于遥操作**：`follow_axes` 不进入 `execute` / `rollout`，关闭 teleop 后立即失效。
-   **底层通路不分叉**：位姿跟随只在 robot 层做 FK / 目标合成 / IK，控制器仍只接收关节目标；
    Piper 执行臂继续走 `set_joint()` → `move_mit()`。

## 模式与映射

| 模式               | 从臂 target                             | 用途（对外叫法）                   | 语义化命令       |
| ------------------ | --------------------------------------- | ---------------------------------- | ---------------- |
| `absolute`（缺省） | `master_now`（主臂绝对读数直连）        | 示教采集（主从同构、位姿已对齐）   | `robot teach`    |
| `delta`            | `slave_ref + (master_now − master_ref)` | 推理中人工接管，从臂动作连续不突变 | `robot takeover` |

> 两种模式都算「人工接管」（`rollout` 一律被拒，见下文）；差别只在从臂 target 的算法。

### 两个用途与命令别名

| 命令                           | 等价底层调用                      | 用途                 |
| ------------------------------ | --------------------------------- | -------------------- |
| `robot teach <true\|false>`    | `robot teleop <enabled> absolute` | 示教采集（主臂直连） |
| `robot takeover <true\|false>` | `robot teleop <enabled> delta`    | 人工接管（锚点增量） |

-   **命名口径**：_遥操作（teleop）= 人工接管控制权_（一个开关，开启期间 `rollout` 一律被拒）；
    `absolute` / `delta` 只是**映射算法**，对外分别叫「示教」与「接管」。状态里
    `teleop` + `teleop_mode` 即这两层（见 `/v1/adapters` / console「程控 / 遥操作（示教）/ 人工接管（增量）」）。
-   `robot teleop <true|false> [absolute|delta]` 是**底层命令**：模式可选、缺省 `absolute`
    （保持旧调用方与 `/v1/teleop {enabled}` 行为不变）；两个别名**不接受** `mode` 参数
    （模式由命令名唯一决定，显式传 → `rejected`）。
-   单点：命令名 → 模式在 `command/params.py::teleop_mode_for`（词表 `TELEOP_COMMAND_MODES`），
    三个命令共用 `command/config_commands.py::apply_teleop`（node 与会话同一实现）；
    命令名集合 `command/naming.py::TELEOP_COMMANDS`。

-   **覆盖范围**：关节与夹爪是**同一套语义**——关节和夹爪分别以各自锚点逐元素计算增量
    （夹爪 target = 夹爪锚点 + 夹爪增量，钳到 `[0,1]`，不经过关节限速插值）。子类的
    `_read_teleop_joint()` 只返回主臂绝对扁平读数（偏置不含在内）。
-   **位置增量比例与常量偏置**：`pose` + `delta` 的 xyz 位置比例可按轴配置，缺省 `[1, 1, 1]`，保留既有
    1:1 行为。旋转 `rpy` 永远按最短角差 1:1 映射，不提供旋转比例参数。基类依次执行
    `master_joint + joint_offset → FK → master_pose + pose_offset`；`joint_offset` 是逐关节 rad，
    `pose_offset` 是当前位姿坐标系下逐分量的 `xyz`（m）+ `rpy`（rad），`pose_position_scale` 是
    三维 xyz 位置比例，三者都可逐臂配置或由全臂共用一组值。比例三项依次对应 `x/y/z`，不包含旋转；
    `pose_position_scale` 只作用于 `delta`
    模式的主臂相对锚点位置增量：`slave_xyz_target = slave_xyz_ref + pose_position_scale *
(master_xyz_now - master_xyz_ref)`；角度增量则为 `wrap(master_rpy_now - master_rpy_ref)` 并
    1:1 叠加到从臂姿态锚点。位置比例允许有限的正数、零（该轴不跟随）或负数（反向跟随）。
    `absolute` 仍将主臂绝对位姿直接映射到从臂，不应用位置比例；YAML 配置非单位比例却选择 pose + `absolute` 时明确拒绝；
    本次显式传入比例仅接受 pose + delta，即使是单位比例也不接受其他组合，避免比例设置被静默忽略。`joint_offset` 在 FK 前、`pose_offset` 在
    FK 后叠加。joint 跟随的 delta 中，常量关节偏置会抵消；pose 跟随的 delta 中，
    `FK(q_now + joint_offset) - FK(q_ref + joint_offset)` 仍受关节偏置影响，不能认为其抵消。
    `joint_offset` 应用于已知的主臂关节零点校准，缺省为零；不能用它消除任意主从初始位姿差。
    FK 后的常量位置偏置在 delta 差分中抵消，姿态偏置按 RPY wrap 语义计算。`pose_offset` / `pose_position_scale` 只参与 pose
    跟随，不改变 joint 跟随。

### 跟随空间与位姿轴锁定

`robot.teleop` 在机器人配置中声明跟随空间与缺省轴白名单；下例偏置为示例值，并非必填：

```yaml
robot:
    teleop:
        follow_space: pose
        follow_axes: [x, y, z, pitch, yaw] # 未列 roll：锁在启用 teleop 时的从臂锚点
        joint_offset: [0, 0.1, 0, 0, 0, 0] # 每臂关节偏置，rad
        pose_offset: [0, 0, 0.02, 0, 0, 0] # 每臂 xyz（m）+ rpy（rad）偏置
        pose_position_scale: [1, 1, 1] # pose+delta 的 xyz 比例，旋转固定 1:1
```

`pose_position_scale` 可以给一组三轴值供全部臂共用，也可以按物理臂给值；例如双臂配置可写成
`pose_position_scale: {left: [0.5, 0.5, 0.5], right: [0.7, 0.7, 0.7]}`。运行时覆盖沿用同一形状；遗漏臂使用单位比例，不与 YAML 按臂合并。
每项必须是有限数值；`0` 表示该方向不跟随，负值表示反向，绝对值大于 `1` 表示放大主臂位移。
映射不会额外截断目标增量，执行速度仍由现有 `step_rad` 限制；过大的目标仍可能超出工作空间或导致 IK 失败。

| `follow_space`  | 主臂输入到从臂目标的处理                                      | `follow_axes`                                        |
| --------------- | ------------------------------------------------------------- | ---------------------------------------------------- |
| `joint`（缺省） | 主臂关节直接做 absolute / delta 映射                          | 不允许配置                                           |
| `pose`          | 主臂关节 FK → 位姿映射（delta 的 xyz 可缩放、rpy 为 1:1）→ IK | 非空、无重复的 `x/y/z/roll/pitch/yaw` 子集；缺省全部 |

Console / CLI 可在 teleop 关闭时选择 `joint` / `pose`，并在**每次开启**时通过 `follow_space` 下发；pose
模式还可通过 `follow_axes` 覆盖轴白名单，未跟随轴即锁在本次启用的从臂锚点。`pose_position_scale`
可由 YAML 持久配置，也可在本次启用时覆盖；运行时覆盖只对本次启用有效，下次开启未显式传值时回到 YAML
配置。旧调用方不传时仍使用 YAML 缺省，位置比例缺省全 1。空间、锁轴或比例变化遵循“先关闭，再重新映射设置新的锚点”。
当前后端未强制该顺序：重复启用会覆盖参数并清空锚点，即使参数相同也会重锚定；
这属于既有行为，不作为客户端重试或配置入口。关闭只停止主臂刷新目标并清锚点，不清除已有运动目标。位置比例只对 `pose` + `delta` 有效；`absolute` 下传入非单位
比例明确拒绝。joint 跟随收到 `follow_axes` / `pose_position_scale` 会明确拒绝；不具备 pose 动作与 FK/IK
能力的机型收到 `follow_space: pose` 也会明确拒绝。

位姿跟随的锚点在启用后的首次有效主臂读数拍原子采集：
`master_pose_ref = FK(master_now + joint_offset) + pose_offset`，
`slave_pose_ref = FK(slave_command_ref)`。从臂参考依次取 `joint_command`、`joint_target`、
`init_joint`，与关节增量接管共用参考来源。不支持外力手动摆动从臂后按实测位置重新锚定；
切换前应通过关节遥操作完成初始姿态调整，并让从臂跟踪到位。未跟随轴始终取
`slave_pose_ref`；跟随轴按模式计算：

-   `absolute`：取 `master_pose_now` 对应分量；
-   `delta`：位置取 `slave_xyz_ref + pose_position_scale * (master_xyz_now - master_xyz_ref)`；姿态取
    `slave_rpy_ref + wrap(master_rpy_now - master_rpy_ref)`，姿态差与结果均 wrap 到 $(-\pi, \pi]$。

每拍以现有关节目标为 seed 做一次 IK；解算失败或位姿非有限时，本拍保持上一目标（控制器仍向该目标运动，并非停在当前位置）、累计失败计数并按
同一原因限流告警，不影响 health。初始状态通过关节遥操作建立；随后
关闭原遥操作并以 `follow_space=pose, mode=delta` 重新开启，首次有效控制拍会分别采集主从末端锚点，
从该从臂锚点开始按 xyz 位置比例跟随；旋转仍逐拍 1:1 跟随。这里锁定的是既有 `POSE_FRAME`（法兰 / TCP）中的 RPY chart 分量，
接近 pitch = ±90° 时仍受欧拉角奇异性约束；它是**目标运动学约束**，不是外力下的无限刚度。

## 增量锚点

**固定锚点的用途**：直接用当前值减固定参考，避免逐拍积分的数值漂移，并明确本次映射的初始条件。
锚点式将接管瞬间的主从初始值差归入参考：人工把主臂摆到与从臂相近的位姿后进入 `delta`，主从差无论是 0.05 rad 还是
0.2 rad，从臂都从**自身执行命令参考**出发；主臂推多少、从臂走多少；主臂回锚点则从臂回锚点。

锚点在 `delta` 启用后的**首次成功读数那一拍**一并采样（同一个 `step()` 内，两者同拍）：

| 锚点                    | 取值                                                                          |
| ----------------------- | ----------------------------------------------------------------------------- |
| `teleop_master_ref`     | 该拍主臂扁平读数                                                              |
| `teleop_slave_ref`      | joint + delta 的关节目标参考：`joint_command` → `joint_target` → `init_joint` |
| `teleop_slave_pose_ref` | pose 跟随的从臂命令参考经 FK 得到的末端参考位姿                               |

-   **joint + delta 参考取当前执行命令**（`joint_command`）：接管瞬间增量恒 0 →
    `joint_target = slave_ref`，不会跳向此前残留目标。pose 跟随使用同一命令参考做 FK；不提供手动摆动从臂后的实测重锚定。
-   **采样推迟到首次成功读数**：处理启用命令后遥操作运行位已经开启，但锚点可能尚不可用。
    读数或锚点 FK 失败时下一拍重试，旧目标仍被追踪；锚点成功、目标 IK 失败时保留锚点继续重试映射。
    因此不能把启用回执视为已经采锚点或成功更新目标。
-   **锚点在接管期间固定**（`disable_teleop()` / 再次 `enable_teleop()` 时清空），
    即整段接管都以接管瞬间为基准，不会随时间重新对齐。

## 指令链路

### 当前契约

```
console / Edge adapter  ──POST /v1/teleop {enabled, mode?, follow_space?, follow_axes?}──▶  contract_server
        ▼（只入队）
env.commands 队列  ──▶  控制线程 _drain_commands  ──▶  robot.enable_teleop(mode, follow_axes, follow_space)
                                                       └─▶ 下一拍 step()：采锚点 → 写 target
```

-   `/v1/teleop` 请求体在 `enabled` 之外增加可选 `mode`（`absolute` / `delta`，缺省
    `absolute`）：**向后兼容**——只发 `{enabled: true}` 的旧调用方行为不变。
-   `follow_space` 可覆盖本次 joint / pose 空间，`follow_axes` 可覆盖本次 pose 轴白名单，省略时沿用 YAML。
-   当前可用流程：先 `robot teleop false`，确认关闭后执行
    `robot takeover true follow_space=pose follow_axes=x,y,z`。
-   HTTP 返回 `accepted` 表示命令已入队，不表示锚点或 IK 已成功。当前命令回显不应被解释为控制线程执行完成。

-   `mode` 非法 → HTTP 422（server 侧校验，robot 层再兜一层，非法模式保持原模式与 target）。
-   关闭请求应只传 `enabled: false`。当前 HTTP 入口仍校验 `mode` 的合法性，不应携带非法 mode。

### 位置比例与配置回显

`pose_position_scale` 支持共享三元组或逐臂对象。health / Edge 状态通过
`teleop_pose_position_scale` 上报比例，命令回执也提供对应配置。响应 `configuration_status: queued` 和 `configuration` 回显经过解析的入队配置；
控制线程应用后通过 health 上报，入队回执不承诺锚点或 IK 已就绪。

-   本次显式传入比例仅允许 pose + delta，包括单位比例；joint 或 absolute 请求携带比例字段应拒绝。
-   YAML 比例供 pose + delta 使用；选择 pose + absolute 而配置了非单位比例时拒绝，避免静默忽略设置。
-   校验三维、有限数值、已知臂与模式组合；非法请求不修改遥操作状态。
-   运行时覆盖仅对本次启用有效，下次省略则回到 YAML；关闭请求不应用映射配置。
-   带比例的 CLI 示例：先关闭并确认状态，再执行
    `robot takeover true follow_space=pose pose_position_scale=0.5,0.5,0.5`。
-   比例与空间、轴白名单一起在关闭后设置；仍使用现有从臂命令参考，不增加手动摆位重锚定。

## 遥操作期间的推理下发

遥操作一行开启即视为人工接管（**不区分 `absolute` / `delta`**），从臂 target 归人工，
**推理下发必须让位**：

| 指令                  | 遥操作中的行为                                                                        |
| --------------------- | ------------------------------------------------------------------------------------- |
| `rollout`（推理闭环） | **拒绝**：409 + detail，不改 target、不退出遥操作（`BaseRobot.rollout()` 返回 False） |
| `execute` / `reset`   | 照常执行（操作员明确抢回程控，`disable_teleop()` 结束遥操作）                         |
| `safe_stop`           | 照常执行（清 target + 结束遥操作）                                                    |
| `teleop=false`        | 人工交回：此后 `rollout` 自动恢复                                                     |

-   **双保险**：HTTP 线程用状态快照（`env.robot_rollout()` 查 `robot.teleop_enabled`）同步
    返回 409，调用方立刻可感知；控制线程执行时 `BaseRobot.rollout()` **再判一次**（权威）
    ——两者之间遥操作状态可能变化，以控制线程为准（该拍丢弃并记日志）。
-   **拒绝不置 `last_error`**：遥操作中被拒是**预期状态**（推理端在遥操作期间会持续尝试下发），
    不能把 `/v1/health` 打成不健康，只记日志。
-   Edge 侧：adapter 的 `rollout()` 遇 409 返回 False（日志限流），InferSession 持续推理时跳过
    该拍，遥操作关闭（`robot teleop false`）后自动恢复下发。

## 边界与未决

-   **遥操作 vs 程控优先级**：遥操作期间**仅** `rollout` 被拒（推理让位）；`execute` / `reset` /
    `safe_stop` 仍是程控 / 安全优先，执行即结束遥操作（见「遥操作期间的推理下发」）。
-   **采集与 RTC 的边界**：纯遥操作采集使用 `CaptureSession`，没有 RTC 或推理步进引擎，
    不存在交回模型或缓存模型动作的问题。本次末端遥操作采集方案不扩展 RTC 行为。
    `InferSession` 中的录制不改变推理会话性质：持续推理期间接管仍可能有 RTC 动作块。
    当前接管开始会清空 RTC，但持续循环随后仍推进引擎，因此不能保证交回后的首块使用交回时观测。
    这一推理接管问题留在推理/RTC 范围处理；`step_rad` 只限制追踪速度，不保证动作新鲜度。
-   **接管期间的数据**：接管与推理沿用同一套动作记录契约，但这本身不能保证目标或记录数值连续；
    是否在 episode 里标记接管区间（元信息字段）待定。**帧头跳过**已实现
    （`collector.skip_until_motion`，见 [robot-pipeline 运行时](./robot_pipeline_runtime.md)）：
    capture 开始后主臂未出现有效移动前不记录，避免每轮 episode 头部都带一段「还没开始动」的静止帧。
-   **主臂读不到期间被人工移动**：恢复读数后增量会跳变，但从臂仍受 `step_rad` 限速保护
    （每个关节的单拍命令变化受 `step_rad` 约束），但不能据此保证末端运动安全。
-   **主臂读数不可用（`None` / 读取异常）**：本拍**不刷新 target**（保持原值，不突变），只累计
    `teleop_read_failures` + **同一原因限流告警**；**不置 `last_error`**——接管中读不到主臂是预期
    状态（人还没接上 / 瞬时抖动），不能让 `/v1/health` 变不健康（否则一次 CAN 抖动会被 Edge
    当成硬件故障）。读取异常与 `None` 走同一条路径（`_refresh_teleop_targets` 统一 try/except），
    不会冒出 `step()`。
-   **单臂布局**：`step()` 按 `ARM_NAMES` 和每臂维度切块；`SinglePiperRobot` 与双臂机型
    共用机器档案，装配与映射见[单臂机器档案](./robot_pipeline_action_spaces.md#单臂机型复用双臂机器档案)。
-   **接管前的对齐由人工完成**：主臂**不可被程序驱动**（Piper leader 模式只读），因此不提供
    「推理期主臂跟随从臂」；接管前操作员手动把主臂摆到与从臂相近的位姿，剩下的差值由锚点
    零化（见「增量锚点」）。

使用解释、参数示例和重复启用的数值示例见 [末端遥操作使用说明](../../docs/robot-teleop-mapping.md)。
