# RPent 对接与命名统一实施计划

## 摘要

按 [RPent 对接契约](../design/motrix_edge_rpent_bridge.md) 落地两件事：① edge 对外命名统一为
`<scope>/<verb>`（capability 由 `CMD_*` 常量派生，旧拼写保留一版别名）；② 新增 `POST /call`
RPC facade，把 RPent 的 `env.*` 映射到 edge 原生路径（**VLA 由 RPent 侧自跑，edge 不提供
`vla.*` 接口**）。语义单点在 primitives 与 server 设计文档，本计划只写做什么、怎么做。

## Phase A —— 命名统一 + 原语落地

-   [x] `command/naming.py`：新增 `capability_for(cmd)`（`CMD_*` 空格 → `/`）、
        `resolve_capability(capability)` 与 `LEGACY_CAPABILITIES`（旧拼写 → 规范 capability）；
        `CMD_*` 保持单一事实来源。
-   [x] `server/command.py`：`CommandService.execute` 按**命令词**分派（capability 由 `CMD_*`
        派生），回执 `executed` 统一回规范 capability；旧拼写仍可用，回执带 `deprecated=true`；
        submit / push 两类通道不变。
-   [x] `server/app.py`：`CommandResponse` 新增 `deprecated`；`/v1/commands` 与 `CommandRequest`
        文档串同步新命名。
-   [ ] 原语执行器（`server/primitives.py` + node 侧执行线程）：`goto` / `move_rel` /
        `rotate_rel` / `gripper` / `recover` / `wait` / `stop`，到位 / 受阻 / 超时 / 中止终态，
        参数钳制（工作空间盒 / 单步上限 / 值域 / `max_wait`）。
-   [ ] `GET /v1/preview/<cam>.jpg`（单帧 JPEG，给外部喂 LLM）。
-   [x] `tests/`：新增 `tests/test_capability.py`（派生 / 往返 / 别名 / 空值与未知拼写），
        `test_server.py` 命令用例切到规范拼写 + 新增旧拼写别名回执 `deprecated` 用例。
-   [x] 文档同步：`motrix_edge_server.md` 的 capability 表、`motrix_edge_web_console.md`
        与 `robot_pipeline_teleop_plan.md` 的 capability 拼写；前端（不入库）capability 调用点。
-   [ ] `motrix_edge_primitives.md` 的 op 表补 `rotate_rel` / `recover`（随原语落地一起）。

## Phase B —— `POST /call` facade

-   [x] 新增 `server/rpent.py`：`POST /call` 信封解析（`{method, args, kwargs, session_id}`）、
        `{"ok": true, "result"}` / `{"ok": false, "error", "kind", "traceback"}`、**始终 200**；
        NumPy 编解码助手（`__ndarray__` / `__npscalar__`，与 RPent `http_rpc.py` 对称）。
-   [x] 方法分派：`env.get_env_meta` / `get_observation` / `get_robot_state` /
        `get_camera_meta` / `reset` / `move_delta` / `rotate_delta` / `set_gripper` /
        `recover_joint_posture` / `chunk_step` / `step` / `get_task_language` +
        `healthz` + `session.register|close`（no-op）；未知方法 → `ok=false`。`shutdown` 不注册。
-   [x] `env.get_env_meta` 自描述：`explicit_reset_only: true`、`action_dim`、`action_space(s)`、
        `arms`、相机清单 + 分辨率、`pose_convention` / `gripper_range`、
        `agent_observation.inline_cameras`（≤ 4 路进模型上下文）。
-   [x] `env.get_observation` 回 `states`（同帧 qpos）+ `qpos` / `pose` / `raw_camera_frames`
        （JPEG → uint8 RGB ndarray，与 adapter 出图同色序）。
-   [x] 动作布局校验：`step` / `chunk_step` 维度与 `action_dim` 不符 → `ok=false`（不 reshape）。
-   [x] **外部动作块布局转换**（`server.rpent.action_layout: rpent/dual_franka` + `dry_run`）：
        20 维 `xyz + rot6d + 夹爪(±1)` → edge 每臂 `[xyz, rpy, gripper]` 绝对目标，逐帧转换
        （rot6d → 旋转矩阵 → rpy、夹爪域映射、按臂名对齐）；`dry_run` 时只回 `converted`，
        其余下发路径也一律拦住（见下方「`dry_run` 安全修复」）。
-   [x] **到位等待（settle）**：写原语（`move_delta` / `rotate_delta` / `set_gripper` /
        `recover_joint_posture`）默认阻塞到「误差 ≤ 容差」/ 超时 / 停顿，回执带
        `reached` / `final_err`（+ 分项 `final_err_m` 位置米 / `final_err_rad` 姿态或关节弧度）/
        `elapsed_s`（+ `stalled` / `timeout` / 生效容差）——外部 agent 靠它判成败，不等就会读到
        未动的那一帧。可逐次传 `settle` 覆盖或关掉（`timeout_s` 封顶 90s）。
        **位置与姿态分别比容差**（不把米和弧度混进一个阈值）；⚠️ 底层 MIT 只有 P/D、无重力前馈
        （`t_ff = 0`）→ 存在稳态误差，缺省容差取 **5cm / 0.4rad**（先把静态误差盖住，否则
        `reached` 永远不成立、每个写原语都走满 `stall_s` / `timeout_s`），按现场实测收敛后收紧。
-   [x] **每臂状态块**：`env.get_robot_state` 除扁平 `qpos` / `pose` / `gripper` 外，再给
        `left_arm` / `right_arm`（`qpos` / `gripper` / `tcp_pose` quat），兼顾两套约定。
-   [x] **相机元数据自描述**：`env.get_camera_meta` 带 `agent_observation`
        （`inline_cameras` / `agent_view` / `frames`），与 `env.get_env_meta` 共用同一 helper
        ——RPent 的 `dump_state` 只读前者，缺它模型只能拿到路径、盲跑。
-   [x] **`recover_joint_posture` 保夹爪**：有 `HOME["joint"]` 声明时逐关节回 home 并保持夹爪，
        无声明才退回 `adapter.reset()` 并在回执标 `fallback`。
-   [ ] 并发与安全：遥操作（人工接管）中拒绝下发 → `ok=false`（由 adapter 契约承担）；
        与 capture / infer 会话的互斥、primitives 单飞、estop 使在飞原语转 `aborted`
        随 Phase A 的原语执行器一起做。
-   [x] 租约：`server.rpent.lease_id`（固定）→ 否则当前活跃租约；自描述方法（`healthz` /
        `get_env_meta` / `get_camera_meta`）免租约，其余与原生面同规则；`lease` / `settle`
        配置回写进 `env.get_env_meta`，让 agent 启动前能自查（RPent eval 连上即 `reset`）；
        `lease` 段另给 `required` / `expires_at` / `expires_in_s`（租约 TTL 要覆盖整轮 run）。
-   [x] 测试（`tests/test_rpent.py`，61 例）：numpy 往返 / 信封（含未知方法与未注入）/
        租约（免 / 必需 / 失败信封 / `satisfied` = 可用）/ 观测与图像色序 / 四个控制方法与单臂行为 /
        pose_dim = 0 拒绝 / `step` 与 `chunk_step` 的拒拍与计数 / 布局转换（rot6d↔rpy 往返、
        正交化、夹爪域、臂对齐与回填、`dry_run` 不下发、维度与布局名错误分支）/ settle
        （到位 / 停顿 / 超时 / 可逐次覆盖 / 上限封顶 / 容差回执）/ 每臂状态块 /
        `recover_joint_posture` 保夹爪 / 原图与预览回落 / 相机名基名约束 /
        `dry_run` 拦住四条写原语与 `env.reset` + `_push_action` 兜底 + 自描述暴露 dry-run。
-   [x] 文档：`motrix_edge_server.md` 增 `/call` 小节与端点表行；`motrix_edge_rpent_bridge.md`
        换成已实现形态 + 待对齐项。

## Phase C —— 深化

-   [x] `action_layout`（`rpent/dual_franka`：20 维绝对位姿 + rot6d + 夹爪 ±1）+ `dry_run`
        ——让 RPent 的 VLA 动作块可直接灌进 `env.chunk_step`。
-   [ ] socket 传输（`--transport socket`，多帧观测省 JSON 编解码）。
-   [x] 全分辨率图像（`image_source: native` 默认；`preview` 回 320×240 缓存）。
-   [x] 每臂状态块 + quat `tcp_pose` + `get_camera_meta` 自带 `agent_observation`。
-   [x] 按 RPent 侧 review 修正设计表：`view_*` 是**本地 artifact 读取器**（不打 edge）、
        `request_scene_reset` → `reset_home`、相机名基名约束、`is_real_robot=False` 取舍、
        `pyproject` extra + lazy import、prompt 用裸工具名。
-   [x] **RPent 侧 `robots/motrix_edge/`**：已在 RPent 仓库交付（`env_client` / `tools` /
        `toolkit` / `robot_spec` / `prompt_bundle` / `tasks`，8 个工具：`view_env_state` /
        `view_camera_meta` / `move_delta` / `rotate_delta` / `open_gripper` / `close_gripper` /
        `recover_joint_posture` / `reset_home`），attach-only 接 `--env-endpoint`，不复用 franka 包；
        客户端启动自检读 `lease.satisfied` / `expires_in_s` / `pose_dim_per_arm`，
        `settle.*` 上下限从 meta 实时取（不硬编码）。
-   [x] **不加入绝对位移动词**（`env.move_to` / `goto`）：RPent 真机包的 `move_delta` 相对语义
        已够用（客户端可用 `get_robot_state` 的 pose 现算 delta），且绝对跳转需先有工作空间盒 /
        单步上限；待原语执行器落地时一并评估（详见设计文档）。
-   [x] **`dry_run` 安全修复**（RPent 侧 review 发现）：把 `dry_run` 检查前移到四条写原语 +
        `env.reset`（回 `dry_run` / `sent: false` / `reached: null` / `reason`），`_push_action`
        兜底报错；修掉模块与 `_settle_action` 两处旧 docstring、`set_gripper` 的 `sent: true`
        自相矛盾；`settle_status()` 增 `dry_run` / `action_layout`（供 agent 启动告警）。
-   [x] **`lease.satisfied` 语义修正**：= 租约**现在可用**（能过 `require`），不是“解析出了 id”；
        另给 `state` / `reason`，pinned 但未安装 / 未生效 / 已过期都会 `false`。
-   [x] **位姿能力（edge 侧适配）**：`effective_pose_dim_per_arm()` 以观测共享内存 header 的
        `pose_dim` 为准（机器人没写位姿区 = 0，不谎报）；位姿量纲 / 形状防护（`|xyz| ≤ 10 m`、
        `|rpy| ≤ 7 rad`，不合格丢弃并记一条 ERROR）；新增 `POSE_FRAME`（dual piper = `flange`）
        并在 `env.get_env_meta` 暴露 `pose_frame`；facade 各处的每臂位姿判断统一走同一口径。
-   [x] **机器人侧位姿观测（robotics 侧）**：`DualPiperRobot` 已加 `POSE = 12` +
        `get_observation_pose()`（由**同一拍关节角**经 `robot/kinematics` 的正解解算；SDK 法兰位姿
        **不在运行时链路**，只在现场标定时读作对照）——RPent 启动自检的 `pose_dim_per_arm` 因此为 6。
-   [x] **机器人侧位姿动作下发（求解器，不经 `move_p`）**：`action_space=pose` 时
        每臂位姿由 `robot/kinematics` 解算一次 → 关节目标 → 限速 + MIT；
        `DualPiperAdapter.ACTION_SPACES` 已声明 `pose`，故 `env.move_delta` /
        `rotate_delta` / `goto` 对真机 piper 可用（解算失败 → 422、不改既有目标）。
        `move_p` 仍**不引入**（与 MIT 互斥）；见 [位姿动作](../design/robot_pipeline_cartesian.md)。
-   [ ] 端到端联调记录（**唯一硬门槛**）：RPent `--env-endpoint http://<edge>:8000` + RPent 自跑的
        VLA，跑通一次真实任务（`dry_run` 下已用官方 client 验完零下发）。
-   [ ] **写方互斥**（真机跑之前建议补）：facade 目前只校验租约，**不看 node 会话**——
        RPent 与 edge 自己的 infer 会话同时下发会交叉；遥操作已有保护（adapter 拒拍 →
        `ok=false kind=state`），但 infer / capture 会话需显式互斥 + 单飞。

## 未决（待拍板）

1. ~~**图像分辨率**~~：**已定** —— planner 面向给原图（`image_source: native`），
   降级开关放 RPent 侧（`--no-images`），不动 edge 的 WebRTC 预览缓存。
2. ~~**位姿 / 状态形态**~~：**已定**——回执同时给扁平与每臂块（quat `tcp_pose`）。
3. ~~**`recover_joint_posture`**~~：**已定**——关节回 home + 保持夹爪，无 `HOME_QPOS` 才弃守回 `adapter.reset()`。
4. **其他机器人布局**：若对方的块是归一化增量（另一套机器人包），需新增布局名并显式声明语义。
