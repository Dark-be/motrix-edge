# 实时动作块（rtc）

## 摘要

`rtc/` 是**策略无关的实时动作块管理器**：策略（openpi / lerobot-act）只负责「拿到一次推理的原始动作块」，
`RTCManager` 统一负责**动作块三元切分**（`prefix` 过去已失效 / `execution` 实际执行 / `suffix` 过渡到
下一块，只算步数不切数组）、**重叠过渡**（重叠步按「下一段权重曲线」融合）、**异步预取**（后台线程拉
下一块，控制环不阻塞）与**绝对步号推进**。
会话（InferSession）只调用 `rtc.infer(observation)` 取「本步应下发的动作」。

> **落地范围**：本提交只交付 `rtc/` 包、`policy.rtc` 配置段与测试；**运行期接线**（策略客户端
> `infer_chunk` 契约、会话调用 `rtc.infer`、`infer rtc` 命令、`/v1/infers/rtc` 与 `/v1/infers` 的
> `rtc` 字段）随 **#8（MR !7）** 落地——下面「运行时命令」「HTTP 暴露」两节描述的是**接线后**的形态。

## 目标与原则

-   **策略只取结果**：`BasePolicyClient.infer_chunk(observation, index)` 返回原始动作块（`ActionChunk`），
    不做缓存 / 切片 / 过渡；策略实现里不再有游标、timestep 缓存、重叠过渡。
-   **RTC 单一职责**：块缓存、三元切分、重叠过渡（过渡策略）、预取时机、步号推进、状态上报全部收敛到 `rtc/`。
-   **server 零改动**：纯 edge 侧切分与过渡；不向推理端传额外字段（预留后续 wire 扩展）。
-   **可观测 / 可运行期配置**：参数经 `edge.yml` 的 `policy.rtc` 段缺省，运行期可经命令 / HTTP 查改
    （与 `infer config` / `adapter config` 同款机制）。
-   **控制环不阻塞**：预取（推理）在**后台线程**完成，`infer()` 只做「取当前步动作 + 必要时登记一次预取」，
    控制频率不受推理耗时影响；推理期间由队列里的后缀段继续供电。
-   **无硬件可单测**：注入 fake policy 即可验证切分 / 过渡 / 预取，不碰网络。

## 包结构

```
src/motrix_edge/rtc/
├── __init__.py   # build_rtc(policy, config, control_hz) 工厂 + 公开面（见 __all__：ActionChunk /
                  # RTCManager / DEFAULT_RTC_CONFIG / TRANSITION_FUNCTIONS / validate_config / validate_params）
├── base.py       # ActionChunk（原始块：actions + start_index）+ split_lens（三元切分步数）+ 过渡策略表
└── manager.py    # RTCManager：块队列 / 切分 / 过渡 / 异步预取（工作线程）/ configure / status
```

## 动作块三元切分

一次推理返回的**一整块动作**按绝对步号分三段处理（步数由 `base.split_lens` 给出，**只用于上报**；
实际入队按绝对步号过滤，不切数组）：

| 段（`status().last_chunk.lens`） | 含义                                                      | 处置                                           |
| -------------------------------- | --------------------------------------------------------- | ---------------------------------------------- |
| `prefix`                         | 前置段 P：推理期间机器人**已经执行过**的前 P 步（已失效） | **跳过**（不缓存、不补发；仅在 lens 里统计）   |
| `execution`                      | 执行段 E：本次实际执行段                                  | 与上一块后缀重叠步按过渡策略加权，其余直接执行 |
| `suffix`                         | 后缀段 S：留给下一块做过渡                                | 留在队列，与下一块执行段重叠融合（过渡）       |

-   块以 `start_index`（首步绝对步号）对齐；跳过步数 = `max(prefix_len, _index - start_index)`：
    推理本身耗时，块返回时其首步对应的时刻已经过去——那几步再下发会让机械臂**往回走一小段**。
    异步预取下 `_index` 随控制环推进，**真实过期步自动计入**；配置的 `prefix_len` 是额外的人工安全
    余量（取值可参考 `status().last_delay_steps`）。策略未声明 `start_index`（`ActionChunk` 缺省
    `None`）时由管理器按**请求步号**补齐——否则块会被当成从 0 起，整块落进 `prefix` 被丢弃。
-   跳过时**同步推进 `_index`**（步号 = 物理时刻，不再落后）并丢弃队列中已过期步；
    本轮从「未过期」的块首步继续下发，**不断流**（不会丢帧等待）。
-   `action_horizon`（H）= **块长上限**：一次推理只取策略块的前 H 步（如 H=10、10Hz → 1s 预测）；
    `execution_horizon`（E）= 执行段步数；`suffix_len`（S）= 后缀段步数；块长 `H = P + E + S`。
-   **块长 < H 时三段等比缩放**：H 只是上限，策略返回的块可能更短（openpi 16 步 vs H=50）。配置的
    P/E/S 是**绝对步数**、按 H 标定；装不进这一块时按 `实际块长 / H` **等比缩放**，并保证
    `P + E + S = 实际块长`、`E >= 1`、`P + S < 实际块长`（`S` 非 0 时保底 1 步）。不能只截断 E：
    预取提前量 `P + S` 一旦大于块长，就会**每一拍都在推理**（实测块长 16 / H=50：不缩放时 60 步要推理
    60 次，缩放后回到 6 次）。`execution_horizon: null` 时 E 本来就按实际块长推导，不涉及缩放。
-   **兜底校准（`calibrate`，按实测块长收敛 H）**：策略服务端**不一定声明块长**——openpi 原生
    metadata 没有 `action_horizon`，lerobot-act 的 gRPC 协议更是没有任何 metadata，`actions_per_chunk`
    只是请求上界。`infer()` 每步按需调用 `RTCManager.calibrate(policy.observed_chunk_len)`
    （openpi 由预热 / 首次推理回填；lerobot-act 连接前先用请求值 `actions_per_chunk` 兜底，首次推理后以实测覆盖），把 H 收敛为 `min(H, 实测)`、P/E/S 等比缩放、`E` 回到缺省推导，
    于是**首个 rollout 就用与真实块长匹配的三段**，不必每块靠上面的临时缩放兜底。只在实测更小时**校准一次**
    （实测块长是「最近一次」，可能被瞬时短块污染；已校准 / 已显式 `configure` 过就不再跟随），
    参数不合法或未观测到块长时保持原配置（校准是兜底，不抛异常、不影响推理）。
-   **预取提前量 = `P + S`**（用**本次生效**的三段，短块已缩放）：队列剩余 `<= P + S`（= 执行段还剩
    P 步）时发起下一块预取；响应大约在 P 步后到达 → 后缀段（S 步）还在队列里，重叠步数 ≈ `min(S, E)`。
-   **上报口径**：`status().last_chunk` 的 `height` = **实际块长**（`min(H, 模型块长)`），`lens` =
    本次生效的规划值（已缩放，`prefix` 还包含推理期间真实走过的过期步）；真实发生的是「跳过 `prefix`
    步 → 与上一块融合 `overlap_steps` 步 → 其余新块独占」，判断过渡是否生效看 `overlap_steps`。

## 重叠过渡（过渡策略）

**问题**：块边界处若直接从本段末步跳到下一段首步，动作会跳变（机械冲击）。

**机制**：把一块按 `P + E + S` 三段切开：`P`（前置段，推理期间机器人**已经执行过**，跳过）、
`E`（执行段）、`S`（后缀段，留给下一块）。**预取提前量 = `P + S`**（执行段还剩 P 步时发请求）——
推理耗时的 P 步正好吃掉**执行段的尾巴**，响应回来时**后缀段**还在队列里，下一段跳过过期步后的
动作正好落在后缀段上 → 重叠步按**过渡策略**融合。

```text
块 k 执行段： [====E====][====S====]
                     ↑ 还剩 P 步时发请求（推理耗时 ≈ P 步 → 控制环继续消费后缀段）
块 k+1 到达：  [过期步跳][====E====][====S====]
                     └─ 与上一块后缀段重叠 → 按过渡策略加权；未重叠部分直接执行 ─┘
```

**过渡策略** = 重叠步上「**下一段**（新块）」的权重曲线 `alpha(pos, length)`（`pos` = 该步在重叠窗口内
的序号，`0` = 离当前最近的一步；`length` = 重叠步数），融合式 `动作 = (1 - alpha) * 本段 + alpha * 下一段`：

| 策略                       | `alpha`              | 语义                                           |
| -------------------------- | -------------------- | ---------------------------------------------- |
| `weighted_average`（默认） | `0.7`（常数）        | 权重过渡：0.3 本段 + 0.7 下一段，新决策占主导  |
| `conservative`             | `0.3`（常数）        | 权重过渡：0.7 本段 + 0.3 下一段，抑制跳变      |
| `average`                  | `0.5`（常数）        | 权重过渡：各半                                 |
| `latest_only`              | `1.0`（常数）        | 全取下一段（硬切换，不做过渡）                 |
| `continuous`               | `pos / (length - 1)` | 连续过渡：按动作步数线性——本段 1→0、下一段 0→1 |

-   **权重过渡**（固定搭配）：整段重叠区用同一组权重，与步号无关。
-   **连续过渡**（`continuous`）：按**动作步数**逐步让下一段接手——重叠区最早的一步仍以本段为主（避免
    抖动），越靠后越采信下一段（跟上新观测），到重叠区末尾完全由下一段接管 → 块间连续换手，没有硬切换点；
    重叠仅 1 步（`length = 1`）时无法过渡，退化为直接采用下一段（`alpha = 1`）。
-   融合在**入队时**完成（绝对步号 → 动作的字典），消费时只取融合后的值；`alpha` 超出 `[0, 1]`
    会被裁剪（防自定义曲线外推）。
-   稳态重叠步数 ≈ `min(S, E)`（真实值取决于响应到达时刻，上报为 `status().last_chunk.overlap_steps`）；
    `suffix_len = 0` → 退化为「块耗尽才推理」（无重叠、无过渡）。
-   策略表即 `rtc.base.TRANSITION_FUNCTIONS`（配置键沿用 `aggregate_fn`，默认值见 `base.DEFAULT_AGGREGATE_FN`）；
    新增策略只需在表内登记。

## 异步预取时序

```text
控制环（10Hz）:  infer() → 弹出当前步 → 返回                 ← 从不等待推理
                      └─ 队列剩余 <= P + S 时：登记一次预取
预取工作线程:           infer_chunk(obs, index) → 落块（按落地时的步号切分 + 重叠融合）
```

-   **单飞**：一块在途时不重复发起（避免请求堆积 / 策略端排队）。实现上分两态：**待执行的登记**
    （`_job`）与**策略端正在被调用**（`_running`）——`reset()` / `close()` 只作废前者（它从没
    到过策略端），后者的占用由执行者自己结清；因此既不会并发叠出第二个请求，也不会留下永久占用。
-   **前提（由策略客户端保证）**：客户端需可跨线程调用，且**每次 `infer_chunk` 都必须带超时**——
    单飞槽的释放取决于调用返回，没有超时就没有上界，策略端一挂就会退化成「队列耗尽后不再下发动作」。
-   **观测快照**：发起预取时拷贝观测（`ndarray.copy()` + 容器递归）——观测里的数组常是共享内存视图。
-   **动作隔离**：入队动作与策略返回的数组不共享内存（`ActionChunk.head` 拷一次），策略复用自己那块
    解码 buffer 不会改写已入队动作。
-   **按世代作废**：`reset()` / `close()` 后到达的响应被丢弃——结果、实测耗时与失败计数
    （`last_error` / `failed_chunks`）一并作废，旧世代只结清单飞槽；`enabled=false` 的内联路径
    同样按世代丢弃（调用期间被复位 → 本轮结果与统计不采用）。
-   **工作线程回收**：`close()`（会话退出调用）停止线程；线程为 daemon，忘调也不阻塞进程退出。
    线程**不会因单次失败而死**：策略异常与落块意外都在线程内兜住并记入 `status()`（`failed_chunks` /
    `last_error`），单飞槽由执行路径的 `finally` 结清。

## RTCManager

```python
rtc = build_rtc(policy, config, control_hz=None)  # 会话进入时构造（持策略引用）
rtc.infer(observation)   # 本步动作：必要时登记预取（后台线程）→ 弹出当前步（**不阻塞**）
rtc.reset()              # 清队列 / 作废在途预取 / 步号归零（策略连接不变）
rtc.configure(**params)  # 运行期改参数（校验后生效，下一块起用；内部加锁）
rtc.calibrate(block_len) # 按实测块长收敛 H（幂等，仅第一次生效；infer() 每步自动调用）
rtc.status() -> dict     # 上报：enabled / params / index / remaining / inflight /
                         #       fetches / stale_chunks / failed_chunks /
                         #       last_chunk / last_delay / last_delay_steps / last_error
rtc.close()              # 停止预取工作线程（会话退出调用；幂等）
```

-   **重置时机（会话层约定）**：会话**进入**、**暂停**（`infer rollout stop`）与**接管开始**
    （`robot teleop true`）都调 `rtc.reset()`。后两者丢弃未执行的
    块并**按世代作废在途请求**，**交回 / 恢复后第一块用当时观测现算**——否则旧块会把机械臂朝接管
    **前**的轨迹拉。交回（`teleop=false`）不额外重置（那一刻没有要作废的块）；「等模型动作接近
    当前位姿再交回」的过渡策略见 `robot_pipeline_teleop.md`。
-   **步号**：`_index` 单调递增（reset 归零）；策略按 `index` 组织观测（lerobot-act 用它做 `TimedObservation.timestep`）。
-   **前置段跳过**：块返回时从 `max(prefix_len, _index - 块首步)` 之后开始下发；跳过后同步推进 `_index`
    并丢弃队列中已过期步（步号 = 物理时刻）。实测推理耗时（`time.monotonic`）折算成的步数经
    `status().last_delay_steps` 上报，可作为 `prefix_len` 取值参考。
-   **异步预取**：队列剩余 `<= prefix_len + suffix_len` 时把请求交给**工作线程**，`infer()` 立即返回
    队列里的当前步 → 控制频率不受推理耗时影响；推理期间走过的步在响应落地时自动计入 `prefix`。
-   **观测快照**：发起预取时对观测做防御性拷贝；调用方可在 `infer()` 返回后立即复用 / 覆写原 buffer。
-   **异常 / 非有限值 / 空块**：`policy.infer_chunk` 返回 `None` / 空块 → 计 `failed_chunks`，`infer` 返回
    `None`（会话跳过本步，**不升级为任务错误**）；块里含 NaN / Inf（真机不能收）→ `ActionChunk` 构造即
    `ValueError`，与策略异常同款处理：异步路径不外抛（工作线程必须存活），记入 `status().last_error`
    （限长）+ `failed_chunks`；本步必须等结果时（首块 / 断流）在调用线程**内联**完成，异常照旧抛给调用方
    （与会话回执语义一致）。
-   **`enabled: false`**：无 RTC 退化模式——每步请求一次、只取块首步；同样**按 `action_horizon`（H）
    截断**并**跳过过期步**（与 RTC 路径同口径：超过 H 的步一律视为过期，不拿过期动作驱动真机，整块
    过期返回 `None`）。
-   **可观测**：`stale_chunks`（整块过期被丢弃 = 策略滞后）/ `failed_chunks`（策略没数据 / 抛异常）/
    `inflight` 用来区分「某一步没有动作可下发」的原因。

## 配置（policy.rtc 段）

```yaml
policy:
    rtc:
        enabled: true # 关闭 → 每步一次推理只取块首步（无块缓存 / 无过渡）
        action_horizon: 50 # 块长上限 H：一次推理只取策略块的前 H 步（如 10 = 10Hz × 1s）
        prefix_len: 0 # 前置段 P：额外强制跳过的前 P 步（真实过期步自动跳过）；查 rtc.last_delay_steps 估算
        execution_horizon: 30 # 执行段 E；须 > P（也允许 null = 实际块长 - P - S，代码兜底缺省）
        suffix_len: 20 # 后缀段 S（与下一块的重叠窗口，同时是预取提前量）；0 = 关闭过渡
        aggregate_fn: weighted_average # 过渡策略：weighted_average/conservative/average/latest_only/continuous
```

上表即包内 `edge.yml` 的**下发值**（`tests/test_rtc.py` 校验它满足键名与全部约束）；`edge.yml` 缺该段时
生效的代码兜底缺省（`rtc.DEFAULT_RTC_CONFIG`）为 `execution_horizon: null`、`suffix_len: 10`。

约束：`P + S < H`（块内必须有执行段）、`P + E + S <= H`（E 显式设置时）、`E > P`（否则每步都触发推理）；
`H` 应对齐**模型输出块长**（lerobot-act 的 `actions_per_chunk` / openpi 服务端 metadata 的 `action_horizon`）：
H 远大于模型块长时，若策略端**声明了**块长则 `calibrate` 会把它收敛到实测值（见上文「兜底校准」），
否则三段按实际块长等比缩放（过渡与执行段都交给缩放而非配置）。
`aggregate_fn` 必须是 `rtc.base.TRANSITION_FUNCTIONS` 已注册的策略名（见上文策略表）。
预取提前量 = `P + S`（执行段还剩 P 步时发起；在工作线程完成）。
`control_hz` 由会话按 `1 / step_interval`（`policy.infer_freq`）自动传入，仅用于实测耗时折算上报，不需配置。

## 运行时命令（infer rtc）

> 接线后形态：命令由 **#8（MR !7）** 在命令总线注册（`CMD_INFER_RTC` / `CMD_INFER_RTC_SET`）；
> 本提交只提供参数校验（`validate_params` / `validate_config`）与会话级 `configure`。

| 命令                   | 位置参数 | 语义                                                        | 状态可用性 |
| ---------------------- | -------- | ----------------------------------------------------------- | ---------- |
| `infer rtc`            | —        | 查询当前 RTC 参数 + 运行状态（index / remaining / 切分）    | 全局       |
| `infer rtc set <json>` | `json`   | 设置 RTC 参数（JSON 对象，可部分；写入内存态 `policy.rtc`） | 全局       |

-   与 `infer config` 同款：配置级命令，任何状态可用；写内存态不写回 yaml；会话内设置同时
    应用到**正在运行的** `RTCManager`（下一块生效），会话外只影响下次 `session run infer`。
-   参数缺失 / 非法（负数、未知过渡策略）→ `rejected`（400，不崩溃）。

## HTTP 暴露

> 接线后形态：`POST /v1/infers/rtc` 与 `/v1/infers` 响应里的 `rtc` 字段由 **#8（MR !7）** 在 server 落地。

| 方法 | 路径             | 租约 | 说明                                                                                                                                                      |
| ---- | ---------------- | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GET  | `/v1/infers`     | 无   | status 增加 `rtc` 字段（enabled / params / index / remaining / inflight / fetches / stale_chunks / failed_chunks / last_chunk / last_delay / last_error） |
| POST | `/v1/infers/rtc` | 必需 | body 为 RTC 参数（可部分）→ `infer rtc set`，回执生效后参数                                                                                               |

## 相关文档

-   策略客户端（只取推理结果）：[推理策略客户端（policy）](./motrix_edge_policy.md)
-   会话消费 RTC：[会话（session）](./motrix_edge_session.md)
-   命令与状态暴露：[命令总线（CommandBus）](./motrix_edge_command_bus.md) / [HTTP 控制面（server）](./motrix_edge_server.md)
-   前端面板：[Edge Web Console](./motrix_edge_web_console.md)
-   配置段：[配置与命令行](./motrix_edge_config.md)
