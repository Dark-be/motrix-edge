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

from pathlib import Path

import numpy as np
import pytest
from motrix_simulation_pipeline.piper_kinematics import PIPER_LIMITS, PiperKinematics
from motrix_simulation_pipeline.runtime import Settings, Simulation

from motrix_edge.geometry.transforms import rpy_to_matrix, transform_to_pose


def _simulation(tmp_path: Path) -> Simulation:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = tmp_path / "session.toml"
    scene = tmp_path / "scene.json"
    session.write_text("", encoding="utf-8")
    scene.write_text("{}", encoding="utf-8")
    sim = Simulation(Settings(workspace=workspace, session=session, scene=scene, output=tmp_path / "output"))
    sim.limits = PIPER_LIMITS.copy()
    sim.kinematics = PiperKinematics(sim.limits)
    sim.home[:6] = np.array([0.0, 0.4, -0.7, 0.0, 0.2, 0.0])
    sim.target = sim.home.copy()
    sim.healthy = lambda: True
    return sim


def test_pose_with_gripper_is_resolved_to_one_atomic_joint_target(tmp_path):
    sim = _simulation(tmp_path)
    expected = np.array([0.1, 0.45, -0.75, 0.05, 0.15, -0.05])
    target_pose = sim.kinematics.pose(expected)

    receipt = sim.submit([*target_pose, 0.25], layout="pose+gripper", arms=["right"])

    assert receipt["request"]["layout"] == "pose+gripper"
    assert receipt["resolved_layout"] == "joint+gripper"
    resolved = np.asarray(receipt["resolved_action"])
    assert resolved[-1] == pytest.approx(0.25)
    assert sim.kinematics.pose(resolved[:6]) == pytest.approx(target_pose, abs=1e-3)
    assert np.array(sim.target) == pytest.approx(sim.home)


def test_policy_pose_uses_tcp_origin_not_flange_origin(tmp_path):
    sim = _simulation(tmp_path)
    q = sim.home[:6]
    flange = transform_to_pose(sim.kinematics.fk(q))
    tcp = sim.kinematics.pose(q)

    assert tcp[:3] == pytest.approx(flange[:3] + rpy_to_matrix(flange[3:6]) @ np.array([0.0, 0.0, 0.12]))
    assert tcp[3:] == pytest.approx(flange[3:])


def test_unreachable_pose_is_rejected_without_queue_or_target_change(tmp_path):
    sim = _simulation(tmp_path)
    before = sim.target.copy()

    with pytest.raises(ValueError, match="pose target rejected"):
        sim.submit([10.0, 10.0, 10.0, 0.0, 0.0, 0.0], layout="pose")

    assert not sim.commands
    assert not sim.pending
    assert sim.target == pytest.approx(before)
