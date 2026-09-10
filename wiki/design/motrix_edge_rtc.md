# 实时动作块（rtc）

## 摘要

`rtc/` 是**策略无关的实时动作块管理器**：策略（openpi / act）只负责「拿到一次推理的原始动作块」，
`RTCManager` 统一负责**动作块三元切分**（`prefix_actions` 过去已失效 / `execution_actions` 实际执行 /
`suffix_actions` 过渡到下一块）、**时序平滑**（块重叠加权融合）、**拉取时机**与**绝对步号推进**。
会话（InferSession）只调用 `rtc.infer(observation)` 取「本步应下发的动作」。

## 目标与原则

-   **策略只取结果**：`BasePolicyClient.infer_chunk(observation, index)` 返回原始动作块（`ActionChunk`），
    不做缓存 / 切片 / 平滑；策略实现里不再有游标、timestep 缓存、重叠聚合。
-   **RTC 单一职责**：块缓存、三元切分、重叠聚合（时序平滑）、预取时机、步号推进、状态上报全部收敛到 `rtc/`。
-   **server 零改动**：纯 edge 侧切分与平滑；不向推理端传额外字段（预留后续 wire 扩展）。
-   **可观测 / 可运行期配置**：参数经 `edge.yml` 的 `policy.rtc` 段缺省，运行期可经命令 / HTTP 查改
    （与 `infer ip` / `adapter config` 同款机制）。
-   **无硬件可单测**：注入 fake policy 即可验证切分 / 平滑 / 预取，不碰网络。

## 包结构

```
src/motrix_edge/rtc/
├── __init__.py   # build_rtc(policy, config) 工厂 + 导出 ActionChunk / ChunkSlice / RTCManager / DEFAULT_RTC_CONFIG
├── base.py       # ActionChunk（原始块：actions + start_index）+ ChunkSlice（三元切分结果）+ 聚合函数表
└── manager.py    # RTCManager：块队列 / 切分 / 平滑 / 预取 / configure / status
```

## 动作块三元切分

一次推理返回的**一整块动作**按绝对步号切三段（`ChunkSlice`）：

| 段                  | 含义                                                      | 处置                                       |
| ------------------- | --------------------------------------------------------- | ------------------------------------------ |
| `prefix_actions`    | 前置段 P：推理期间机器人**已经执行过**的前 P 步（已失效） | **跳过**（不缓存、不补发；仅统计上报）     |
| `execution_actions` | 执行段 E：本次实际执行段                                  | 与上一块后缀重叠部分加权平均，其余直接执行 |
| `suffix_actions`    | 后缀段 S：留给下一块做过渡                                | 留在队列，与下一块执行段重叠融合（平滑）   |

-   块以 `start_index`（首步绝对步号）对齐；`P` 由 **edge 侧配置**（不是策略返回的）：推理本身耗时，
    块返回时其首步对应的时刻已经过去——那几步再下发会让机械臂**往回走一小段**。
-   跳过 P 时**同步推进 `_index`**（步号 = 物理时刻，不再落后）并丢弃队列中已过期步；
    本轮从「未过期」的块首步继续下发，**不断流**（不会丢帧等待）。
-   `action_horizon`（H）= **块长上限**：一次推理只取策略块的前 H 步（如 H=10、10Hz → 1s 预测）；
    `execution_horizon`（E）= 执行段步数（缺省 = 实际块长 - P - S）；`suffix_len`（S）= 后缀段步数；
    块长 `H = P + E + S`（策略返回的块比 H 短时按实际块长截断，`E` 至少 1）。
-   **请求时机 = 执行段还剩 P 步**（`remaining <= P + S`）：推理耗时的 P 步正好吃掉执行段尾巴，
    响应回来时后缀段完整保留 → 重叠步数 = `min(S, E)`。

## 时序平滑（RTC 统一承载）

**问题**：块边界处若直接从旧块末步跳到新块首步，动作会跳变（机械冲击）。

**机制**：把一块按`P + E + S`三段切开：`P`（前置段，推理期间机器人**已经执行过**，跳过）、
`E`（执行段）、`S`（后缀段，留给下一块）。**请求时机 = 执行段还剩 P 步**（推理本身耗时 P 步）——
推理耗时的 P 步正好吃掉**执行段的尾巴**，响应回来时**整个后缀段**还在队列里，新块跳过 P 步后的
动作正好落在后缀段上 → 重叠加权融合（默认 `weighted_average`：`0.3*旧 + 0.7*新`，对齐 lerobot
`AGGREGATE_FUNCTIONS`）。

```text
块 k 执行段： [====E====][====S====]
                     ↑ 还剩 P 步时发请求（推理耗时 = P 步）
块 k+1 到达：  [P 跳][====E====][====S====]
                     └─ 与上一块后缀段重叠 → 加权平均；未重叠部分直接执行 ─┘
```

-   聚合在**入队时**完成（绝对步号 → 动作的字典），消费时只取融合后的值。
-   重叠步数 = `min(S, E)`；`suffix_len = 0` → 退化为「块耗尽才推理」（无重叠、无平滑）。
-   聚合函数：`weighted_average`（默认）/ `latest_only` / `average` / `conservative`。

## RTCManager

```python
rtc = build_rtc(policy, config, control_hz=None)  # 会话进入时构造（持策略引用；control_hz=1/step_interval）
rtc.infer(observation)                   # 本步动作：必要时拉块 → 切分 → 跳前置段 → 入队聚合 → 返回当前步
rtc.reset()                              # 清队列 / 步号归零（策略连接不变）
rtc.configure(**params)                  # 运行期改参数（校验后生效）
rtc.status() -> dict                     # 上报：enabled / params / index / remaining / fetches / last_chunk / last_delay
```

-   **步号**：`_index` 单调递增（reset 归零）；策略按 `index` 组织观测（act 用它做 `TimedObservation.timestep`）。
-   **前置段跳过**：P 是**人工设置**的（推理耗时期间机器人已经执行过的步数），块返回后从 `P` 步之后
    开始下发；跳过后同步推进 `_index` 并丢弃队列中已过期步（步号 = 物理时刻）。实测推理耗时
    （`time.monotonic`）折算成的步数经 `status().last_delay_steps` 上报，**仅供**人工定 P 参考，
    不自动参与切分。
-   **预取时机**：队列剩余 `<= prefix_len + suffix_len`（= **执行段还剩 P 步**）时同步拉下一块；
    推理耗时的 P 步吃掉执行段尾巴，后缀段完整保留给下一块做重叠融合（不断流）。
-   **异常 / 空块**：`policy.infer_chunk` 抛错或返回 `None` → `infer` 返回 `None`（会话跳过本步，**不升级为任务错误**）。
-   **`enabled: false`**：无 RTC 退化模式——每步请求一次、只取块首步（不做块缓存 / 重叠 / 跳过）。

## 配置（policy.rtc 段）

```yaml
policy:
    rtc:
        enabled: true # 关闭 → 每步一次推理只取块首步（无块缓存 / 无平滑）
        action_horizon: 50 # 块长上限 H：一次推理只取策略块的前 H 步（如 10 = 10Hz × 1s）
        prefix_len: 0 # 前置段 P：推理期间已被执行的前 P 步（跳过）；查 rtc.last_delay_steps 估算
        execution_horizon: null # 执行段 E；null = 实际块长 - P - S
        suffix_len: 10 # 后缀段 S（与下一块的重叠窗口）；0 = 关闭平滑
        aggregate_fn: weighted_average # 重叠聚合：weighted_average/latest_only/average/conservative
```

约束：`P + S < H`（块内必须有执行段）、`P + E + S <= H`（E 显式设置时）、`E > P`（否则每步都触发推理）；
`aggregate_fn` 已注册。请求时机 = 执行段还剩 P 步（`remaining <= P + S`）。
`control_hz` 由会话按 `1 / step_interval`（`policy.infer_freq`）自动传入，仅用于实测耗时折算上报，不需配置。

## 运行时命令（infer rtc）

| 命令                   | 位置参数 | 语义                                                        | 状态可用性 |
| ---------------------- | -------- | ----------------------------------------------------------- | ---------- |
| `infer rtc`            | —        | 查询当前 RTC 参数 + 运行状态（index / remaining / 切分）    | 全局       |
| `infer rtc set <json>` | `json`   | 设置 RTC 参数（JSON 对象，可部分；写入内存态 `policy.rtc`） | 全局       |

-   与 `infer ip` / `infer port` 同款：配置级命令，任何状态可用；写内存态不写回 yaml；会话内设置同时
    应用到**正在运行的** `RTCManager`（下一块生效），会话外只影响下次 `session run infer`。
-   参数缺失 / 非法（负数、未知聚合函数）→ `rejected`（400，不崩溃）。

## HTTP 暴露

| 方法 | 路径             | 租约 | 说明                                                                                               |
| ---- | ---------------- | ---- | -------------------------------------------------------------------------------------------------- |
| GET  | `/v1/infers`     | 无   | status 增加 `rtc` 字段（enabled / params / index / remaining / fetches / last_chunk / last_delay） |
| POST | `/v1/infers/rtc` | 必需 | body 为 RTC 参数（可部分）→ `infer rtc set`，回执生效后参数                                        |

## 相关文档

-   策略客户端（只取推理结果）：[推理策略客户端（policy）](./motrix_edge_policy.md)
-   会话消费 RTC：[会话（session）](./motrix_edge_session.md)
-   命令与状态暴露：[命令总线（CommandBus）](./motrix_edge_command_bus.md) / [HTTP 控制面（server）](./motrix_edge_server.md)
-   前端面板：[Edge Web Console](./motrix_edge_web_console.md)
-   配置段：[配置与命令行](./motrix_edge_config.md)
