# Edge Web Console（前端）实施计划

> **状态**：**前端源码不在本仓库**（`frontend/` 未入库）——master 已提供本计划依赖的只读契约
> （health / leases / captures / preview / webrtc，以及 `capture_status`、`POST /v1/uploads/pack`）；
> TODO 里未勾的实现项属控制台仓库，不代表后端缺接口。

## 摘要

基于 [Edge Web Console（前端）](../design/motrix_edge_web_console.md)：Vite + React + TS +
Tailwind 的浏览器测试控制台，消费现有 Edge HTTP API（health / leases / captures / preview /
webrtc）。纯前端、零后端改动。功能完整落地后删除本文档并更新本索引。

> **范围说明**：本计划交付**设计文档与索引登记**（已完成）；前端实现由**控制台仓库**承担——本仓库的
> `frontend/edge-console/` 只是本机工作副本（未入库），下文 TODO 的路径按该目录书写。所以 TODO 里
> 未勾的项**不代表后端缺接口**，只表示控制台实现状态不随本仓库跟踪。

## TODO

-   [x] `wiki/design/motrix_edge_web_console.md`：设计文档（拓扑 / 布局 / 契约要点）
-   [ ] `frontend/edge-console/`：Vite + React + TS + Tailwind(v4) 工程骨架（package.json /
        vite.config / tsconfig / index.html / main.tsx / index.css）
-   [ ] `frontend/edge-console/src/api.ts` + `.../types.ts`：API client（`X-Lease-Id` 注入、错误 `{status, detail}` 解析）
    -   契约类型（health / adapters / leases / captures / preview / webrtc）
-   [ ] Lease 管理：`useLease`（激活 / 续租 / 释放 / 自动续租定时器 / 到期倒计时）
-   [ ] Adapter 列表：`GET /v1/adapters` 展示能力 + 选择（进入会话提交 `adapter_id`）
-   [ ] 会话控制 + 信号按钮（采集：进入 / 退出 / 采集开始 / 采集结束；推理：进入 / 连接 /
        推理一步 / 持续推理 / 停止推理 / 录制开始 / 录制结束 / 退出）+ 顶部全局急停，
        按钮标注底层命令；状态轮询 `GET /v1/captures` + `GET /v1/health`
-   [ ] WebRTC 视频：`POST /v1/webrtc/offer` 协商 + `<video>` 播放 + 连接状态
-   [ ] 预览面板：`GET /v1/preview` 的数值字段（qpos / gripper / action / EEF 实测与目标）+ **WebRTC 视频**（图像不内联）——形态见 [Edge Web Console](../design/motrix_edge_web_console.md) 第 1 条
-   [ ] `npm install` + `npm run build`（tsc + vite build）通过
-   [ ] 与运行中的 Edge（`edge.yml` 默认配置，:8000）联调：租约自动续租 + 进入/退出会话 + 视频流
-   [x] 更新 `wiki/design/index.md` / `wiki/plan/index.md` 登记文档

## 后续重构（对齐 edge 接口 + 可用性）

-   [x] API/类型对齐：`infer connect`（`POST /v1/infers/connect`，`InferStatus.connected`）、
        `capture sync`（`POST /v1/captures/sync`，`CapturesStatus.capture_status`）
-   [x] 会话卡片折叠（采集 / 推理可收起；当前会话切换时自动收起另一张）
-   [x] 按钮 / 输入框最小长度（`whitespace-nowrap`），状态值小屏纵向堆叠防覆盖输入框
