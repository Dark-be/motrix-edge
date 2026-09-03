# ACT 走 Lerobot gRPC AsyncInference + transport 通用化 实施计划

## 摘要

-   `policy/act` 从「WebSocket + MsgPack（同 openpi）」改为**完全走 lerobot 官方
    gRPC AsyncInference**（`Ready` / `SendPolicyInstructions` / `SendObservations`
    流式 / `GetActions`），采用 lerobot 原生流式语义：**edge 同步按需**
    （块耗尽才推理，`GetActions` 阻塞取块、无后台线程）——`BasePolicyClient` 接口
    不变，`infer_session` 无需改动；动作块缓存为 act 自有（timestep→动作），并支持
    **时序平滑（同步重叠预取 + 加权聚合）**。
-   `policy/openpi` **保持 WebSocket + MsgPack 不变**（块缓存下沉为 openpi 自有）。
-   `motrix_edge/transport` 重构成**通用传输层**（ws / grpc 可插拔），与 lerobot
    解耦；lerobot 按需文件以**内置依赖**形式 vendored 进仓库（`src/lerobot`，
    仅保留客户端互通所需最小模块，Apache-2.0 头保留）。
-   服务端目标 = lerobot 官方 `async_inference/policy_server.py`（真实互通），
    edge **不引入 `pip lerobot`**，通过 vendored 包 + CPU torch 解析 wire。

## 设计要点（见 wiki/design/motrix_edge_policy.md）

-   wire 序列化 = pickle（与官方一致）；pickle 类身份依赖 `lerobot.*` 模块路径
    → vendored `src/lerobot` 提供同名可导入模块。
-   动作载荷为 torch.Tensor 的 pickle → edge 需 **CPU torch**（最小依赖；不入
    pyproject 硬依赖，act 部署按 cpu 源单独装）。
-   动作块缓存**策略自有**（已删通用 `ActionChunkBroker`）：openpi 块切片 / act
    timestep 键控。
-   时序平滑设计见 [推理策略客户端（policy）](../design/motrix_edge_policy.md)
    「ACT 时序平滑」节。

## TODO

-   [x] 依赖探测：内网源仅 CUDA 构建 torch（无 GPU 机 import 失败）；CPU 版来自
        `https://download.pytorch.org/whl/cpu`（已在本机 venv 装 `torch==2.9.1+cpu` 验证）
-   [x] `pyproject.toml`：增 `grpcio`（runtime）与 dev `grpcio-tools`/`protobuf`；
        **torch 不入 pyproject**（默认源是 CUDA 构建），act 部署按上方案手动装 CPU 版
-   [x] vendored lerobot：从 `/home/pc16/project/lerobot/src/lerobot` 复制最小模块
        到 `src/lerobot`（`transport/`、`async_inference/helpers.py` 裁剪为仅
        wire 数据类），保留 Apache-2.0 头；已验证 `import lerobot` / pb2 / torch
        张量动作 pickle 往返
-   [x] pb2：随 vendored `src/lerobot/transport` 原样可用（生成物以 `lerobot.transport`
        为包根，vendored 后路径即满足），无需单独重生成
-   [x] `motrix_edge/transport` 通用化：`BaseTransport` + `WsTransport`（msgpack-over-ws）+
        `AsyncInferenceGrpcTransport`（channel/stub 封装，grpc/pb2 延迟导入）；
        `policy/transport.py` / `policy/msgpack_numpy.py` 别名已删
-   [x] `policy/act` 重写为 lerobot gRPC 流式客户端（**同步按需，无后台线程**）：
        `connect()`：Ready 握手；首次 `infer` 才 `SendPolicyInstructions`（pickle
        `RemotePolicyConfig`）；`infer(obs)`：缓存耗尽 → raw obs + `TimedObservation`
        （timestep/must_go=True）分块 `SendObservations` → `GetActions` 轮询取块落入
        `_actions: {timestep→动作}`；`drain` 只消费缓存；`reset` 清缓存、timestep 不回退
-   [x] 观测适配：edge 观测（qpos + jpeg/ndarray 相机）→ lerobot raw obs（state 分量
        标量 + **edge 侧 letterbox 到 image_size（默认 224×224，上下留黑边）** 的 uint8
        RGB 图）；`lerobot_features` 按 state 维度 + 相机（rename_map）生成
-   [x] 通用 broker 移除：删 `policy/broker.py`，openpi/act 动作缓存策略自持
-   [x] `session/infer_session.py`：接口（connect/infer/drain/reset/disconnect）不变，
        同步流式无需收线程，**无改动**
-   [x] 测试：`tests/test_act_grpc_client.py`（fake AsyncInference servicer，覆盖握手/
        流式/落块/reset/letterbox）+ `tests/test_policy.py`（openpi 自有缓存）+ 全量回归
-   [ ] 配置：`policy` 段 act 专用键（`pretrained_name_or_path` / `actions_per_chunk` /
        `fps` / `task` / `rename_cameras` / `image_size` / 平滑键）登记 edge.yml 兜底
-   [ ] **act 时序平滑**（见 design「ACT 时序平滑」）：
        - [ ] 客户端：缓存剩余步 ≤ `smooth_overlap` 时同步重叠预取；`_store_action_chunk`
              对重叠步做 `aggregate_fn` 加权（对齐 lerobot `AGGREGATE_FUNCTIONS`）
        - [ ] 配置：`smooth_overlap`（0=关）/ `aggregate_fn`；默认关或小窗口
        - [ ] 测试：fake servicer 返回可重叠块，断言重叠步 = 加权结果、drain 不触发推理
-   [ ] 联调：对本地 `/home/pc16/project/lerobot` 的 `policy_server` 真实互通（可选）
-   [ ] wiki：`wiki/design/motrix_edge_policy.md` 已更新到最终形态（含时序平滑）；本 plan
        落地后删除并更新 `plan/index.md`

