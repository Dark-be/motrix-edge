# Design

## 摘要

本目录保存项目的方案设计、架构设计与算法设计。设计文档**按 `src/motrix_edge/` 子包组织**
（分包导航），每包一篇、总览一篇；从总览进入，按需查阅对应包文档。
另外收录与 edge 并列的 `robot-pipeline/` 子项目（机器人底层承载）的运行时设计。

## 索引

-   [边缘节点架构总览](./motrix_edge_architecture.md)（导航入口：分层 / 数据流 / 分包索引）
-   [节点生命周期（node）](./motrix_edge_node.md)
-   [机器人适配器（adapter）](./motrix_edge_adapter.md)
-   [会话（session）](./motrix_edge_session.md)
-   [推理策略客户端（policy）](./motrix_edge_policy.md)
-   [实时动作块（rtc）](./motrix_edge_rtc.md)
-   [上传会话（UploadSession）](./motrix_edge_upload_session.md)
-   [命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   [配置与命令行（config / CLI）](./motrix_edge_config.md)
-   [HTTP 控制面（server）](./motrix_edge_server.md)
-   [Edge 级租约（lease）](./motrix_edge_lease.md)
-   [Edge Web Console（前端）](./motrix_edge_web_console.md)
-   [采集元信息选项（capture meta）](./motrix_edge_capture_meta.md)
-   [robot-pipeline 运行时（env / robot 双线程）](./robot_pipeline_runtime.md)

<!-- 新增设计文档后在此登记标题链接。 -->
