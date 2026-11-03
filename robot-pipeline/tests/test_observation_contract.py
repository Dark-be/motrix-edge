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

"""BaseRobot 观测组装契约。"""

import numpy as np
from robot.base_robot import BaseRobot
from server.contract_server import _robot_observation_keys


class _Robot(BaseRobot):
    QPOS = 2
    POSE = 6
    GRIPPER = 1

    def get_observation_qpos(self):
        return np.array([1.0, 2.0])

    def get_observation_images(self):
        return []

    def get_observation_pose(self, qpos=None):
        return np.arange(self.POSE, dtype=float)

    def get_observation_gripper(self):
        return np.asarray([0.5], dtype=float)


class _SingleArmRobot(BaseRobot):
    QPOS = 6
    GRIPPER = 1

    def __init__(self):
        super().__init__({"init_joint": [0.0] * self.QPOS, "init_gripper": [1.0]})
        self.applied = None

    def get_observation_qpos(self):
        return np.zeros(self.QPOS)

    def get_observation_gripper(self):
        return np.ones(self.GRIPPER)

    def get_observation_images(self):
        return []

    def _apply_action(self, action):
        self.applied = action.copy()


def test_build_observation_includes_timestamp_key():
    robot = _Robot({"init_joint": [0.0, 0.0]})
    robot.sample_qpos()

    observation = robot.build_observation()

    assert observation is not None
    assert observation[robot.KEY_QPOS].tolist() == [1.0, 2.0, 0.5]
    assert isinstance(observation[robot.KEY_TIMESTAMP], float)


def test_single_thread_observation_includes_timestamp_key():
    robot = _Robot({"init_joint": [0.0, 0.0]})

    observation = robot.get_observation()

    assert isinstance(observation[robot.KEY_TIMESTAMP], float)


def test_discover_observation_keys_include_pose_when_published():
    robot = _Robot({"init_joint": [0.0, 0.0]})

    assert _robot_observation_keys(robot) == [robot.KEY_QPOS, robot.KEY_ACTION, robot.KEY_POSE]


def test_robot_name_falls_back_for_null_or_blank_config():
    assert _Robot({"name": None, "init_joint": [0.0, 0.0]}).name == _Robot.NAME
    assert _Robot({"name": "  ", "init_joint": [0.0, 0.0]}).name == _Robot.NAME


def test_required_devices_reject_missing_or_blank_values():
    robot = _Robot({"init_joint": [0.0, 0.0], "ports": {"left": "can0", "right": ""}})

    try:
        robot._required_devices("ports", ("left", "right"))
    except ValueError as exc:
        assert "right" in str(exc)
    else:
        raise AssertionError("blank required device must be rejected")


def test_required_devices_returns_trimmed_declared_values():
    robot = _Robot(
        {
            "init_joint": [0.0, 0.0],
            "ports": {"left": " can0 ", "right": "can1", "typo": "ignored"},
        }
    )

    assert robot._required_devices("ports", ("left", "right")) == {"left": "can0", "right": "can1"}


def test_step_supports_single_arm_layout():
    robot = _SingleArmRobot()
    robot.set_target_action(np.array([0.2] * 6))
    robot.set_target_gripper(np.array([0.5]))

    robot.step()

    np.testing.assert_allclose(robot.applied, [0.1] * 6 + [0.5])
