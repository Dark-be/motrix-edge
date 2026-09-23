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

"""Piper 运动学 / IK / 阻抗求解器的离线测试（纯 numpy，不需要硬件 SDK）。

钉住三件事：① 运动学自身正确（rpy 往返、FK↔pose 一致、雅可比 = FK 数值导数）；
② IK 能到位且解在限位内、不可达时**明确失败**（不返回一个凑合的解）；③ 阻抗映射的
方向与限幅。设计见 ``wiki/design/robot_pipeline_cartesian.md``。
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


@pytest.fixture(scope="module")
def kinematics():
    """导入 robot-pipeline 的 robot.kinematics（纯 numpy，无硬件依赖）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.kinematics")


# ---- 姿态工具 ----------------------------------------------------------------


def test_rpy_roundtrip(kinematics):
    """``rpy → R → rpy`` 往返（含正负 pitch）；万向锁时约定 ``yaw = 0``。"""
    rng = np.random.default_rng(0)
    for _ in range(50):
        rpy = np.array([rng.uniform(-np.pi, np.pi), rng.uniform(-np.pi / 2, np.pi / 2), rng.uniform(-np.pi, np.pi)])
        assert np.allclose(kinematics.matrix_to_rpy(kinematics.rpy_to_matrix(rpy)), rpy, atol=1e-9)
    locked = kinematics.matrix_to_rpy(kinematics.rpy_to_matrix([0.3, np.pi / 2, 0.0]))
    assert locked[1] == pytest.approx(np.pi / 2)
    assert locked[2] == pytest.approx(0.0)


def test_log3_matches_axis_angle(kinematics):
    """``log3`` 对「绕 z 转 theta」应给出 ``[0, 0, theta]``（小角度走一阶近似也要一致）。

    只用 ``theta < π`` 的样本：``theta → π`` 时 ``sin θ → 0``，``log3`` 数值上本就病态
    （零位形的 180° 翻转），不在本用例的考察范围。
    """
    for theta in (0.0, 1e-9, 1e-3, 0.7, 2.5, 3.0):
        axis = np.array([0.0, 0.0, 1.0])
        matrix = kinematics.rpy_to_matrix([0.0, 0.0, theta])
        assert np.allclose(kinematics.log3(matrix), axis * theta, atol=1e-6)


# ---- 正运动学 ----------------------------------------------------------------


def test_pose_matches_fk(kinematics):
    """``pose()`` 必须就是 ``fk()`` 的平移 + rpy 分解（同源，不可各算一套）。"""
    kine = kinematics.PiperKinematics()
    q = np.array([0.2, 0.8, -0.9, 0.1, 0.5, -0.3])
    transform = kine.fk(q)
    pose = kine.pose(q)

    assert pose.shape == (6,) and transform.shape == (4, 4)
    assert np.allclose(pose[:3], transform[:3, 3])
    assert np.allclose(kinematics.rpy_to_matrix(pose[3:]), transform[:3, :3], atol=1e-12)
    assert np.allclose(kine.pose_matrix(pose), transform, atol=1e-12)


def test_jacobian_matches_finite_difference(kinematics):
    """雅可比 = FK 的数值导数（线速度 + ``log3`` 角速度）——IK 收敛的前提。"""
    kine = kinematics.PiperKinematics()
    q = np.array([0.3, 0.9, -1.0, 0.2, 0.6, -0.4])
    jacobian = kine.jacobian(q)
    eps = 1e-6
    for index in range(6):
        q_plus = q.copy()
        q_plus[index] += eps
        pose0, pose1 = kine.pose(q), kine.pose(q_plus)
        linear = (pose1[:3] - pose0[:3]) / eps
        angular = kinematics.log3(kinematics.rpy_to_matrix(pose1[3:]) @ kinematics.rpy_to_matrix(pose0[3:]).T) / eps
        assert np.allclose(jacobian[:3, index], linear, atol=1e-6)
        assert np.allclose(jacobian[3:, index], angular, atol=1e-6)


def test_dh_and_limits_shape(kinematics):
    assert len(kinematics.PIPER_DH) == 6
    kine = kinematics.PiperKinematics()
    assert kine.DOF == 6
    assert kine.joint_limits.shape == (6, 2)
    assert np.all(np.isfinite(kine.pose(np.zeros(6))))

    with pytest.raises(ValueError):
        kinematics.PiperKinematics(joint_limits=np.zeros((6, 3)))
    with pytest.raises(ValueError):
        kinematics.PiperKinematics(joint_limits=np.array([[1.0, -1.0]] * 6))


def test_joint_limits_clip_and_check(kinematics):
    kine = kinematics.PiperKinematics()
    assert kine.within_limits(np.zeros(6))
    assert not kine.within_limits(np.array([0.0, -0.5, 0.0, 0.0, 0.0, 0.0]))  # j2 下界 0
    assert not kine.within_limits(np.array([0.0, 0.0, 0.5, 0.0, 0.0, 0.0]))  # j3 上界 0

    clipped = kine.clip_joints(np.array([9.0, -1.0, 1.0, 9.0, 9.0, 9.0]))
    assert kine.within_limits(clipped)
    assert clipped[0] == pytest.approx(kine.joint_limits[0, 1])
    assert clipped[1] == pytest.approx(kine.joint_limits[1, 0])
    assert clipped[2] == pytest.approx(kine.joint_limits[2, 1])


# ---- 逆运动学 ----------------------------------------------------------------


def test_ik_reaches_reachable_pose_and_respects_limits(kinematics):
    """随机可达位姿（限位内的关节角生成）→ IK 应基本都能到位，且解在限位内。"""
    kine = kinematics.PiperKinematics()
    rng = np.random.default_rng(1)
    attempted = solved = 0
    for _ in range(20):
        q_target = kine.clip_joints(rng.uniform(-1.0, 1.0, size=6))
        target = kine.pose(q_target)
        result = kinematics.solve_ik(kine, target, q_target + 0.05)
        attempted += 1
        if not result.ok:
            continue
        solved += 1
        assert result.q is not None and kine.within_limits(result.q, tol=1e-6)
        # 位置直接比；姿态比旋转矩阵（rpy 在 ±π 主值域附近会把小角度差放大，不适合当判据）
        assert np.allclose(kine.pose(result.q)[:3], target[:3], atol=1e-3)
        delta = kine.pose_matrix(kine.pose(result.q))[:3, :3] @ kine.pose_matrix(target)[:3, :3].T
        assert np.linalg.norm(kinematics.log3(delta)) <= 3e-3

    assert solved == attempted, f"IK converged on {solved}/{attempted} reachable targets"


def test_ik_reports_failure_for_unreachable_target(kinematics):
    """不可达目标必须**明确失败**（``ok=False`` + 原因），绝不返回凑合解。"""
    kine = kinematics.PiperKinematics()
    target = np.array([5.0, 5.0, 5.0, 0.0, 0.0, 0.0])
    result = kinematics.solve_ik(kine, target, np.zeros(6))

    assert not result.ok
    assert result.q is None
    assert result.reason in {"max_iters", "joint_limit", "stalled"}
    assert result.pos_err > 0.5  # 离目标很远（诊断值可信）
    assert "failed" in result.describe()


def test_ik_rejects_bad_input(kinematics):
    kine = kinematics.PiperKinematics()
    result = kinematics.solve_ik(kine, np.full(6, np.nan), np.zeros(6))
    assert not result.ok and result.reason == "bad_input"

    # 起点不可用（维数不符）→ 交给 fallback 起点
    result = kinematics.solve_ik(
        kine,
        kine.pose(np.zeros(6)),
        np.zeros(3),
        fallback_seeds=(np.zeros(6),),
    )
    assert result.ok


def test_ik_honours_fallback_seed(kinematics):
    """首选起点不可用时用 fallback 起点求解（机器人侧传 home 作为兜底）。"""
    kine = kinematics.PiperKinematics()
    target = kine.pose(np.array([0.2, 0.9, -1.1, 0.3, 0.6, -0.2]))
    result = kinematics.solve_ik(kine, target, np.full(6, np.nan), fallback_seeds=(np.zeros(6),))

    assert result.ok and result.q is not None
    assert np.allclose(kine.pose(result.q)[:3], target[:3], atol=1e-3)
    delta = kine.pose_matrix(kine.pose(result.q))[:3, :3] @ kine.pose_matrix(target)[:3, :3].T
    assert np.linalg.norm(kinematics.log3(delta)) <= 3e-3
