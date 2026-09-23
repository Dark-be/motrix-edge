# 推理策略客户端（policy）

## 摘要

`policy/` 是边缘节点到推理节点的**策略客户端层**：`InferSession` 采集观测后交给它，
它负责「把观测按该策略的契约发出去、把动作块取回来」。策略**只负责取原始推理结果**
（`infer_chunk`）；动作块缓存 / 三元切分 / 时序平滑 / 异步预取统一由 `motrix_edge.rtc`
负责（见 [RTC 动作块管理](./motrix_edge_rtc.md)）。

**各策略相对独立**：每个策略自己的 **wire 契约**（消息键、观测组装）、**图像约定**
（尺寸与几何、相机名）、**块长协商**、**超时**都由该策略目录内的代码与文档定义，互不牵连；
共用层只提供边缘观测键与图像原语（见下）。

## 目标与原则

-   生命周期由 `InferSession` 驱动：连接与预热走 `infer connect`（连接 + prepare + 取一块丢弃，
    **异步可中断**，见「端点与预热」）；`warmup_required=false` 的会话保留首个 `infer rollout` 前经
    `ensure_connected()` **惰性自连**（单次尝试限时，重试由会话驱动）；`session_finish` 先取消在飞预热
    再调 `disconnect`。
-   注册式懒加载：`POLICY_REGISTRY` 登记策略类型，`get_policy()` 选中该类型时才 `import`
    对应模块（连带加载其第三方依赖，避免导入 `motrix_edge` 时缺依赖报错）。
-   新增策略 = 实现 `infer_chunk`（+ 可选的 `prepare` / `bind_adapter` / `reset` 等）
    并在注册表登记；**不得**在客户端里做块缓存 / 切分（那是 rtc 的职责）。

## 包结构

```
policy/
├── __init__.py     # POLICY_REGISTRY / POLICY_CONFIG_ITEMS / get_policy / policy_adapters
├── base.py         # BasePolicyClient：策略最小接口 + 共用状态（prompt / observed_chunk_len）
├── contract.py     # 共用层：边缘观测键 + 图像原语（解码/归一、letterbox）
├── openpi/
│   ├── client.py   # OpenPIClient：websocket + openpi 官方 flat 契约
│   └── contract.py # openpi 官方 wire 助手（键、观测组装、图像预处理、响应解析）
├── lerobot_act/
│   └── client.py   # LerobotActClient：lerobot AsyncInference gRPC（流式动作块）
└── （LLM 不走策略路径：它是外部 agent，edge 只提供原语接口，见 motrix_edge_primitives.md）

transport/          # 传输层（与策略解耦，见 motrix_edge/transport/）
├── ws.py           # WsTransport：msgpack-over-websocket（openpi）
├── grpc.py         # AsyncInferenceGrpcTransport：lerobot AsyncInference gRPC
├── base.py         # BaseTransport：connect / close / connected / endpoint
└── msgpack_numpy.py
```

## BasePolicyClient

| 成员                                     | 语义                                                                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `connected`                              | 是否已连上推理节点（委托传输层）                                                                                    |
| `connect()` / `ensure_connected()`       | 显式连接 / 惰性连接                                                                                                 |
| `prepare(observation)`                   | 可选预热（下发策略指令 / 触发服务端加载模型）；无实现 → no-op                                                       |
| `bind_adapter(action_dim, camera_names)` | 会话进入时绑定 adapter 运行时布局（启用相机等），策略据此过滤下发内容                                               |
| `infer_chunk(observation, index=None)`   | **策略唯一职责**：真实请求一次推理，返回 `ActionChunk`（含首步绝对步号）                                            |
| `observed_chunk_len`                     | **块长**（最近一次服务端返回的步数；lerobot-act 连接前先用请求值 `actions_per_chunk` 兜底），rtc 据此校准块长上限 H |
| `reset()` / `disconnect()`               | 复位策略状态 / 断开连接（幂等）                                                                                     |
| `requires_prompt` / `prompt`             | 是否语言条件策略 + 当前文本指令（仅语言条件策略使用）                                                               |

`index` = rtc 传入的**绝对步号**：需要按步号组织请求的策略（lerobot-act 的
`TimedObservation.timestep`）使用；openpi 忽略它，只用于回填 `start_index`。

## 传输层（motrix_edge.transport）

与策略解耦的通用传输实现，均支持 `connected` / `endpoint`（连接目标由 host / port 在 connect 时现算，端点**随会话固化**：改端点=退出会话改配置再进）：

-   `WsTransport`（openpi 用）：连接后**先收服务端首条 metadata**；`request()` 发送并阻塞等
    响应，**单次限时**（`request_timeout`，默认 60s）；超时 / 连接异常 / 服务端以**文本帧**
    回错误（官方服务端发完 traceback 即断连）→ **关闭连接**并抛出原异常，`connected` 置
    False，由会话重连（避免复用连接造成响应错位 / 误报在线）。
-   `AsyncInferenceGrpcTransport`（lerobot-act 用）：建立 channel 并等 READY（`connect_timeout`
    ，默认 5s），暴露 gRPC `stub`；具体 RPC 语义由策略客户端组合。

## 共用契约层（contract.py）

只提供两样东西，**不含任何策略专属字段**：

-   边缘观测键：`KEY_OBS_QPOS = "observations/qpos"`、`KEY_OBS_IMAGE_PREFIX =
"observations/images/"`（= adapter / robot-pipeline 的 `observe()` 输出键）；
-   图像原语：`to_rgb_uint8(image)`（jpeg bytes / ndarray → uint8 RGB，浮点按约定归一而不
    静默截断）、`resize_with_pad(image, h, w)`（等比缩放 + 居中补零，复刻 openpi 的
    `tf.image.resize_with_pad`，即 **letterbox**）。

## 各策略的契约与图像约定

### 汇总

|                 | `OpenPIClient`                                   | `LerobotActClient`                                        |
| --------------- | ------------------------------------------------ | --------------------------------------------------------- | --- | ------------ | -------- | -------- | --- | -------------------- | ------ | ---------------------------------- |
| 注册类型        | `openpi`                                         | `lerobot-act`                                             |
| 传输            | websocket（msgpack-numpy）                       | gRPC（lerobot AsyncInference）                            |
| 观测 wire       | `{"state", "images", "prompt"?}`（官方 flat）    | pickle(`TimedObservation`) → 分块 `SendObservations`      |
| 响应 wire       | `{"actions": [H, dim]}`                          | `list[TimedAction]`（`[K, dim]` + `timestep`）            |
| 图像尺寸 / 几何 | `image_size`（默认 224×224）**letterbox**        | `image_size`（默认 224×224）**letterbox**                 |
| 相机名          | 边缘名，或服务端 metadata 声明的 `cameras`       | 边缘名 → `rename_cameras` → **checkpoint 训练名**         |
| 块长            | **实测**（`observed_chunk_len`）→ rtc 校准上限 H | `actions_per_chunk`（请求上界，实测块长以服务端返回为准） |
| 文本指令        | **必需**（`requires_prompt = True`）             | 不使用（ACT 非语言条件）                                  |     | 输出动作语义 | 关节空间 | 关节空间 |     | 连接后需重发策略指令 | 不涉及 | **是**（`Ready` 会重置服务端会话） |

### 图像约定（两个策略一致）

**640×480 原始帧 → 等比缩放 + 居中黑边（letterbox）→ 224×224**，不做拉伸、不裁剪，保留完整
视野。理由与约束：

-   与**训练数据生成方式一致**（数据集由同样的 letterbox 生成），推理时才不会有 train /
    inference 几何差异——这是「发符合训练集的观测」这条隐含契约的一部分；
-   openpi 侧：官方服务端 / 训练管线本身就用 `image_tools.resize_with_pad`（同样是
    letterbox），客户端先缩到 224 使服务端那次缩放成为 no-op（省带宽且不变形）；
-   lerobot-act 侧：gRPC 协议**不声明任何尺寸**，服务端会把收到的图 `interpolate`（拉伸）
    到 checkpoint 的 `image.features` 形状——客户端发的尺寸与训练一致时该步为 no-op；
-   尺寸是**每策略独立配置项** `policy.image_size`（默认 224，可用 `infer config set` 在会话
    内改；openpi 还会被 `bind_adapter` 的相机集与 metadata 声明的 `cameras` 影响下发内容）。

### OpenPIClient（openpi）

**官方 flat 契约**（`policy/openpi/contract.py` 单点定义）：

```
client → server: {"state": <qpos ndarray>, "images": {<相机名>: uint8 RGB [h, w, c]}, "prompt": <str>?}
server → client: {"actions": [horizon, dim], ...}      # 附带 state / policy_timing / server_timing 等（忽略）
                 {"error": <str>}                       # 服务端异常（文本帧）→ 客户端抛错并断开
```

-   `connect()`：连接后接收 metadata。本仓对接的 openpi piper 分支会在 `policy_metadata` 里
    额外声明 `action_horizon` / `cameras` / `action_dim` 等；原生 openpi 只有 `reset_pose`
    之类。客户端**只采纳 `cameras`**（有声明则以它过滤下发相机，并告警 adapter 未启用的
    相机）；`action_horizon` 留在 `server_metadata` 里展示，**不参与块长决策**——服务端声明的
    「模型预测多长」与 rtc 的 H「只执行前多少步」不是一个概念。
-   **块长口径**：块长一律以**实测**为准 —— 预热 / 首次推理后回填 `observed_chunk_len`，
    rtc 据此把块长上限 H 收敛到 `min(H, 实测)`（见 `RTCManager.calibrate`）。
-   `prepare(observation)`：预热——发一帧触发服务端加载模型并**丢弃结果**（官方推荐做法），
    副产品是拿到实测块长。
-   `infer_chunk(observation, index)`：qpos 原样上传（服务端按 norm_stats 归一化）；相机经
    `bind_adapter` / metadata 过滤后 letterbox 到 `image_size` 并转 uint8；`prompt` 每次请求
    携带（服务端每帧重新 tokenize，可运行时更换）。
-   超时：`connect_timeout`（默认 5s）、`request_timeout`（默认 60s）。
-   **每帧自包含**（服务端**无会话状态**）：`websocket_policy_server` 连接后先发一次 metadata，
    之后循环「收一帧 obs → `policy.infer(obs)` → 回 `actions` + `server_timing`」。所以
    state / 每个相机的图像 / prompt 都**逐帧携带**，没有 lerobot 那种握手声明。
-   **prompt 逐帧发、逐帧重新 tokenize**：帧里带 `prompt` 时用它，缺了才由 checkpoint 的
    `default_prompt` 兜底（`transforms.InjectDefaultPrompt`），再经 `TokenizePrompt` 编成语言
    条件 —— 这正是本客户端 `prompt` 能在会话内换、下一帧即生效的原因。
-   **`image_size` 是"端侧预压缩"，模型端还会兜底**：模型输入分辨率来自 checkpoint
    （`IMAGE_RESOLUTION`，默认 224×224）；帧里的尺寸不匹配时模型端用
    `image_tools.resize_with_pad`（letterbox）补一次（`models/model.py`）。故本项
    = **上传前的压缩目标**：等于模型输入尺寸时模型那次是 no-op，不等于时只是白传带宽
    （模型会再 letterbox：不变形、不报错）。

### LerobotActClient（lerobot-act）

与 **lerobot 官方 `AsyncInference` gRPC 服务端**（`lerobot.async_inference.policy_server`）
互通，wire 语义与官方 `RobotClient` 一致：

```
connect()  →  Ready(Empty)                     # 服务端据此重置会话状态（清空 policy）
（首次 infer / prepare 时）→ SendPolicyInstructions(pickle(RemotePolicyConfig)，含模型路径与 features)
每次 infer_chunk(index) → SendObservations(pickle(TimedObservation) 分块流式，must_go=True)
                        → GetActions(Empty) 轮询，取回该观测的整块 list[TimedAction]
```

-   **协议没有 metadata**：`services.proto` 里没有 metadata RPC，官方客户端也没有这个概念。
    因此 `server_metadata` 是**本地声明**（协议 / 策略类型），供会话与前端展示；布局契约
    （图像尺寸、相机名、state 维数、动作块长）**全部存在于 checkpoint 内**，只能靠配置与实测
    对齐——这正是上面「图像约定」与「块长兜底」存在的原因。
-   **观测组装**：`lerobot_features`（随 `SendPolicyInstructions` 下发）由客户端按当前观测
    声明，格式与官方 `hw_to_dataset_features(..., use_video=False)` 同形：
    `observation.state`（`dtype=float32` + `shape` + `names=[qpos_i]`）与
    `observation.images.<相机名>`（`dtype=image` + `shape` + `names`，HWC）。服务端
    `build_dataset_frame` 按这些名字取值，并**用客户端声明的相机名去索引 checkpoint 的
    `image.features`**，故相机名必须等于训练时的名字（`rename_cameras` 负责改名；`image_cameras`
    用于只下发策略需要的相机，多给的相机在服务端会 KeyError）。
-   **时序约束**：`Ready` 会重置服务端会话（清空已加载的 policy），故策略指令必须在
    `Ready` 之后下发、且**每次重连都要重发**（客户端在 `connect()` / `disconnect()` 重置该状态，
    未握手即下发直接报错，避免静默失效）。
-   **块长**：`actions_per_chunk` 是**请求上界**（服务端返回 `chunk[:actions_per_chunk]`），真实
    块长 = `min(checkpoint chunk, actions_per_chunk)`，以服务端实际返回为准并回填
    `observed_chunk_len`。
-   **轮询与失败**：`GetActions` 空响应表示服务端尚未推理完（或没有观测），客户端**节流重试**
    （0.02s）直到 `get_actions_timeout`；拿到首个非空块即返回（**不做步号门控**，与官方
    `RobotClient` 一致），整体过期的块交由 rtc 统一丢弃并计 `stale_chunks`。
-   超时：`connect_timeout`（5s，`Ready`）、`policy_setup_timeout`（300s，加载模型）、
    `observation_timeout`（10s，上传观测）、`get_actions_timeout`（10s，等待动作块；
    须大于服务端单次推理耗时）。
-   **vendored lerobot**：proto / wire 数据类与分块工具裁剪自 lerobot 官方仓库，放在
    `src/lerobot/`（Apache-2.0，独立于 `motrix_edge` 包；`ruff.toml` 的 `extend-exclude`
    排除 lint / format）。升级：替换对应文件 → 用 dev 依赖 `grpcio-tools` 重新生成
    `transport/services_pb2{,_grpc}.py` → 跑 `tests/test_lerobot_act_client.py`。
    ⚠️ `npm run add-header` 会把 Apache-2.0 头换成 Motphys 专有头，跑完需
    `git checkout -- src/lerobot` 还原。
-   **运行时依赖**：服务端动作载荷是 `torch.Tensor`（pickle 反序列化需要 torch），edge 侧固定
    用 **CPU 版**（`pyproject.toml` 的 `torch==…+cpu` + 官方 CPU 源，不拉 CUDA / `nvidia-*`）。
-   **`actions_per_chunk` / `image_size` 的真实语义**（对照官方实现，别按名字想当然）：
    -   `actions_per_chunk`：官方契约参数（`RemotePolicyConfig.actions_per_chunk`，官方客户端 CLI
        同名）。服务端只**截断**模型原生块 —— `chunk[:, :actions_per_chunk, :]`
        （`async_inference/policy_server.py`）。所以它是**请求上界**：超过模型原生块长时"有多少给
        多少"，不补齐、不重采样；含义 = 客户端一次要多少步（步数 ÷ 控制频率 = 一次推理能撑多久，
        RTC 的预取提前量按它算）。
    -   `image_size`：官方客户端**没有**这个概念 —— 客户端按相机原分辨率上传，服务端按
        **checkpoint** 的 `policy.config.image_features` 形状做 `F.interpolate(mode="bilinear")`
        **拉伸**到模型输入尺寸（`helpers.resize_robot_observation_image`），既不裁剪、也不
        letterbox；客户端在 `SendPolicyInstructions` 里声明的 `features.shape` 只用于组装
        `observation.state` / 相机键名与「观测是否相似」的去重比较，**不决定** resize 目标。
        因此 `image_size` 是**端侧上传前的预处理目标**（省带宽 / 省端侧算力），**不改变模型输入
        尺寸**，须与训练分辨率一致：edge 侧 letterbox 保证「原图 → 目标尺寸」这一步不拉伸，几何
        与训练管线一致（两侧同为 224×224 时，服务端那次 interpolate 是 no-op）。

## 配置项（POLICY_CONFIG_ITEMS）

公共项（与策略自身项**完全同级**、同一张表单、同一通道）：`host` / `port`（`group=endpoint`
仅用于前端分组）、`warmup_required`（预热门控，缺省 true）。它们也是**会话级**配置
（`runtime: False`：进入会话时读取，会话内改需退出重进），**没有专用命令**——同一个键只有一套规则。

| 策略          | 专有配置项                                                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `openpi`      | `prompt`（必填，语言指令）、`image_size`（默认 224）                                                                                       |
| `lerobot-act` | `pretrained_name_or_path`（必填，服务端加载的 checkpoint）、`device`（默认 cpu）、`actions_per_chunk`（默认 50）、`image_size`（默认 224） |

-   清单由 `policy_config_items(policy_type)` 提供（CLI `infer config`、前端表单、HTTP
    `/v1/infers/config` 同一来源）；`runtime_keys` 由 `runtime` 派生。
-   长文本项（`prompt` / `system_prompt`）带 `multiline: true`：前端渲染**多行 textarea**
    （占满整行、可拖拽调高），键名不在前端硬编码。
-   **`runtime`（会话内改能否立即生效）按「配置何时下发给服务端」划分**，不是按客户端能不能读：
    -   `openpi`（每请求独立，无握手状态）：`prompt` / `image_size` 在每次 `infer_chunk` **现读**
        → 会话内改立即生效（`runtime: True`）；
    -   `lerobot-act`（握手级）：`pretrained_name_or_path` / `device` / `actions_per_chunk` /
        `image_size` 只在 `Ready` 之后的 `SendPolicyInstructions` 里随 `RemotePolicyConfig`
        下发一次（服务端据此加载 checkpoint、定动作块长），会话内改**不重发、不生效** → 标
        `runtime: False`，**退出会话重进才生效**（前端据 `runtime_keys` 在会话内禁用这些输入框，
        避免「status 显示新值、推理仍用旧值」）；
    -   端点项 `host` / `port`（推理端点，**仅需端点的策略**）：**会话级**——进入会话时用配置构造策略客户端与传输层，
        会话内改只写内存态配置、下一会话生效（回执 `deferred` 列出未即时生效的键）→ 同样标
        `runtime: False`。
    -   公共项 `warmup_required`（预热门控）：同为**会话级**（进入会话时读取，会话内改需退出重进）；
        语义见「端点与预热」。
-   运行时写入内存态 `base_cfg["policy"]`（不写回 yaml），按策略 schema 白名单校验（类型不符 /
    `min`–`max` 越界 / 必填为空 → 400；**空值 `null` 或空串 = 清除该项回到缺省**，必填项空值 →
    400；`int` 项只接受整数，`bool` / 带小数的浮点 → 400，不静默截断）；**先全量校验通过才写入**
    （一批里任一项非法 → 整批 400，不留部分写入）；未在
    schema 内的键（如 `rename_cameras` / `image_cameras` / 各类超时）可在 `edge.yml` 的
    `policy` 段静态配置。

### 每项配置的真实作用（谁在什么时候用它）

| 键                          | 使用者               | 作用                                                                                        |
| --------------------------- | -------------------- | ------------------------------------------------------------------------------------------- |
| `host` / `port`             | 客户端传输层         | 推理节点端点（连接目标）；进入会话时固化（改需退出重进）                                    |
| `warmup_required`           | 会话（InferSession） | 未预热时是否允许 `infer rollout`（缺省 true = 不允许，先 `infer connect`）                  |
| `prompt`（openpi）          | 服务端（每帧）       | 语言条件：帧里带就用帧里的，缺了才由 checkpoint 的 `default_prompt` 兜底，随后逐帧 tokenize |
| `image_size`（openpi）      | **客户端**           | **端侧上传前的 letterbox 压缩目标**（省带宽 / 省端侧算力）；模型端按 checkpoint 尺寸兜底    |
| `image_size`（lerobot-act） | **客户端**           | 同上（端侧 letterbox 压缩），但 lerobot 服务端**只拉伸不兜底** → 必须与训练分辨率一致       |
| `pretrained_name_or_path`   | 服务端               | 加载哪个 checkpoint（握手时下发）                                                           |
| `device`                    | 服务端               | checkpoint 跑在哪块设备（握手时下发）                                                       |
| `actions_per_chunk`         | 服务端               | 模型原生动作块的**截断上界** `chunk[:, :K, :]`（不补齐、不重采样；握手时下发）              |

要点：**两个策略的 `image_size` 都只是"客户端压缩带宽用的目标尺寸"，不是"告诉模型用多大输入"** ——
模型输入尺寸由 checkpoint 决定：

-   openpi：模型端 `Observation.from_dict` 发现帧里的尺寸 ≠ checkpoint 的 `IMAGE_RESOLUTION`
    时会自己 `image_tools.resize_with_pad`（letterbox）补一次 → 客户端尺寸只影响带宽；
-   lerobot-act：服务端 `helpers.resize_robot_observation_image` 用 `F.interpolate(bilinear)`
    **拉伸**到 checkpoint 尺寸，**没有 letterbox 兜底** → 客户端尺寸直接决定几何（故须与训练
    分辨率一致）。

## 端点与预热（`infer connect`）

端点**没有专用命令、也没有专用状态字段**：它就是 `policy_config` 里的一项（`group=endpoint`
只影响前端分组），与 `prompt` / 模型路径 / 动作块长度共用同一 schema、同一校验（端口 1–65535）、
同一通道。语义上与其它**会话级**配置一致：**进入会话时固化**——会话内改只写内存态配置
（回执 `deferred` 列出未即时生效的键），退出会话重进才生效；因此不存在「连接后锁定」这条
额外的 409 轴。

| 场景                 | 通道                                                                                                                                         |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 查询端点与其它配置项 | `infer config`（HTTP：`GET /v1/infers` 的 `policy_config.items`）                                                                            |
| 设置端点             | `infer config set '{"host":"10.0.0.9","port":9000}'`（HTTP：`POST /v1/infers/config`，会话内；或 `POST /v1/infers` 的 `config`，进入会话时） |
| 连接 + 预热          | `infer connect`（HTTP：`POST /v1/infers/connect`，须已在推理会话）                                                                           |

`host` / `port` **非必填**：`openpi` / `lerobot-act`
未配置端点会在连接时报错，前端据「host 已填 + port 合法」门控「进入推理」按钮。

### 预热（推理但不上真机）

`connect()` → `prepare(obs)`（lerobot-act 下发策略指令、服务端加载 checkpoint；openpi 发一帧并
丢弃）→ 如果本连接还没真正取到过块（`policy.chunk_seen`，lerobot-act 的
`actions_per_chunk` 只是请求值，不能当实测）再 `infer_chunk(obs)` 取一块**丢弃**。全程**不调用
`adapter.rollout`**，故真机不动；取到的那一块同时给出实测块长（`observed_chunk_len` → RTC 校准）。
`chunk_seen` 是**连接级**状态：重连后由客户端复位（lerobot-act 在 `_forget_policy` 里），否则新连接
的预热会误判成已取过块、跳过那次真正取块。

-   **门控**：`warmup_required`（公共配置项，同 schema / 同通道，会话级）缺省 true = 未预热时
    `infer rollout` rejected 409（不惰性自连）；置 false = 允许 rollout 惰性自连（脚本 / 联调）。
    **预热进行中一律 409**（与 `warmup_required` 无关）：策略客户端的契约是「可跨线程调用，同一时刻
    至多一个请求」（`rtc/manager.py`）——预热线程与 rollout 并发会破坏 ws 的一问一答 / lerobot-act
    的单飞取块，并并发 `adapter.observe()`。预热期间允许的其它命令（`infer prompt` / `infer config` /
    `infer rtc` / 录制 / status）都**不发起策略请求**，因此不在此限。
-   **异步**：预热可能持续几十秒～几分钟（加载 checkpoint / 首帧推理），占住会话循环就会把急停、
    退出、状态查询一起挡住（node 主循环在任务运行期间不 poll 普通命令）。故 `infer connect` 把它
    交给**工作线程**并**立刻回执**：`started`（本次是否新启动）/ `warming` / `warmed_up` /
    `warmup_error`；**重复调用幂等**（回执即当前预热状态，不必另加状态命令），HTTP 侧另有
    `GET /v1/infers` 的同名字段供轮询。
-   **可中断**：`robot estop`（走命令总线旁路，任何状态即时生效）或 `session quit` → 置取消标志 +
    **关传输**（gRPC channel / ws 关闭会打断在飞调用）→ 工作线程以 `cancelled` 收尾，`warmed_up`
    保持 false（可重新预热）。预热期间 `infer prompt` / `infer config` / `capture meta` 等普通命令
    照旧响应。
-   **闩锁绑定在连接上**：`warmed_up` 是「本**连接**已预热」——连接一断（推理服务端重启 / 链路断开），
    服务端会话与已加载模型都不再可信（lerobot-act 重连后会重发策略指令、**重新加载 checkpoint**），
    故把 `warmed_up` 自动失效并记下 `warmup_error`（连接丢失）；此时 `infer connect` **不再**被幂等
    短路，重新预热即可恢复；未重新预热前 `infer rollout` 仍 409，不会退回「惰性重连 + 首块内联等
    模型加载」那条路。

`infer rollout` / `robot execute` 在下发动作前还会自查**回执有效期**（`deadline_exceeded`）：提交方
已超时放弃 → **丢弃动作**（真机不动）+ 回执 504，计数见 status `dropped_actions`。

## 虚拟推理端点（scripts/test_infer_point.py）

无真实推理的**模拟官方 `WebsocketPolicyServer`** 的联调端点：连接后先下发 metadata（
`--publish-horizon` 可选带上 `action_horizon`），按 openpi 官方 flat 契约接收观测，返回一段
**有界随机游走**的动作块 `{"actions": [horizon, dim]}`（块间连续），用于在无真实模型时验证
「edge → 推理端」的传输契约与 rtc 的取块 / 切分。与 Edge 的耦合仅限 wire 契约
（`policy/openpi/contract.py` / `transport/msgpack_numpy.py`），可独立运行：

```
uv run python scripts/test_infer_point.py --host 0.0.0.0 --port 8765 --action-dim 14 --action-horizon 16
```

## 相关文档

-   动作块消费（RTC）：[RTC 动作块管理](./motrix_edge_rtc.md)
-   推理会话：[会话（session）](./motrix_edge_session.md)
-   命令总线：[命令总线（CommandBus）](./motrix_edge_command_bus.md)
-   边缘观测来源：[机器人适配（adapter）](./motrix_edge_adapter.md)
-   P1 跨项目接入对齐点：[Kleinkram InferenceService 对接对齐点](../research/motrix_edge_kleinkram_alignment.md)
