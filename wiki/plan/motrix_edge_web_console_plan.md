# Edge Web Console（前端）实施计划

## 摘要

基于 [Edge Web Console（前端）](../design/motrix_edge_web_console.md)：Vite + React + TS +
Tailwind 的浏览器测试控制台，消费现有 Edge HTTP API（health / leases / captures / preview /
webrtc）。纯前端、零后端改动。功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）交付设计文档与 index 登记；本计划前端实现项由后续 MR [!4](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [x] `wiki/design/motrix_edge_web_console.md`：设计文档（拓扑 / 布局 / 契约要点）
-   [ ] `frontend/edge-console/`：Vite + React + TS + Tailwind(v4) 工程骨架（package.json /
        vite.config / tsconfig / index.html / main.tsx / index.css）
-   [ ] `src/api.ts` + `src/types.ts`：API client（`X-Lease-Id` 注入、错误 `{status, detail}` 解析）
    -   契约类型（health / adapters / leases / captures / preview / webrtc）
-   [ ] Lease 管理：`useLease`（激活 / 续租 / 释放 / 自动续租定时器 / 到期倒计时）
-   [ ] Adapter 列表：`GET /v1/adapters` 展示能力 + 选择（进入会话提交 `adapter_id`）
-   [ ] Session 控制 + 信号按钮：enter / start / stop / interrupt / resume / exit / estop，
        按钮标注底层信号；状态轮询 `GET /v1/captures` + `GET /v1/health`
-   [ ] WebRTC 视频：`POST /v1/webrtc/offer` 协商 + `<video>` 播放 + 连接状态
-   [ ] 预览面板：`GET /v1/preview` qpos / action / 摄像头 jpeg
-   [ ] `npm install` + `npm run build`（tsc + vite build）通过
-   [ ] 与运行中的 Edge（`config/edge.yml`，:8000）联调：租约自动续租 + 进入/退出会话 + 视频流
-   [x] 更新 `wiki/design/index.md` / `wiki/plan/index.md` 登记文档
