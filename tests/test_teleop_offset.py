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

_REPO_ROOT = Path(__file__).resolve().parents[1]
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

        def _get_teleop_target(self):
            return np.asarray(_MASTER, dtype=np.float64)

        def _get_teleop_gripper(self):
            return np.asarray([1.0, 1.0], dtype=np.float64)

        def get_observation_qpos(self):
            return np.zeros(self.QPOS)

        def get_observation_gripper(self):
            return np.ones(self.GRIPPER)

        def _apply_action(self, action):
            self.applied = np.asarray(action, dtype=np.float64).copy()

    robot = _Stub({"init_joint": [0.0] * 12, **(config or {})})
    robot.applied = None
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
