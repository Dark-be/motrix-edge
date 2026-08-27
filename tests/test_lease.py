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

"""lease 包测试 —— Edge 级租约（Console 权威，Edge 只保留 + 校验），单活跃，无硬件可跑。

覆盖：install（签发镜像 / 单活跃 409 / 过期·撤销可覆盖）、renew（版本递增 / 版本回退
拒绝 / 404 / 重新激活）、revoke（撤销失效 / 幂等 / 404）、mirror（查询 200 / 404）、
status 状态汇总、require 校验（缺失 409 / 不匹配 403 / 过期 410 / 撤销 403 / reserved 403）。
"""

from datetime import datetime, timedelta, timezone

import pytest

from motrix_edge.lease import LeaseError, LeaseManager, LeaseState
from motrix_edge.lease.base import Lease

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Clock:
    """可注入时钟：推进时间模拟租约过期。"""

    def __init__(self, start=_BASE):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float):
        self.t += timedelta(seconds=seconds)


def make_manager(ttl: float = 10.0):
    clock = _Clock()
    return LeaseManager(default_ttl=ttl, now=clock), clock


def make_lease(
    lease_id: str = "ls_console_issued",
    edge_id: str = "edge-test-001",
    holder_subject_id: str = "operator-1",
    purpose: str = "capture",
    state: LeaseState = LeaseState.ACTIVE,
    ttl: float = 10.0,
    lease_version: int = 1,
    renewed_at=None,
):
    """构造 Console 签发的租约镜像（缺省 Active + 10s 后过期）。"""
    return Lease(
        lease_id=lease_id,
        edge_id=edge_id,
        holder_subject_id=holder_subject_id,
        purpose=purpose,
        state=state,
        expires_at=_BASE + timedelta(seconds=ttl),
        renewed_at=renewed_at,
        lease_version=lease_version,
        ttl=ttl,
    )


def test_install_stores_console_mirror():
    """Console 签发镜像 → install → Edge 只保留 + 校验（require）；状态反映镜像字段。"""
    mgr, _ = make_manager()
    mgr.install(make_lease(ttl=10))
    snap = mgr.status()
    assert snap["lease_id"] == "ls_console_issued"
    assert snap["edge_id"] == "edge-test-001"
    assert snap["holder_subject_id"] == "operator-1"
    assert snap["purpose"] == "capture"
    assert snap["state"] == LeaseState.ACTIVE.value
    assert snap["lease_version"] == 1
    assert snap["leasable"] is False
    # Edge 只校验：携带镜像 lease_id 的受控操作放行；非镜像 → 403
    assert mgr.require("ls_console_issued").lease_id == "ls_console_issued"
    with pytest.raises(LeaseError) as exc:
        mgr.require("other")
    assert exc.value.status_code == 403
    # 已有活跃租约时再 install → 409
    with pytest.raises(LeaseError) as exc2:
        mgr.install(make_lease(lease_id="ls_second"))
    assert exc2.value.status_code == 409


def test_install_overwrites_expired_or_revoked():
    """过期 / 撤销租约不占用单活跃名额：可覆盖为新镜像。"""
    mgr, clock = make_manager()
    mgr.install(make_lease(lease_id="ls_first", ttl=10))
    clock.advance(11)  # 过期
    second = mgr.install(make_lease(lease_id="ls_second", ttl=10))
    assert second.lease_id == "ls_second"
    # 撤销后可重新签发
    mgr.revoke("ls_second")
    third = mgr.install(make_lease(lease_id="ls_third", ttl=10))
    assert third.lease_id == "ls_third"


def test_install_sets_renewed_at():
    mgr, _ = make_manager()
    lease = mgr.install(make_lease(renewed_at=None))
    assert lease.renewed_at == _BASE


def test_renew_extends_expiry_with_version():
    """续约：更高 lease_version 原地延长 expires_at，renewed_at 更新。"""
    mgr, clock = make_manager()
    lease = mgr.install(make_lease(ttl=10))
    old = lease.expires_at
    clock.advance(5)
    renewed = mgr.renew("ls_console_issued", lease_version=2, expires_at=clock.t + timedelta(seconds=10))
    assert renewed.lease_version == 2
    assert renewed.expires_at > old
    assert renewed.state == LeaseState.ACTIVE
    assert renewed.renewed_at == clock.t


def test_renew_rejects_version_rollback():
    """版本回退（lease_version 不高于当前）→ 409。"""
    mgr, _ = make_manager()
    mgr.install(make_lease(ttl=10))
    with pytest.raises(LeaseError) as ei:
        mgr.renew("ls_console_issued", lease_version=1, expires_at=_BASE + timedelta(seconds=30))
    assert ei.value.status_code == 409


def test_renew_not_found_404():
    mgr, _ = make_manager()
    with pytest.raises(LeaseError) as ei:
        mgr.renew("ls_none", lease_version=2, expires_at=_BASE + timedelta(seconds=30))
    assert ei.value.status_code == 404


def test_renew_reactivates_reserved_or_expired():
    """续约使租约回到 Active（新 expires_at 在未来）——Edge 在旧租约到期前收到新镜像保持控制。"""
    mgr, clock = make_manager()
    mgr.install(make_lease(state=LeaseState.RESERVED, ttl=10))
    clock.advance(11)  # reserved 到期
    renewed = mgr.renew("ls_console_issued", lease_version=2, expires_at=clock.t + timedelta(seconds=10))
    assert renewed.state == LeaseState.ACTIVE


def test_revoke_marks_revoked_and_blocks_control():
    """撤销：租约进入 Revoked 直接失效，受控操作拒绝（403）；幂等。"""
    mgr, _ = make_manager()
    mgr.install(make_lease(ttl=10))
    mgr.revoke("ls_console_issued")
    assert mgr.mirror("ls_console_issued")["state"] == LeaseState.REVOKED.value
    with pytest.raises(LeaseError) as ei:
        mgr.require("ls_console_issued")
    assert ei.value.status_code == 403
    # 幂等：重复撤销仍返回 Revoked
    assert mgr.revoke("ls_console_issued").state == LeaseState.REVOKED


def test_revoke_not_found_404():
    mgr, _ = make_manager()
    with pytest.raises(LeaseError) as ei:
        mgr.revoke("ls_none")
    assert ei.value.status_code == 404


def test_mirror_returns_lease_info():
    mgr, _ = make_manager()
    mgr.install(make_lease(ttl=10))
    info = mgr.mirror("ls_console_issued")
    assert info["lease_id"] == "ls_console_issued"
    assert info["edge_id"] == "edge-test-001"
    assert info["holder_subject_id"] == "operator-1"
    assert info["purpose"] == "capture"
    assert info["state"] == LeaseState.ACTIVE.value
    assert info["lease_version"] == 1
    assert info["expires_at"]
    # 不存在 → 404
    with pytest.raises(LeaseError) as ei:
        mgr.mirror("ls_none")
    assert ei.value.status_code == 404


def test_status_no_lease():
    mgr, _ = make_manager()
    snap = mgr.status()
    assert snap["lease_id"] is None
    assert snap["state"] is None
    assert snap["leasable"] is True
    assert snap["renew_interval"] == 60.0  # 默认建议续租间隔（DEFAULT_RENEW_INTERVAL）
    assert snap["default_ttl"] == 10.0


def test_status_expired_state_and_leasable():
    """过期租约保留为 expired 状态，leasable=True（可重新签发）。"""
    mgr, clock = make_manager(ttl=10)
    mgr.install(make_lease(ttl=10))
    clock.advance(11)
    snap = mgr.status()
    assert snap["lease_id"] == "ls_console_issued"
    assert snap["state"] == LeaseState.EXPIRED.value
    assert snap["leasable"] is True


def test_require_missing_lease_409():
    mgr, _ = make_manager()
    with pytest.raises(LeaseError) as ei:
        mgr.require("ls_x")
    assert ei.value.status_code == 409


def test_require_mismatch_403():
    mgr, _ = make_manager()
    mgr.install(make_lease(ttl=10))
    with pytest.raises(LeaseError) as ei:
        mgr.require("ls_wrong")
    assert ei.value.status_code == 403


def test_require_reserved_403():
    """仅 Active 允许控制：Reserved 租约 → 403。"""
    mgr, _ = make_manager()
    mgr.install(make_lease(state=LeaseState.RESERVED, ttl=10))
    with pytest.raises(LeaseError) as ei:
        mgr.require("ls_console_issued")
    assert ei.value.status_code == 403


def test_require_expired_410():
    mgr, clock = make_manager(ttl=10)
    mgr.install(make_lease(ttl=10))
    clock.advance(11)
    with pytest.raises(LeaseError) as ei:
        mgr.require("ls_console_issued")
    assert ei.value.status_code == 410
