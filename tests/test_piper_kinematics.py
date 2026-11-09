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

"""Piper MotrixSim 运动学与 IK 的离线测试（不需要硬件 SDK）。

钉住运动学自身正确性、MotrixSim 原生 Pose7/雅可比契约，以及 IK 到位、限位和明确失败语义。
设计见 ``wiki/design/robot_pipeline_cartesian.md``。
"""

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_PIPER_ROBOT_TYPES = {"dual_alicia_piper_robot", "dual_piper_robot", "single_piper_robot"}


@pytest.fixture(scope="module")
def kinematics():
    """导入 robot-pipeline 的 robot.kinematics（MotrixSim 后端，无硬件依赖）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.kinematics")


# ---- Pose7 契约 ---------------------------------------------------------------


def _quaternion_angle(first: np.ndarray, second: np.ndarray) -> float:
    return 2.0 * float(np.arccos(np.clip(abs(float(np.dot(first, second))), -1.0, 1.0)))


def _quaternion_delta_vector(current: np.ndarray, target: np.ndarray) -> np.ndarray:
    """测试用的 xyzw 相对旋转向量，仅用于有限差分校验 MotrixSim Jacobian。"""
    x1, y1, z1, w1 = target
    x0, y0, z0, w0 = (-current[0], -current[1], -current[2], current[3])
    vector = np.array(
        [
            w1 * x0 + x1 * w0 + y1 * z0 - z1 * y0,
            w1 * y0 - x1 * z0 + y1 * w0 + z1 * x0,
            w1 * z0 + x1 * y0 - y1 * x0 + z1 * w0,
        ]
    )
    scalar = w1 * w0 - x1 * x0 - y1 * y0 - z1 * z0
    norm = np.linalg.norm(vector)
    return np.zeros(3) if norm < 1e-12 else vector / norm * (2.0 * np.arctan2(norm, scalar))


class _StubKinematics:
    """只隔离测试 IK 适配策略；真实 MotrixSim 收敛由集成用例覆盖。"""

    DOF = 1

    def __init__(self, solver_result=None, *, stalled=False, limited=False):
        self.solver_result = solver_result
        self.stalled = stalled
        self.limited = limited

    def clip_joints(self, q):
        values = np.asarray(q, dtype=np.float64)
        return np.clip(values, 0.0, 1.0) if self.limited else values

    def pose(self, q):
        return np.array([float(q[0]), 0.0, 0.0, 0.0, 0.0, 0.0, 1.0])

    def within_limits(self, q, tol=0.0):
        return not self.limited or bool(np.all(np.asarray(q) >= -tol) and np.all(np.asarray(q) <= 1.0 + tol))

    def solve_dls(self, *args, **kwargs):  # noqa: ARG002
        if self.stalled:
            raise RuntimeError("solver stalled")
        return self.solver_result


def test_pose6_pose7_boundary_roundtrip_and_validation(kinematics):
    pose6 = np.array([0.25, -0.1, 0.42, 0.2, -0.3, 1.1])
    pose7 = kinematics.pose6_to_pose7(pose6)
    assert np.allclose(kinematics.pose7_to_pose6(pose7), pose6, atol=1e-12)
    for pitch in (-np.pi / 2.0, np.pi / 2.0):
        locked = np.array([0.0, 0.0, 0.0, 0.3, pitch, 0.0])
        assert np.allclose(kinematics.pose7_to_pose6(kinematics.pose6_to_pose7(locked)), locked, atol=1e-12)

    for invalid in (np.zeros(6), np.zeros(7), np.zeros(8), np.full(7, np.nan)):
        with pytest.raises(ValueError):
            kinematics.validate_pose7(invalid)
    outside_tolerance = pose7.copy()
    outside_tolerance[3:] *= 1.0 + 2e-6
    with pytest.raises(ValueError, match="normalized"):
        kinematics.validate_pose7(outside_tolerance)

    within_tolerance = pose7.copy()
    within_tolerance[3:] *= 1.0 - 5e-7
    normalized = kinematics.validate_pose7(within_tolerance)
    assert np.linalg.norm(normalized[3:]) == pytest.approx(1.0)


# ---- 正运动学 ----------------------------------------------------------------


def test_pose_uses_motrixsim_pose7(kinematics):
    kine = kinematics.PiperKinematics()
    q = np.array([0.2, 0.8, -0.9, 0.1, 0.5, -0.3])
    pose = kine.pose(q)

    assert pose.shape == (7,)
    assert np.linalg.norm(pose[3:]) == pytest.approx(1.0)


def test_jacobian_matches_finite_difference(kinematics):
    """MotrixSim 雅可比 = FK 数值导数；float32 后端使用足够大的差分步长。"""
    kine = kinematics.PiperKinematics()
    q = np.array([0.3, 0.9, -1.0, 0.2, 0.6, -0.4])
    jacobian = kine.jacobian(q)
    eps = 1e-3
    for index in range(6):
        q_plus = q.copy()
        q_plus[index] += eps
        pose0, pose1 = kine.pose(q), kine.pose(q_plus)
        linear = (pose1[:3] - pose0[:3]) / eps
        angular = _quaternion_delta_vector(pose0[3:], pose1[3:]) / eps
        assert np.allclose(jacobian[:3, index], angular, atol=4e-4)
        assert np.allclose(jacobian[3:, index], linear, atol=4e-4)


def test_motrixsim_model_and_limits_shape(kinematics):
    kine = kinematics.PiperKinematics()
    assert kinematics.PIPER_MODEL_PATH.is_file()
    assert kine.DOF == 6
    assert kine.joint_limits.shape == (6, 2)
    assert np.all(np.isfinite(kine.pose(np.zeros(6))))

    with pytest.raises(ValueError):
        kinematics.PiperKinematics(joint_limits=np.zeros((6, 3)))
    with pytest.raises(ValueError):
        kinematics.PiperKinematics(joint_limits=np.array([[1.0, -1.0]] * 6))
    with pytest.raises(ValueError, match="finite"):
        kinematics.PiperKinematics(joint_limits=np.full((6, 2), np.nan))


def test_motrixsim_scene_data_is_isolated_between_threads(kinematics):
    kine = kinematics.PiperKinematics()
    samples = (np.array([0.2, 0.8, -1.0, 0.3, 0.4, -0.2]), np.array([-0.4, 1.2, -1.7, -0.5, 0.6, 0.9]))
    expected = tuple(kine.pose(q) for q in samples)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(kine.pose, samples[index % 2]) for index in range(100)]

    for index, future in enumerate(futures):
        np.testing.assert_allclose(future.result(), expected[index % 2], atol=1e-7)


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

    integer_clipped = kine.clip_joints(np.array([9, -1, 1, 9, 9, 9]))
    assert integer_clipped.dtype == np.float64
    assert integer_clipped[0] == pytest.approx(kine.joint_limits[0, 1])


def test_joint_limits_are_read_only(kinematics):
    first = kinematics.PiperKinematics()
    second = kinematics.PiperKinematics()
    custom = kinematics.PiperKinematics(joint_limits=np.tile([-1.0, 1.0], (6, 1)))

    assert not first.joint_limits.flags.writeable
    assert not custom.joint_limits.flags.writeable
    with pytest.raises(ValueError):
        first.joint_limits[0, 1] = 0.0
    assert np.array_equal(first.joint_limits, second.joint_limits)


def test_j6_sdk_limit_is_tighter_than_the_mjcf_mechanical_range(kinematics):
    """真机软限位服从 SDK 的 ±120°，而不是 URDF 的 ±170° 机械范围。"""
    assert np.rad2deg(kinematics.PIPER_SDK_LIMITS[5]) == pytest.approx([-120.0, 120.0], abs=1e-4)


def test_soft_limits_shrink_the_sdk_limits_by_the_margin(kinematics):
    """生效软限位 = SDK 机型限位**内缩** ``JOINT_LIMIT_MARGIN``（0 边界不内缩）。

    内缩是为了边界值经 float32 舍入后不越过限位（现场表现为下发被拒）；0 边界在 float32 里精确，
    且厂商 home 就落在 0 上，内缩反而会让 home 越界。
    """
    sdk_limits, margin = kinematics.PIPER_SDK_LIMITS, kinematics.JOINT_LIMIT_MARGIN
    limits = kinematics.PiperKinematics().joint_limits
    assert margin > 0
    expected = np.column_stack(
        [
            np.where(sdk_limits[:, 0] == 0.0, sdk_limits[:, 0], sdk_limits[:, 0] + margin),
            np.where(sdk_limits[:, 1] == 0.0, sdk_limits[:, 1], sdk_limits[:, 1] - margin),
        ]
    )
    assert np.allclose(limits, expected)
    # 逐关节都在机型限位内（SDK 按机型限位裁，比它松就会下发 SDK 不接受的值）
    assert np.all(limits[:, 0] >= sdk_limits[:, 0]) and np.all(limits[:, 1] <= sdk_limits[:, 1])


def test_shipped_defaults_are_inside_the_soft_limits(kinematics):
    """Piper 机型的出厂 ``init_qpos`` 必须在 Piper 软限位内（夹爪维不参与比较）。"""
    import yaml

    config_dir = _ROBOT_PIPELINE_SRC / "config"
    limits = kinematics.PiperKinematics().joint_limits
    checked = 0
    for path in sorted(config_dir.glob("*.yml")):
        robot = yaml.safe_load(path.read_text())["robot"] or {}
        if robot.get("type") not in _PIPER_ROBOT_TYPES:
            continue
        checked += 1
        init_qpos = robot.get("init_qpos")
        if init_qpos is None:
            continue
        values = np.asarray(init_qpos, dtype=np.float64).reshape(-1)
        assert len(values) % 7 == 0, f"{path.name} 的 init_qpos 必须按每臂 6 关节 + 1 夹爪排列"
        for index in range(0, len(values), 7):
            block = values[index : index + 6]
            assert np.all(block >= limits[:, 0]) and np.all(block <= limits[:, 1]), (
                f"{path.name} 的 init_qpos[{index // 7}] = {block.tolist()} 超出软限位"
            )
    assert checked > 0, "至少应检查一个 Piper 机型配置，避免筛选条件失效后测试空跑"


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
        # 位置直接比；姿态使用旋转矩阵误差，避免四元数 q/-q 等价造成分量比较误判。
        assert np.allclose(kine.pose(result.q)[:3], target[:3], atol=1e-3)
        assert _quaternion_angle(kine.pose(result.q)[3:], target[3:]) <= 3e-3
        assert np.isfinite(result.residual)

    assert solved == attempted, f"IK converged on {solved}/{attempted} reachable targets"


def test_ik_reports_failure_for_unreachable_target(kinematics):
    """不可达目标必须**明确失败**（``ok=False`` + 原因），绝不返回凑合解。"""
    kine = kinematics.PiperKinematics()
    target = np.array([5.0, 5.0, 5.0, 0.0, 0.0, 0.0, 1.0])
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

    result = kinematics.solve_ik(kine, kine.pose(np.zeros(6)), np.zeros(3))
    assert not result.ok and result.reason == "bad_seed" and result.iterations == 0

    for target in (None, ["bad"] * 7, np.zeros(6), np.zeros(7)):
        result = kinematics.solve_ik(kine, target, np.zeros(6))
        assert not result.ok and result.reason == "bad_input"

    result = kinematics.solve_ik(kine, kine.pose(np.zeros(6)), ["bad"] * 6)
    assert not result.ok and result.reason == "bad_seed"

    result = kinematics.solve_ik(kine, kine.pose(np.zeros(6)), ["bad"] * 6, fallback_seeds=(["bad"] * 6,))
    assert not result.ok and result.reason == "bad_seed"


@pytest.mark.parametrize(
    "overrides",
    (
        {"max_iters": 0},
        {"max_iters": 1.5},
        {"pos_tol": -1.0},
        {"rot_tol": np.nan},
        {"damping": 0.0},
        {"damping": 1e308},
        {"step_size": np.inf},
        {"step_size": True},
        {"damping": Fraction(10**1000, 1)},
    ),
)
def test_ik_rejects_bad_solver_config(kinematics, overrides):
    kine = kinematics.PiperKinematics()
    result = kinematics.solve_ik(kine, kine.pose(np.zeros(6)), np.zeros(6), **overrides)

    assert not result.ok and result.reason == "bad_config" and result.iterations == 0


def test_ik_normalizes_real_solver_config(kinematics):
    kine = kinematics.PiperKinematics()
    result = kinematics.solve_ik(kine, kine.pose(np.zeros(6)), np.zeros(6), damping=Fraction(1, 100))

    assert result.ok


def test_virtual_kinematics_matches_real_input_and_config_contract(kinematics):
    """虚拟控制器不得吞多余维度、非法配置或拼错的配置名。"""
    from robot.controller.test_arm_controller import TestArmController

    target = TestArmController.joint_to_pose(np.ones(6))
    exact_result = TestArmController.pose_to_joint(target, pos_tol=Fraction(1, 100))
    assert exact_result.ok
    assert exact_result.residual == 0.0

    for invalid in (np.zeros(5), np.full(6, np.nan), ["bad"] * 6):
        result = TestArmController.pose_to_joint(invalid)
        assert not result.ok and result.reason == "bad_input"

    result = TestArmController.pose_to_joint(target, damping=0.0)
    assert not result.ok and result.reason == "bad_config"

    result = TestArmController.pose_to_joint(target, seed=["bad"] * 6, fallback_seeds=(["bad"] * 6,))
    assert not result.ok and result.reason == "bad_seed"

    result = TestArmController.pose_to_joint(target, seed=["bad"] * 6, fallback_seeds=(np.zeros(6),))
    assert result.ok

    with pytest.raises(TypeError, match="unexpected keyword"):
        TestArmController.pose_to_joint(target, pos_tlo=1e-4)

    for invalid in (np.zeros(5), np.full(6, np.nan)):
        with pytest.raises(ValueError, match="exactly six finite values"):
            TestArmController.joint_to_pose(invalid)


@pytest.mark.parametrize(
    "target",
    (
        [0.40005, 0.2, 0.2, 1.0, 1.0, 1.0],  # j1 = 2.00025，位置残差小于默认 pos_tol
        [0.2, 0.2, 0.2, 2.0005, 1.0, 1.0],  # j4 = 2.0005，姿态残差小于默认 rot_tol
        [-0.00001, 0.2, 0.2, 1.0, 1.0, 1.0],  # 轻微越过下界同样必须失败
    ),
)
def test_virtual_ik_rejects_slight_joint_limit_violations(target, kinematics):
    """容差只判定求解残差，不得把已知越过虚拟关节域的目标伪装成成功。"""
    from robot.controller.test_arm_controller import TestArmController

    result = TestArmController.pose_to_joint(target, pos_tol=10.0, rot_tol=10.0)

    assert not result.ok
    assert result.q is None
    assert result.reason == "joint_limit"


def test_ik_honours_fallback_seed(kinematics):
    """首选起点不可用时用 fallback 起点求解（机器人侧传 home 作为兜底）。"""
    kine = kinematics.PiperKinematics()
    target = kine.pose(np.array([0.2, 0.9, -1.1, 0.3, 0.6, -0.2]))
    result = kinematics.solve_ik(kine, target, np.full(6, np.nan), fallback_seeds=(np.zeros(6),))

    assert result.ok and result.q is not None
    assert np.allclose(kine.pose(result.q)[:3], target[:3], atol=1e-3)
    assert _quaternion_angle(kine.pose(result.q)[3:], target[3:]) <= 3e-3


def test_ik_failure_keeps_best_diagnostic_across_fallbacks(kinematics):
    """较差的 fallback 失败结果不得覆盖先前更接近目标的诊断。"""
    result = kinematics.solve_ik(
        _StubKinematics(stalled=True),
        np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]),
        np.array([0.0]),
        fallback_seeds=(np.array([1.0]),),
    )

    assert not result.ok and result.reason == "stalled"
    assert result.pos_err == pytest.approx(0.1)


def test_ik_reports_joint_limit_when_solver_leaves_soft_limits(kinematics):
    result = kinematics.solve_ik(
        _StubKinematics(np.array([1.0, 0.0, -0.1]), limited=True),
        np.array([-0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]),
        np.array([0.0]),
    )

    assert not result.ok and result.reason == "joint_limit"


def test_ik_accepts_converged_motrixsim_candidate(kinematics):
    result = kinematics.solve_ik(
        _StubKinematics(np.array([1.0, 0.0, 0.1])),
        np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]),
        np.zeros(1),
        max_iters=1,
    )

    assert result.ok and result.iterations == 1


def test_ik_normalizes_valid_target_quaternion_before_error_measurement(kinematics):
    target = np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0 - 5e-7])

    result = kinematics.solve_ik(_StubKinematics(stalled=True), target, np.array([0.1]))

    assert result.ok
    assert result.rot_err == pytest.approx(0.0)
