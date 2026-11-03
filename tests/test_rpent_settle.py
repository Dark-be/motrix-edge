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

"""Fresh feedback, rather than repeated cached samples, establishes stable arrival."""

import pytest

from motrix_edge.adapter.base import ActionSpace
from motrix_edge.server.rpent.settle import MotionSample, SettleConfig, wait_for_reached


@pytest.fixture
def clock(monkeypatch):
    from motrix_edge.server.rpent import settle

    now = [0.0]
    monkeypatch.setattr(settle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(settle.time, "sleep", lambda duration: now.__setitem__(0, now[0] + duration))
    return now


@pytest.mark.parametrize("excursion", [1.0, float("nan")])
def test_dwell_restarts_after_departure_from_tolerance(clock, excursion):
    seq = [0]

    def probe():
        seq[0] += 1
        error = excursion if 0.15 <= clock[0] < 0.25 else 0.0
        return MotionSample((error, None, error), seq[0], 0)

    result = wait_for_reached(
        probe, space=ActionSpace.JOINT, config=SettleConfig(dwell_s=0.3, poll_s=0.05, timeout_s=1)
    )
    assert result["reached"] is True and result["elapsed_s"] >= 0.55


@pytest.mark.parametrize("sample", [MotionSample((0, None, 0), 1, 0), MotionSample((0, None, 0), 1, 10), None])
def test_cached_or_old_feedback_cannot_complete_dwell(clock, sample):
    result = wait_for_reached(
        lambda: sample, space=ActionSpace.JOINT, config=SettleConfig(dwell_s=0.1, max_sample_age_s=0.2, poll_s=0.05)
    )
    assert result["reached"] is False and result["stale_observation"] is True


def test_timeout_includes_dwell(clock):
    seq = [0]

    def probe():
        seq[0] += 1
        return MotionSample((0, None, 0), seq[0], 0)

    result = wait_for_reached(
        probe, space=ActionSpace.JOINT, config=SettleConfig(dwell_s=1, timeout_s=0.2, poll_s=0.05)
    )
    assert result["reached"] is False and result["timeout"] is True


def test_joint_and_gripper_tolerances_are_independent():
    config = SettleConfig(rot_tol=0.5, joint_tol=0.02, gripper_tol=0.1)
    assert config.within(None, 0.08, ActionSpace.GRIPPER)
    assert not config.within(None, 0.08, ActionSpace.JOINT)
    assert config.within(0, 0.08, ActionSpace.POSE)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_settle_config_is_rejected(value):
    with pytest.raises(ValueError, match="finite"):
        SettleConfig.from_mapping({"dwell_s": value})
