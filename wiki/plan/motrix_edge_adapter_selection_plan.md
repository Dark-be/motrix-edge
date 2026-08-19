# Adapter 身份与选择 实施计划

## 摘要

基于 [机器人适配器（adapter）](../design/motrix_edge_adapter.md) 的演进：
`adapter` 配置从单 dict 改为**数组**（只声明身份 `id` / `name` / `type`，能力由适配器
`capabilities` 返回），进入会话（capture / infer）时按 `id` 选择 adapter——CLI 输入
`id`（`read_key` 改多字母 + 回车），HTTP 从响应列出、`POST` 附带 `id`。功能完整落地后
删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) / [!5（任务运行时）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/5) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] 设计：adapter 数组配置 + `id` 身份解析 + 会话选择（CLI / HTTP）
-   [ ] `adapter/__init__.py`：`adapter_catalog(base_cfg)` + `get_adapter(base_cfg, adapter_id)`
        （兼容单 dict / 旧 `robot` 段；按 `id`/`type` 查找）
-   [ ] `adapter/test_adapter.py`：`TestRobotAdapter` 身份 / 行为参数改类级常量内置
        （`NAME` / `ACTION_DIM` / `IMAGES` / `STEP_RAD` / `SAVE_DIR` 等），不再从配置
        读取（config 仅作可选覆盖，如测试注入临时 `save_dir`）
-   [ ] `config/test.yml`：`adapter` 改数组（`id` / `name` / `type`）
-   [ ] `session`：`BaseSession` / `CaptureSession` / `InferSession` /
        `get_session` 透传 `adapter_id`
-   [ ] `node.py`：`adapter_selector`（CLI 交互注入）+ `pending_adapter_id`（HTTP 槽位）+
        `_select_session(session_type, adapter_id)`
-   [ ] `utils/data_handler.py`：`read_key()` 重写为「多字母 + 回车」行输入
-   [ ] `__main__.py`：共享输入协调器（键盘线程读 stdin，信号 / adapter id 分发）+
        `adapter_selector` 注入
-   [ ] `server/app.py`：`GET /v1/health` 的 `adapters.robots` 返回 catalog
        （`[{id, name, type}]`）
-   [ ] `server/capture.py`：`POST /v1/captures`（enter）请求体携带 `adapter_id`，
        校验 + 响应回显选中 adapter
-   [ ] 测试：`test_adapter` / `test_capture_session` / `test_server` / `test_node` 同步
-   [ ] 全量 `uv run ruff check src tests` + `uv run pytest`（全绿）
