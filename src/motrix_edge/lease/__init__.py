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

"""lease 子包 —— Edge 级租约（独立于机器人 / 任务）。

**租约权威在 Console**：Console 生成 Lease 并经 ``/v1/leases`` 把租约**镜像**部署到
Edge；Edge 经 ``LeaseManager.install`` 只保留 + ``require`` 校验，**不生成租约**。
受控操作（进入 / 控制任务、commands 含 estop）须携带匹配租约（``X-Lease-Id``）。

**过期时间由 Edge 权威时钟计算**：install / renew 均按 Edge 自身时钟 ``now + ttl``
计算 ``expires_at``，**不信任客户端 / Console 传入的过期时刻**（避免跨机时钟偏差
导致租约被提前误判过期）；对外时间字段统一以北京时间（``Asia/Shanghai``，+08:00）
序列化，客户端时钟只用于展示倒计时。

Edge 监听（Console → Edge）：
  - ``POST /v1/leases``          签发租约镜像（install）
  - ``POST /v1/leases/{id}:renew``  续约（lease_version 递增，版本回退拒绝）
  - ``GET /v1/leases/{id}``       查询本地镜像（200 / 404）
  - ``POST /v1/leases/{id}:revoke`` 撤销（直接 Revoked 失效）
  - ``GET /v1/leases``            当前租约状态汇总（Edge 侧只读，供展示 / 轮询）

单活跃控制租约：同一 edge 同一时刻至多一个；``LeaseManager`` 线程安全。预留扩展字段
（``ttl`` 信息字段）。
"""

import threading
from datetime import datetime, timedelta

from .base import BEIJING_TZ, Lease, LeaseError, LeaseState

# 默认租约有效期（秒）：Console 前端「签发租约」表单的缺省持续时间（信息字段）
DEFAULT_LEASE_TTL = 120.0
# 默认建议续租间隔（秒）：Console 前端按此定时续租保持租约活跃（通常小于 TTL）
DEFAULT_RENEW_INTERVAL = 60.0


def _now() -> datetime:
    return datetime.now(BEIJING_TZ)


def _as_beijing(dt: datetime | None) -> datetime | None:
    """归一化到北京时间（Asia/Shanghai）：所有对外时间字段统一序列化为 +08:00。"""
    if dt is None:
        return None
    return dt.astimezone(BEIJING_TZ) if dt.tzinfo is not None else dt.replace(tzinfo=BEIJING_TZ)


class LeaseManager:
    """Edge 级租约管理器（单活跃控制租约，线程安全）。

    **Console 权威，Edge 只保留 + 校验镜像**：
    - ``install``：部署 Console 签发的租约**镜像**（Edge 不生成 lease_id）；已有活跃
      控制租约 → ``LeaseError(409)``（过期 / 撤销 / 无租约可覆盖）。
    - ``renew``：Console 续约 —— 以更高 ``lease_version`` 原地延长 ``expires_at``
      （版本回退拒绝）；不存在 → ``404``。
    - ``revoke``：Console 撤销 —— 租约直接进入 ``Revoked`` 失效（幂等）。
    - ``mirror``：查询本地镜像（``GET /v1/leases/{id}``）；不存在 → ``404``。
    - ``status``：当前租约状态汇总（``GET /v1/leases``，供展示 / 轮询）。
    - ``require``：受控操作校验 —— 缺失 ``409`` / 不匹配 ``403`` / 过期 ``410`` /
      撤销 ``403``；仅 ``Active`` 且未过期放行。
    """

    def __init__(
        self, default_ttl: float = DEFAULT_LEASE_TTL, renew_interval: float = DEFAULT_RENEW_INTERVAL, now=None
    ):
        self._default_ttl = default_ttl
        self._renew_interval = renew_interval
        self._now = now or _now  # 可注入时钟（测试用）
        self._lease: Lease | None = None
        self._lock = threading.Lock()

    # ---- Console → Edge 镜像管理（权威在 Console）---------------------------
    def install(self, lease: Lease) -> Lease:
        """部署 Console 签发的租约**镜像**（Edge 只保留 + 校验，不生成 lease_id）。

        **过期时间由 Edge 权威时钟计算**：``expires_at = now + ttl``（``ttl`` 缺省用
        ``default_ttl``）；**不信任传入的 ``expires_at``** —— 跨机时钟偏差不会让租约
        被提前误判过期。``renewed_at`` 取当前时刻（签发即最近一次续约时间）。

        单活跃控制租约：已有 ``Active`` 且未过期租约 → ``LeaseError(409)``；过期 /
        撤销 / 无租约 → 覆盖为新镜像。
        """
        with self._lock:
            cur = self._lease
            if cur is not None and cur.state == LeaseState.ACTIVE and cur.expires_at > self._now():
                raise LeaseError("lease already active", 409)
            now = self._now()
            ttl = lease.ttl if lease.ttl is not None and lease.ttl > 0 else self._default_ttl
            lease.ttl = ttl
            lease.expires_at = now + timedelta(seconds=ttl)
            lease.renewed_at = now
            self._lease = lease
            return self._lease

    def renew(self, lease_id: str, lease_version: int, ttl: float | None = None) -> Lease:
        """Console 续约：以更高 ``lease_version`` 原地延长（版本回退拒绝）。

        **过期时间由 Edge 权威时钟计算**：``expires_at = now + ttl``（``ttl`` 缺省沿用
        当前租约的 ``ttl``，再回退 ``default_ttl``）；不信任客户端传入的过期时刻。
        续约后新 ``expires_at`` 在未来 → ``Active``（可控制），否则 → ``Expired``。
        """
        with self._lock:
            lease = self._require_mirror(lease_id)
            if lease_version <= lease.lease_version:
                raise LeaseError("lease version rollback rejected", 409)
            now = self._now()
            next_ttl = ttl if ttl is not None and ttl > 0 else (lease.ttl or self._default_ttl)
            lease.lease_version = lease_version
            lease.ttl = next_ttl
            lease.expires_at = now + timedelta(seconds=next_ttl)
            lease.renewed_at = now
            lease.state = LeaseState.ACTIVE if lease.expires_at > now else LeaseState.EXPIRED
            return lease

    def revoke(self, lease_id: str) -> Lease:
        """Console 撤销：租约直接进入 ``Revoked`` 失效（不可控制）。幂等；不存在 →
        ``LeaseError(404)``。撤销 / 释放统一为 ``Revoked``。
        """
        with self._lock:
            lease = self._require_mirror(lease_id)
            if lease.state != LeaseState.REVOKED:
                lease.state = LeaseState.REVOKED
            return lease

    def revoke_current(self) -> Lease | None:
        """撤销当前租约（无需 id；管理员清理幽灵租约用）。无租约 → None；幂等。

        撤销后 ``status()`` 清空当前租约槽位（``leasable=True``），新控制端可重新签发。
        """
        with self._lock:
            if self._lease is None:
                return None
            if self._lease.state != LeaseState.REVOKED:
                self._lease.state = LeaseState.REVOKED
            return self._lease

    def mirror(self, lease_id: str) -> dict:
        """查询本地租约镜像（``GET /v1/leases/{id}``）：返回状态 dict；不存在 →
        ``LeaseError(404)``。"""
        with self._lock:
            return self._lease_info(self._require_mirror(lease_id))

    # ---- 只读 / 校验 --------------------------------------------------------
    def status(self) -> dict:
        """当前租约状态汇总（``GET /v1/leases``，供展示 / 轮询）。

        - ``lease_id`` = 当前租约 id（无租约 → ``None`）；**Revoked 直接失效 → 清空
          当前槽位（``lease_id=None``，须重新签发）**；Expired / Reserved 保留 id
          （Expired 可续约原地重新激活）。
        - ``state`` 由本地按 ``expires_at`` 兜底计算（过期 → ``expired``）。
        - ``leasable`` = 可签发新租约（无活跃控制租约）。
        """
        with self._lock:
            if self._lease is None:
                return {
                    "lease_id": None,
                    "edge_id": None,
                    "holder_subject_id": None,
                    "purpose": None,
                    "state": None,
                    "expires_at": None,
                    "renewed_at": None,
                    "lease_version": None,
                    "ttl": None,
                    "renew_interval": self._renew_interval,
                    "default_ttl": self._default_ttl,
                    "leasable": True,
                }
            info = self._lease_info(self._lease)
            # 撤销 = 直接失效：清空当前槽位（重新签发新租约），state 仍保留 revoked 供展示
            if self._lease.state == LeaseState.REVOKED:
                info["lease_id"] = None
            info["renew_interval"] = self._renew_interval
            info["default_ttl"] = self._default_ttl
            info["leasable"] = not self._lease.is_active(self._now())
            return info

    def require(self, lease_id: str) -> Lease:
        """受控操作校验：仅 ``Active`` 且未过期放行；缺失 ``409`` / 不匹配 ``403`` /
        过期 ``410`` / 撤销 ``403``。"""
        with self._lock:
            return self._require_active(lease_id)

    # ---- 内部 ---------------------------------------------------------------
    def _require_mirror(self, lease_id: str) -> Lease:
        """按 id 取本地镜像；不存在 → 404。"""
        if self._lease is None or self._lease.lease_id != lease_id:
            raise LeaseError("lease not found", 404)
        return self._lease

    def _require_active(self, lease_id: str) -> Lease:
        # 无租约：先 install（409）
        if self._lease is None:
            raise LeaseError("no active lease (install first)", 409)
        lease = self._lease
        # 异租约：优先报「过期」（即使 lease_id 不匹配，也明确状态而非异租约）
        if lease.lease_id != lease_id:
            if lease.expires_at <= self._now():
                raise LeaseError("lease expired", 410)
            raise LeaseError("lease mismatch: request does not own the active lease", 403)
        if lease.state == LeaseState.REVOKED:
            raise LeaseError("lease revoked", 403)
        if lease.expires_at <= self._now():
            raise LeaseError("lease expired", 410)
        if lease.state != LeaseState.ACTIVE:
            raise LeaseError(f"lease not active (state={lease.state.value})", 403)
        return lease

    def _lease_info(self, lease: Lease) -> dict:
        """租约镜像状态 dict（本地按 expires_at 兜底计算 state）。"""
        expired = lease.expires_at <= self._now()
        state = lease.state.value
        if state == LeaseState.ACTIVE.value and expired:
            state = LeaseState.EXPIRED.value
        return {
            "lease_id": lease.lease_id,
            "edge_id": lease.edge_id,
            "holder_subject_id": lease.holder_subject_id,
            "purpose": lease.purpose,
            "state": state,
            "expires_at": _as_beijing(lease.expires_at).isoformat() if lease.expires_at is not None else None,
            "renewed_at": _as_beijing(lease.renewed_at).isoformat() if lease.renewed_at is not None else None,
            "lease_version": lease.lease_version,
            "ttl": lease.ttl,
        }


def build_lease_manager(base_cfg: dict) -> LeaseManager:
    """从配置 ``lease`` 段构建 LeaseManager（ttl / renew_interval，缺省用默认值）。

    - ``ttl``：默认租约有效期（秒）—— Console 前端「签发租约」表单的缺省持续时间。
    - ``renew_interval``：建议续租间隔（秒）—— Console 前端按此定时续租。
    """
    lease_cfg = base_cfg.get("lease", {})
    return LeaseManager(
        default_ttl=lease_cfg.get("ttl", DEFAULT_LEASE_TTL),
        renew_interval=lease_cfg.get("renew_interval", DEFAULT_RENEW_INTERVAL),
    )


__all__ = [
    "DEFAULT_LEASE_TTL",
    "DEFAULT_RENEW_INTERVAL",
    "Lease",
    "LeaseError",
    "LeaseManager",
    "LeaseState",
    "build_lease_manager",
]
