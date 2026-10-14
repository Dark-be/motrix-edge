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

"""``robot.teleop.joint_offset``：主臂常量偏置的配置解析与映射语义（纯 numpy，不需要硬件 SDK）。

钉住三件事：① 缺省（未配置）恒等——行为与没有这个键时一致；② ``absolute`` 下偏置叠在读数上、
``delta`` 下由锚点抵消（不进入增量）；③ 配置写错（长度不符 / 非有限值 / 未知臂）一律按现场值
口径报错，不静默忽略。设计见 ``wiki/design/robot_pipeline_teleop.md``。
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_ARMS = ("left", "right")
_MASTER = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, -0.2, -0.3, -0.4, -0.5, -0.6, -0.7]  # 主臂读数（每臂 6）
_OFFSET = [0.0, 0.1, 0.0, 0.0, 0.0, 0.0]


@pytest.fixture(scope="module")
def base_robot():
    """导入 robot-pipeline 的 ``BaseRobot``（只依赖 numpy 与契约常量）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.base_robot")


def _robot(base_robot, config: dict | None = None):
    """双臂 BaseRobot 桩：主臂读数固定，``step()`` 一次 = 一拍遥操作刷新。"""

    class _Stub(base_robot.BaseRobot):
        NAME = "stub"
        ARM_NAMES = _ARMS
        QPOS = 12
        POSE = 12
        GRIPPER = 2
        ARM_CONTROLLERS = {"left": "arm_left", "right": "arm_right"}
        ACTION_SPACES = (
            base_robot.BaseRobot.ACTION_SPACE_JOINT,
            base_robot.BaseRobot.ACTION_SPACE_POSE,
            base_robot.BaseRobot.ACTION_SPACE_POSE_DELTA,
            base_robot.BaseRobot.ACTION_SPACE_GRIPPER,
        )

        def _get_teleop_target(self):
            return self.master.copy()

        def _get_teleop_gripper(self):
            return np.asarray([1.0, 1.0], dtype=np.float64)

        def get_observation_qpos(self):
            return np.zeros(self.QPOS)

        def get_observation_gripper(self):
            return np.ones(self.GRIPPER)

        def _apply_action(self, action):
            self.applied = np.asarray(action, dtype=np.float64).copy()

        def _pose_of_joints(self, joints):
            return np.asarray(joints, dtype=np.float64).reshape(-1).copy()

        def _solve_pose(self, pose, seed):
            if self.fail_ik:
                raise base_robot.CartesianActionError("synthetic IK failure")
            return np.asarray(pose, dtype=np.float64).reshape(-1).copy()

    robot = _Stub({"init_joint": [0.0] * 12, **(config or {})})
    robot.applied = None
    robot.master = np.asarray(_MASTER, dtype=np.float64)
    robot.fail_ik = False
    return robot


def _target_joints(robot):
    return robot.target_action[: robot.QPOS]


def _offset_of(robot, arm: str):
    per_arm = robot.QPOS // len(robot.ARM_NAMES)
    index = robot.state_arms().index(arm)
    return robot.teleop_joint_offset[index * per_arm : (index + 1) * per_arm]


# ---- 解析：缺省 / 两种取值形态 / 逐臂独立 -------------------------------------------------


def test_absent_config_is_identity(base_robot):
    assert np.allclose(_robot(base_robot).teleop_joint_offset, 0.0)


def test_flat_list_applies_to_every_arm(base_robot):
    robot = _robot(base_robot, {"teleop": {"joint_offset": _OFFSET}})
    assert np.allclose(_offset_of(robot, "left"), _OFFSET)
    assert np.allclose(_offset_of(robot, "right"), _OFFSET)


def test_per_arm_mapping_keeps_arms_independent(base_robot):
    robot = _robot(base_robot, {"teleop": {"joint_offset": {"left": _OFFSET}}})
    assert np.allclose(_offset_of(robot, "left"), _OFFSET)
    assert np.allclose(_offset_of(robot, "right"), 0.0)  # 未列出的臂 = 无偏置


# ---- 映射：absolute 叠加、delta 抵消 ------------------------------------------------------


def test_absolute_maps_master_plus_offset(base_robot):
    robot = _robot(base_robot, {"teleop": {"joint_offset": _OFFSET}})
    assert robot.enable_teleop("absolute")
    robot.step()
    expected = list(_MASTER)
    expected[1] += 0.1  # 左臂 j2
    expected[7] += 0.1  # 右臂 j2（每臂 6 维）
    assert np.allclose(_target_joints(robot), expected)


def test_absolute_without_offset_is_master_verbatim(base_robot):
    robot = _robot(base_robot)
    assert robot.enable_teleop("absolute")
    robot.step()
    assert np.allclose(_target_joints(robot), _MASTER)


def test_delta_cancels_offset(base_robot):
    plain, biased = _robot(base_robot), _robot(base_robot, {"teleop": {"joint_offset": _OFFSET}})
    for robot in (plain, biased):
        assert robot.enable_teleop("delta")
        robot.step()  # 首拍采样锚点：增量恒 0（偏置已被零化）
    assert np.allclose(_target_joints(plain), _target_joints(biased))
    assert np.allclose(_target_joints(biased), 0.0)  # 从臂未被主臂读数 / 偏置拉动


# ---- 位姿跟随：轴白名单 / absolute / delta / 失败保持 -------------------------------------


def _set_slave_action(robot, joints):
    robot.action = np.concatenate([np.asarray(joints, dtype=np.float64), np.ones(robot.GRIPPER)])


def test_pose_absolute_locks_unfollowed_roll(base_robot):
    axes = ["x", "y", "z", "pitch", "yaw"]
    robot = _robot(base_robot, {"teleop": {"follow_space": "pose", "follow_axes": axes}})
    slave = np.zeros(robot.QPOS)
    slave[3], slave[9] = 0.9, -0.8
    _set_slave_action(robot, slave)

    assert robot.enable_teleop("absolute")
    robot.step()

    expected = robot.master.copy()
    expected[3], expected[9] = slave[3], slave[9]
    assert np.allclose(_target_joints(robot), expected)


def test_pose_delta_applies_only_followed_axes_from_anchor(base_robot):
    axes = ["x", "y", "z", "pitch", "yaw"]
    robot = _robot(base_robot, {"teleop": {"follow_space": "pose", "follow_axes": axes}})
    slave = np.asarray([0.1, 0.0, 0.2, 0.7, 0.1, -0.2, -0.1, 0.2, 0.0, -0.6, -0.1, 0.3])
    _set_slave_action(robot, slave)

    assert robot.enable_teleop("delta")
    robot.step()  # 首拍采锚点：目标等于从臂锚点
    assert np.allclose(_target_joints(robot), slave)

    delta = np.asarray([0.02, -0.03, 0.04, 0.5, 0.06, -0.07] * 2)
    robot.master += delta
    robot.step()
    expected = slave + delta
    expected[3], expected[9] = slave[3], slave[9]  # roll 未跟随
    assert np.allclose(_target_joints(robot), expected)


def test_runtime_pose_axes_override_only_one_teleop_enable(base_robot):
    configured = ["x", "y", "z", "pitch", "yaw"]
    runtime = ["x", "z", "yaw"]
    robot = _robot(base_robot, {"teleop": {"follow_space": "pose", "follow_axes": configured}})

    assert robot.enable_teleop("absolute", runtime)
    assert robot.teleop_follow_axes == tuple(runtime)
    robot.disable_teleop()

    assert robot.enable_teleop("absolute")
    assert robot.teleop_follow_axes == tuple(configured)


def test_runtime_follow_space_override_only_one_teleop_enable(base_robot):
    robot = _robot(base_robot)

    assert robot.enable_teleop("absolute", follow_space="pose")
    assert robot.teleop_follow_space == "pose"
    assert robot.teleop_follow_axes == robot.TELEOP_POSE_AXES
    robot.disable_teleop()

    assert robot.enable_teleop("absolute")
    assert robot.teleop_follow_space == "joint"
    assert robot.teleop_follow_axes == ()


def test_runtime_follow_space_rejects_unknown_value(base_robot):
    robot = _robot(base_robot)
    with pytest.raises(ValueError, match="follow_space"):
        robot.enable_teleop("absolute", follow_space="cartesian")


def test_runtime_pose_axes_reject_empty_and_joint_follow(base_robot):
    pose_robot = _robot(base_robot, {"teleop": {"follow_space": "pose"}})
    with pytest.raises(ValueError, match="nonempty unique subset"):
        pose_robot.enable_teleop("absolute", [])

    joint_robot = _robot(base_robot)
    with pytest.raises(ValueError, match="only valid for pose"):
        joint_robot.enable_teleop("absolute", ["x"])


def test_pose_mapping_failure_keeps_previous_target(base_robot):
    robot = _robot(base_robot, {"teleop": {"follow_space": "pose"}})
    _set_slave_action(robot, np.zeros(robot.QPOS))
    assert robot.enable_teleop("delta")
    robot.step()
    before = robot.target_action.copy()

    robot.master += 0.1
    robot.fail_ik = True
    robot.step()

    assert np.array_equal(robot.target_action, before)
    assert robot.teleop_mapping_failures == 1


# ---- 配置写错：报错，不静默忽略 -------------------------------------------------------------


def test_rejects_wrong_length(base_robot):
    with pytest.raises(ValueError, match="joint_offset"):
        _robot(base_robot, {"teleop": {"joint_offset": [0.0, 0.1]}})


def test_rejects_unknown_arm(base_robot):
    with pytest.raises(ValueError, match="未知臂"):
        _robot(base_robot, {"teleop": {"joint_offset": {"middle": [0.0] * 6}}})


def test_rejects_non_finite(base_robot):
    with pytest.raises(ValueError, match="non-finite"):
        _robot(base_robot, {"teleop": {"joint_offset": [float("nan")] + [0.0] * 5}})


def test_rejects_pose_axes_in_joint_follow(base_robot):
    with pytest.raises(ValueError, match="only valid"):
        _robot(base_robot, {"teleop": {"follow_space": "joint", "follow_axes": ["x"]}})


@pytest.mark.parametrize("axes", [[], ["x", "x"], ["x", "spin"], "x,y"])
def test_rejects_invalid_pose_axes(base_robot, axes):
    with pytest.raises(ValueError, match="follow_axes"):
        _robot(base_robot, {"teleop": {"follow_space": "pose", "follow_axes": axes}})


def test_rejects_pose_follow_without_pose_capability(base_robot):
    class _JointOnly(base_robot.BaseRobot):
        ARM_NAMES = _ARMS
        QPOS = 12
        GRIPPER = 2

    with pytest.raises(ValueError, match="needs pose action capability"):
        _JointOnly({"init_joint": [0.0] * 12, "teleop": {"follow_space": "pose"}})
