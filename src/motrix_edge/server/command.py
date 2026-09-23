# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Product and Trade Secret source code contains trade secrets of Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

"""CommandService —— **唯一的命令写通道**：`/v1/commands`（capability）与 REST 端点（命令词）。

派发与本地 CLI **共用** :class:`~motrix_edge.command.CommandDispatcher`（注册校验 / push
分流 / 等回执 / 超时），失败也抛同一个 :class:`~motrix_edge.command.CommandError`：两端
语义完全相同，**唯一差异是租约**。本类只加 HTTP 入口需要的三件事：

1. **租约门**：受控命令须持有 Edge 级活跃租约（``LeaseManager.require``：缺失 409 /
   不匹配 403 / 过期 410）—— CLI 在进程内直连总线，不经此处；
2. **capability 通道**（``POST /v1/commands``）：capability 由命令词派生（``infer rollout``
   → ``infer/rollout``），旧拼写保留一版（回执带 ``deprecated=true``），见 wiki/design
   motrix_edge_rpent_bridge.md「命名约定」；
3. **回执 → HTTP**：成功回 ``data``（直接摊平进响应体），失败按回执的 ``code`` 抛
   ``CommandError``，由 ``server/app.py`` 的单一错误处理器渲染成 ``{"detail": ...}``。

**HTTP 与 CLI 一一对应，唯一差异是租约**：路由不做业务判断，只把 HTTP 入参映射成命令参数；
命令语义唯一实现在 ``command/`` 包（注册表 / 参数解析）与 node / session（消费）。新增一条
命令只需注册 ``CommandSpec``，HTTP 与 CLI 同时获得。CLI 在进程内直连 ``CommandBus``、
**不经本类**，因此不受租约约束；本类是 HTTP 入口，租约校验在此**强制**（缺失 409 /
不匹配 403 / 过期 410）。

通道语义：

- **租约**：受控命令须持有 Edge 级活跃租约（``LeaseManager.require``：缺失 409 / 不匹配 403 /
  过期 410）；``/v1/commands`` 无 capability 的骨架分支同样先校验租约。
- **命令词校验**：必须是已注册命令，否则 **404**（不回骨架，避免「静默成功」——拼错
  capability 时会等满超时）。
- **push 型**（``robot estop`` / ``node reset``，见 ``command.PUSH_COMMANDS``）：即发即忘，
  只入总线不等回执（``accepted``）；急停走总线**旁路队列**，任务运行期间也即时生效。
- **submit 型**：同步等回执；成功回 ``data``，业务拒绝按回执的 ``code`` 抛
  ``CommandError``（edge 错误码），命令未被消费（超时）→ ``timeout``。
  **超时 ≠ 取消**：命令已被消费、处理器会继续执行——需要「调用方放弃就不产生副作用」的
  处理器（下发动作到真机）在动作前用 ``deadline_exceeded`` 自查（见 ``command/core.py``）。

``idempotency_key``（HTTP 请求体字段）为 Console 幂等契约**预留**：当前**未实现**去重
（并发 check-dispatch-put 非原子，无法保证同 key 只下发一次），调用方须自行处理重试、
勿依赖去重。
"""

from motrix_edge.command import (
    META_SOURCE,
    SOURCE_HTTP,
    CapabilityRef,
    CommandBus,
    CommandDispatcher,
    CommandError,
    resolve_capability,
)
from motrix_edge.errors import ErrorCode
from motrix_edge.lease import LeaseError, LeaseManager


class CommandService:
    """HTTP 命令写通道：``CommandDispatcher``（与 CLI 共用）+ 租约门 + capability / 回执映射。"""

    def __init__(self, bus: CommandBus, leases: LeaseManager | None = None, registry=None):
        self._leases = leases or LeaseManager()  # Edge 级租约（HTTP 入口的权限门）
        self._dispatcher = CommandDispatcher(bus, registry)

    @property
    def registry(self):
        """命令注册表（路由 / 自省用）。"""
        return self._dispatcher.registry

    # ---- REST 端点通道（命令词直调）------------------------------------------
    def submit(
        self,
        name: str,
        params: dict | None = None,
        *,
        lease_id: str | None = None,
        timeout: float | None = None,
        source: str = SOURCE_HTTP,
    ) -> dict:
        """提交命令并同步等回执：**成功回 ``data``**（dict，直接摊平进 HTTP 响应体）。

        未知命令 → ``unknown_command``；租约无效 → ``lease_required`` / ``forbidden`` /
        ``lease_expired``；业务拒绝 → 回执的 ``code``（漏给按 ``internal``）；命令未被消费
        （超时）→ ``timeout``。失败一律抛 :class:`CommandError`。
        """
        self._check_lease(lease_id)
        result = self._dispatcher.dispatch(name, params, meta=self._meta(lease_id, source), timeout=timeout)
        if result.status != "ok":  # 判成败只看回执 status；漏给错误码 → 按服务端错误
            raise CommandError(result.error or f"{name} rejected", code=result.code or ErrorCode.INTERNAL)
        return dict(result.data)

    # ---- /v1/commands（capability 通道）--------------------------------------
    def execute(
        self,
        command_id: str,
        lease_id: str | None = None,
        capability: str | None = None,
        params: dict | None = None,
        source: str = SOURCE_HTTP,
    ) -> dict:
        """capability → 命令词 → 总线；回执形状对外固定。

        返回 ``{"status", "command_id", "executed", "deprecated", "data", "error", "code"}``：``push``
        型命令无回执通道 → ``status="accepted"``；无 ``capability`` → 骨架 ``accepted``
        （预留 Capability 校验 / 具体下发）。幂等（idempotency_key）预留、未实现，见模块
        docstring。
        """
        self._check_lease(lease_id)

        ref = resolve_capability(capability)
        if ref is None:
            # 无 capability：骨架（accepted，预留 Capability 校验 / 具体下发）
            return self._receipt(command_id, None, {"status": "accepted", "executed": None})

        result = self._dispatcher.dispatch(ref.command, params, meta=self._meta(lease_id, source))
        return self._receipt(
            command_id,
            ref,
            {
                "status": result.status,  # push 型 = accepted（无回执通道）
                "executed": ref.capability,
                "data": result.data,
                "error": result.error,
                "code": result.code,  # edge 错误码（成功 / push 型为 None）
            },
        )

    # ---- 内部 ---------------------------------------------------------------
    @staticmethod
    def _meta(lease_id: str | None, source: str) -> dict:
        """命令控制元数据：``lease_id``（HTTP 入口注入）+ ``source``（仅可观测性）。"""
        return {"lease_id": lease_id, META_SOURCE: source}

    @staticmethod
    def _receipt(command_id: str, ref: CapabilityRef | None, fields: dict) -> dict:
        """拼回执：``executed`` 统一回**规范 capability**；旧拼写请求标记 ``deprecated``。"""
        return {"command_id": command_id, "deprecated": ref.deprecated if ref else False, **fields}

    def _check_lease(self, lease_id: str | None) -> None:
        """HTTP 入口的租约门（CLI 不经此处）：把租约错误码原样上报（缺失 / 不匹配 / 过期）。"""
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise CommandError(str(exc), code=exc.code) from exc


__all__ = ["CommandService"]
