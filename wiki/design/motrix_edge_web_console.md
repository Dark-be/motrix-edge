# Edge Web Console（前端）

## 摘要

Edge Web Console 是 MotrixEdge 的**浏览器调试 / 测试前端**（Vite + React + TypeScript +
Tailwind CSS）：经 Edge HTTP API（`/v1/*`）展示 Edge 状态、管理租约、驱动采集 / 推理会话、
发送控制信号，并经 WebRTC 播放机器人摄像头实时视频流。定位为**测试项目**——信息展示尽可能完整
（租约到期倒计时、命令名、状态、能力、磁盘等），按钮即信号。

## 目标与原则

-   **纯前端、零后端改动**：只消费现有 HTTP API 契约（见 [HTTP 控制面（server）](./motrix_edge_server.md)
    / [Edge 级租约（lease）](./motrix_edge_lease.md) / [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)）。
-   **信息尽可能完整**：每个面板展示原始契约字段 + 派生信息（如租约到期倒计时）；错误展示 HTTP status / detail。
-   **按钮即命令**：每个控制按钮即一个 HTTP 动作，UI 标注其对应命令名。
-   **租约自动维护**：激活后按 `renew_interval` 自动续租（浏览器端定时器）+ 到期倒计时；可手动续租 / 释放。
-   **受控操作带租约**：所有受控请求携带 `X-Lease-Id` 头；退出经 query `lease_id` 提交。
-   **轮询刷新**：只读状态轮询（1s，`usePolling`）；**上一发未返回则跳过本轮**、页面不可见时暂停（切回立即补一次），避免慢请求叠加；观测预览（qpos / 图像）独立开关。

## 页面布局（单页分区）

总体：**顶部 sticky 状态条** + 两栏（左：操作，右：状态与观测）；操作结果即时 toast，完整历史进左下角日志抽屉。

0. **顶部状态条（sticky）**：Edge Base URL（默认 `http://localhost:8000`，localStorage 记忆 + **刷新自动重连**）+ 连接 / 断开 + 在线 / **离线（数据已过期）** + `node_state` + 当前会话类型 / 状态 + 租约状态与到期倒计时 + **全局急停**（须持租约；快捷键 `Esc`）。
    - **顶部状态条常驻**：急停、租约倒计时、节点 / 会话状态不必滚到下面的卡片里找。
1. **会话区（采集 / 推理二选一）**：同一时刻只可能有一个会话，故两张卡共用一块位置——顶部「会话类型」分段控件切换（会话进行中则锁定为当前会话）；只有被选中的那张面板参与渲染。
2. **采集会话面板**：进入采集（enter）/ 退出采集（exit）；**采集开始 / 结束（`capture episode start/end`）按采集运行位互锁**——开始后（收到回执或 `capture_status.running=true`）「开始」置灰、「结束」使能，反之亦然，避免误触反序操作；另提供「预检」（`GET /v1/captures/precheck`：节点 / 会话 / 机器人就绪 + 磁盘 + 可否签发租约）。**采集元信息**（`capture meta`）按 `GET /v1/captures/meta` 返回的**分类动态渲染**（不硬编码分类，`capture.yml` 新增分类即出现），选中后 `POST /v1/captures/sync` 同步到机器人进程（进程保存数据时附加）；同一面板的「管理选项」展开后可直接增 / 改 / 删选项与分类（`POST` / `PATCH` / `DELETE /v1/captures/meta`，配置级、与节点 / 会话状态无关，写操作须持租约，回执 `{meta: 全量}` 就地刷新列表）。面板另展示 `GET /v1/captures` 的 `capture_status`（运行位 + 已同步的 `meta` 元信息全集 + 数据目录）。
3. **机器人命令卡片**：estop / reset / execute / teleop / **人工接管（`mode=delta` 增量）** 命令控制；头部显示**当前遥操作位**（程控 / 遥操作（absolute）/ 人工接管（增量）），关遥操作在未开启时置灰。
4. **租约面板**：租约状态 + 到期倒计时 + 签发 / 续租 / 撤销 + 自动续租开关（默认开）。
5. **上传会话面板**：输入目录并扫描 `.mcap` / `.json`，按 episode 选择，加入上传队列或重试失败项；远端上传未配置时上传接口返回 `501`，此场景可用「**打包**」（`POST /v1/uploads/pack`）：包名默认 `pack<选中数量>`（预填 `suggested_pack_name`，可改名），把选中 episode 的 `.mcap` + `.json` **移动**到 `<扫描目录>/<包名>/`；同名目录已存在 → `409`（改名后重试）；成功后用回执的 `scan` 刷新列表（源文件已移走）。
6. **状态面板**（右栏）：节点状态（`node_state`）、会话状态（`state`）、**匹配到的适配器**（name / type / running / control_hz / measured_hz）、磁盘。
7. **推理面板**：从 `/v1/health` 的已注册策略列表中必选策略，再进入推理；进入会话后先「连接推理节点」（`POST /v1/infers/connect`，`connected` 字段反映连接状态）。**策略配置卡片按所选策略动态渲染**（schema 来自 `/v1/health` 的 `adapters.policies[].config_items`，会话内改用 `GET /v1/infers` 的 `policy_config`）：先是**公共项推理端点** `host` / `port`（与其它项同层级、同一张表单；仅在**策略已连接**时置灰锁定，未连接时（含会话内）可改，另有「保存端点」按钮直接写内存态），随后是策略项——openpi → 文本指令 `prompt`（必填，`POST /v1/infers/prompt`，推理/录制前必须）；act → 模型路径 `pretrained_name_or_path`（必填，可选 `/path/to/pretrained_model`）/ `device` / `actions_per_chunk`——**未进入会话时随「进入推理」一并下发**（`POST /v1/infers` body 的 `config`），会话内经 `POST /v1/infers/config`（`infer config set`）运行时应用（端点项仅在策略已连接时置灰）；必填项缺失（`missing` 非空）或端点未配置时推理 / 录制按钮禁用。推理按钮：**推理一步**（`infer rollout`）、**持续推理**（`infer rollout continuous`）/ **停止推理**（`infer rollout stop`，`continuous` 运行位控制两者互斥）、**开始录制 / 结束录制**（`POST /v1/infers/episode/start·end` → `capture episode start/end`，robot 不关心模式；开始录制自动 `POST /v1/infers/sync` 同步 `{operator: "policy"}`，需要 prompt 的策略额外带 `task_name=prompt`）。多步推理与「消耗缓存」模式已取消。退出推理后结束会话，连接成功时展示策略服务器 metadata。
8. **RTC 卡片（推理面板内，默认收起）**：展示 `enabled` / 块长上限 H / 执行段 E / 后缀段 S / 前置段 P（跳过）/ 步号与剩余 / 最近切分 `P/E/S` / 实测推理耗时（`rtc.last_delay` 及其折算步数，供定 P 参考）；「展开参数」后可输入 H / P / S / E / 聚合函数并「应用 RTC 参数」（`POST /v1/infers/rtc`），参数会话内即时生效（下一块起）。
9. **观测预览面板（右栏，默认收起）**：WebRTC `<video>` 逐相机播放 + 连接状态 + 连接 / 断开按钮 + `GET /v1/preview` 的 qpos / action 数值与摄像头名列表（图像不内联）。**观测无需进入会话**（节点级持续观测）；头部「预览显示」开关打开后才轮询与推流。
10. **操作反馈**：每次受控操作弹 **toast**（右侧下方，约 4.5s 自动消失，只给一行摘要），完整记录进**左下角日志抽屉**（可展开 / 清空）；完整 JSON 回执留在各面板内部，不刷日志。

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
| `POST /v1/infers/sync`                                 | `capture sync`             |
| `POST /v1/infers/config`                               | `infer config set <json>`  |
| `POST /v1/infers/prompt`                               | `infer prompt <text>`      |
| `POST /v1/infers/rtc`                                  | `infer rtc set <json>`     |
| `DELETE /v1/infers?lease_id=`                          | `session quit`             |
| `POST /v1/commands`（`capability=estop`）              | `robot estop`              |

## 契约要点（前端实现，单点定义）

-   受控请求封装：统一注入 `X-Lease-Id`；非 2xx 抛出 `{ status, detail }`；**请求带超时**（缺省 15s，上传扫描 / 打包 120s），超时抛明确文案而不是永久 busy。
-   命令回执 `POST /v1/commands`：`status` 即命令结果（`ok` / `rejected` / `error`；`accepted` = push 型无回执），`data` 带结果字段（teleop 的 `teleop`/`mode`、episode 的 `episode`/`recording`）；前端据此判定成功并即时翻转按钮状态（不等下一次轮询）。
-   租约状态 `GET /v1/leases`：`expires_at` 为 ISO 字符串（北京时区），倒计时 = 本地时间差；续租定时器 = `renew_interval * 1000` ms。
-   会话状态 `GET /v1/captures` / `/v1/infers`：`node_state` / `session_type` / `state` / adapter / policy / lease_id；`capture_status` = 机器人进程采集状态缓存（`running` + `meta` 元信息全集，推理状态同构，分类可拓展，前端按 key 逐行展示）；adapter 段带**遥操作位**（`teleop` / `teleop_mode`）；推理状态额外返回连接成功后的 `metadata`、`prompt` / `recording` / **`continuous`**（持续推理运行位）。
-   采集元信息选项：`GET /v1/captures/meta` 读列表（免租约）与写回执同构 `{meta: {分类: [选项]}}`；前端按返回的分类动态渲染选择框与管理的下拉（不硬编码分类，新分类无需改前端）。
-   推理面板提供 Prompt 输入（`POST /v1/infers/prompt` 预置，推理/录制前必须非空）；单步响应展示最后动作及 `actions` 列表。
-   **RTC 卡片**：展示 `GET /v1/infers` 的 `rtc`（enabled / 块长 H / 执行段 E / 后缀 S / 聚合函数 /
    步号与剩余 / 最近一块的 prefix·execution·suffix 切分），并可经 `POST /v1/infers/rtc` 运行期改参数。
-   WebRTC：`RTCPeerConnection` recvonly 视频轨 → `createOffer` → `setLocalDescription` →
    `POST /v1/webrtc/offer`（body `{sdp: pc.localDescription.sdp, type}`）→ `setRemoteDescription(answer)`；
    **必须发送含 ICE 候选的 `localDescription.sdp`**。
-   错误语义：`409`（无租约 / 已在环境 / 非法状态转移 / 节点未就绪）、`403`（异租约）、`410`（租约过期）、
    `501`（服务未注入）。UI 一律展示。

## 布局与可用性

-   **卡片可折叠**：收起暂时不用的卡片；头部保留关键摘要（租约状态 + 到期倒计时、node_state / session_state、「可发命令」、日志条数），收起也不丢状态。
-   **默认收起**：观测预览（占屏大）、RTC 参数（高级）默认收起，需要时再展开。
-   **按钮互锁与原因提示**：成对动作（采集开始 / 结束、持续推理 / 停止、录制开始 / 结束）按运行位互锁；置灰时 `title` 说明「为什么不能点」（而不只是默默变灰）。
-   **快捷反馈**：右下角 toast（一行摘要，自动消失）+ 左下角日志抽屉（完整历史）；快捷键 `Esc` = 急停（输入框 / 下拉聚焦时不抢键）。
-   按钮 / 输入框设最小长度与 `whitespace-nowrap`，防止文字被挤压到两行。
-   状态值（如会话状态）在小屏**纵向堆叠**（label 在上、value 在下），避免右对齐覆盖输入框。

## 后续

-   多路相机选择 / 切换；遥操作（双向数据通道）按钮。
-   部署：`vite build` 产物由 Edge 静态托管（`server` 挂载 dist）。
