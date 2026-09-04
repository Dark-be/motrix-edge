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

"""Lease —— Edge 级租约（独立于机器人 / 任务）的领域模型。

**租约权威在 Console**：Console 生成 Lease（含 ``lease_id`` / ``edge_id`` /
``holder_subject_id`` / ``purpose`` / ``expires_at`` / ``lease_version``）并经
``POST /v1/leases`` 把租约**镜像**部署到 Edge；Edge **只保留 + 校验**
（``LeaseManager.install`` / ``require``），**不生成租约**。受控操作（进入 / 控制任务、
commands 含 estop）须携带匹配租约（``X-Lease-Id``），由 Edge 校验后放行。

单活跃控制租约：同一 edge 同一时刻至多一个；已有活跃租约时再 install → 409。
状态机：``Reserved → Active → Revoked / Expired``；仅 ``Active`` 允许控制。续约 = 在
``expires_at`` 到期前以更高 ``lease_version`` 原地延长；撤销 / 释放统一为 ``Revoked``
直接失效。预留扩展字段（``ttl`` 信息字段），后续 Capability 校验 / Safety Guard /
多租约并发在此基础上扩展。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

# 北京时间（UTC+8）：Edge 租约的过期时间 / 时间戳统一使用北京时间
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class LeaseState(str, Enum):
    """租约生命周期状态（Console 权威；Edge 本地按 expires_at 兜底计算过期）。"""

    RESERVED = "reserved"  # 已签发（预留），尚未生效 —— 不可控制
    ACTIVE = "active"  # 活跃 —— 允许受控操作
    REVOKED = "revoked"  # 撤销 / 释放 —— 直接失效，不可控制
    EXPIRED = "expired"  # 过期 —— 到期未续约，不可控制


@dataclass
class Lease:
    """Edge 级租约（Console 签发的**镜像**，Edge 只保留 + 校验；可变 —— renew 原地延长）。

    - ``lease_id``：租约唯一标识（**Console 生成**，Edge 不生成）。
    - ``edge_id``：租约所属 edge 设备（同一 edge 同一时刻最多一个控制租约）。
    - ``holder_subject_id``：租约所属操作员。
    - ``purpose``：租约用途（如 capture / rollout / maintenance）。
    - ``state``：状态（Reserved / Active / Revoked / Expired；Active 才允许控制）。
    - ``expires_at``：过期时间（北京时间）。
    - ``renewed_at``：最近一次续约时间。
    - ``lease_version``：租约版本；续约时递增，版本回退拒绝。
    - ``ttl``：有效期（秒）—— 信息字段，Console 签发 / 续约时的建议时长。
    """

    lease_id: str
    edge_id: str
    holder_subject_id: str
    purpose: str
    state: LeaseState
    expires_at: datetime  # 到期时间（北京时间；由 Edge 权威时钟 now+ttl 计算）
    renewed_at: datetime | None = None  # 最近一次续约时间
    lease_version: int = 1  # 租约版本；续约递增
    ttl: float | None = None  # 有效期（秒，信息字段）

    def is_expired(self, now: datetime | None = None) -> bool:
        """是否已过期（``expires_at`` 已到）。"""
        now = now or datetime.now(BEIJING_TZ)
        return self.expires_at <= now

    def is_active(self, now: datetime | None = None) -> bool:
        """是否可控制（Active 且未过期）。"""
        return self.state == LeaseState.ACTIVE and not self.is_expired(now)


class LeaseError(Exception):
    """租约操作被拒绝（缺失 / 不匹配 / 过期 / 撤销 / 已有活跃 / 版本回退）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code
