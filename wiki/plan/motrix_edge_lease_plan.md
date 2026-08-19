# Edge 级租约机制 实施计划

## 摘要

基于 [Edge 级租约机制（lease）](../design/motrix_edge_lease.md)：把租约提升为 Edge 层级的
独立授权机制（单活跃），`/v1/leases/*` 提供 activate / renew / release（revoke 为 Edge
内部强制撤销，不暴露 HTTP），受控操作（captures 控制 + commands 全部）需持有有效租约。
功能完整落地后删除本文档并更新本索引。

> **范围说明**：本 MR（脚手架 + 文档）仅交付包骨架与文档；本计划实现项由后续 MR [!4（HTTP 控制面）](/motphys-robotics/motrix-loop/motrix-edge/-/merge_requests/4) 落地，在本 MR 内均为 `[ ]`。

## TODO

-   [ ] `lease/` 子包：`Lease` dataclass（lease_id / lessee / ttl / renew_interval /
        expires_at / metadata / state）+ `LeaseState`（ACTIVE / EXPIRED / RELEASED /
        REVOKED）+ `LeaseError`
-   [ ] `lease/`：`LeaseManager`（单活跃）：activate / renew / release / revoke（内部、无参、
        幂等）/ get / require（缺失 / 不匹配 / 过期 → 对应错误）；续租与访问校验单点收敛于此
-   [ ] `server/app.py`：注册 `/v1/leases/*` 端点（activate / renew / release / get；revoke
        不暴露 HTTP）；`lease` 配置段（ttl / renew_interval）
-   [ ] `server/app.py`：`/v1/commands` 增加租约校验；`capability=estop` → push
        SIG_ROBOT_ESTOP（注入 CommandService / node 通道）
-   [ ] `server/capture.py`：`CaptureService` 注入 `LeaseManager`，移除内嵌 `_lease_id`；
        enter 要求先持有租约；回合控制改经 LeaseManager 校验
-   [ ] `__main__.py`：创建并注入 `LeaseManager` / `CommandService`（web 线程共享）
-   [ ] `config/test.yml`：`lease` 段（ttl: 120 / renew_interval: 60）
-   [ ] 测试：`test_lease.py`（activate / renew / release / revoke / 过期 / 校验）+
        `test_server.py` 同步（/v1/leases 端点、commands 租约校验、estop 执行、captures 迁移）
-   [ ] 全量 `uv run ruff check src tests` + `uv run pytest`（全绿）
