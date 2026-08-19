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
3. **状态面板**：节点状态（`node_state`）、会话状态（`state`）、机器人就绪、磁盘、保存目录、数据列表。
4. **适配器面板**：主显示 discover 绑定的唯一适配器；「查看全部适配器」toggle 展开 `GET /v1/adapters`（只读展示）。
5. **采集会话面板**：进入采集（enter）/ 退出采集（exit）/ 急停（estop），标注底层命令与合法状态。
6. **推理面板**：进入推理（enter）/ 推理 rollout / 退出推理（exit）/ 急停（estop），策略可选。
7. **视频面板**：WebRTC `<video>` 播放 + 连接状态 + 连接 / 断开按钮。
8. **观测预览面板**：`GET /v1/preview` 的 qpos / action 数值 + 摄像头名列表（与 WebRTC 并存）。

## 动作 → 命令映射

| HTTP 动作                                 | 底层命令              |
| ----------------------------------------- | --------------------- |
| `POST /v1/captures`（enter）              | `session run capture` |
| `DELETE /v1/captures?lease_id=`           | `session quit`        |
| `POST /v1/infers`（可选 `policy_type`）   | `session run infer`   |
| `POST /v1/infers/rollout`                 | `infer rollout`       |
| `DELETE /v1/infers?lease_id=`             | `session quit`        |
| `POST /v1/commands`（`capability=estop`） | `robot estop`         |

## 契约要点（前端实现，单点定义）

-   受控请求封装：统一注入 `X-Lease-Id`；非 2xx 抛出 `{ status, detail }`。
-   租约状态 `GET /v1/leases`：`expires_at` 为 ISO 字符串（北京时区），倒计时 = 本地时间差；续租定时器 = `renew_interval * 1000` ms。
-   会话状态 `GET /v1/captures` / `/v1/infers`：`node_state` / `session_type` / `state` / adapter / policy / lease_id。
-   WebRTC：`RTCPeerConnection` recvonly 视频轨 → `createOffer` → `setLocalDescription` →
    `POST /v1/webrtc/offer`（body `{sdp: pc.localDescription.sdp, type}`）→ `setRemoteDescription(answer)`；
    **必须发送含 ICE 候选的 `localDescription.sdp`**。
-   错误语义：`409`（无租约 / 已在环境 / 非法状态转移 / 节点未就绪）、`403`（异租约）、`410`（租约过期）、
    `501`（服务未注入）。UI 一律展示。

## 后续

-   多路相机选择 / 切换；遥操作（双向数据通道）按钮。
-   部署：`vite build` 产物由 Edge 静态托管（`server` 挂载 dist）。
