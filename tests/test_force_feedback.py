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

"""``force_single_piper_robot`` 双向耦合的纯逻辑与状态机测试（不连接硬件）。"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_SRC = Path(__file__).resolve().parents[1] / "robot-pipeline" / "src"
_PROFILE = {
    "max_initial_error": [0.25] * 6,
}


def _load(name):
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    if "pyAgxArm" not in sys.modules:
        sdk = types.ModuleType("pyAgxArm")
        sdk.AgxArmFactory = type("AgxArmFactory", (), {})
        sdk.ArmModel = type("ArmModel", (), {})
        sdk.PiperFW = type("PiperFW", (), {"V188": "v188"})
        sdk.create_agx_arm_config = lambda *args, **kwargs: {}
        sys.modules["pyAgxArm"] = sdk
    return importlib.import_module(name)


class _Hardware:
    pass


class _FakeController:
    def __init__(self, name):
        self.name = name
        self.q = np.zeros(6)
        self.gripper = 1.0
        self.role = "follower"
        self.port = None
        self.robot = _Hardware()
        self.joint_calls = []

    def connect(self, *, port, role, **_kwargs):
        self.port = port
        self.role = role

    def disconnect(self):
        self.robot = None

    def get_joint(self):
        return None if self.q is None else self.q.copy()

    def get_gripper(self):
        return self.gripper

    def set_joint(self, joint, torque_ff=None):
        self.joint_calls.append(np.asarray(joint).copy())

    def set_gripper(self, value):
        self.gripper = float(value)


@pytest.fixture()
def force_robot(monkeypatch):
    module = _load("robot.force_single_piper_robot")
    monkeypatch.setattr(module, "PiperController", _FakeController)
    robot = module.ForceSinglePiperRobot(
        {
            "init_joint": [0.0] * 6,
            "init_gripper": [1.0],
            "ports": {"master": "master_left", "slave": "right"},
            "force_feedback": _PROFILE,
        }
    )
    robot.connect()
    return robot


def test_config_only_accepts_initial_alignment_limit():
    module = _load("robot.force_single_piper_robot")
    with pytest.raises(ValueError, match="unknown keys"):
        module.ForceSinglePiperRobot(
            {
                "ports": {"master": "master_left", "slave": "right"},
                "force_feedback": {**_PROFILE, "kp": [2.0] * 6},
            }
        )


def test_force_pair_connects_both_arms_as_followers(force_robot):
    master = force_robot.controllers["master"]
    slave = force_robot.controllers["slave"]
    assert (master.port, master.role) == ("master_left", "follower")
    assert (slave.port, slave.role) == ("right", "follower")


def test_absolute_feedback_cross_drives_both_arms(force_robot):
    force_robot.controllers["master"].q[:] = 0.1
    force_robot.controllers["slave"].q[:] = 0.0
    assert force_robot.enable_teleop("absolute") is True

    force_robot.step()

    master_cmd = force_robot.controllers["master"].joint_calls[-1]
    slave_cmd = force_robot.controllers["slave"].joint_calls[-1]
    assert np.allclose(master_cmd, 0.0)  # 从臂没动 → 主臂被反向拉回
    assert np.allclose(slave_cmd, 0.1)  # 主臂向前 → 从臂跟随完整实测位置


def test_delta_feedback_cross_drives_both_arms_from_enable_anchors(force_robot):
    force_robot.controllers["master"].q[:] = 0.2
    force_robot.controllers["slave"].q[:] = 0.1
    assert force_robot.enable_teleop("delta") is True

    force_robot.controllers["master"].q[:] = 0.3
    force_robot.controllers["slave"].q[:] = 0.05
    force_robot.step()

    assert np.allclose(force_robot.controllers["master"].joint_calls[-1], 0.15)
    assert np.allclose(force_robot.controllers["slave"].joint_calls[-1], 0.2)


def test_absolute_feedback_rejects_bad_initial_alignment(force_robot):
    force_robot.controllers["master"].q[:] = 1.0
    assert force_robot.enable_teleop("absolute") is False
    assert force_robot.teleop_enabled is False
    assert force_robot.controllers["master"].role == "follower"
    assert "max_initial_error" in force_robot.last_error


def test_enable_initializes_both_follower_commands_from_measured_joints(force_robot):
    force_robot.controllers["master"].q[:] = 0.1
    force_robot.controllers["slave"].q[:] = -0.1
    assert force_robot.enable_teleop("delta") is True
    for side, expected in (("master", 0.1), ("slave", -0.1)):
        assert np.allclose(force_robot.controllers[side].joint_calls[-1], expected)


def test_reset_moves_both_arms_from_measured_positions_with_offset(force_robot):
    force_robot.controllers["master"].q[:] = 0.2
    force_robot.controllers["slave"].q[:] = -0.2
    force_robot.teleop_joint_offset[:] = 0.1

    force_robot.reset()
    assert np.allclose(force_robot._master_reset_target, -0.1)
    force_robot.step()

    master_cmd = force_robot.controllers["master"].joint_calls[-1]
    slave_cmd = force_robot.controllers["slave"].joint_calls[-1]
    assert np.allclose(master_cmd, 0.1)
    assert np.allclose(slave_cmd, -0.1)
    assert force_robot.controllers["slave"].gripper == 1.0


def test_teleop_execute_and_safe_stop_cancel_pair_reset(force_robot):
    force_robot.reset()
    assert force_robot.enable_teleop("delta") is True
    assert force_robot._master_reset_target is None

    force_robot.reset()
    force_robot.execute(np.zeros(6))
    assert force_robot._master_reset_target is None

    force_robot.reset()
    force_robot.safe_stop()
    assert force_robot._master_reset_target is None
    assert force_robot.target_action is None


def test_pair_reset_read_failure_stops_both_arm_reset(force_robot):
    force_robot.reset()
    force_robot.controllers["master"].q = None

    force_robot.step()

    assert force_robot._master_reset_target is None
    assert force_robot.target_action is None
    assert "pair reset failed" in force_robot.last_error


def test_missing_master_reading_decouples_but_keeps_follower_roles(force_robot):
    assert force_robot.enable_teleop("delta") is True
    force_robot.controllers["master"].q = None

    force_robot.step()

    assert force_robot.teleop_enabled is False
    assert force_robot.target_action is None
    assert force_robot.controllers["master"].role == "follower"
    assert force_robot.controllers["slave"].role == "follower"
    assert "decoupled" in force_robot.last_error
