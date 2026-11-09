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

"""MotrixSim 阻尼最小二乘 IK 的控制层适配。

求解目标：给定 MotrixSim Pose7 ``[x, y, z, qx, qy, qz, qw]`` 与种子关节角，求 ``q`` 使
``kinematics.pose(q)`` 落在容差内。迭代由 MotrixSim ``DlsSolver`` 执行；本模块保留输入校验、
多起点、SDK 软限位复核和稳定的 ``IkResult`` 失败契约。

失败**不静默**：返回 ``IkResult(ok=False, reason=...)``，由调用方（robot 层）决定拒绝指令还是
保持原目标——绝不把「解不出来的位姿」当成关节角下发。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real

import numpy as np

from .piper import PiperKinematics
from .transforms import validate_pose7

_MAX_DAMPING = float(np.sqrt(np.finfo(np.float64).max))


@dataclass(frozen=True)
class IkResult:
    """单次 IK 求解结果（``ok=False`` 时 ``q`` 为 None，只有误差与原因可供诊断）。"""

    ok: bool
    q: np.ndarray | None
    pos_err: float  # 位置误差（米）
    rot_err: float  # 姿态误差（rad）
    iterations: int
    reason: str = ""  # 失败原因：max_iters / joint_limit / stalled / bad_input / bad_seed / bad_config
    residual: float = float("inf")  # MotrixSim 求解器组合残差；起点直接满足时为 0

    def describe(self) -> str:
        """一行诊断文本（日志 / HTTP detail 用）。"""
        state = "ok" if self.ok else f"failed({self.reason})"
        return (
            f"ik {state}: residual={self.residual:.4g} pos_err={self.pos_err:.4f}m "
            f"rot_err={self.rot_err:.4f}rad iters={self.iterations}"
        )


def _pose_errors(kinematics: PiperKinematics, q: np.ndarray, target: np.ndarray) -> tuple[float, float]:
    """使用 MotrixSim Pose7 计算位置误差（米）和最短四元数角距离（弧度）。"""
    current = kinematics.pose(q)
    pos_err = float(np.linalg.norm(target[:3] - current[:3]))
    quaternion_dot = abs(float(np.dot(target[3:], current[3:])))
    rot_err = 2.0 * float(np.arccos(np.clip(quaternion_dot, -1.0, 1.0)))
    return pos_err, rot_err


def normalize_ik_config(
    pos_tol: float,
    rot_tol: float,
    max_iters: int,
    damping: float,
    step_size: float,
) -> tuple[float, float, int, float, float] | None:
    """校验并规范化真假控制器共享的 IK 配置；非法配置返回 ``None``。"""
    scalar_config = (pos_tol, rot_tol, damping, step_size)
    if (
        type(max_iters) is not int
        or max_iters <= 0
        or not all(type(value) is not bool and isinstance(value, Real) for value in scalar_config)
    ):
        return None
    try:
        pos_tol, rot_tol, damping, step_size = (float(value) for value in scalar_config)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not all(isfinite(value) for value in (pos_tol, rot_tol, damping, step_size))
        or pos_tol < 0.0
        or rot_tol < 0.0
        or damping <= 0.0
        or damping > _MAX_DAMPING
        or step_size <= 0.0
    ):
        return None
    return pos_tol, rot_tol, max_iters, damping, step_size


def normalize_ik_seeds(seed: object, fallback_seeds: object, dof: int) -> tuple[np.ndarray, ...] | None:
    """规范化真假控制器共享的 IK 起点；没有任何有效起点时返回 ``None``。"""
    try:
        candidates = (seed, *fallback_seeds)
    except TypeError:
        return None
    starts: list[np.ndarray] = []
    for candidate in candidates:
        try:
            values = np.asarray(candidate, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError, OverflowError):
            continue
        if values.shape == (dof,) and np.all(np.isfinite(values)):
            starts.append(values)
    return tuple(starts) or None


def solve_ik(
    kinematics: PiperKinematics,
    target_pose: np.ndarray,
    seed: np.ndarray,
    *,
    pos_tol: float = 1e-4,
    rot_tol: float = 1e-3,
    max_iters: int = 200,
    damping: float = 1e-2,
    step_size: float = 0.5,
    fallback_seeds: tuple[np.ndarray, ...] = (),
) -> IkResult:
    """解一个目标位姿（多起点：``seed`` → ``fallback_seeds``）。

    - ``seed``：首选起点（机器人侧传**当前关节读数**，解与现位形最近、运动最小）；
    - ``fallback_seeds``：``seed`` 不收敛时的备选起点（如「默认位形」/「上一解」）；
    - ``damping`` / ``step_size`` / ``max_iters``：直接对应 MotrixSim ``DlsSolver``；
    - 返回**首个收敛解**；全部失败时返回误差最小的那次（``ok=False`` + ``reason``）。
    """
    try:
        target = np.asarray(target_pose, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError, OverflowError):
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")
    if target.shape != (7,) or not np.all(np.isfinite(target)):
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")
    try:
        target = validate_pose7(target)
    except ValueError:
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")

    config = normalize_ik_config(pos_tol, rot_tol, max_iters, damping, step_size)
    if config is None:
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_config")
    pos_tol, rot_tol, max_iters, damping, step_size = config

    starts = normalize_ik_seeds(seed, fallback_seeds, kinematics.DOF)
    if starts is None:
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_seed")
    best = IkResult(False, None, float("inf"), float("inf"), 0, "bad_seed")
    for start in starts:
        q = kinematics.clip_joints(start)
        initial_pos_err, initial_rot_err = _pose_errors(kinematics, q, target)
        if initial_pos_err <= pos_tol and initial_rot_err <= rot_tol:
            return IkResult(True, q.copy(), initial_pos_err, initial_rot_err, 0, "", 0.0)

        # MotrixSim 使用一个组合残差作为停止条件；用更严格的用户容差驱动迭代，结束后再分别核验。
        tolerance = max(min(pos_tol, rot_tol), np.finfo(np.float64).eps)
        residual = float("inf")
        try:
            solved = kinematics.solve_dls(
                target,
                q,
                max_iters=max_iters,
                step_size=step_size,
                tolerance=tolerance,
                damping=damping,
            )
        except (RuntimeError, ValueError, FloatingPointError):
            candidate = q
            iterations = 0
            reason = "stalled"
        else:
            if solved.shape != (kinematics.DOF + 2,) or not np.all(np.isfinite(solved)):
                candidate = q
                iterations = 0
                reason = "stalled"
            else:
                iterations = int(solved[0])
                residual = float(solved[1])
                candidate = solved[2:]
                reason = "joint_limit" if not kinematics.within_limits(candidate) else "max_iters"

        pos_err, rot_err = _pose_errors(kinematics, candidate, target)
        if reason != "joint_limit" and pos_err <= pos_tol and rot_err <= rot_tol:
            return IkResult(True, candidate.copy(), pos_err, rot_err, iterations, "", residual)
        if (pos_err, rot_err) < (best.pos_err, best.rot_err):
            best = IkResult(False, None, pos_err, rot_err, iterations, reason, residual)
    return best
