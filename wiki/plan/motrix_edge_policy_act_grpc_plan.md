# ACT 走 Lerobot gRPC AsyncInference + transport 通用化实施计划

> **状态**：**主体已落地（在 master）**——`transport/grpc.py`、`policy/lerobot_act/client.py`、
> `tests/test_lerobot_act_client.py`、vendored `src/lerobot` 均已存在；仅剩 TODO 里未勾的几项
> （配置兜底 / 与官方 `policy_server` 联调 / wiki 收尾）。
>
> ⚠️ 本计划写下时的「块缓存 / 三元切分 / 时序平滑」**后来统一收归 `motrix_edge.rtc`（RTCManager）**
> ——策略侧现在只负责「发观测、取块」。下文讲 `smooth_overlap` / 「策略自有缓存」的语句是**当时的形态**，
> 现状见 [实时动作块（rtc）](../design/motrix_edge_rtc.md)。

## 摘要

-   `policy/lerobot_act` 从「WebSocket + MsgPack（同 openpi）」改为**完全走 lerobot 官方
    gRPC AsyncInference**（`Ready` / `SendPolicyInstructions` / `SendObservations`
    流式 / `GetActions`），采用 lerobot 原生流式语义：**edge 同步按需**
    （块耗尽才推理，`GetActions` 阻塞取块、无后台线程）——`BasePolicyClient` 接口
    不变，`infer_session` 无需改动（当时块缓存与平滑都在策略侧，**后统一收归 rtc**，见「设计要点」）。
-   `policy/openpi` **保持 WebSocket + MsgPack 不变**（当时块缓存下沉策略自有，后同样收归 rtc）。
-   `motrix_edge/transport` 重构成**通用传输层**（ws / grpc 可插拔），与 lerobot
    解耦；lerobot 按需文件以**内置依赖**形式 vendored 进仓库（`src/lerobot`，
    仅保留客户端互通所需最小模块，Apache-2.0 头保留）。
-   服务端目标 = lerobot 官方 `async_inference/policy_server.py`（真实互通），
    edge **不引入 `pip lerobot`**，通过 vendored 包 + CPU torch 解析 wire。

## 设计要点（见 wiki/design/motrix_edge_policy.md 与 motrix_edge_rtc.md）

-   wire 序列化 = pickle（与官方一致）；pickle 类身份依赖 `lerobot.*` 模块路径
    → vendored `src/lerobot` 提供同名可导入模块。
-   动作载荷为 torch.Tensor 的 pickle → edge 需 **CPU torch**（最小依赖；不入
    pyproject 硬依赖，act 部署按 cpu 源单独装）。
-   动作块缓存 / 三元切分 / 时序平滑 / 预取时机**统一由 `motrix_edge.rtc`（RTCManager）负责**，
    策略只负责「把观测发出去、把块取回来」——见 [实时动作块（rtc）](../design/motrix_edge_rtc.md)
    的「动作块三元切分」「重叠过渡（过渡策略）」「异步预取时序」三节。
    （本计划落地时缓存曾在策略侧自持、`policy/broker.py` 被删；后来又一次收归 rtc。）
-   平滑 / 过渡的配置键在 **rtc 段**：`aggregate_fn` / `prefix_len` / `suffix_len`——
    早期草案里的 `smooth_overlap` **当前代码中不存在**。

## TODO

-   [x] 依赖探测：内网源仅 CUDA 构建 torch（无 GPU 机 import 失败）；CPU 版来自
        `https://download.pytorch.org/whl/cpu`（已在本机 venv 装 `torch==2.9.1+cpu` 验证）
-   [x] `pyproject.toml`：增 `grpcio`（runtime）与 dev `grpcio-tools`/`protobuf`；
        **torch 不入 pyproject**（默认源是 CUDA 构建），act 部署按上方案手动装 CPU 版
-   [x] vendored lerobot：从 lerobot 官方仓库复制最小模块到 `src/lerobot`
        （`transport/`、`async_inference/helpers.py` 裁剪为仅
        wire 数据类），保留 Apache-2.0 头；已验证 `import lerobot` / pb2 / torch
        张量动作 pickle 往返
-   [x] pb2：随 vendored `src/lerobot/transport` 原样可用（生成物以 `lerobot.transport`
        为包根，vendored 后路径即满足），无需单独重生成
-   [x] `motrix_edge/transport` 通用化：`BaseTransport` + `WsTransport`（msgpack-over-ws）+
        `AsyncInferenceGrpcTransport`（channel/stub 封装，grpc/pb2 延迟导入）；
        `policy/transport.py` / `policy/msgpack_numpy.py` 别名已删
-   [x] `policy/lerobot_act` 重写为 lerobot gRPC 流式客户端（**同步按需，无后台线程**）：
        `connect()`：Ready 握手；首次取块才 `SendPolicyInstructions`（pickle
        `RemotePolicyConfig`）；`infer_chunk(observation, index)`：每次调用真实请求一次推理
        （`index` = RTCManager 的绝对步号 → `TimedObservation.timestep` + `must_go=True`；raw obs
        分块 `SendObservations` → `GetActions` 取块）；`reset()` 复位、timestep 不回退
-   [x] 观测适配：edge 观测（qpos + jpeg/ndarray 相机）→ lerobot raw obs（state 分量
        标量 + **edge 侧 letterbox 到 image_size（默认 224×224，上下留黑边）** 的 uint8
        RGB 图）；`_lerobot_features` 按 state 维度 + 相机（edge 配置 `rename_cameras` → lerobot `rename_map`）生成
-   [x] 通用 broker 移除：删 `policy/broker.py`（当时缓存由策略自持，后统一收归 `motrix_edge.rtc`）
-   [x] `session/infer_session.py`（act 流式适配）：`BasePolicyClient` 接口不变、同步流式
        无需收线程，此阶段**无改动**；连接语义调整见下方「连接生命周期内聚 policy」
-   [x] 测试：`tests/test_lerobot_act_client.py`（fake AsyncInference servicer，覆盖握手/
        流式/落块/reset/letterbox）+ `tests/test_policy.py`（openpi）+ 全量回归
-   [ ] 配置：`policy` 段 lerobot-act 专用键（`pretrained_name_or_path` / `actions_per_chunk` /
        `task` / `rename_cameras` / `image_size` / `device`）登记 edge.yml 兜底；平滑 / 过渡参数属
        **rtc 段**（`prefix_len` / `suffix_len` / `aggregate_fn`）
-   [x] **连接生命周期内聚 policy**（见 design「BasePolicyClient」/ session）：
    -   [x] `BasePolicyClient`：`connected` property + `ensure_connected()`（惰性）+ `prepare(obs)`（默认 no-op）；
            openpi/act 实现 `connected`；act 实现 `prepare`（首次发策略指令预热）
    -   [x] `transport`：`WsTransport` 暴露 `connected`（grpc 已有）
    -   [x] `infer_session`：删 `_connected`/503，rollout 前 `ensure_connected()`；`infer connect` 改为可选预连+预热
            （`adapter.observe()` → `policy.prepare(obs)`）
    -   [x] 测试更新（test_infer_session / test_server：惰性自连 + prepare）
-   [x] **act 时序平滑**（当时在策略侧实现，**后收归 rtc**，见
        [rtc 设计](../design/motrix_edge_rtc.md)「重叠过渡（过渡策略）」）：
    -   [x] 客户端：缓存剩余步到阈值时同步重叠预取；对重叠步做 `aggregate_fn` 加权
            （对齐 lerobot `AGGREGATE_FUNCTIONS`）
    -   [x] 配置：当时的键 `smooth_overlap`（默认 10 开启，0 = 关闭）——**该键当前已不存在**，
            平滑改由 rtc 段的 `prefix_len` / `suffix_len` / `aggregate_fn` 控制
    -   [x] 测试：fake servicer 返回可重叠块，断言重叠步 = 加权结果、drain 不触发推理
-   [ ] 联调：对官方 `async_inference/policy_server.py` 真实互通（可选）
-   [ ] wiki：策略侧说明见 `motrix_edge_policy.md`（平滑 / 过渡已迁至 `motrix_edge_rtc.md`）；
        本 plan 落地后删除并更新 `plan/index.md`
