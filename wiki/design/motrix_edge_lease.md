# Edge 级租约（lease）

## 摘要

租约是 **Edge 层级的独立授权机制**，独立于机器人 / 任务（`lease/`，`LeaseManager` 单点管理、
线程安全）。**租约权威在 Console**：Console Backend **生成 Lease**（`lease_id` / `edge_id` /
`holder_subject_id` / `purpose` / `lease_version` 等）并经 HTTP API 把租约
**镜像**下发到 Edge；Edge **只保留 + 校验**（`LeaseManager.install` / `require`），
**自身不生成租约**。Edge 在租约**有效（Active 且未过期）**时才允许执行受限操作（进入 /
控制任务、`/v1/commands` 含 estop）——受限操作须携带匹配租约（`X-Lease-Id`），由 Edge
校验后放行。

> **过期时间由 Edge 权威时钟计算**：`expires_at` 在 install / renew 时按 Edge 自身时钟
> `now + ttl` 计算，**不信任客户端 / Console 传入的过期时刻**——跨机时钟偏差不会导致租约
> 被提前误判过期（`410`）；对外时间字段（`expires_at` / `renewed_at`）统一以北京时间
> （`Asia/Shanghai`，+08:00）序列化，客户端时钟只用于展示倒计时。

> **前端暂代 Console**：Console Backend 尚未接入前，`frontend/edge-console` 的「租约编辑
> 生成栏」暂代 Console 生成 / 续约 / 撤销租约（见「前端暂代 Console」）。

## 目标与原则

-   **权威在 Console**：Lease 由 Console 生成并下发镜像；Edge **不生成** lease_id，只
    `install` 保留 + `require` 校验。
-   **单活跃控制租约**：同一 edge 同一时刻至多一个；已有活跃租约时再 `install` → `409`。
-   **租约独立于任务**：session **只消费（校验）租约**，不产生 / 销毁。
-   **续约（心跳）**：Console 定时 `renew`（建议间隔 `renew_interval`，默认 60s）并以更高
    `lease_version` 原地延长；Edge 在旧租约到期前收到新镜像即可保持控制。
-   **单点校验**：访问校验（`require`）由 `LeaseManager` 实现；Service 只调用
    `leases.require()`，不复制逻辑。
-   **Edge 权威时钟（过期）**：`expires_at` 由 Edge 按自身时钟 `now + ttl` 计算，不信任
    客户端传入的过期时刻——跨机时钟偏差不影响租约存活期判定。
-   **时间统一北京时间**：所有对外时间字段（`expires_at` / `renewed_at`）统一以
    `Asia/Shanghai`（+08:00）序列化。

## 数据流（正确模型）

租约部分：edge 接受 console backend 发送的 lease，edge 在 lease 有效时才能执行受限操作。
edge 具体监听的 http api 设计（Edge 接收 Console 下发的 Lease 镜像，Console 是租约权威状态）：
收到租约后 edge 本地若未收到续约请求，则在 lease 过期后进入 lease 无效状态，不能执行受限操作。

| 方法 | 路径                     | 方向           | 说明                                                        | 响应                                         |
| ---- | ------------------------ | -------------- | ----------------------------------------------------------- | -------------------------------------------- |
| POST | `/v1/leases`             | Console → Edge | console 生成 lease 并下发，edge 接收保存本地镜像            | `200 OK`                                     |
| POST | `/v1/leases/{id}:renew`  | Console → Edge | console 续约 lease（lease_version 递增），edge 更新本地镜像 | `200 OK`                                     |
| GET  | `/v1/leases/{id}`        | Console → Edge | 查询 edge 本地 lease 镜像状态                               | `200 OK`（返回 lease 信息）/ `404 Not Found` |
| POST | `/v1/leases/{id}:revoke` | Console → Edge | console 撤销 lease，edge 进入无效状态，不能执行受限操作     | `200 OK`                                     |

> -   租约权威状态由 Console 保存；Edge 只维护现场运行态与安全状态（RobotControlState），中心与现场状态不一致时以 Edge 更安全状态为准。
> -   Lease 状态机：Reserved → Active → Revoked/Expired；Active 才允许控制。续约 = 在 expires_at 到期前以更高 lease_version 原地延长，Edge 在旧租约到期前收到新镜像即可保持控制；撤销/释放统一为 Revoked 直接失效。
> -   edge 在 lease 有效时才能执行受限操作。

lease 信息暂包括：
| 字段 | 类型 | 说明 |
|------|------|------|
| lease_id | string | 租约唯一标识 |
| edge_id | string | 租约所属 edge 设备；同一 edge 同一时刻最多一个控制租约 |
| holder_subject_id | string | 租约所属操作员 |
| purpose | string | 租约用途（如 capture / rollout / maintenance） |
| state | string | 状态：Reserved、Active、Revoked、Expired |
| expires_at | datetime | 租约过期时间（Edge 权威时钟 now+ttl 计算；北京时间 +08:00） |
| renewed_at | datetime | 最近一次续约时间 |
| lease_version | int | 租约版本；续约时递增，版本回退拒绝 |

> 另：`GET /v1/leases` 为 Edge 侧**当前租约状态汇总**（`lease_id?` / `state` / `expires_at?` /
> `ttl?` / `renew_interval` / `leasable`），非 Console → Edge 镜像接口，供前端展示 / 轮询；
> 撤销（Revoked）后 Edge 清空当前租约槽位（`lease_id=None`，需重新签发），过期（Expired）
> 保留 id（可续约原地重新激活）。时间统一使用**北京时间**（`Asia/Shanghai`）。

## 租约生命周期

```
无租约 ──POST /v1/leases（Console 签发镜像）──▶ Reserved / Active
                                                     │
                                                     ├──renew（更高 lease_version 原地延长）──▶ Active
                                                     ├──revoke（Console 撤销）───────────────▶ Revoked（直接失效）
                                                     └──expires_at 到期（未续约）──────────────▶ Expired（可续约重新激活 / 重新签发）
```

`LeaseState`：`RESERVED` / `ACTIVE` / `REVOKED` / `EXPIRED`。仅 `ACTIVE` 且未过期允许控制
（`require`）；过期租约保留为 `expired` 状态（`leasable=true`，可续约重新激活，也可重新签发）。

## 请求体

`POST /v1/leases`（Console 签发租约 → Edge 存储镜像，Edge 不生成；**`expires_at` 由 Edge
按 `now + ttl` 计算，请求体无需携带**）：

```json
{
    "lease_id": "ls_console_issued",
    "edge_id": "edge-test-001",
    "holder_subject_id": "operator-1",
    "purpose": "capture",
    "state": "active",
    "lease_version": 1,
    "ttl": 120
}
```

`POST /v1/leases/{id}:renew`（Console 续约：更高 `lease_version` + 可选 `ttl`；
`expires_at` 由 Edge 按 `now + ttl` 重算）：

```json
{ "lease_version": 2, "ttl": 120 }
```

### 受控操作（Edge 校验）

`POST /v1/captures` / `DELETE /v1/captures`、`POST /v1/infers` / `DELETE /v1/infers`、
`GET /v1/preview`、`POST /v1/webrtc/offer`、`POST /v1/commands`（全部）——须携带匹配的
`X-Lease-Id`，Edge 经 `LeaseManager.require` 校验；仅 `ACTIVE` 且未过期放行。

## 前端暂代 Console（测试占位）

Console Backend 尚未接入时，`frontend/edge-console` 的**租约编辑生成栏**暂代 Console 生成 /
管理租约（Edge 自身不生成租约）：

-   填写部分字段（`lease_id` / `edge_id` / `holder_subject_id` / `purpose` / `state`）与
    **持续时间（ttl）**，页面提示**预计过期时间**（≈ Edge 时钟 + 时长）；提交
    `POST /v1/leases` 后以返回的权威 `expires_at` 为准（Edge 计算）。
-   按 `renew_interval` 定时 `POST /v1/leases/{id}:renew`（携带 `ttl`，Edge 按 `now+ttl`
    续期）自动续约（心跳，暂代 Console）。
-   撤销：`POST /v1/leases/{id}:revoke`。

## 过期时间与时钟（Edge 权威）

-   **为什么由 Edge 算**：租约是"Edge 允不允许受控操作"的授权，过期判定在 Edge 侧
    （`require` 用 Edge 本地 `expires_at` 比较）。若 `expires_at` 由远端（浏览器 / Console）
    按它自己的时钟生成，跨机时钟偏差会让 Edge"提前"或"滞后"判定过期——表现为刚签发 /
    续约过却立刻 `410 lease expired`。因此 `expires_at` 统一在 Edge 用**自身时钟** +
    `ttl` 计算（install 与 renew 都如此），客户端时钟只用于展示倒计时。
-   **时区统一**：Edge 内部时钟为北京时间（`Asia/Shanghai`）；对外序列化前把 `expires_at` /
    `renewed_at` 归一化到 `+08:00`，避免 UTC（前端 `toISOString` 恒为 UTC）与北京混用造成
    的"差 8 小时"歧义。
-   **返回权威值**：`POST /v1/leases` / `:renew` / `GET /v1/leases`（状态汇总与镜像查询）返回的
    `expires_at` 均为 Edge 计算后的权威值（+08:00），前端以其做到期倒计时。

## 错误语义

-   `404 Not Found`：`renew` / `revoke` / `GET /v1/leases/{id}` 时租约不存在。
-   `409 Conflict`：`install` 时已有活跃控制租约；`renew` 时 `lease_version` 版本回退；
    `require` 时无活跃租约（先 install）。
-   `403 Forbidden`：`require` 时不匹配 / 已撤销 / 非 `Active`。
-   `410 Gone`：`require` 时租约已过期（即使 lease_id 不匹配也优先报 410）。

## 配置（lease 段）

```yaml
lease:
    ttl: 120 # 租约默认有效期（秒）：Console 前端「签发租约」表单的缺省持续时间
    renew_interval: 60 # 建议续租间隔（秒）：Console 前端按此定时续租
```

## 相关文档

-   会话消费租约：[会话（session）](./motrix_edge_session.md)
-   HTTP 端点注册：[HTTP 控制面（server）](./motrix_edge_server.md)
-   代码入口：`src/motrix_edge/lease/`
