# Kleinkram InferenceService 对接对齐点（P1）

## 摘要

与 `motrix-loop-docs`（`07-motrix-edge.md` 的 Rollout Runtime）做过跨项目文档对照，结论：
**职责分层、失败语义、「动作只经 Adapter 下发」三条一致，无冲突**。本备忘记下接入 Kleinkram
InferenceService（Console 评测回合 + RolloutEvidenceManifest）时的四个 P1 对齐点，供 P1 契约
冻结时逐条确认。

## 对照结论（与 motrix-loop-docs）

loop-docs「Rollout Runtime」的各环节在本仓的落点：

| loop-docs 环节                   | 本仓实现                                                                              |
| -------------------------------- | ------------------------------------------------------------------------------------- |
| 观测预处理（尺寸 / 编码 / 相机） | `policy/contract.py` + 各策略客户端（openpi letterbox、lerobot 服务端拉伸语义）       |
| 推理请求（会话生命周期 / 预热）  | `policy/*/client.py` + `transport/*` + `InferSession` 的预热与预热门控                |
| 动作后处理（缓存 / 切分 / 平滑） | `rtc/`（策略侧**不做**任何块缓存）                                                    |
| 节拍（控制频率）                 | 会话 `infer_freq` + RTC 的绝对步号推进                                                |
| 超时                             | 各传输 / 策略超时（`connect_timeout` / `request_timeout` / `policy_setup_timeout`）等 |

-   **失败语义一致**：推理异常 → 不下发动作（`RTCManager` 记录失败并跳过该步，不升级为任务错误）；
    任务异常 / 急停 → `safe_stop()` 后进入 ERROR。
-   **动作只经 Adapter 下发**：策略取回的只是原始动作块，唯一通向真机的路径是
    `adapter.rollout` / `adapter.execute`（在 `infer rollout` 内，且有回执有效期门，见
    [policy 设计](../design/motrix_edge_policy.md) 的「端点与预热」）。

## P1 接入对齐点

1.  **「rollout」双义会在接入后放大**：本仓 `infer rollout` 指「一步推理 + 下发动作」；rollout
    **录制**走 `capture episode start/end`（机器人进程按帧录 mcap，是**本地产物**）。而 loop-docs
    的 Rollout 是 **Console 评测回合 + RolloutEvidenceManifest**（跨产品产物）。前端与文档需要明确
    区分两条产物通道（本地 episode ↔ 证据清单），避免同一个词指两件事。
2.  **gRPC 传输缺凭证钩子**：`transport/grpc.py` 用 `grpc.insecure_channel(...)`，没有 TLS /
    凭证入口（`WsTransport` 已有 `api_key` → `Authorization: Api-Key`）。对接 Kleinkram Endpoint
    （AccessGrant + TLS）需要扩展传输层的凭证与安全通道（倾向统一凭据来源，而不是各传输各写一套）。
3.  **每拍标识**：loop-docs 时序图里每拍带 `rollout_id` + `sequence`；本仓与之对应的是 RTC 的
    **绝对步号**（`ActionChunk.start_index` / `RTCManager` 内部步号）。接入时需要确定 `sequence`
    到绝对步号的映射口径（或把它作为新策略类型契约的一部分）。
4.  **任务标识口径**：推理录制 episode 目前 `task_name = prompt`（自由文本）；跨产品的
    `TaskDefinitionVersion`（Console 冻结）尚未对接 —— 留待 P1 契约冻结时定：继续用 prompt，
    还是改为携带冻结的 TaskDefinitionVersion。

## 相关文档

-   推理策略客户端：[policy](../design/motrix_edge_policy.md)
-   推理会话：[session](../design/motrix_edge_session.md)
-   动作块消费：[RTC](../design/motrix_edge_rtc.md)
-   边缘观测来源：[adapter](../design/motrix_edge_adapter.md)
