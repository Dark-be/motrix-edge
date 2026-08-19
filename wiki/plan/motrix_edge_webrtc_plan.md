# FrameManager 与 WebRTC 推流 实施计划

## 摘要

基于 [FrameManager 与 WebRTC 推流](../design/motrix_edge_frame_webrtc.md)：`FrameManager` 单点
管理最新观测帧缓存（session 写入、preview / WebRTC 读取）；aiortc 标准信令 + 视频轨道推流。
功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] `frame/` 子包：`FrameManager`（update / latest / clear，线程安全，图像降采样 jpeg）
-   [ ] `session`：改用 `FrameManager.update`（替换 session 自维护 `latest_obs`）
-   [ ] `server/capture.py`：preview 从 `FrameManager.latest()` 读取
-   [ ] `node.py` / `__main__.py`：创建并注入 `FrameManager`（Edge 级）
-   [ ] 依赖：`aiortc` 加入 `pyproject.toml`
-   [ ] `server/webrtc.py`：`FrameStreamTrack`（从 FrameManager 取帧 → av.VideoFrame）+ 信令处理
-   [ ] `server/app.py`：注册 `POST /v1/webrtc/offer`（需租约）
-   [ ] 测试：`FrameManager` 单测 + webrtc offer 端点（mock aiortc）
-   [ ] 全量 `uv run ruff check src tests` + `uv run pytest`（全绿）
