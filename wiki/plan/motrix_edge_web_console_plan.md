# Edge Web Console（前端）实施计划

> **状态**：Console 已以 `console/` 正式子项目纳入本仓库——master 已提供本计划依赖的只读契约
> （health / leases / captures / preview / webrtc，以及 `capture_status`、`POST /v1/uploads/pack`）；
> TODO 里未勾的实现项属控制台仓库，不代表后端缺接口。

> **位置比例配套状态**：后端与 Console 都已支持 `pose_position_scale`、状态与入队配置回显。
> 初始姿态通过关节遥操作建立并跟踪到位，不提供手动摆动从臂后的实测重锚定。

## 摘要

基于 [Edge Web Console（前端）](../design/motrix_edge_web_console.md)：Vite + React + TS +
Tailwind 的浏览器控制台，Node BFF 负责飞书登录、设备白名单和 Edge API 代理。功能完整落地后删除本文档并更新本索引。

> **范围说明**：Console UI 与 Node BFF 均在本仓库 `console/` 中维护；TODO 反映当前子项目实现状态。

## TODO

-   [x] `wiki/design/motrix_edge_web_console.md`：设计文档（拓扑 / 布局 / 契约要点）
-   [ ] `console/`：Vite + React + TS + Tailwind(v4) 工程骨架（package.json /
        vite.config / tsconfig / index.html / main.tsx / index.css）
-   [ ] `console/src/api.ts` + `.../types.ts`：API client（`X-Lease-Id` 注入、错误 `{status, detail}` 解析）
    -   契约类型（health / adapters / leases / captures / preview / webrtc）
-   [ ] Lease 管理：`useLease`（激活 / 续租 / 释放 / 自动续租定时器 / 到期倒计时）
-   [ ] Adapter 信息：`GET /v1/adapters` 展示已注册能力，`GET /v1/health` 展示节点当前绑定的
        单一 adapter；进入会话不提交 adapter id
-   [ ] 会话控制 + 信号按钮（采集：进入 / 退出 / 采集开始 / 采集结束；推理：进入 / 连接 /
        推理一步 / 持续推理 / 停止推理 / 录制开始 / 录制结束 / 退出）+ 顶部全局急停，
        按钮标注底层命令；状态轮询 `GET /v1/captures` + `GET /v1/health`
-   [ ] 机器人遥操作卡片：teleop 关闭时设置映射；pose + delta 时提供 xyz 三轴位置比例，双臂支持共用或逐臂，
        旋转固定 1:1。操作顺序为通过关节遥操作建立初始姿态并跟踪到位；
        **先关闭，再重新映射设置新的锚点**：确认关闭后选择 pose + delta 和比例，再开启；
        启用请求提交 `follow_space` / `follow_axes` / `pose_position_scale`，开启期间冻结输入，状态 / 回执显示生效配置。
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
-   [x] 采集 / 推理 / 手动 / 调试页复用 3D 场景；实测 qpos 用实色臂，Robot `action` 用蓝色半透明臂
-   [x] 策略 schema 增加 `options`，layout 类配置由通用表单渲染为固定候选下拉并在后端校验
-   [x] `/v1/preview` 与数值观测卡展示 Robot 显式提供的逐臂 `qpos_err`
-   [x] 采集开始门禁：`operator` / `task_name` 两项选择并同步后才允许开始录制；缺失时按钮置灰并提示原因
