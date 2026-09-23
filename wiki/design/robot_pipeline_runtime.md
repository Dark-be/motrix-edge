# robot-pipeline 运行时（控制 / 观测双线程）

## 摘要

robot-pipeline（仓库根 `robot-pipeline/`，独立 uv 子项目）在 robot server 进程内由 `BaseEnv`
驱动机器人：**机械臂控制与相机观测跑在两条独立线程**，相机或磁盘卡顿只让观测丢帧，不会拖慢
机械臂的限速步进。本文只描述 env / robot 这一层运行时；项目结构、硬件接入与 /v1 契约见
[robot-pipeline README](../../robot-pipeline/README.md) 与 [机器人适配器（adapter）](./motrix_edge_adapter.md)。

## 目标与约束

-   **控制节拍必须稳定**：`step_rad` 限速步进按固定频率下发，掉拍会让机械臂运动不连续。
    相机取帧（RealSense `wait_for_frames` 最长 5s、V4L2 `select` 0.2s）与 MCAP 落盘都可能阻塞。
-   **观测 = 最近一拍机械臂状态 + 本拍相机帧**：采集 / 推理需要「下发命令时的 qpos + action +
    画面」，而两部分分处两线程（见「观测组装」），时刻最多差一个控制周期（`1/HZ` ≈ 33ms）。
-   **每个硬件组件单写者**：控制器 SDK、collector 都不是线程安全的，写者必须唯一。
-   **不新增 edge 侧契约**：观测键、health 字段继续复用 `motrix_edge.adapter` 的单点定义。

## 拓扑

```mermaid
flowchart LR
    H["HTTP 线程（server handler）<br/>只入队 / 只读缓存"] -->|运动指令| Q1["commands 队列"]
    H -->|采集指令| Q2["capture_commands 队列"]
    H -->|只读| OBS[("env.observation")]
    subgraph ENV["BaseEnv 双线程"]
        Q1 --> C["控制线程 HZ（默认 30Hz）"]
        Q2 --> OB["观测线程 OBS_HZ（默认 10Hz）"]
    end
    C -->|"step() / sample_qpos()"| R["BaseRobot（控制器 + 传感器）"]
    OB -->|"build_observation()"| R
    OB -->|"on_frame(observation)"| PUB["server：写观测共享内存"]
    OB -->|"collect()"| COL["collector：MCAP 流式落盘"]
```

-   **HTTP 线程**（server handler）只做两件事：把指令入队、读 env 缓存（`observe()` / `health()` /
    `capture_status()`），不直接碰 robot 与 collector。
-   **控制线程**：消费运动指令 → `robot.step()` 限速接近 target → `robot.sample_qpos()` 采样机械臂状态。
-   **观测线程**：消费采集指令 → `robot.build_observation()`（缓存状态 + 本拍相机帧）→ 帧钩子
    `on_frame`（server 据此发布共享内存）→ 采集落盘。

## 线程职责

| 线程     | 频率             | 唯一写者                                                           | 消费队列           | 每拍动作                                       |
| -------- | ---------------- | ------------------------------------------------------------------ | ------------------ | ---------------------------------------------- |
| 控制线程 | `HZ`（30Hz）     | 机器人控制器 / `action` / `target_action` / `motion_state` / `seq` | `commands`         | 运动指令 → `step()` → `sample_qpos()`          |
| 观测线程 | `OBS_HZ`（10Hz） | collector / `observation`                                          | `capture_commands` | 采集指令 → `build_observation()` → 发布 → 落盘 |

指令分类固定：`robot_reset` / `robot_execute` / `robot_rollout` / `robot_safe_stop` /
`robot_set_teleop` 入 `commands`；`robot_capture_start` / `robot_capture_end` /
`robot_capture_sync` 入 `capture_commands`。两条队列各自只有一个消费者线程，FIFO 只保证
**队列内**顺序（`capture sync` 先于 `capture end` 生效）；**跨队列顺序不保证**，见下。

控制线程每拍**取空** `commands`：同一拍内多条运动指令按序应用后只 `step()` 一次，表现为
「最新目标优先」——中间目标不各占一拍（限速仍由 `step()` 统一约束）。观测线程同理取空
`capture_commands`，而 `capturing` 在该拍**开头**统一应用，故采集起止的**落点**决定产出：

-   **同一拍**内 drain 到 `capture start` + `capture end`（`capturing` 先真后假、`_episode_open`
    从未置位）→ **不产出任何文件**（静默丢弃，连空 episode 都没有）；
-   **相邻两拍**各自生效 → **只含 1 帧**的 episode（start 那拍记 1 帧，end 在下一拍开头生效并在该拍末尾收尾）。

### 跨队列顺序（有意弱化）

采集指令在**观测拍**生效（≤ 1/`OBS_HZ`，默认 ≈ 100ms），运动指令在**控制拍**生效（≤ 1/`HZ`，
默认 ≈ 33ms）——两类指令的相对顺序**没有保证**：同一批下发的 `capture start` 可能**晚于**
`execute` 生效，于是 episode 首尾最多 ~0.1s 的运动不落盘（单队列旧实现下窗口 ~33ms 且严格
有序）。

这是**有意**的取舍：采集落盘必须跟着观测拍走（`collector` 的单写者 = 观测线程），才能让相机 /
磁盘卡顿不拖慢机械臂节拍（见「目标与约束」）。

**当前范围只支持外部驱动采集**：调用方是人在 console / HTTP 上点击 `capture start` / `capture end`，
与后续动作的间隔远大于 100ms，实际影响可忽略；env **不在控制流程内自行启动采集**，故 `capture start`
与动作的相对顺序不需要强保证。后续若要支持「**在控制流程中启动采集**」（程序化：`capture start`
后立即下发动作），**必须先解决这个同步点**——可选方向：把采集开关归拢到控制线程消费，或由调用方
等待开关生效（如 `/v1/capture/start` 同步等生效后再回执）之后才下发动作。

## 观测组装（单点定义在 BaseRobot）

`BaseRobot` 持有观测键常量（`KEY_QPOS` / `CAMERA_PREFIX` / `KEY_TIMESTAMP`）与两个**取数
钩子**（子类实现，返回原始数据，不含契约键 / 时间戳）：

| 钩子                       | 实现方       | 取数内容                                  | 所属线程 |
| -------------------------- | ------------ | ----------------------------------------- | -------- |
| `get_observation_qpos()`   | 各机器人子类 | 控制器读出的扁平 qpos                     | 控制线程 |
| `get_observation_images()` | 各机器人子类 | 各相机 raw RGB 帧（顺序 = `IMAGE_NAMES`） | 观测线程 |

在钩子之上，`BaseRobot` 提供按线程划分的入口：

-   `sample_qpos()`（控制线程）：调 `get_observation_qpos()`，附上 `action`，`seq` 自增，
    结果**缓存**进 `motion_state`（机械臂侧状态快照，**不含帧时刻**）；机器人提供位姿时
    （`get_observation_pose()` 非 None）同拍把 `observations/pose` 一并放进快照——位姿与
    qpos **同拍**，下游不会读到错拍的组合。
-   `capture_images()`（观测线程）：调 `get_observation_images()`，组装为
    `observations/images/<cam_name>`。
-   `build_observation()`（观测线程）：`motion_state` + 本拍相机帧，并写入帧时刻 `timestamp`
    （**本线程打点**，观测拍取帧之前）；`motion_state` 为空（控制线程尚未采到第一拍）时返回
    `None`，该拍不出观测，下一拍重试。
-   `get_observation()`（单线程脚本 / 调试）：`sample_qpos()` + `capture_images()` 现场取整帧
    （同样在取帧前打点 `timestamp`）。

⚠️ 相机取帧会阻塞，**控制线程不得调用取相机的方法**（`capture_images` / `get_observation` /
`build_observation`）；机械臂读取只在控制线程，观测线程只读 `motion_state` 快照。

⚠️ 观测里的 `timestamp` 由**观测线程**在 `build_observation()` 打点（**观测拍取帧之前**的时刻），
**不是**控制拍的采样时刻——`motion_state`（qpos / action）来自上一个控制拍，与该时刻的偏移
**有界**（≤ `1/HZ` ≈ 33ms）但**逐帧小幅波动**（控制 / 观测是两条独立限速循环，线程唤醒抖动
逐帧不同；控制拍超时会让「最近一个控制拍」跳档）。故对 `dt` 与 episode 时长（末帧 − 首帧）
无影响；但下游（mcap → ACT / LeRobot 转换）**不要按固定滞后做时间平移校正**，需要严格对齐时
须知该滞后并非定值。

## 末端位姿观测（`POSE` / `pose_dim`）

笛卡尔原语（LLM agent 会话）与前端预览都要末端位姿，它在 robot 侧由类常量 **`POSE`** 声明：
0 = 不提供（共享内存保持 v2 布局，下游 `pose_dim = 0`，位姿显示为不可用）；> 0 = 提供，
`_ShmPublisher` 创建写者时传 `pose_dim`（**布局 v3**，在 action 之后新增一块位姿区），每帧写
`observations/pose`（与 qpos / action 同一帧，硬件上同拍采样）。

-   **每臂 6 维** `xyz + rpy`，扁平顺序与 qpos 的臂布局一致（对齐 Edge 侧适配器的
    `POSE_DIM_PER_ARM = 6`）；
-   **test_robot**（虚拟）无真实运动学：用固定可逆映射从**同一拍 qpos** 派生——「关节动 → EEF
    跟着动」，预览与笛卡尔闭环都可解释；
-   **真机**（piper 系列）应读法兰位姿（`PiperController.get_position()` / `get_flange_pose()`）
    并在 `get_observation_pose()` 返回；若还需接受笛卡尔目标，则要补机器人侧 IK（见
    [边缘原语接口](./motrix_edge_primitives.md) 的未决项）。
-   采集（`ActMcapCollector`）只落 qpos / action / images，**位姿不入 mcap**（数据集格式未定，
    暂不扩 schema）。

## 频率与诊断

-   **频率**：`HZ`（控制线程，默认 30Hz）与 `OBS_HZ`（观测线程，默认 10Hz）是 `BaseEnv` 类常量；
    `OBS_HZ` 是**真实取观测的频率**（取帧 + 发布 + 落盘），低一档是**有意**的——相机取帧 +
    MCAP 落盘成本远高于机械臂步进，观测 10Hz 足够，机械臂仍按 30Hz 稳定步进。`OBS_SLOW_FRAME_S`（0.5s）为观测慢帧告警阈值。
-   **实测频率**：`measured_hz` = 控制线程最近 30 拍平均周期倒推；相机 / 落盘卡顿不影响它。
    `measured_observe_hz` = 观测线程实测频率，仅用于慢帧告警文案。
    窗口由各自线程追加、由 HTTP 线程（`/v1/health`）读取，**不额外加锁**：纯统计量，最坏读到
    相邻两拍混算的平均值（差 < 1 拍）。
-   **慢帧告警**：观测拍耗时超过 `OBS_SLOW_FRAME_S` 时 `WARNING` 一条（最多每秒一条），带上耗时与
    当时观测实测频率，用于区分"相机卡顿"与"控制出问题"。
-   **ready 语义**：`health.ready` 要求机器人就绪、两线程存活、无指令执行错误；相机单帧异常只记日志，
    不置 `ready=False`（避免相机抖动把 Edge 打成 ERROR）。

## 停机与安全停止

-   `stop()`：置运行位为假 → join 控制线程（2s）→ join 观测线程（6s，可能正阻塞在相机取帧，
    最长 5s）→ `robot.disconnect()`。超时未退出的线程是 daemon，随进程退出。
-   `safe_stop` 语义不变：清空 `target_action`，`step()` 不再下发；线程与观测继续运行，可直接恢复。
-   **采集落盘边界**：`capture start/end` 都在观测拍**开头**（`_drain_capture_commands()`）生效，
    `capture end` 使**本拍** `capturing=False` → **本拍帧不写入**，末尾 `_update_capture()` 才
    调用 `finish()`（写 footer + 元信息 JSON）；相机卡顿时 episode 落盘相应延后。与运动指令的
    相对顺序不保证，见「跨队列顺序（有意弱化）」。

## 与 Edge 的交互

-   控制面：Edge adapter 经 `/v1/*` 调用 env 的 `robot_*` / `capture_*` 方法，方法本身只入队并立即返回。
-   观测面：共享内存由 server 在 `on_frame` 回调中发布，发布频率等于观测线程频率；env 不碰共享内存。
-   健康面：`/v1/health` 的 `control_hz` / `measured_hz` 由 env 提供，字段契约单点定义在
    `motrix_edge.adapter.http_contract`。
