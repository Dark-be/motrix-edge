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

"""DualPiperRobot 位姿观测与位姿动作下发的离线测试（硬件 SDK 用占位模块）。

钉住两件事：

- **位姿观测**：``observations/pose`` = **同一拍关节角经 FK 解算**（与位姿目标解算共用
  ``robot.kinematics``，观测与目标同系），读不到关节 → NaN 让 edge 丢弃该帧；
- **位姿动作下发**：``action_space=pose`` 解算成关节目标后，仍走限速 + MIT 关节通路
  （不经 ``move_p``）；解算失败 / 维度不符 → 报错且**不改既有目标**；下发只有关节角。

底层始终只有**一条**目标向量 ``[关节段 | 夹爪段]``：三个动作空间各写自己那一段。
"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_QPOS = [0.0] * 12  # joint 空间：每臂 6 关节角 × 2 臂
_GRIPPER = [1.0, 1.0]  # gripper 空间：每臂 1 夹爪 × 2 臂（1 = 张开）
# 硬件接线必填（robot.ports / robot.cameras）：离线测试给占位值（不连真机）
_DEVICES = {
    "ports": {"left_master": "can0", "right_master": "can1", "left": "can2", "right": "can3"},
    "cameras": {"cam_head": "cam-head", "cam_left_wrist": "cam-lw", "cam_right_wrist": "cam-rw"},
}
_TARGET_JOINTS = np.array([0.3, 0.8, -0.9, 0.2, 0.5, -0.3])  # 限位内的可用位形（IK 往返用）


def _stub_hardware_sdks() -> None:
    """硬件 SDK 只装在机器人端：本机离线测试用占位模块（导入期只需这些名字存在）。"""
    if "pyAgxArm" not in sys.modules:
        sdk = types.ModuleType("pyAgxArm")
        sdk.AgxArmFactory = type("AgxArmFactory", (), {})
        sdk.ArmModel = type("ArmModel", (), {})
        sdk.PiperFW = type("PiperFW", (), {"V188": "V188"})
        sdk.create_agx_arm_config = lambda *args, **kwargs: {}
        sys.modules["pyAgxArm"] = sdk
    for name in ("pyrealsense2", "v4l2"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


@pytest.fixture(scope="module")
def dual_piper():
    """导入 robot-pipeline 的 DualPiperRobot。"""
    _stub_hardware_sdks()
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.dual_piper_robot")


def _kinematics():
    """运动学模块（``robot.kinematics``）：测试里造可达位姿，并给假控制器提供真实 FK / IK。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.kinematics")


def _cartesian_error():
    """``CartesianActionError``：定义在 ``robot.base_robot``（robot 层抛，server 映射 422）。"""
    return importlib.import_module("robot.base_robot").CartesianActionError


class _FakeArm:
    """假 PiperController：**只管关节 / 夹爪读写**（与真控制器一致——位姿解算是静态函数，
    不经控制器实例），并记录每次下发。"""

    def __init__(self, joint=None, gripper=1.0):
        self.joint = None if joint is None else np.asarray(joint, dtype=np.float64)
        self.gripper = None if gripper is None else float(gripper)
        self.joint_calls: list[np.ndarray] = []
        self.torque_ff_calls: list[np.ndarray | None] = []
        self.gripper_calls: list[float] = []

    def get_joint(self):
        return None if self.joint is None else self.joint.copy()

    def get_gripper(self):
        return None if self.gripper is None else self.gripper

    def set_joint(self, joint, torque_ff=None):
        values = np.asarray(joint, dtype=np.float64).copy()
        # 真实 PiperController 会逐关节裁到软限位（此处只演示 j3，足以钉住"下发的是解出的关节角"）
        values[2] = np.clip(values[2], -2.96706, 0.0)
        self.joint_calls.append(values)
        self.torque_ff_calls.append(None if torque_ff is None else np.asarray(torque_ff, dtype=np.float64))

    def set_gripper(self, value):
        self.gripper_calls.append(float(value))


def _robot(module, *, left_joint=(0.0,) * 6, right_joint=(0.0,) * 6):
    robot = module.DualPiperRobot(robot_config={"init_joint": _QPOS, "init_gripper": _GRIPPER, **_DEVICES})
    robot.controllers = {
        "left_arm": _FakeArm(left_joint),
        "right_arm": _FakeArm(right_joint),
        "left_master": _FakeArm(None),
        "right_master": _FakeArm(None),
    }
    return robot


def _cartesian_action(left_joint, right_joint):
    """由两臂关节角经 FK 生成一份**可达**的位姿动作（每臂 ``xyz + rpy``，不含夹爪）。"""
    kinematics = _kinematics().PiperKinematics()
    left = kinematics.pose(np.asarray(left_joint, dtype=np.float64))
    right = kinematics.pose(np.asarray(right_joint, dtype=np.float64))
    return np.concatenate([left, right])


def _gripper_action(left=0.8, right=0.2):
    """夹爪动作（每臂 1 维，归一化 [0, 1]）——独立空间，与位姿分开下发。"""
    return np.array([left, right], dtype=np.float64)


# ---- 声明 / 布局 --------------------------------------------------------------


def test_pose_and_action_space_layout(dual_piper):
    """四个动作空间（joint / pose / pose_delta / gripper）：前三个同形，只能靠空间名区分。"""
    robot = _robot(dual_piper)
    assert robot.QPOS == 12 and robot.GRIPPER == 2
    assert robot.POSE == 12 and robot.POSE_DIM_PER_ARM == 6
    assert robot.ARM_NAMES == ("left", "right")
    assert robot.action_dims() == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
    assert dual_piper.DualPiperRobot.ACTION_SPACES == ("joint", "pose", "pose_delta", "gripper")
    assert robot.normalize_action_space(None) == "joint"
    with pytest.raises(ValueError):
        robot.normalize_action_space("wrench")


# ---- 位姿观测（FK，与 IK 同源）------------------------------------------------


def test_observation_pose_is_fk_of_same_tick_joints(dual_piper):
    """位姿 = 同一拍关节角的 FK（与笛卡尔 IK 同一模型），左先右后拼接。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS, right_joint=np.zeros(6))
    pose = robot.get_observation_pose(robot.get_observation_qpos())

    expected_left = _kinematics().PiperKinematics().pose(_TARGET_JOINTS)
    expected_right = _kinematics().PiperKinematics().pose(np.zeros(6))
    assert pose.shape == (12,)
    assert np.allclose(pose[:6], expected_left)
    assert np.allclose(pose[6:], expected_right)


def test_observation_pose_follows_joint_motion(dual_piper):
    """关节动 → 位姿跟着动（观测与 IK 同源，笛卡尔闭环才有意义）。"""
    still = _robot(dual_piper, left_joint=np.zeros(6))
    moved = _robot(dual_piper, left_joint=_TARGET_JOINTS)

    assert not np.allclose(still.get_observation_pose()[:6], moved.get_observation_pose()[:6])


def test_observation_pose_publishes_nan_on_read_failure(dual_piper):
    """关节读数不可用 / 非有限 → NaN 向量（写进位姿区后被 edge 丢弃），而不是 None 留旧值。"""
    robot = _robot(dual_piper, left_joint=None)
    assert np.all(np.isnan(robot.get_observation_pose()))

    robot = _robot(dual_piper, left_joint=np.full(6, np.nan))
    assert np.all(np.isnan(robot.get_observation_pose()))


# ---- 笛卡尔下发（IK → 关节目标 → MIT）-----------------------------------------


def test_cartesian_action_resolves_to_joint_target(dual_piper):
    """位姿解算成关节目标后**只写关节段**；夹爪是另一段，未下发就不动。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS, right_joint=np.zeros(6))
    action = _cartesian_action(_TARGET_JOINTS, np.zeros(6))

    robot.execute(action, "pose")

    assert robot.target_action.shape == (14,)  # 底层一条：[关节段 12 | 夹爪段 2]
    assert np.allclose(robot.target_action[:6], _TARGET_JOINTS, atol=1e-3)
    assert np.allclose(robot.target_action[6:12], np.zeros(6), atol=1e-3)
    assert np.allclose(robot.target_action[12:], _GRIPPER)  # 夹爪段保持 init_gripper
    # 解算与下发都只在 robot 层：**robot 自己不持有位姿状态**（无 cartesian_target / last_ik 字段）
    assert not hasattr(robot, "cartesian_target") and not hasattr(robot, "last_ik")


def test_gripper_action_only_touches_gripper_segment(dual_piper):
    """夹爪空间只写夹爪段：不会把关节目标拉回 home（三个空间互不覆盖）。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS)
    robot.execute(_cartesian_action(_TARGET_JOINTS, np.zeros(6)), "pose")
    joints_before = robot.target_action[:12].copy()

    robot.execute(_gripper_action(), "gripper")

    assert np.allclose(robot.target_action[:12], joints_before)  # 关节段未被动过
    assert np.allclose(robot.target_action[12:], [0.8, 0.2])


def test_cartesian_action_is_sent_through_mit(dual_piper):
    """解算结果经 ``step()`` 限速后仍走 ``set_joint``（MIT）——底层控制模式零改动。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS, right_joint=np.zeros(6))
    robot.execute(_cartesian_action(_TARGET_JOINTS, np.zeros(6)), "pose")

    robot.step()

    left = robot.controllers["left_arm"]
    right = robot.controllers["right_arm"]
    assert left.joint_calls and np.allclose(left.joint_calls[-1], _TARGET_JOINTS, atol=1e-3)
    assert right.joint_calls and np.allclose(right.joint_calls[-1], np.zeros(6), atol=1e-3)
    assert left.torque_ff_calls[-1] is None  # 只下关节角：t_ff / kp / kd 全用 MIT 缺省值
    assert left.gripper_calls and right.gripper_calls


def test_cartesian_action_rejects_unreachable_target(dual_piper):
    """解不出来 → 报错且**不改既有目标**（机械臂保持原动作）。"""
    robot = _robot(dual_piper)
    robot.set_target_action(np.zeros(12))
    before = robot.target_action.copy()
    unreachable = np.concatenate([np.array([5.0, 5.0, 5.0, 0.0, 0.0, 0.0])] * 2)

    with pytest.raises(_cartesian_error(), match="pose target rejected"):
        robot.execute(unreachable, "pose")

    assert np.allclose(robot.target_action, before)  # 失败一帧都不下发


def test_cartesian_action_rejects_bad_input(dual_piper):
    robot = _robot(dual_piper)
    with pytest.raises(_cartesian_error()):
        robot.execute(np.zeros(11), "pose")  # 维度不符（每臂 6，双臂 12）
    with pytest.raises(_cartesian_error()):
        robot.execute(np.full(12, np.nan), "pose")


def test_joint_action_is_forwarded_after_pose_action(dual_piper):
    """位姿动作之后转回关节动作：目标仍然是关节空间（同一条 MIT 通路）。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS)
    robot.execute(_cartesian_action(_TARGET_JOINTS, np.zeros(6)), "pose")

    robot.execute(np.zeros(12))

    assert np.allclose(robot.target_action[:12], np.zeros(12))
    assert np.allclose(robot.target_action[12:], _GRIPPER)  # 夹爪段仍未被关节命令碰到


def test_teleop_master_read_exception_keeps_target_and_counts(dual_piper, monkeypatch):
    """主臂读取**异常**：``step()`` 不抛（否则控制线程 _step_failed → 连续 10 拍后 health 不健康），
    target 不变，只计数。"""
    robot = _robot(dual_piper)
    robot.set_target_action(np.zeros(12))
    before = robot.target_action.copy()
    robot.enable_teleop("delta")

    def _boom():
        raise RuntimeError("can bus down")

    monkeypatch.setattr(robot.controllers["left_master"], "get_joint", _boom)
    robot.step()  # 不应抛异常

    assert np.allclose(robot.target_action, before)
    assert robot.teleop_read_failures == 1
    robot.step()
    assert robot.teleop_read_failures == 2


def test_teleop_master_read_none_counts_and_recovers(dual_piper):
    """主臂读数为 ``None``（未使能 / 读不到）：同样只计数；读数恢复后正常刷新 target（delta 锚点 0 起步）。"""
    robot = _robot(dual_piper)
    robot.set_target_action(np.zeros(12))
    robot.enable_teleop("delta")
    left_master = robot.controllers["left_master"]
    right_master = robot.controllers["right_master"]
    assert left_master.joint is None  # `_robot` 缺省：主臂读不到

    robot.step()
    assert robot.teleop_read_failures == 1
    assert np.allclose(robot.target_action[:12], np.zeros(12))  # 原 target 保持（不突变）

    left_master.joint = np.array([0.4] * 6)
    right_master.joint = np.array([0.4] * 6)
    robot.step()  # 首拍成功读数 → 采锚点，增量恒 0
    assert robot.teleop_read_failures == 1  # 恢复后不再累加
    assert np.allclose(robot.target_action[:12], np.zeros(12))

    left_master.joint = np.array([0.6] * 6)  # 主臂推 0.2 rad
    right_master.joint = np.array([0.6] * 6)
    robot.step()
    assert np.allclose(robot.target_action[:12], 0.2)  # 两臂 target = 锚点 + 0.2


def test_rollout_refused_during_teleop_for_pose(dual_piper):
    """遥操作（人工接管）期间位姿 rollout 同样被拒，且不改目标。"""
    robot = _robot(dual_piper, left_joint=_TARGET_JOINTS)
    robot.set_target_action(np.zeros(12))
    before = robot.target_action.copy()
    robot.enable_teleop()

    accepted = robot.rollout(_cartesian_action(_TARGET_JOINTS, np.zeros(6)), "pose")

    assert accepted is False
    assert np.allclose(robot.target_action, before)


def test_pose_ik_config_is_honoured(dual_piper):
    """``robot.cartesian.ik`` 的求解参数由 robot 层持有并传给静态解算（压到 1 次迭代即解不出）。"""
    robot = dual_piper.DualPiperRobot(
        robot_config={"init_joint": _QPOS, **_DEVICES, "cartesian": {"ik": {"max_iters": 1}}}
    )
    robot.controllers = {
        "left_arm": _FakeArm(_TARGET_JOINTS),
        "right_arm": _FakeArm(np.zeros(6)),
        "left_master": _FakeArm(None),
        "right_master": _FakeArm(None),
    }

    assert robot.ik_config == {"max_iters": 1}
    with pytest.raises(_cartesian_error(), match="pose target rejected"):
        robot.execute(_cartesian_action(_TARGET_JOINTS, np.zeros(6)), "pose")


def test_pose_delta_accumulates_on_target_not_measured(dual_piper):
    """``pose_delta`` 叠加在**关节段目标**的正解位姿上（不是实测位姿）——MIT 稳态误差不累积。

    构造「目标 ≠ 实测」（实测关节全 0、目标为 ``_TARGET_JOINTS``）：增量必须落在**目标**的位姿上，
    否则每下发一条增量就把当前稳态误差写进新目标（逐条累积，且闭环永不收敛）。
    """
    robot = _robot(dual_piper, left_joint=np.zeros(6), right_joint=np.zeros(6))
    robot.set_target_action(np.concatenate([_TARGET_JOINTS, np.zeros(6)]))  # 目标 ≠ 实测
    base = np.asarray(robot.get_target_pose(), dtype=np.float64)
    delta = np.array([0.01, 0.0, -0.02, 0.0, 0.0, 0.03, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    robot.execute(delta, "pose_delta")

    applied = robot.target_action[:12]
    achieved = np.asarray(robot._pose_of_joints(applied), dtype=np.float64)
    expected = np.array(base)
    for start in (0, 6):
        expected[start : start + 6] += delta[start : start + 6]
    assert np.allclose(achieved, expected, atol=1e-4)  # 目标位姿 + 增量
    # 若错用**实测**位姿当基准，落点会差出「目标与实测的位姿差」（这一步就把它钉住）
    measured = np.asarray(robot.get_observation_pose(robot.get_observation_qpos()), dtype=np.float64)
    assert np.linalg.norm(achieved[:3] - (measured[:3] + delta[:3])) > 0.05


def test_pose_delta_chains_without_drifting(dual_piper):
    """连续增量按**理想叠加**累积：N 条小增量后位姿位移 ≈ N × 单条（无逐条误差累积）。

    增量基准是目标而不是实测，所以即便机械臂停在稳态误差平台上，目标也不会被逐条拽走。
    """
    robot = _robot(dual_piper, left_joint=np.zeros(6), right_joint=np.zeros(6))
    robot.set_target_action(np.concatenate([_TARGET_JOINTS, np.zeros(6)]))
    base = np.asarray(robot.get_target_pose(), dtype=np.float64)
    # 只推左臂：右臂紧贴奇异位形（全 0 关节），不适合当 IK 起点
    step = np.array([0.005, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    for _ in range(4):
        robot.execute(step, "pose_delta")

    achieved = np.asarray(robot._pose_of_joints(robot.target_action[:12]), dtype=np.float64)
    # 容差取逆解残差量级（1e-4 m）：要挡住的是**逐条累积的稳态误差**（cm 级），不是逆解残差
    assert np.allclose(achieved - base, step * 4, atol=5e-4)


def test_pose_delta_needs_target_pose_base(dual_piper):
    """基准不可用 → 拒绝（不猜语义）：拿不到目标位姿（FK 不可用）时不下发任何增量。"""
    robot = _robot(dual_piper)
    robot.set_target_action(np.zeros(12))
    before = robot.target_action.copy()
    robot.get_target_pose = lambda: None  # FK 不可用（观测里就没有 pose_target 那种情形）

    with pytest.raises(_cartesian_error(), match="needs a valid target pose"):
        robot.execute(np.zeros(12), "pose_delta")

    assert np.allclose(robot.target_action, before)  # 失败一帧都不下发


def test_sample_qpos_includes_pose_target(dual_piper):
    """``sample_qpos()`` 每拍带上**目标位姿**（= ``FK(关节段目标)``）——增量原语的解算结果可见。"""
    robot = _robot(dual_piper, left_joint=[0.1] * 6, right_joint=[0.2] * 6)
    robot.set_target_action(np.concatenate([np.full(6, 0.3), np.full(6, -0.2)]))  # 关节段（12 维）
    robot.set_target_gripper(_GRIPPER)

    state = robot.sample_qpos()

    assert state[robot.KEY_POSE_TARGET].shape == (12,)
    expected = _kinematics().PiperKinematics().pose(np.full(6, 0.3))
    assert np.allclose(state[robot.KEY_POSE_TARGET][:6], expected)
    # 目标位姿与实测位姿同拍且同源（实测关节 0.1 ≠ 目标关节 0.3，两者必须不同）
    assert not np.allclose(state[robot.KEY_POSE_TARGET][:6], state[robot.KEY_POSE][:6])


def test_sample_qpos_includes_pose(dual_piper):
    """``sample_qpos()`` 每拍带上位姿与夹爪（与关节角同一拍）——server 据此写各观测区。"""
    robot = _robot(dual_piper, left_joint=[0.1] * 6, right_joint=[0.2] * 6)
    state = robot.sample_qpos()

    assert state[robot.KEY_QPOS].shape == (12,)
    assert state[robot.KEY_GRIPPER].shape == (2,)
    assert state[robot.KEY_POSE].shape == (12,)
    # 位姿由这一拍的 qpos 派生（观测与 IK 同源，不允许错拍组合）
    expected = _kinematics().PiperKinematics().pose(np.asarray(state[robot.KEY_QPOS][:6]))
    assert np.allclose(state[robot.KEY_POSE][:6], expected)
