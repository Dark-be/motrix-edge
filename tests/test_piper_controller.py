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

"""PiperController 的关节限位把关测试（假 SDK，无硬件）。

钉住「下发前显式限位判断」：``set_joint`` 逐关节裁到软限位（与 ``robot.kinematics`` 的 IK
**同一份表**），越界值一帧都不交给 SDK——SDK 的 ``set_joint_limits_enabled(True)`` 会对越界值
报错并打印；限位内原值透传；同一组超限关节只告警一条（30Hz 调用不刷屏）。
"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


def _stub_hardware_sdks() -> None:
    """pyAgxArm 只装在机器人端：本机离线测试用占位模块。"""
    if "pyAgxArm" not in sys.modules:
        sdk = types.ModuleType("pyAgxArm")
        sdk.AgxArmFactory = type("AgxArmFactory", (), {})
        sdk.ArmModel = type("ArmModel", (), {})
        sdk.PiperFW = type("PiperFW", (), {"V188": "V188"})
        sdk.create_agx_arm_config = lambda *args, **kwargs: {}
        sys.modules["pyAgxArm"] = sdk


@pytest.fixture(scope="module")
def piper_module():
    _stub_hardware_sdks()
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.controller.piper_controller")


class _FakeArm:
    """假 pyAgxArm：记录每次 ``move_mit`` 的实参。"""

    def __init__(self):
        self.commands: list[dict] = []

    def move_mit(self, **kwargs):
        self.commands.append(kwargs)


def _controller(module):
    controller = module.PiperController("arm")
    controller.robot = _FakeArm()  # 绕过 connect（_require_robot 只看 self.robot 是否就绪）
    return controller


def _limits() -> np.ndarray:
    """限位的唯一来源（``robot.kinematics``）：解算裁切与下发裁切用的是同一份。"""
    return importlib.import_module("robot.kinematics").PIPER_JOINT_LIMITS


def _sent(controller) -> np.ndarray:
    return np.array([command["p_des"] for command in controller.robot.commands], dtype=np.float64)


def test_clipping_uses_the_kinematics_limits(piper_module):
    """下发前的裁切范围必须就是 ``robot.kinematics`` 那份（单一事实来源）——否则解算与下发不一致。"""
    controller = _controller(piper_module)

    controller.set_joint(np.array([9.0, 9.0, -9.0, 9.0, 9.0, 9.0]))

    sent = _sent(controller)
    assert np.all(sent >= _limits()[:, 0] - 1e-12) and np.all(sent <= _limits()[:, 1] + 1e-12)


def test_set_joint_clips_every_joint_to_limits(piper_module):
    """任一关节越界都在下发前裁到边界（以前只裁 j3，其它关节会直接交给 SDK 报错）。"""
    controller = _controller(piper_module)
    limits = _limits()

    controller.set_joint(np.array([9.0, -9.0, 9.0, 9.0, 9.0, 9.0]))

    assert len(controller.robot.commands) == 6
    sent = _sent(controller)
    assert np.all(sent >= limits[:, 0] - 1e-12) and np.all(sent <= limits[:, 1] + 1e-12)
    assert sent[0] == pytest.approx(limits[0, 1])
    assert sent[1] == pytest.approx(limits[1, 0])
    assert sent[2] == pytest.approx(limits[2, 1])
    assert sent[3] == pytest.approx(limits[3, 1])


def test_set_joint_keeps_values_within_limits(piper_module):
    """限位内的指令原样透传（不因裁切改变正常动作）。"""
    controller = _controller(piper_module)
    target = np.array([0.1, 0.4, -0.5, 0.2, 0.3, -0.4])

    controller.set_joint(target.copy())

    assert np.allclose(_sent(controller), target)


def test_set_joint_clips_in_place(piper_module):
    """就地裁切（既有语义）：调用方传 copy 时自身状态不受影响，传进来的数组被改写。"""
    controller = _controller(piper_module)
    target = np.full(6, 9.0)

    controller.set_joint(target)

    assert np.allclose(target, _limits()[:, 1])


def test_set_joint_warns_once_per_over_limit_group(piper_module, monkeypatch):
    """同一组超限关节只告警一条；回到限位内后再次超限会重新告警。"""
    controller = _controller(piper_module)
    warnings: list[str] = []
    monkeypatch.setattr(
        piper_module,
        "debug_print",
        lambda name, message, level="INFO": warnings.append(f"{level}:{message}"),
    )

    controller.set_joint(np.full(6, 9.0))
    assert sum(entry.startswith("WARNING") for entry in warnings) == 1
    controller.set_joint(np.full(6, 9.0))
    assert sum(entry.startswith("WARNING") for entry in warnings) == 1  # 同一组：不刷屏

    controller.set_joint(np.array([0.0, 0.0, -0.5, 0.0, 0.0, 0.0]))  # 回到限位内
    controller.set_joint(np.full(6, 9.0))
    assert sum(entry.startswith("WARNING") for entry in warnings) == 2


def test_set_joint_forwards_torque_ff(piper_module):
    """``t_ff`` 按关节下发；缺省用 MIT 配置的 ``t_ref``（当前为 0）。"""
    controller = _controller(piper_module)
    torque = np.arange(6, dtype=np.float64) * 0.5

    controller.set_joint(np.zeros(6), torque_ff=torque)
    assert [command["t_ff"] for command in controller.robot.commands] == pytest.approx(torque.tolist())

    controller.robot.commands.clear()
    controller.set_joint(np.zeros(6))
    assert all(command["t_ff"] == 0.0 for command in controller.robot.commands)


def test_set_joint_rejects_bad_dimensions(piper_module):
    """维数不符不下发（宁可不动，也不给 SDK 半个指令）。"""
    controller = _controller(piper_module)

    controller.set_joint(np.zeros(5))
    controller.set_joint(np.zeros(6), torque_ff=np.zeros(5))

    assert controller.robot.commands == []


def test_controller_has_no_sdk_pose_io(piper_module):
    """控制器**不实现** SDK 位姿读写：``get_position`` / ``set_position`` / ``move_p`` 均无路可达。

    位姿转换是**静态纯函数**（``joint_to_pose`` / ``pose_to_joint``，robot 层调用），不是 SDK 的
    ``move_p``：调用它们只解算 / 只正解，一帧都不下发。SDK 侧位姿只在标定脚本
    （``verify_cartesian.py``）里直接读。
    """
    assert "get_position" not in piper_module.PiperController.__dict__
    assert "set_position" not in piper_module.PiperController.__dict__
    assert isinstance(piper_module.PiperController.__dict__["pose_to_joint"], staticmethod)
    with pytest.raises(NotImplementedError):
        piper_module.PiperController("arm").get_position()

    result = piper_module.PiperController.pose_to_joint(
        piper_module.PiperController.joint_to_pose(np.zeros(6)), np.zeros(6)
    )
    assert result.ok  # 正解 → 解算往返（零位形对应的位姿）


# ---- 运动学（静态转换函数：robot 层调用，无实例状态）----------------------------------


def test_joint_to_pose_and_pose_to_joint_share_one_model(piper_module):
    """``joint_to_pose``（正解）与 ``pose_to_joint``（解算，只解算不下发）同一模型 → 往返一致。"""
    joints = np.array([0.3, 0.8, -0.9, 0.2, 0.5, -0.3])

    pose = piper_module.PiperController.joint_to_pose(joints)
    result = piper_module.PiperController.pose_to_joint(pose, seed=joints)

    assert result.ok and result.q is not None
    assert np.allclose(piper_module.PiperController.joint_to_pose(result.q)[:3], pose[:3], atol=1e-6)
    assert np.allclose(result.q, joints, atol=1e-3)


def test_pose_to_joint_failure_is_reported_not_silent(piper_module):
    """解不出来 → ``ok=False`` + 原因（不静默给一个凑合解）；可解时不传 seed 也能解。"""
    unreachable = np.array([5.0, 5.0, 5.0, 0.0, 0.0, 0.0])
    failed = piper_module.PiperController.pose_to_joint(unreachable, seed=np.zeros(6))
    assert not failed.ok and failed.q is None and failed.reason

    reachable = piper_module.PiperController.joint_to_pose(np.zeros(6))
    assert piper_module.PiperController.pose_to_joint(reachable).ok  # 缺省种子 = 零位形


def test_ik_config_overrides_are_honoured(piper_module):
    """``**ik_config`` 覆盖真的参与解算（此处把迭代上限压到 1）。"""
    from_pose = piper_module.PiperController.joint_to_pose
    target = from_pose(np.array([0.3, 0.8, -0.9, 0.2, 0.5, -0.3]))

    result = piper_module.PiperController.pose_to_joint(target, seed=np.zeros(6), max_iters=1)

    assert not result.ok or result.iterations <= 1


def test_set_joint_only_sends_joint_angles(piper_module):
    """控制器只下关节角：解算后的目标下发时 ``t_ff`` 仍是 MIT 缺省值（控制器不自算力矩）。"""
    controller = _controller(piper_module)
    q = np.array([0.0, 0.8, -1.0, 0.0, 0.5, 0.0])
    target = piper_module.PiperController.joint_to_pose(q)
    target[2] += 0.05  # 抬高 5cm

    assert piper_module.PiperController.pose_to_joint(target, seed=q).ok
    controller.set_joint(q.copy())

    assert controller.robot.commands[-1]["t_ff"] == 0.0  # 无前馈：kp / kd / t_ref 全用 MIT 缺省值
