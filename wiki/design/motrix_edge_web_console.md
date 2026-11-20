# Edge Web Console（前端）

> **状态**：Console 已作为根目录 `console/` 正式子项目纳入本仓库，包含 React UI 与 Node BFF。文中契约分两类：
> master 已提供（`/v1/leases`、`/v1/captures` 的 `capture_status`、`POST /v1/uploads/pack` 等）
> 与**后续 MR 才提供**（`<scope>/<verb>` 能力名如 `robot/estop`、人工接管 `mode=delta`、
> `/v1/preview` 的 `pose`·`arms` / `gripper` / `pose_target`、`infer rollout stop`、
> 策略 schema 的 `multiline`、服务端 3 位小数取整）。未标注的均为 master 现状。

> **位置比例配套状态**：后端已支持 `pose_position_scale`、状态与入队配置回显；
> Console 的比例输入、共享/逐臂编辑随本子项目维护。
> 初始姿态通过关节遥操作建立并跟踪到位，不提供手动摆动从臂后的实测重锚定。

## 摘要

Edge Web Console 是 MotrixEdge 的**中心操作台**（Vite + React + TypeScript + Node BFF +
Tailwind CSS）：经同源 `/api` 登录并代理 Edge HTTP API，展示 Edge 状态、管理租约、驱动采集 / 推理会话、
发送控制信号，并经 WebRTC 播放机器人摄像头实时视频流；另提供 RPent Harness 入口，嵌入 RPent
原生 Dashboard。定位为**测试项目**——信息展示尽可能完整
（租约到期倒计时、命令名、状态、能力、磁盘等），按钮即信号。

## 目标与原则

-   **中心 BFF**：Node 进程托管静态页面、飞书 Session 与 Edge 静态白名单；HMAC 设备由 Edge 验签，
    固定可信局域网设备由 Console Session 覆盖采集身份并依赖网络 ACL；Edge 仍拥有最终租约和状态校验（见 [HTTP 控制面（server）](./motrix_edge_server.md)
    / [Edge 级租约（lease）](./motrix_edge_lease.md) / [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)）。
-   **信息尽可能完整**：每个面板展示原始契约字段 + 派生信息（如租约到期倒计时）；错误展示 HTTP status / detail。
-   **按钮即命令**：每个控制按钮即一个 HTTP 动作，UI 标注其对应命令名。
-   **租约自动维护**：激活后按 `renew_interval` 自动续租（浏览器端定时器）+ 到期倒计时；可手动续租 / 释放。
-   **受控操作带租约**：所有受控请求携带 `X-Lease-Id` 头；退出经 query `lease_id` 提交。
-   **轮询刷新**：只读状态轮询（1s，`usePolling`）；**上一发未返回则跳过本轮**、页面不可见时暂停（切回立即补一次），避免慢请求叠加；观测预览（qpos / 图像）独立开关。

## 页面布局（单页分区）

总体：**顶部 sticky 状态条** + 两栏（左：观测与操作，右：租约与状态）；操作结果即时 toast，完整历史进左下角日志抽屉。

0. **顶部状态条（sticky）**：Edge Base URL（默认 `http://localhost:8000`，localStorage 记忆 + **刷新自动重连**）+ 连接 / 断开 + 在线 / **离线（数据已过期）** + `node_state` + 当前会话类型 / 状态 + 租约状态与到期倒计时 + **全局急停**（须持租约；快捷键 `Esc`）。节点状态直接显示，`node reset` 作为独立按钮，仅 ERROR + 持租约时可用，不再藏进状态下拉；机器人回 home 保留在页面操作区。飞书未登录时顶栏直接显示「登录飞书」按钮；登录后显示当前用户，点击用户名才展开紧凑的「切换账号 / 退出登录」菜单。
    - **顶部状态条常驻**：急停、租约倒计时、节点 / 会话状态不必滚到下面的卡片里找。
1. **共享观测与 3D 预览区域**：采集、推理、手动和调试页复用同一个 3D 场景与相机视角。实色模型由
   `GET /v1/preview` 的实测 `qpos` 驱动；同场景中的**蓝色半透明模型**由 `action`（每臂
   `[joint6, gripper1]`）驱动，明确表达 Robot 当前目标，不拿推理回执或前端暂存值冒充已下发动作。
   WebRTC `<video>` 逐相机播放；数值区展示 `qpos` / `gripper` / `qpos_err`（每臂
   `[joint_error6, gripper_error1]`）/ `action` / EEF 实测 `pose` / 目标 `pose_target`，并按
   `arms` 切成每臂一段、保留 3 位小数。观测无需进入会话。

> **原语接口（外部 agent 执行层）不走会话**：外部 agent 用 `GET /v1/primitives`（能力）/
> `POST /v1/primitives`（执行，同步等到位）直接驱动机器人（持租约即可），见
> [边缘原语接口](./motrix_edge_primitives.md)。LLM 页是一个前端 RPent 工作台：先读取
> `GET /v1/rpent` 与 `env.get_env_meta`，再按服务端返回的方法动态生成可选操作；写操作必须由用户点击
> 执行并受 Edge 租约保护。当前仓库没有 LLM 推理端点，因此聊天区提供本地会话与操作建议，实际模型
> 接入可在后续为该页替换 provider，不把 API key 或模型调用逻辑塞进 Edge。

### LLM / RPent 工作台

-   顶部显示 RPent 连接状态、可用方法数和租约状态；连接后自动读取环境能力元数据。
-   左侧聊天区保留用户问题、助手对当前 RPent 能力的解释和执行回执；发送文本只生成安全的本地建议，
    不会隐式驱动机器人。
-   右侧操作区由 RPent 方法清单驱动，提供环境能力、机器人状态、观测、复位、移动、看向目标和夹爪等
    可选卡片。读操作直接执行；移动、复位、夹爪等写操作明确显示“需要租约”，并由按钮确认执行。
-   复杂参数使用 JSON 编辑器，所有调用都经现有 `POST /call` RPent 信封返回，错误原样展示在回执中。

### Agent Harness

Harness 与上述 Edge RPent 兼容面是两个控制面：兼容面让外部 Agent 调用 Edge 的 `env.*`；Harness
由 Console 提供通用任务、对话、运行状态、事件、画面和中断界面，RPent 是首个 Provider。任务目录、
任务到 memory/recipe/tool profile 的映射、planner 生命周期仍由 RPent 权威维护；Console 只消费
Provider 自描述，不硬编码 task ID 或记忆内容。

-   Provider 地址只能来自 Console BFF 的 `harnessProviders` 静态注册表。浏览器走同源
    `/api/harness/providers/<id>/*`，不能提交任意目标，避免 SSRF、CORS 和 mixed-content 分叉。
-   `HARNESS` 页不依赖 Edge UI 是否已连接：先读取 Provider `/api/commands` 生成任务选择，再经
    `/api/session/messages` 启动任务或向运行中的 Agent 追加消息；状态、事件、时间线与帧分别来自
    Provider session API。切换任务必须创建新的 RunConfig、memory preparation、toolkit 与 planner 上下文。
-   planner / model 在 Provider 进程启动时固定，Console 只展示、不在任务边界修改；“中断当前调用”
    请求 planner 在安全边界取消当前工具/推理，“终止任务”取消整个 TaskRun 并回到任务选择，两者不混用。
-   对话区按 RPent transcript 类型展示 user / thinking / text / tool_call / tool_result，保留完整参数、结果、
    usage 与错误；读取图像的工具结果旁压缩展示当前 Provider 帧，右栏始终列出全部相机/状态帧。缩略图
    明示为“当前帧”，不冒充已缺失历史映射的调用时原图。Harness 右栏复用其它标签页的 `CameraRail`
    WebRTC 相机列表，不维护第二套相机 UI；中间消息区固定占据可用高度并独立滚动，长文本、thinking、
    工具结果和原始事件默认折叠，短内容直接展开。
-   Motrix Edge 是 attach-only 外部环境；启动 RPent 专用 Provider：
    `python -m robots.motrix_edge.harness --env-endpoint http://<edge>:8000`。默认只监听
    `127.0.0.1:8765`；Console BFF 与 RPent 不同机时，网络边界必须只允许 Console 主机访问。
-   Provider API 可以由 RPent 之外的 Agent 实现；Console 契约只依赖任务目录、run/session 状态、
    消息、中断、事件和画面，不把某个 planner SDK 或 API key 放进浏览器。

2. **会话区（采集 / 推理二选一）**：同一时刻只可能有一个会话，故两张卡共用一块位置——顶部「会话类型」分段控件切换（会话进行中则锁定为当前会话）；只有被选中的那张面板参与渲染。

3. **采集会话面板**：进入采集（enter）/ 退出采集（exit）；**采集开始 / 结束 / 取消（`capture episode start/end/cancel`）按采集运行位互锁**——未录制时显示“开始录制”，录制中并排显示“停止录制”和“取消录制”；停止会保存本轮文件，取消经确认后结束当前录制并删除本轮文件。另提供「预检」（`GET /v1/captures/precheck`：节点 / 会话 / 机器人就绪 + 磁盘 + 可否签发租约）。**采集元信息**（`capture meta`）按 `GET /v1/captures/meta` 返回的**分类动态渲染**（不硬编码其它分类，`capture.yml` 新增分类即出现）；`operator`（采集人）是独立文本输入，飞书姓名只作负责人默认值，支持用英文逗号填写多人；`task_name`（采集任务）来自选项。两项都非空并同步后「开始录制」才使能。选中后 `POST /v1/captures/sync` 同步到机器人进程（请求仍为扁平 `meta`，兼容旧版 Edge）；同一面板的「管理选项」展开后可直接增 / 改 / 删选项与分类（`POST` / `PATCH` / `DELETE /v1/captures/meta`，配置级、与机器人进程 / 会话状态无关，写操作须持租约，回执 `{meta: 全量}` 就地刷新列表）。面板另展示 `GET /v1/captures` 的 `capture_status`（运行位 + 已同步的 `meta` 元信息全集 + 数据目录）。

4. **机器人命令卡片**：estop / reset / execute / teleop / **人工接管（`mode=delta` 增量）** 命令控制；头部显示**当前遥操作位**（程控 / 遥操作（absolute）/ 人工接管（增量）），关闭按钮在未开启时置灰。teleop 关闭时可选择跟随空间与映射模式；`pose + delta` 时显示 xyz 三轴位置比例输入与轴锁定选项，旋转始终按 1:1 映射。双臂可选择共用比例或分别设置。操作顺序：通过关节遥操作建立初始姿态并跟踪到位；**先关闭，再重新映射设置新的锚点**：确认 teleop 关闭后，选择 `pose + delta` 并设置比例，再点击启用；请求携带本次 `follow_space` / `mode` / `follow_axes` / `pose_position_scale`，机器人在控制线程首个有效样本处采集主从锚点。运行中锁定这些输入；当前后端重复启用仍会重锚定，不能将启用作为幂等重试。配置回执与状态轮询展示映射配置；入队成功不表示锚点或 IK 已成功。

5. **像素深度 + 3D 坐标卡片（紧跟机器人命令之后）**：输入**归一化**坐标（`u` / `v`，0–1，0.5 = 画面中心；预览是 320×240 降采样图，故不直接填预览像素）→ `GET /v1/depth` 回显该像素的深度（米）+ **3D 坐标**：`xyz_camera`（相机光学系）与 `xyz_world`（`world` = 左臂基座，帧名与物理别名一并展示）。坐标由 Edge 侧用**同相机彩色内参 + 标定外参**算好（腕相机还要用**同一拍**臂位姿），不可用一律 `null`（不是 `0`）：卡片按「无有效深度 / 未标定 / 腕相机缺同拍位姿」分别说明，且外参缺失时深度仍照常给。可用深度相机列表来自 `/v1/preview` 的 `observation.depth`（与预览同源）。**放在机器人命令下方**：先摆好物 / 动完臂，再读像素的 3D 坐标。
6. **租约面板（右栏）**：租约状态 + 到期倒计时 + 签发 / 续租 / 撤销 + 自动续租开关（默认开）。

7. **上传面板**：输入目录并扫描 `.mcap` / `.json`，按 episode 选择，加入上传队列或重试失败项；远端上传未配置时上传接口返回 `501`，此场景可用「**打包**」（`POST /v1/uploads/pack`）：包名默认 `pack<选中数量>`（预填 `suggested_pack_name`，可改名），把选中 episode 的 `.mcap` + `.json` **移动**到 `<扫描目录>/<包名>/`；同名目录已存在 → `409`（改名后重试）；成功后用回执的 `scan` 刷新列表（源文件已移走）。

8. **状态面板（右栏）**：节点状态（`node_state`）、会话状态（`state`）、**匹配到的适配器**（name / type / running / control_hz / measured_hz）、磁盘。

9. **推理面板（操作在前，配置可折叠）**：会话区选中「推理」后，常用会话操作保持在前；
   「推理配置」作为默认收起的次级区域，会话外紧跟「进入推理会话」按钮，会话内紧跟连接 / 预热、
   「推理一步」与「持续推理」操作区。

    - **策略配置卡片**：从 `/v1/health` 的已注册策略列表确定可用策略，并用所选策略的 schema 解析、
      校验一份完整 YAML（schema 来自 `/v1/health` 的 `adapters.policies[].config_items`，会话内改用
      `GET /v1/infers` 的 `policy_config`）。每个策略的端点形态独立：OpenPI 使用 `host` / `port`，
      Lerobot Remote 使用 Zenoh `endpoint`；schema 的 `options` 约束仍由前端解析器和后端共同校验，
      `action_layout` 与 `observation_layout` 不允许填写声明范围外的值。界面不再为这些键生成第二套
      逐字段编辑器，避免 YAML 与表单双写。
      **配置在「进入推理」时随会话下发并锁定**（会话内整张卡片只读，没有「应用配置」按钮）；要改
      先退出推理（写内存态，下一会话生效）。必填项缺失（`missing` 非空）或端点项
      （`group=endpoint`）为空 / 越界时「进入推理」禁用（端点门控按 schema 的端点分组推导，
      不硬编码键名）。
      为减少逐项填写，Console BFF 的 `GET /api/policy-presets` 在每次请求时读取「策略配置」预设：
      运行目录 `$MOTRIX_EDGE_DIR/policy/` **存在时只读取该目录**；目录不存在时才读取包内
      `src/motrix_edge/config/policy/*.yml` 作为随 Edge 包发布的只读兜底和格式示例，两者不合并。前端本身
      不内置预设。每份文件用顶层
      `preset.id` / `preset.label` 描述选项，并在同一个 `policy` 对象内携带策略参数与 `rtc`。接口不要求
      飞书登录，断网模式仍可读取本机任务；非法 YAML、重复 ID 或缺字段必须明确报错，不能部分静默加载。
      BFF 不缓存目录，现场新增、修改或删除 YAML 后刷新页面即可生效，无需 Git 提交或重新构建 Console。
      内置 OpenPI、Lerobot Remote 两份默认预设；其它任务只进入运行目录。选择预设后立即解析完整策略草稿；
      `rtc` 与其它策略配置一起写入进入会话 payload，
      不存在第二条 RTC 写接口。「推理配置」收起时只展示当前预设、策略类型与校验状态，不展示字段计数
      或 RTC 的 H/P/E/S 数字串；展开后提供预设选择、YAML 唯一编辑入口及重新解析操作，不再重复渲染
      所有解析字段的只读网格。用户需要修改时只能编辑 YAML 并重新解析，避免出现两个事实展示源；
      「恢复预设」可丢弃本地编辑并重新载入目录文本。
      未知键、未知策略、类型或候选值不合法都在本地明确报错，不静默丢弃。
      完整 YAML 按 Edge 地址写入浏览器本地缓存；节点离线、会话异常结束、页面刷新或重新连接时优先恢复
      缓存，不用远端状态或目录预设覆盖用户最近一次已解析配置。目录预设仅在该 Edge 尚无缓存时载入。
      缓存键包含配置契约版本；当旧缓存与新版包内 schema / 预设不再安全兼容时提升版本，使用新命名空间
      重新载入预设，避免历史映射继续污染新策略连接。
    - **进入推理 = 进入会话 + 自动连接预热**：`POST /v1/infers` 成功后**前端自动调用**
      `POST /v1/infers/connect`（`infer connect`：连接策略服务端 → lerobot-act 下发策略指令 /
      服务端加载模型，openpi 预热一帧），**连接成功后其余操作（推理一步 / 持续推理 / 重新连接 /
      RTC）才使能**；连接失败**不撤销会话**（提示「可点重新连接重试」，状态经 `GET /v1/infers` 的
      `connected` 回读）。
    - **推理会话卡片按钮区**（一个按钮 = 一个命令）：**进入推理**、**连接推理节点 / 重新连接**
      （同一命令 `infer connect`：未连接时作重试入口，已连接时用于服务端变更后重连并预热）、
      **推理一步**（`infer rollout`）、**持续推理**（`infer rollout continuous`）/ **停止推理**
      （`infer rollout stop`，`continuous` 运行位控制两者互斥）、**开始录制 / 结束录制 / 取消录制**
      （`POST /v1/infers/episode/start·end·cancel` → `capture episode start/end/cancel`，robot 不关心模式；
      开始录制自动 `POST /v1/infers/sync` 同步 `{operator: "policy"}`，需要 prompt 的策略额外带
      `task_name=prompt`）、**退出推理**。推理只提供「单步」与「持续」两种模式。
    - 卡片下部：按钮区下方的可折叠策略配置（内嵌 RTC）→ **策略服务器 metadata**（只展示 `metadata` 与「已连接 / 未连接」徽标，
      连接 / 重连按钮统一在按钮区）→ 推理参数（Prompt / 录制 operator / task_name
      / 已同步 meta）。
    - **已同步 meta**（采集 / 推理两处）：只展示**非空值**（`null` / 空串 / 空列表 / 空对象不显示）——
      meta 分类多时不该把面板撑长；全空时提示「尚未同步元信息」。前端用 `metaEntries()` 统一过滤。
    - **任务成功状态**：推理页以 5 Hz 轮询 `GET /v1/task/status`。仅当 adapter 返回
      `available=true` 时，在推理状态区展示通用 `task` / `status` / `success` 与 `progress[]`；
      每个进度项只依赖 `key` / `label` / `state` / `complete`，指标放在悬浮详情中。Console 不按任务名
      分支，新增任务状态提供器无需修改页面。

10. **RTC 配置（内嵌在策略配置内）**：完整参数只存在于同一份 YAML 的 `policy.rtc`，折叠摘要不再
    额外展示 `H/P/E/S`；解析后与策略端点、观测映射一起进入同一个 payload。运行中的步号、
    剩余、最近切分与实测推理耗时继续由推理状态区只读展示。
11. **操作反馈**：每次受控操作弹 **toast**（右下角，约 4.5s 自动消失，只给一行摘要），完整记录进**左下角日志抽屉**（可展开 / 清空）；完整 JSON 回执留在各面板内部，不刷日志。
12. **数值展示口径（日志 = 网页）**：动作 / qpos 等浮点**统一保留 3 位小数**（`-0.062` 而不是
    `-0.06196591258049011`）。⚠️ **本节的服务端取整随后续控制面 MR 提供**：master 上
    `utils/data_handler.py` 只有日志函数（无 `round_floats` / `DISPLAY_FLOAT_DIGITS`），
    `GET /v1/preview` 也原样返回 float。该 MR 之后的口径是：`utils/data_handler.round_floats`
    （常量 `DISPLAY_FLOAT_DIGITS = 3`）统一作用于推理 rollout 回执与日志、`GET /v1/preview`
    的**所有数值字段**（`qpos` / `gripper` / `pose` / `pose_target` / `action`）与
    `robot execute` 回执；**内部链路（推理、控制、录制）仍用全精度**。不论服务端是否取整，
    **前端展示一律按 3 位小数渲染**（`toFixed(3)`）。

## 动作 → 命令映射

| HTTP 动作                                              | 底层命令                   |
| ------------------------------------------------------ | -------------------------- |
| `POST /v1/captures`（enter）                           | `session run capture`      |
| `DELETE /v1/captures?lease_id=`                        | `session quit`             |
| `POST /v1/infers`（必填 `policy_type`；可选 `config`） | `session run infer`        |
| `POST /v1/infers/rollout`（缺省）                      | `infer rollout`（单步）    |
| `POST /v1/infers/rollout`（body `mode=continuous`）    | `infer rollout continuous` |
| `POST /v1/infers/episode/start`                        | `capture episode start`    |
| `POST /v1/infers/episode/end`                          | `capture episode end`      |
| `POST /v1/infers/episode/cancel`                       | `capture episode cancel`   |
| `POST /v1/infers/sync`                                 | `capture sync`             |
| `POST /v1/infers/config`                               | `infer config set <json>`  |
| `DELETE /v1/infers?lease_id=`                          | `session quit`             |
| `POST /v1/commands`（`capability=robot/estop`）        | `robot estop`              |

> ⚠️ 上表的 `<scope>/<verb>` 能力名（`robot/estop` 一类）来自**命名统一 MR**，master 上仍是
> `estop` / `robot_execute` 这样的扁平名（兼容别名也在同一 MR 内）。

## 契约要点（前端实现，单点定义）

-   受控请求封装：统一注入 `X-Lease-Id`；非 2xx 抛出 `{ status, detail }`；**请求带超时**（缺省 15s，上传扫描 / 打包 120s），超时抛明确文案而不是永久 busy。
-   浏览器凭据只跟随请求目标：同源 Console 认证、注册表和 Harness 请求携带登录 Cookie；跨域直连
    Edge（`http://<edge>:8000`）不携带 Console Cookie。Console 是否启用认证不能让任意直连 Edge 请求进入
    `credentials=include`，否则通配 CORS 响应会被浏览器拒绝。
-   Edge 连接地址只持久化真实 `baseUrl`；启动时若发现旧版本遗留的同源
    `/api/edges/<id>` 代理地址，必须清除缓存并回到连接页，不能继续向 discovery-only Console 发送
    `/v1/*` 请求。
-   命令回执 `POST /v1/commands`：`status` 即命令结果（`ok` / `rejected` / `error`；`accepted` = push 型无回执），`data` 带结果字段（teleop 当前的 `teleop`/`mode`/`follow_space`/`follow_axes`，以及本地后端已支持的 `pose_position_scale`、episode 的 `episode`/`recording`）；前端据此展示命令处理结果；teleop `data.configuration_status=queued` 的配置回显不代表锚点或 IK 就绪，运行位由状态轮询确认。
-   租约状态 `GET /v1/leases`：`expires_at` 为 ISO 字符串（北京时区），倒计时 = 本地时间差；续租定时器 = `renew_interval * 1000` ms。
-   租约签发字段不向操作员暴露内部身份：每次签发生成新的 `lease_id`，`edge_id` 自动绑定当前
    health 的 `X-Edge-Id`，登录开启时 holder 自动绑定当前用户；Edge 安装镜像时再次校验
    `lease.edge_id == local identity.edge_id`，拒绝跨设备租约。
-   会话状态 `GET /v1/captures` / `/v1/infers`：`node_state` / `session_type` / `state` / adapter / policy / lease_id；`capture_status` = 机器人进程采集状态缓存（`running` + `meta` 元信息全集，推理状态同构，分类可拓展，前端按 key 逐行展示）；adapter 段带**遥操作位**（`teleop` / `teleop_mode` / `teleop_follow_space` / `teleop_follow_axes`，以及本地后端已支持的 `teleop_pose_position_scale`）。teleop 关闭时可选择空间与模式；pose + delta 时提供 xyz 三轴位置比例输入与锁轴选项，双臂支持共用或分臂，旋转固定 1:1，启用请求发送 `mode` / `follow_space` / `pose_position_scale` / `follow_axes`。位置比例只对 pose + delta 有效。不支持 pose 的机型由机器人进程明确拒绝。推理状态额外返回连接成功后的 `metadata`、`prompt` / `recording` / **`continuous`**（持续推理运行位）。
-   采集元信息选项：`GET /v1/captures/meta` 读列表（免租约）与写回执同构 `{meta: {分类: [选项]}}`；前端按返回的分类动态渲染选择框与管理的下拉（不硬编码分类，新分类无需改前端）。
-   推理面板提供 Prompt 输入（经 `POST /v1/infers/config` 写入策略配置项 `prompt`，推理/录制前必须非空）；单步响应展示最后动作及 `actions` 列表。
-   **RTC 卡片**：展示 `GET /v1/infers` 的 `rtc`（enabled / 块长 H / 执行段 E / 后缀 S / 聚合函数 /
    步号与剩余 / 最近一块的 prefix·execution·suffix 切分）。修改参数需退出会话并重新解析完整策略配置。
-   WebRTC：`RTCPeerConnection` recvonly 视频轨 → `createOffer` → `setLocalDescription` →
    `POST /v1/webrtc/offer`（body `{sdp: pc.localDescription.sdp, type}`）→ `setRemoteDescription(answer)`；
    **必须发送含 ICE 候选的 `localDescription.sdp`**。PeerConnection 由 Console 外壳常驻持有，页面标签只
    消费媒体流；切换 MANUAL / COLLECT / INFER / DEBUG 不重新协商，仅在租约、Edge 目标或相机集合变化时重连。
-   错误语义：`409`（无租约 / 已在环境 / 非法状态转移 / 节点未就绪）、`403`（异租约）、`410`（租约过期）、
    `501`（服务未注入）。UI 一律展示。

## 布局与可用性

-   **卡片可折叠**：收起暂时不用的卡片；头部保留关键摘要（租约状态 + 到期倒计时、node_state / session_state、「可发命令」、日志条数），收起也不丢状态。
-   **默认收起**：RTC 参数（高级）默认收起，需要时再展开；观测预览是**左栏首卡、默认展开**（见第 1 条），只有头部「预览显示」开关能收起它。
-   **按钮互锁与原因提示**：成对动作（采集开始 / 结束、持续推理 / 停止、录制开始 / 结束）按运行位互锁；置灰时 `title` 说明「为什么不能点」（而不只是默默变灰）。
-   **快捷反馈**：右下角 toast（一行摘要，自动消失）+ 左下角日志抽屉（完整历史）；快捷键 `Esc` = 急停（输入框 / 下拉聚焦时不抢键）。
-   按钮 / 输入框设最小长度与 `whitespace-nowrap`，防止文字被挤压到两行。
-   状态值（如会话状态）在小屏**纵向堆叠**（label 在上、value 在下），避免右对齐覆盖输入框。

## 后续

-   多路相机选择 / 切换；遥操作（双向数据通道）按钮。
-   部署：`vite build` 产物由 Edge 静态托管（`server` 挂载 dist）。
