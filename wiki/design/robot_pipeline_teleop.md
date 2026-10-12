# robot-pipeline 遥操作（绝对映射 / 增量接管）

## 摘要

robot-pipeline 的遥操作由三个正交维度组成：**跟随空间**（`joint` / `pose`）、**映射模式**
（`absolute` / `delta`）和位姿跟随时的**轴白名单**（`follow_axes`）。关节跟随保留既有行为；位姿跟随
把主臂关节先正解为 `xyz + rpy`，按映射模式与轴白名单合成从臂目标，再逆解成关节目标。两者最终
共用一条链路——写 `target_action` → `step_rad` 限速逼近 → MIT 下发。

该能力只属于 robot-pipeline 的 teleop 映射：Edge 的 `rollout` / `execute`、动作 `layout` 与观测
契约均不改变。配置缺省 `follow_space: joint`，升级不改变既有真机行为。

## 目标与约束

-   **接管不突变**：下发接管指令的那一刻，从臂 target 必须等于从臂**当时位姿**——此前残留的
    （可能已发散的）推理 target 被丢弃。这是接管可用性的底线：突变会同时毁掉安全与数据质量
    （`action` 是采集的指令列，跳变会污染整条轨迹）。
-   **限速唯一出处**：增量链路只决定 `target_action`，逐拍逼近幅度仍由 `step_rad` /
    `_step_toward()` 唯一负责，增量本身**不额外限幅**（主臂抖动放大由 `step_rad` 兜住）。
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

-   **覆盖范围**：关节与夹爪是**同一套语义**——`QPOS` 扁平向量逐元素增量
    （夹爪 target = 夹爪锚点 + 夹爪增量）。因此映射函数无需知道 action 布局，子类的
    `_get_teleop_target()` 只返回主臂绝对扁平读数（偏置不含在内）。
-   **1:1 与常量偏置**：增量映射不带缩放（`k = 1`）；主臂读数上的**常量**偏置由基类统一叠加
    （`_refresh_teleop_target()` 里取锚点之前），取值来自配置 `robot.teleop.joint_offset`
    （逐关节 rad，逐臂一份或全臂共用；缺省全 0——主从零位差属现场值，子类不再写死）。
    偏置在锚点式下自动抵消、不进入增量，因此只有 `absolute`（示教）下可见。

### 跟随空间与位姿轴锁定

`robot.teleop` 在机器人配置中声明跟随空间与缺省轴白名单：

```yaml
robot:
    teleop:
        follow_space: pose
        follow_axes: [x, y, z, pitch, yaw] # 未列 roll：锁在启用 teleop 时的从臂锚点
```

| `follow_space`  | 主臂输入到从臂目标的处理             | `follow_axes`                                        |
| --------------- | ------------------------------------ | ---------------------------------------------------- |
| `joint`（缺省） | 主臂关节直接做 absolute / delta 映射 | 不允许配置                                           |
| `pose`          | 主臂关节 FK → 位姿映射 → IK          | 非空、无重复的 `x/y/z/roll/pitch/yaw` 子集；缺省全部 |

Console 可在 teleop 关闭时选择 `joint` / `pose`，并在**每次开启**时通过 `follow_space` 下发；pose
模式还可通过 `follow_axes` 覆盖轴白名单，未列轴即为界面里勾选的“锁定轴”。运行时覆盖只对本次
启用有效；旧调用方不传时仍使用 YAML 缺省。空间或锁轴变化必须先关闭 teleop，再重新开启并采集
新锚点，不允许运行中静默改变约束。joint 跟随收到 `follow_axes` 会明确拒绝；不具备 pose 动作与
FK/IK 能力的机型收到 `follow_space: pose` 也会明确拒绝。

位姿跟随的锚点在启用后的首次有效主臂读数拍原子采集：`master_pose_ref = FK(master_now)`，
`slave_pose_ref = FK(slave_current_action)`。未跟随轴始终取 `slave_pose_ref`；跟随轴按模式计算：

-   `absolute`：取 `master_pose_now` 对应分量；
-   `delta`：取 `slave_pose_ref + (master_pose_now - master_pose_ref)`，姿态差与结果均 wrap 到
    $(-\pi, \pi]$。

每拍以现有关节目标为 seed 做一次 IK；解算失败或位姿非有限时，本拍保持上一目标、累计失败计数并按
同一原因限流告警，不影响 health。这里锁定的是既有 `POSE_FRAME`（法兰 / TCP）中的 RPY chart 分量，
接近 pitch = ±90° 时仍受欧拉角奇异性约束；它是**目标运动学约束**，不是外力下的无限刚度。

## 增量锚点

**为什么是锚点而不是逐拍积分**：`target += (master_now − master_prev)` 会累积漂移，且主从
位姿差被永久保留（接管时主从差多少，全程就差多少）。锚点式把接管瞬间的主从差一次性
**零化**：人工把主臂摆到与从臂相近的位姿后进入 `delta`，主从差无论是 0.05 rad 还是
0.2 rad，从臂都从**自身位姿**出发；主臂推多少、从臂走多少；主臂回锚点则从臂回锚点。

锚点在 `delta` 启用后的**首次成功读数那一拍**一并采样（同一个 `step()` 内，两者同拍）：

| 锚点                | 取值                                                    |
| ------------------- | ------------------------------------------------------- |
| `teleop_master_ref` | 该拍主臂扁平读数                                        |
| `teleop_slave_ref`  | 该拍从臂位姿：`action` → `target_action` → `init_joint` |

-   **从臂参考取当前执行位置**（`action`）：即从臂此刻所在位置；接管瞬间增量恒 0 →
    `target_action = slave_ref` = 从臂当前位姿。
-   **采样推迟到首次成功读数**，而不是在指令处理时同步读主臂：主臂瞬时读不到时不进入半接管
    状态，target 保持不变（不突变），下一拍继续尝试；一旦读到，增量必然从 0 开始。
-   **锚点在接管期间固定**（`disable_teleop()` / 再次 `enable_teleop()` 时清空），
    即整段接管都以接管瞬间为基准，不会随时间重新对齐。

## 指令链路

```
console / Edge adapter  ──POST /v1/teleop {enabled, mode?, follow_space?, follow_axes?}──▶  contract_server
        ▼（只入队）
env.commands 队列  ──▶  控制线程 _drain_commands  ──▶  robot.enable_teleop(mode, follow_axes, follow_space)
                                                       └─▶ 下一拍 step()：采锚点 → 写 target
```

-   `/v1/teleop` 请求体在 `enabled` 之外增加可选 `mode`（`absolute` / `delta`，缺省
    `absolute`）：**向后兼容**——只发 `{enabled: true}` 的旧调用方行为不变。
-   `follow_space` 是可选的 `joint` / `pose` 本次启用覆盖；`follow_axes` 是可选的非空 pose 轴
    白名单。两者缺省沿用 YAML。机器人 health 回传
    `teleop_follow_space` / `teleop_follow_axes`，Edge 状态透传给 Console 决定是否显示锁轴复选框。
-   `mode` 非法 → HTTP 422（server 侧校验，robot 层再兜一层，非法模式保持原模式与 target）。
-   `enabled: false` 时忽略 `mode`。

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
-   **交回模型**：`robot teleop false` 后第一拍 `rollout` 的 target 是模型当前输出，
    与人工位姿的差由 `step_rad` 逐拍拉过去。其中「模型当前输出」由会话侧保证：**接管开始即
    `rtc.reset()`**（丢弃未执行的块与在途请求，按世代作废），所以交回后第一块是拿**当时**
    观测现算的——接管前预取的块（机械臂已被带离原轨迹）不会在交回时被执行；暂停
    （`infer rollout stop`）同理。是否加「交回时机 / 缓慢过渡」（等待模型动作与当前位姿
    足够接近再交回）待定，见 `motrix_edge_rtc.md`。
-   **接管期间的数据**：接管与推理共用同一条 `action` 轨迹，采集到的 `action` 因此连续；
    是否在 episode 里标记接管区间（元信息字段）待定。**帧头跳过**已实现
    （`collector.skip_until_motion`，见 [robot-pipeline 运行时](./robot_pipeline_runtime.md)）：
    capture 开始后主臂未出现有效移动前不记录，避免每轮 episode 头部都带一段「还没开始动」的静止帧。
-   **主臂读不到期间被人工移动**：恢复读数后增量会跳变，但从臂仍受 `step_rad` 限速保护
    （单拍 ≤ `step_rad`），不会飞车。
-   **主臂读数不可用（`None` / 读取异常）**：本拍**不刷新 target**（保持原值，不突变），只累计
    `teleop_read_failures` + **同一原因限流告警**；**不置 `last_error`**——接管中读不到主臂是预期
    状态（人还没接上 / 瞬时抖动），不能让 `/v1/health` 变不健康（否则一次 CAN 抖动会被 Edge
    当成硬件故障）。读取异常与 `None` 走同一条路径（`_refresh_teleop_target` 统一 try/except），
    不会冒出 `step()`。
-   **单臂布局**：`step()` 目前硬编码双臂切片（`action[6:12]` / `action[12:]`），
    `SinglePiperRobot` 跑到 `step()` 会 IndexError——单臂接入遥操作前需按类常量泛化布局。
-   **接管前的对齐由人工完成**：主臂**不可被程序驱动**（Piper leader 模式只读），因此不提供
    「推理期主臂跟随从臂」；接管前操作员手动把主臂摆到与从臂相近的位姿，剩下的差值由锚点
    零化（见「增量锚点」）。
