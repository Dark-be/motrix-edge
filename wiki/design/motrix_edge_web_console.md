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
6. **采集会话面板**：进入采集（enter）/ 退出采集（exit）/ 急停（estop），标注底层命令与合法状态；支持同步采集元信息（采集员 / 任务名，`POST /v1/captures/sync`，进程保存数据时附加），并展示 `GET /v1/captures` 返回的 `capture_status`（采集员 / 任务名 / 运行位）。
7. **推理面板**：从 `/v1/health` 的已注册策略列表中必选策略，再进入推理；进入会话后「连接推理节点」（`POST /v1/infers/connect`，`connected` 字段反映连接状态）；提供「推理一步」和按输入次数执行的「推理多步」两个按钮；退出推理后结束会话，连接成功时展示策略服务器 metadata。
8. **视频面板**：WebRTC `<video>` 播放 + 连接状态 + 连接 / 断开按钮。
9. **观测预览面板**：`GET /v1/preview` 的 qpos / action 数值 + 摄像头名列表（与 WebRTC 并存）。**观测无需进入会话**（节点级持续观测）；面板常驻，头部「预览显示」开关控制收起 / 显示（关闭时停止轮询与推流）。

## 动作 → 命令映射

| HTTP 动作                                 | 底层命令              |
| ----------------------------------------- | --------------------- |
| `POST /v1/captures`（enter）              | `session run capture` |
| `DELETE /v1/captures?lease_id=`           | `session quit`        |
| `POST /v1/infers`（必填 `policy_type`）   | `session run infer`   |
| `POST /v1/infers/rollout`（body `count`） | `infer rollout count` |
| `DELETE /v1/infers?lease_id=`             | `session quit`        |
| `POST /v1/commands`（`capability=estop`） | `robot estop`         |

## 契约要点（前端实现，单点定义）

-   受控请求封装：统一注入 `X-Lease-Id`；非 2xx 抛出 `{ status, detail }`。
-   租约状态 `GET /v1/leases`：`expires_at` 为 ISO 字符串（北京时区），倒计时 = 本地时间差；续租定时器 = `renew_interval * 1000` ms。
-   会话状态 `GET /v1/captures` / `/v1/infers`：`node_state` / `session_type` / `state` / adapter / policy / lease_id；推理状态额外返回连接成功后的 `metadata`，前端按 JSON 展示。
-   推理面板提供 rollout 次数输入（1–100）；一次请求携带 `{count}`，展示响应中的最后动作及 `actions` 列表。
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
