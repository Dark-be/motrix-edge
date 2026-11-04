# robot-pipeline 统一多频运行时计划

## 摘要

在唯一 BaseEnv 中加入可配置的独立控制、状态和逐相机生产流，并将采集 action 改为实际成功提交的
限速命令。多频机型保持显式启用，合入前要求自动测试，设为全机型默认前要求真机验证。

## 实施

-   [x] BaseRobot 提供按相机取帧与缓存帧组装接口。
-   [x] BaseEnv 增加独立状态线程和逐相机 worker，观测线程只消费最新缓存。
-   [x] Recorder 增加独立 action 与逐相机队列。
-   [x] MCAP 分开写 qpos 与实际 command action，保留直接 collector API 兼容。
-   [x] 增加频率、生命周期、失败隔离和 action 语义测试。
-   [x] 删除重复/未接线实验实现，更新文档与现场验证清单。
-   [x] 完成格式化、静态检查和自动测试。
-   [ ] 真机验证 100Hz 控制 / 状态、逐相机 profile、SDK 并发安全、CAN 错误和 MCAP 回读；完成前 MR 保持 Draft。
