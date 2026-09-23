# Plan

## 摘要

本目录保存基于设计文档制定的实现计划。每篇计划维护 TODO list，完成一项勾选一项；计划完整落地后删除对应文档并更新本索引。

> 已随 feat/6（任务运行时核心）与 feat/3（HTTP 控制面）落地的计划（webrtc / observe_preview /
> adapter_discover / robot_adapter / capture_session / captures / infer_connect_capture_sync /
> infer_test）已删除；feat/7 的 upload_session / upload_pack 两份计划亦已落地删除
> （未实现项见 [上传会话](../design/motrix_edge_upload_session.md)「后续版本」）；推理策略选择
> （策略类型随会话选择 + 状态上报 + 前端下拉）已随 policy 客户端落地，计划亦已删除。
> 以下为保留的后续 / 在途计划（primitives / RPent 的实施计划随其特性 MR 合入，暂未登记）。

## 索引

-   [边缘节点开发计划](./motrix_edge_development_plan.md)
-   [Adapter 身份与选择实施计划](./motrix_edge_adapter_selection_plan.md)
-   [Edge Web Console（前端）实施计划](./motrix_edge_web_console_plan.md)
-   [ACT 走 Lerobot gRPC AsyncInference + transport 通用化实施计划](./motrix_edge_policy_act_grpc_plan.md)
-   [robot-pipeline 控制 / 观测双线程实施计划](./robot_pipeline_control_thread_plan.md)
-   [robot-pipeline 位姿动作（求解器）实施计划](./robot_pipeline_cartesian_plan.md)
-   [RTC 过渡策略（权重过渡 / 连续过渡）实施计划](./motrix_edge_rtc_transition_plan.md)
-   [robot-pipeline 遥操作（增量接管）实施计划](./robot_pipeline_teleop_plan.md)

<!-- 新增计划文档后在此登记标题链接。 -->
