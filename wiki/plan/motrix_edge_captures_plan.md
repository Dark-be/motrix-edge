# captures HTTP 接口实施计划

## 摘要

基于[HTTP 控制面（server）](../design/motrix_edge_server.md)的落地计划。
功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] `utils/signals.py`：新增 `SignalBus`（线程安全信号总线：web / CLI push，EdgeNode poll）
-   [ ] `server/capture.py`：`CaptureService(node, bus)` —— 绑定「正在运行的 node + 共享 SignalBus」，**不持有 / 不运行 node**；动作翻译 `precheck / start / stop / interrupt / resume / close / status`，前置状态校验（非法转移 `409` 且不推信号）
-   [ ] adapter 采集录制提供只读 `save_dir`（供 status 上报保存目录）
-   [ ] `server/app.py`：`create_app(base_cfg, captures=None)` 挂 `/v1/captures/*` 路由（未注入时 `501`）
-   [ ] `__main__.py`：默认运行 = node 主线程 + web 独立线程（共享 SignalBus + CLI 按键线程保留）
-   [ ] 测试：`test_server.py` 补 captures 契约（合法/非法转移、信号翻译、status/precheck、501、端到端落盘），用注入 fake node + 真实 EdgeNode，无硬件可跑
-   [ ] 全量 `uv run ruff check .` + `uv run pytest` 通过

## 增量（2026-08：显式 enter/exit 会话生命周期）

-   [ ] `CaptureService` 增加显式会话生命周期：`enter`（`y`+`q`）/ `exit`（`p`）；已在环境 `enter` 或未在环境 `exit` → `409`；`start` 改为需先 `enter`（不再自动激活）
-   [ ] `node._on_active` 支持 `SIG_CAPTURE_FINISH`（`cf`）退出已连接的采集会话回 IDLE
-   [ ] `server/app.py`：会话资源 `POST /v1/captures`（创建/enter）、`GET /v1/captures`（status/precheck）、`DELETE /v1/captures?lease_id=`（退出+销毁租约）；回合控制 `POST /v1/captures/{start,stop,interrupt,resume}`
-   [ ] 测试：补 enter/exit 契约（已在环境 enter 409、未在环境 exit 409、enter→start→stop→exit 全流程）
-   [ ] 全量 `uv run ruff check .` + `uv run pytest` 通过
