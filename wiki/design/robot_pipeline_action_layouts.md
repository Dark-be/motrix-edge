# robot-pipeline 下发契约收敛（layout + arms）

## 摘要

动作下发收敛为**一个入口、一个形状概念、三个正交轴**：

| 轴         | 字段     | 取值                                                              | 回答的问题                             |
| ---------- | -------- | ----------------------------------------------------------------- | -------------------------------------- |
| 值语义     | `layout` | `joint` / `pose` / `pose_delta` / `gripper` / `joint+gripper` / … | 每臂块里**有哪些段、按什么顺序**       |
| 作用域     | `arms`   | 臂名子集（缺省 = 全部臂）                                         | 这次**写哪些臂**（其余臂目标保持不动） |
| 遥操作策略 | 端点     | `/v1/rollout`（让位）/ `/v1/execute`（抢回）                      | 遥操作进行中**让位还是抢回**           |

请求体只有三个字段：`action`（扁平值）、`layout`（缺省 `joint`）、`arms`（缺省全部臂）。
**块内段序与臂序都写在请求/契约里**，不再依赖「先左后右」「每臂固定 7 维」这类习惯。

收敛后下发只有一条入口：不另立端点（组合与作用域都写在请求里）、也不把一条 qpos 拆成两条命令
（RPent 的「值段 + 夹爪段」就是一条 `layout="<space>+gripper"`）。

## 目标与约束

-   **隐式习惯显式化**：块内**段序** = `layout` 的书写顺序；块间**臂序** = `arms` 的顺序
    （缺省 = 机器人广播的物理臂序）。两处顺序都是契约的一部分，不再是"约定俗成"。
-   **一份数据一次写入**：一条请求里的所有段（含关节与夹爪）在**同一个控制拍**写进
    `target_action`，不存在"夹爪更新了、关节还是上一步"的中间态。
-   **作用域可裁剪且不含 home**：`arms` 给定时，未列出的臂**目标原样保留**。`HOME` 填充是
    **调用方**的便利（edge 侧），机器人端不认识"home"这个概念。
-   **扩展不加端点**：新增一种组合 = 在 layout 表里加一行；新增一种量（如末端四元数、力/力矩）
    = 加一个段类型（kind）。端点数量与组合数量解耦。
-   **与观测同构**：`layout=joint+gripper` 的每臂块与 `observations/qpos` 的每臂块**逐位对齐**
    （值段 6 + 夹爪 1），模型「观测 → 动作」不需要任何重新排布。
-   **不动底层**：控制通路仍是"关节目标 + MIT"，`pose` / `pose_delta` 仍在落 target 前解算一次
    （见 [位姿动作](./robot_pipeline_cartesian.md)）。

## 请求契约

`POST /v1/rollout`（让位）/ `POST /v1/execute`（抢回），body：

| 字段     | 必填 | 说明                                                                                          |
| -------- | ---- | --------------------------------------------------------------------------------------------- |
| `action` | 是   | 扁平数组：`arms` 顺序逐臂块拼接，每臂块 = 按 `layout` 段序拼接                                |
| `layout` | 否   | 每臂块的段序列（见下表）；缺省 `joint`                                                        |
| `arms`   | 否   | 作用域：缺省 = 机器人**全部臂**（`action` 维度按全部臂算）；给出 = 只写这些臂，其余臂目标不变 |

### layout 词表

| layout               | 段序列                      | 每臂块宽 | 语义                                                 |
| -------------------- | --------------------------- | -------- | ---------------------------------------------------- |
| `joint`（缺省）      | `(joint 6)`                 | 6        | 关节角绝对目标（rad）                                |
| `pose`               | `(pose 6)`                  | 6        | 末端位姿绝对目标（`xyz + rpy`，法兰系）              |
| `pose_delta`         | `(pose_delta 6)`            | 6        | 位姿**增量**（叠加在**当前关节段目标**的正解位姿上） |
| `gripper`            | `(gripper 1)`               | 1        | 夹爪归一化绝对开合（`[0, 1]`，1 = 张开）             |
| `joint+gripper`      | `(joint 6, gripper 1)`      | 7        | 关节 + 夹爪一次写入（与 `observations/qpos` 同构）   |
| `pose+gripper`       | `(pose 6, gripper 1)`       | 7        | 位姿 + 夹爪一次写入                                  |
| `pose_delta+gripper` | `(pose_delta 6, gripper 1)` | 7        | 位姿增量 + 夹爪一次写入                              |

-   **命名** = 段名用 `+` 连接，**书写顺序即块内顺序**：`joint+gripper` 与 `gripper+joint` 是两个
    不同的 layout（后者合法但少见）——不允许"实现按固定顺序重排"。
-   **段类型（kind）与槽位一一对应**（见 [写入内核](#写入内核实现单点)）；同一臂内**不重复
    kind**（多夹爪 / 多末端这类需求用**新增 kind** 表达，如 `gripper2`，不用重复段）。
-   **声明的 layout 集合由机器人的动作能力推出**：`ACTION_SPACES` 的每个单段 + 「值段 ×
    `gripper`」组合。机器人未声明的 layout（如没接位姿却发 `pose+gripper`）→ 422 并附可用清单。
-   **对外广播**：discover / `GET /v1/adapters` 声明 `action_layouts`（每项 = `{name, per_arm,
segments}`）+ `state_arms`（物理臂序）；旧的 `action_spaces` / `action_dims` 保留为兼容字段。

### arms 作用域

-   取值 = `state_arms()` 的子集，**非空、不重复**；顺序即 `action` 里的块序。
-   缺省 = **全部臂**。此时 `action` 的维度按全部臂算：调用方想"只控部分臂"必须显式给 `arms`
    （这就是它与 `HOME` 填充的分界——机器人不再替调用方补值）。
-   与 layout **正交**：`layout=joint, arms=["right"]` = 只写右臂关节，左臂与两路夹爪都不动；
    `layout=gripper, arms=["right"]` = 只动右夹爪。两者今天都需要靠"补全臂 + 整体覆盖"绕开。

### 校验与错误

| 情况                                              | 结果                                                                |
| ------------------------------------------------- | ------------------------------------------------------------------- |
| `layout` 未知 / 机器人未声明（缺位姿能力等）      | 422（附可用 layout 清单）                                           |
| `arms` 为空 / 重复 / 含未知臂                     | 422                                                                 |
| `len(action) != Σ段宽 × len(arms)`                | 422                                                                 |
| 值非有限（NaN / inf）                             | 422                                                                 |
| `pose` / `pose_delta` 解算失败（超限位 / 不收敛） | 422 且**不改目标**（保持上一目标）                                  |
| 遥操作（人工接管）中                              | `rollout` → 409（让位，不结束遥操作）；`execute` → 执行并结束遥操作 |

校验**只读**：任何一条失败都不得改动 `target_action`（与现行 `execute` / `rollout` 一致）。

## 写入内核（实现单点）

机器人侧只保留**一处**写目标：

```text
_apply_targets(action, layout, arms) -> None
  1. 按 layout 段序把每臂块切成段值（校验长度 / 有限性）；
  2. 按 kind 映射到目标向量槽位（槽位布局见下）；
  3. 整条 target_action 一次写好（同一控制拍生效）。
```

| kind         | 目标向量槽位                | 处理                                                |
| ------------ | --------------------------- | --------------------------------------------------- |
| `joint`      | 值段 `[arm_index*6 : +6]`   | 直写（下发前按关节限位裁切，沿用现状）              |
| `pose`       | 值段（同上）                | 先解算成关节角（`pose_to_joint`，静态转换函数）     |
| `pose_delta` | 值段（同上）                | 基准 = **当前关节段目标**的 FK 位姿，叠加增量后解算 |
| `gripper`    | 夹爪段 `[QPOS + arm_index]` | 值钳到 `[0, 1]`                                     |

写入路径就这一张表 + 一个循环：`execute` / `rollout` / 原语 / RPent 都走 `plan_layout()` →
`apply_layout()`（不再有按空间直写与逐臂块各写一份）；`reset()` 仍走 `set_target_action(init_joint)` +
`set_target_gripper(init_gripper)`（它是"两段整体复位"，与 layout 无关）。

## 与观测 / 策略的对齐

-   **观测端已经自描述**：`state_layout()` 的 `state_dims` 用同一套 kind 名（`joint` / `pose` /
    `gripper`）；`observations/qpos` 每臂块 = `joint+gripper`，`observations/pose` 每臂块 =
    `pose`。故"下发 layout"与"观测布局"是同一套词汇，不存在第二套命名。
-   **策略端声明同名词表**：`policy.action_layout` 取值改为 layout 名（`joint` /
    `joint+gripper` / …）。会话据此选择下发时的 `layout`，`arms` 取 adapter 的启用臂——
    于是"模型输出什么形状"与"edge 怎么下发"是同一条声明链（见
    [policy 设计](./motrix_edge_policy.md)）。

## 落地

-   **请求契约**：`layout` + `arms` 是唯一下发入口（HTTP `/v1/execute` / `/v1/rollout`、
    `adapter.execute` / `rollout`、`BaseRobot.execute` / `rollout`、env 队列载荷
    `(action, layout, arms)`）——单空间字面量（`joint` / `pose` / `pose_delta` / `gripper`）就是
    单段 layout，不是另一套入口。
-   **写入内核单点**：`BaseRobot.plan_layout()` 校验（段名 / 维度 / 有限性 / 作用域）、
    `apply_layout()` 唯一写目标——`layout="joint+gripper"` 的关节与夹爪在**同一控制拍**落地。
-   **RPent**：`_push_qpos` 一条 `layout="<space>+gripper"`（qpos 本身即「每臂 值 + 夹爪」块），
    不再拆两条；`set_gripper` 走 `joint+gripper`，`move_delta` / `rotate_delta` 走 `pose_delta`。
-   **旧入口不留别名**：`action_space` 请求字段与形参、`rollout_state()`、
    `POST /v1/rollout/state`、`_expand_action()` 已删除；命令层与 HTTP 层对旧键**显式报错**
    （rejected / 422），不静默降级。

## 为什么不是一个「动作空间」

下发要同时表达两个正交的轴，单空间枚举表达不了：

-   **组合值**：一条请求里既有值段又有夹爪（如每臂「6 关节 + 1 夹爪」），单空间只能表一段。
-   **作用域**：只写部分臂、其余臂保持不动（单臂 VLA），而单空间动作的维度天然按全臂算。

维度同形让这两个轴的缺失更危险：`joint` / `pose` / `pose_delta` 的**扁平维度相同**，字段名
写错时维度校验照样通过——位姿会被当成关节下发，所以名字必须是真的、不能靠缺省猜。

自测判据：**"要下发 `pose` + 夹爪 / 只写右臂关节 / 双夹爪，是不是得再加端点或再写一次拆分？"**

## layout 不携带控制方式（边界）

三个轴与「控制方式」正交，写清楚以免误读：

| 轴                      | 决定什么                                                                                                                     | 谁声明                        |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `layout`                | 块里**有哪些段**、每段什么意思（`joint` / `pose` / `pose_delta` / `gripper`）                                                | 请求 / 策略的 `action_layout` |
| `arms`                  | 写**哪些臂**（其余保持）                                                                                                     | 请求                          |
| 遥操作策略              | 遥操作中**让位**（`rollout`）还是**抢回**（`execute`）                                                                       | 端点                          |
| 控制方式（`ctrl_mode`） | 用**哪个控制环**（`mit` 力矩环 / `joint` 位置速度模式；采集里记 `control_mode` = `mit` / `mit+gravity` / `joint` / `mixed`） | **机型配置**，请求改不了      |

-   `pose` / `pose_delta` 段的唯一额外含义是「**需要机器人先解算成关节**」——解算完仍走同一条
    关节通路（`move_mit`，控制器注释：`move_p` 无路可达）。因此 `joint+gripper` 与 `pose+gripper`
    到底层是同一条环，差别只在「模型给的是关节还是位姿」。
-   将来若要支持力控 / 阻抗，另开 `mode` 轴（如 `mode: position | impedance`，缺省 `position`）或
    独立端点，**不并入 layout**：形状跟着模型变、控制环跟着机型 / 现场变，两者变化频率不同。

## 与既有文档的关系

-   本文是**下发契约**的唯一出处；`robot_pipeline_action_spaces.md` 保留观测契约（`state_layout()` /
    采集 JSON）与各空间语义，下发一节以指针指向本文。

## 相关文档

-   [robot-pipeline 动作空间与观测契约（现行）](./robot_pipeline_action_spaces.md)
-   [robot-pipeline 位姿动作（求解器）](./robot_pipeline_cartesian.md)
-   [Adapter 契约与发现](./motrix_edge_adapter.md)
-   [推理策略客户端（policy）](./motrix_edge_policy.md)
-   [推理会话（session）](./motrix_edge_session.md)
-   [RPent 对接契约](./motrix_edge_rpent_bridge.md)
