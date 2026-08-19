# 采集会话与租约（本地实现）实施计划

## 摘要

基于[会话（session）](../design/motrix_edge_session.md)的落地计划。
功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] 设计：会话 = 任务环境 + 租约（`enter` 产生、运行期维持、`exit` 释放、异租约 `403`）
-   [ ] `CaptureService` 租约：`enter` 生成并绑定 `lease_id`；控制动作校验 `X-Lease-Id`
        （缺失 / 异租约 `403`，未 `enter` `409`）；`exit` 释放；`status` 回报 `lease_id`
-   [ ] `server/app.py`：控制动作路由读 `X-Lease-Id` 头并透传
-   [ ] 测试：`test_server.py` 补租约契约（`enter` 返回 lease / 异租约 `403` / 缺失租约 `403` /
        未 `enter` `409` / 退出释放后重进新租约 / 端到端落盘）
-   [ ] 全量 `uv run ruff check .` + `uv run pytest`

## 后续（M11/M12，不在本期）

-   [ ] **CaptureBundle**：按 hardware adapter 契约（`data_status()` 返回的数据文件夹）
        生成 manifest / checksum → Local Spool → Uploader（服务端确认后才删）
-   [ ] 详细预检（磁盘水位 / 时钟 / 相机 / 必需 topic / 进程冲突）
-   [ ] WebRTC 信令 + 只读流 + `confirm` 后再 `start`
-   [ ] Supervisor 版本化采集模板 + 持续监控（heartbeat / frame_rate / error / disk）
-   [ ] `stop` 冻结 manifest / checksum / 摘要
-   [ ] Kleinkram 上传（断言 + upload_session + 断点直传 + commit）
-   [ ] 本地按水位 + 保留策略回收（仅服务端确认后可删）
