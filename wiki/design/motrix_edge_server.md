# HTTP 控制面（server）

## 摘要

`server/` 用 FastAPI 暴露 MotrixEdge HTTP 控制面（`/v1/*`）。**web 是 node 进程内的独立线程**：
`CaptureService` / `InferService` / `CommandService` 只绑定「正在运行的 node 实例 + 共享
`CommandBus`」，经 `CommandBus.submit` 提交命令并**同步等待回执**驱动 EdgeNode，**不持有 / 不创建 /
不运行 node**（观测语义唯一实现在 session，web 只做命令提交与状态读取）。

## 目标与原则

-   **单点定义**：任务语义唯一实现在 session / node；server 只做「HTTP 动作 → 命令」翻译 + 状态读取。
-   **命令化驱动**：HTTP 动作经 `submit` 同步等回执（`session run` / `session quit` / `infer rollout`），
    无需轮询节点状态伪造同步。
-   **租约校验**：受控操作（enter / exit / preview / commands / webrtc）须持有 Edge 级活跃租约
    （`X-Lease-Id`，经 `LeaseManager` 校验）；只读操作（status / precheck / health / leases）免租约。
-   **状态校验在 HTTP 层**：提交命令前校验当前状态，非法转移返回 `409`，不污染命令队列。
-   **服务未注入 → 501**：`create_app` 未注入对应 Service 时端点返回 501。

## 端点总览

| 方法                  | 路径                                                            | 说明                                           | 服务           |
| --------------------- | --------------------------------------------------------------- | ---------------------------------------------- | -------------- |
| GET                   | `/v1/health`                                                    | 版本 / identity / 已绑定 adapter / 磁盘 / 时钟 | —（内建）      |
| GET                   | `/v1/adapters`                                                  | 静态列出全部注册适配器（不 discover / 不探活） | —（内建）      |
| POST                  | `/v1/commands`                                                  | 受控命令（capability 映射，须租约）            | CommandService |
| POST/GET              | `/v1/leases`、`/v1/leases/{id}:renew·revoke`、`/v1/leases/{id}` | Edge 级租约（Console 签发镜像）                | LeaseManager   |
| GET/POST/PATCH/DELETE | `/v1/captures` + `…/precheck` + `…/meta`、`…/sync`              | 采集会话控制 + 采集元信息选项                  | CaptureService |
| GET/POST              | `/v1/uploads` + `/v1/uploads/select·pack·upload·retry`          | 本地 episode 扫描 / 选择 / 打包与上传队列      | UploadSession  |
| GET/POST/DELETE       | `/v1/infers` + `/v1/infers/rollout`                             | 推理会话控制                                   | InferService   |
| GET                   | `/v1/preview`                                                   | 最新观测预览（须租约）                         | CaptureService |
| POST                  | `/v1/webrtc/offer`                                              | WebRTC 推流信令（须租约）                      | WebRTCService  |

correlation 中间件：`X-Correlation-Id` 贯穿请求与响应（缺省自动生成）。

## /v1/commands（受控命令）

`POST /v1/commands` 请求体：`command_id` / `lease_id` / `capability` / `params` /
`idempotency_key`（**预留、未实现**：幂等去重尚未落地，字段仅回显；调用方需自行处理
重试，勿依赖去重）。`CommandService.execute` 先校验租约，再按 capability 映射为总线命令：

| capability                  | 总线命令                            | 说明                                |
| --------------------------- | ----------------------------------- | ----------------------------------- |
| `estop`                     | `robot estop`（push）               | 全局急停：安全停止 + 节点转 ERROR   |
| `reset`                     | `node reset`（push）                | 节点复位（ERROR → IDLE）            |
| `robot_reset`               | `robot reset`（push）               | 机器人复位（adapter.reset）         |
| `robot_execute`             | `robot execute`（submit）           | 直接下发 raw 动作（qpos），回执透传 |
| `robot_teleop`              | `robot teleop`（push）              | 遥操作开关（enabled=true/false）    |
| `capture_episode_start/end` | `capture episode start/end`（push） | 开始 / 结束一轮采集                 |
| 其他                        | —（骨架）                           | 预留 Capability 校验 / 具体下发     |

## /v1/captures（采集会话控制）

采集为**观测会话**，端点经 `CaptureService` 桥接：

| 方法   | 路径                      | 租约          | 说明                                                                                     |
| ------ | ------------------------- | ------------- | ---------------------------------------------------------------------------------------- |
| POST   | `/v1/captures`            | 必需          | `enter`：`session run capture`（READY → ACTIVE，选择 + 启动一步）                        |
| GET    | `/v1/captures`            | 无            | 状态快照：node_state / session / adapter / capture_running / data_dir / disk / lease_id  |
| GET    | `/v1/captures/precheck`   | 无            | 只读预检：节点 / 会话 / 机器人就绪 + 磁盘 + lease_id / leasable                          |
| GET    | `/v1/captures/meta`       | 无            | 采集元信息选项（`config/capture.yml` 的 `meta` 段，前端选择列表）                        |
| POST   | `/v1/captures/meta`       | 必需          | 选项管理：新增 `{key, value}`（分类不存在则创建）；重复 400                              |
| PATCH  | `/v1/captures/meta`       | 必需          | 选项管理：重命名选项 `{key, old, new}`；不存在 / 重复 400                                |
| DELETE | `/v1/captures/meta`       | 必需          | 选项管理：删除选项（`?key=&value=`，分类清空则一并删除该分类）                           |
| DELETE | `/v1/captures/meta/{key}` | 必需          | 选项管理：删除整个分类                                                                   |
| POST   | `/v1/captures/sync`       | 必需          | `sync`：把选中元信息（`{operator, task_name, …}`）同步到机器人进程（进程保存数据时附加） |
| DELETE | `/v1/captures?lease_id=`  | 必需（query） | `exit`：`session quit`（ACTIVE → READY；**租约不随退出销毁**）                           |
| GET    | `/v1/preview`             | 必需          | 最新观测预览（见 [FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)）          |

`POST /v1/captures` 响应：`{status: "accepted", state, lease_id, adapter}`（无请求体，单 adapter 包）。

> **采集数据归属（边界）**：`capture_running` / `data_dir` 来自 `adapter.capture_status()`
> （适配器 / SDK 进程自维护的**状态上报**：是否正在采集 + 数据目录）——Edge 不驱动落盘、
> 不校验；数据的本地组织（扫描 / 选择 / 打包）见 `/v1/uploads` 小节。实际数据落盘 / 校验 / 上传
> **待完成**：后续按 hardware adapter 契约完成
> **CaptureBundle**（manifest / checksum → Local Spool → Uploader，服务端确认后才删），
> 属 M11/M12（未在仓库内保留实施计划，落地时另行立项）。

## /v1/uploads（本地 episode 扫描与打包）

本地采集目录的 episode 扫描 / 查看 / 选择 / **打包**，由 `UploadSession` 实现（**不占**
RobotAdapter、不进节点任务状态机）；设计与字段见 [上传会话（UploadSession）](./motrix_edge_upload_session.md)。

| 方法 | 路径                 | 租约 | 说明                                                                                                                                                                               |
| ---- | -------------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| POST | `/v1/uploads`        | 必需 | 创建 / 重扫；body 可选 `folder_path`（须在白名单内），缺省回退 adapter 数据目录 → `upload.data_dir`                                                                                |
| GET  | `/v1/uploads`        | 必需 | 扫描汇总：episode 列表 / 状态 / 选择集 / 建议包名                                                                                                                                  |
| POST | `/v1/uploads/select` | 必需 | 按 `episode_ids` 替换选择集（只允许可选的 episode）                                                                                                                                |
| POST | `/v1/uploads/pack`   | 必需 | 把选中 episode **移动**到 `<扫描目录>/<包名>/`；重名 409、非法名 400、无扫描 / 无选择 409、源文件缺失 404、并发 409；收尾重扫失败 → `scan=null` + `warnings`（打包已成功，仍 200） |
| POST | `/v1/uploads/upload` | 必需 | 加入上传队列；**未配置 `upload.endpoint` → 501**                                                                                                                                   |
| POST | `/v1/uploads/retry`  | 必需 | 选择集中的失败项重置为 pending（未配置上传目标时不做网络传输）                                                                                                                     |

**受控操作**：uploads 端点全部要求 `X-Lease-Id`（读端点也要——扫描会读目录内容、打包会移动文件，
与 `/v1/preview` 同类）；缺失租约 `409` / 租约不匹配 `403`。

**目录白名单**：`folder_path` 只允许在**数据目录**（adapter 上报的采集目录 / `upload.data_dir`）
及其子目录内，越界 `400`；两个来源都没有 → `409`（不默认放开任意路径）。

**重操作互斥**：`scan` / `pack` 同时只允许一个在跑，并发 `409`（都要对整目录算 SHA-256 / 搬运文件，
避免拖住控制面）。

**并发模型**：`app.py` 里**所有** HTTP handler 都是同步 `def`（FastAPI 交给线程池），唯一的例外是
correlation 中间件（必须 `async def`）。原因：handler 内部全是**阻塞调用**——`CommandBus.submit`
同步等回执（最长 5s）、`scan` 算 SHA-256 / `pack` 搬文件、adapter 的同步 HTTP 查询；写在 `async def`
里会占住 uvicorn 事件循环，连带冻结 `/v1/health`、`/v1/preview` 与 WebRTC 信令。新增端点请沿用
同步 `def`。

> **当前范围**：上传 API 的**消费者是数据平台**，程序化上传在后续版本加入；本版本只做本地文件
> 的查看 / 筛选 / 打包，包目录由数采人员**手动上传**到数据平台（打包是**移动**：源文件进入包目录，
> 原位置不再保留）。

## /v1/infers（推理会话控制）

推理会话**无回合概念**（enter → 持续推理 → exit，`infer rollout` 步进），端点经 `InferService` 桥接：

| 方法   | 路径                   | 租约          | 说明                                                                |
| ------ | ---------------------- | ------------- | ------------------------------------------------------------------- |
| POST   | `/v1/infers`           | 必需          | `enter`：`session run infer`（可选 body `policy_type`，缺省用配置） |
| GET    | `/v1/infers`           | 无            | 状态快照：node_state / session / adapter / policy / lease_id        |
| POST   | `/v1/infers/rollout`   | 必需          | `infer rollout`：上传观测 → 推理 → 下发动作，返回 action 回执       |
| DELETE | `/v1/infers?lease_id=` | 必需（query） | `exit`：`session quit`（ACTIVE → READY）                            |

## 错误语义

| 状态码 | 含义                                                                       |
| ------ | -------------------------------------------------------------------------- |
| `409`  | 非法状态转移（已在会话再 enter / 未在会话 exit / 节点未就绪 / 无活跃租约） |
| `403`  | `X-Lease-Id` 缺失或不匹配（异租约）                                        |
| `410`  | 租约已过期                                                                 |
| `501`  | 服务未注入（create_app 未启用对应模块）                                    |
| `500`  | 内部异常                                                                   |

## 相关文档

-   命令模型与传输：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   会话语义：[会话（session）](./motrix_edge_session.md)
-   租约：[Edge 级租约（lease）](./motrix_edge_lease.md)
-   WebRTC 推流：[FrameManager 与 WebRTC 推流](./motrix_edge_frame_webrtc.md)
-   身份配置：设备身份（identity，`identity` 配置段见 [配置与命令行](./motrix_edge_config.md#配置加载)；
    经 `/v1/health` 上报）
-   代码入口：`src/motrix_edge/server/` —— 随 **feat/3**（HTTP 控制面）落地
