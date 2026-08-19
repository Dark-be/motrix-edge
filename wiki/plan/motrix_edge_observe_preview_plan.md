# 观测语义与预览 实施计划

## 摘要

基于 [FrameManager 与 WebRTC 推流](../design/motrix_edge_frame_webrtc.md) 与 [机器人适配器（adapter）](../design/motrix_edge_adapter.md)：
`observe()` 不再作为采集数据源，改为「预览 + policy 推理」消费；采集由适配器独立实现；
新增 `GET /v1/preview`。功能完整落地后删除本文档并更新本索引。

> **范围**：对应实现随 **feat/6**（观测语义）与 **feat/3**（`GET /v1/preview` 端点）落地；
> 本 MR（chore/2）仅冻结契约文档，相关项标 `[ ]`。

## TODO

-   [ ] `adapter/test_adapter.py`：`observe()` 不再写采集缓冲；`start_capture` 启动独立
        采集线程（模拟外部采集程序），`stop_capture` 停止并写盘
-   [ ] `session/capture_session.py`：经 `FrameManager.update` 写入观测帧（observe 循环每帧）；
        回合循环仅缓存预览帧 + 处理信号，采集由 adapter 启停
-   [ ] `session/infer_session.py`：经 `FrameManager.update` 写入观测帧（供预览）
-   [ ] `server/capture.py`：`CaptureService.preview()` 序列化 `FrameManager.latest()`
        （qpos/action → list，图像不内联 → 仅摄像头名列表；图像经 WebRTC 推流）
-   [ ] `server/app.py`：注册 `GET /v1/preview`
-   [ ] 测试：`test_adapter` / `test_capture_session` 采集独立化；`test_server` 新增
        `GET /v1/preview` 预览用例（断言摄像头名列表，图像不内联）
-   [ ] 全量 `uv run ruff check src tests` + `uv run pytest`（全绿）
