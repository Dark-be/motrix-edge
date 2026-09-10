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
-   **轮询刷新**：只读状态低频率轮询（2s）；预览（qpos / 图像）独立节流。

## 页面布局（单页分区）

1. **连接栏**：Edge Base URL（默认 `http://localhost:8000`，localStorage 记忆）+ 连接 / 刷新 + 版本 / identity 概要。
2. **租约面板**：租约状态 + 到期倒计时 + 激活 / 续租 / 释放 + 自动续租开关（默认开）。
3. **状态面板**：节点状态（`node_state`）、会话状态（`state`）、**匹配到的适配器**（name / type / running）、磁盘、数据列表（1s 轮询；不再展示 `data_dir` / `default robot`）。
4. **机器人命令卡片**：estop / reset / execute / teleop 命令控制（不再显示适配器，也不提供「查看全部适配器」；匹配到的适配器见状态栏）。
5. **上传会话面板**：输入目录并扫描 `.mcap` / `.json`，按 episode 选择，加入上传队列或重试失败项。
6. **采集会话面板**：进入采集（enter）/ 退出采集（exit）/ 急停（estop），标注底层命令与合法状态；**采集元信息**（采集员 / 任务名）从 `GET /v1/captures/meta`（`capture.yml`，`capture meta` 命令维护）下拉选择，再 `POST /v1/captures/sync` 同步到机器人进程（进程保存数据时附加），并展示 `GET /v1/captures` 返回的 `capture_status`（采集员 / 任务名 / 运行位）。
7. **推理面板**：从 `/v1/health` 的已注册策略列表中必选策略，再进入推理；进入会话后先「连接推理节点」（`POST /v1/infers/connect`，`connected` 字段反映连接状态）。**策略配置卡片按所选策略动态渲染**（schema 来自 `/v1/health` 的 `adapters.policies[].config_items`，会话内改用 `GET /v1/infers` 的 `policy_config`）：先是**公共项推理端点** `host` / `port`（与其它项同层级、同一张表单；仅在**策略已连接**时置灰锁定，未连接时（含会话内）可改，另有「保存端点」按钮直接写内存态），随后是策略项——openpi → 文本指令 `prompt`（必填，`POST /v1/infers/prompt`，推理/录制前必须）；act → 模型路径 `pretrained_name_or_path`（必填，可选 `/path/to/pretrained_model`）/ `device` / `actions_per_chunk`——**未进入会话时随「进入推理」一并下发**（`POST /v1/infers` body 的 `config`），会话内经 `POST /v1/infers/config`（`infer config set`）运行时应用（端点项仅在策略已连接时置灰）；必填项缺失（`missing` 非空）或端点未配置时推理 / 录制按钮禁用。推理按钮：**推理一步**（`infer rollout`）、**持续推理**（`infer rollout continuous`，启动即回执、直到退出 / 急停）；**推理时 rollout 录制** = 「开始录制 / 结束录制」（`POST /v1/infers/episode/start·end` → `capture episode start/end`，robot 不关心模式；开始录制自动 `POST /v1/infers/sync` 同步 `{operator: "policy"}`，需要 prompt 的策略额外带 `task_name=prompt`）。多步推理与「消耗缓存」模式已取消。退出推理后结束会话，连接成功时展示策略服务器 metadata。
8. **RTC 卡片（推理面板内）**：展示 `enabled` / 块长上限 H / 执行段 E / 后缀段 S / 前置段 P（跳过）/ 最近切分 `P/E/S` / 实测推理耗时（`rtc.last_delay` 及其折算步数，供定 P 参考），并可输入 H / P / S / E / 聚合函数后「应用 RTC 参数」（`POST /v1/infers/rtc`）；参数会话内即时生效（下一块起）。
9. **视频面板**：WebRTC `<video>` 播放 + 连接状态 + 连接 / 断开按钮。
10. **观测预览面板**：`GET /v1/preview` 的 qpos / action 数值 + 摄像头名列表（与 WebRTC 并存）。**观测无需进入会话**（节点级持续观测）；面板常驻，头部「预览显示」开关控制收起 / 显示（关闭时停止轮询与推流）。

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

-   受控请求封装：统一注入 `X-Lease-Id`；非 2xx 抛出 `{ status, detail }`。
-   租约状态 `GET /v1/leases`：`expires_at` 为 ISO 字符串（北京时区），倒计时 = 本地时间差；续租定时器 = `renew_interval * 1000` ms。
-   会话状态 `GET /v1/captures` / `/v1/infers`：`node_state` / `session_type` / `state` / adapter / policy / lease_id；推理状态额外返回连接成功后的 `metadata`、当前 `prompt` / `recording` / `capture_meta`（默认 operator=policy、task_name=prompt），前端按 JSON 展示。
-   推理面板提供 Prompt 输入（`POST /v1/infers/prompt` 预置，推理/录制前必须非空）；单步响应展示最后动作及 `actions` 列表。
-   **RTC 卡片**：展示 `GET /v1/infers` 的 `rtc`（enabled / 块长 H / 执行段 E / 后缀 S / 聚合函数 /
    步号与剩余 / 最近一块的 prefix·execution·suffix 切分），并可经 `POST /v1/infers/rtc` 运行期改参数。
-   WebRTC：`RTCPeerConnection` recvonly 视频轨 → `createOffer` → `setLocalDescription` →
    `POST /v1/webrtc/offer`（body `{sdp: pc.localDescription.sdp, type}`）→ `setRemoteDescription(answer)`；
    **必须发送含 ICE 候选的 `localDescription.sdp`**。
-   错误语义：`409`（无租约 / 已在环境 / 非法状态转移 / 节点未就绪）、`403`（异租约）、`410`（租约过期）、
    `501`（服务未注入）。UI 一律展示。

## 布局与可用性

-   会话卡片（采集 / 推理）可**折叠**：收起暂时不用的会话；当前会话切换时自动收起另一张卡片。
-   按钮 / 输入框设最小长度与 `whitespace-nowrap`，防止文字被挤压到两行。
-   状态值（如会话状态）在小屏**纵向堆叠**（label 在上、value 在下），避免右对齐覆盖输入框。

## 后续

-   多路相机选择 / 切换；遥操作（双向数据通道）按钮。
-   部署：`vite build` 产物由 Edge 静态托管（`server` 挂载 dist）。
