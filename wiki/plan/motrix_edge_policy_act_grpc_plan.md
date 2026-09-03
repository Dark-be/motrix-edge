# ACT 走 Lerobot gRPC AsyncInference + transport 通用化 实施计划

## 摘要

-   `policy/act` 从「WebSocket + MsgPack（同 openpi）」改为**完全走 lerobot 官方
    gRPC AsyncInference**（`Ready` / `SendPolicyInstructions` / `SendObservations`
    流式 / `GetActions`），采用 lerobot 原生「推 obs + 后台拉取 `TimedAction` 按
    timestep 对齐」的流式语义。
-   `policy/openpi` **保持 WebSocket + MsgPack 不变**。
-   `motrix_edge/transport` 重构成**通用传输层**（ws / grpc 可插拔），与 lerobot
    解耦；lerobot 按需文件以**内置依赖**形式 vendored 进仓库（`src/lerobot`，
    仅保留客户端互通所需最小模块，Apache-2.0 头保留）。
-   服务端目标 = lerobot 官方 `async_inference/policy_server.py`（真实互通），
    edge **不引入 `pip lerobot`**，通过 vendored 包 + CPU torch 解析 wire。

## 设计要点（详见 wiki/design/motrix_edge_policy.md 更新）

-   wire 序列化 = pickle（与官方一致）；pickle 类身份依赖 `lerobot.*` 模块路径
    → vendored `src/lerobot` 提供同名可导入模块。
-   动作载荷为 torch.Tensor 的 pickle → edge 需 **CPU torch**（最小依赖；不入
    pyproject 硬依赖，act 部署按 cpu 源单独装）。

## TODO

-   [x] 依赖探测：内网源仅 CUDA 构建 torch（无 GPU 机 import 失败）；CPU 版来自
        `https://download.pytorch.org/whl/cpu`（已在本机 venv 装 `torch==2.9.1+cpu` 验证）
-   [ ] `pyproject.toml`：增 `grpcio`（runtime）与 dev `grpcio-tools`/`protobuf`；
        **torch 不入 pyproject**（默认源是 CUDA 构建），act 部署按上方案手动装 CPU 版；
        `uv lock` + 容器 `uv sync`（顺带补齐 ws/msgpack）
-   [x] vendored lerobot：从 `/home/pc16/project/lerobot/src/lerobot` 复制最小模块
        到 `src/lerobot`（`transport/`、`async_inference/helpers.py` 裁剪为仅
        wire 数据类），保留 Apache-2.0 头；已验证 `import lerobot` / pb2 / torch
        张量动作 pickle 往返
-   [x] pb2：随 vendored `src/lerobot/transport` 原样可用（生成物以 `lerobot.transport`
        为包根，vendored 后路径即满足），无需单独重生成
-   [ ] `motrix_edge/transport` 通用化：定义 `BaseTransport`（connect/request/
        close/server_metadata）+ `WsTransport`（迁移现 `policy/transport.py`
        msgpack-over-websocket）+ `GrpcTransport`（AsyncInference 封装）；proto/
        chunk 工具归位；`policy/transport.py` 删除或转薄适配
-   [ ] `policy/act` 重写为 lerobot gRPC 流式客户端：
        `connect()`：Ready 握手 + `SendPolicyInstructions`（pickle
        `RemotePolicyConfig`：policy_type=act / pretrained_name_or_path /
        lerobot_features / actions_per_chunk / device）；后台 action 接收线程
        `GetActions` 拉块 → 按 timestep 排队；`infer(obs)`：组装 raw obs +
        `TimedObservation(timestamp/timestep/must_go)` 分块 `SendObservations`，
        阻塞取本步 action；`reset/disconnect` 收敛线程
-   [ ] 观测适配：edge 观测（qpos + jpeg 相机）→ lerobot raw obs（state 分量名 +
        uint8 图像数组，图像尺寸对齐策略 `image_features`）；feature 字典按
        启用臂/相机生成
-   [ ] `session/infer_session.py` 适配 act 流式（timestep 推进 / drain / exit /
        estop 下收敛 receiver 线程）；openpi 路径不改
-   [ ] 配置：`policy` 段增 lerobot act 专用键（`pretrained_name_or_path` /
        `actions_per_chunk` / `fps` / `task` / `client_device` 等），edge.yml 兜底
-   [ ] 测试：单测（vendored 包可导入、features/obs 组包、TimedAction 反序列化）；
        联调对本地 `/home/pc16/project/lerobot` 的 `policy_server`（fake ACT）
-   [ ] wiki：更新 `wiki/design/motrix_edge_policy.md` 到最终形态并登记索引；
        落地后删除本 plan
