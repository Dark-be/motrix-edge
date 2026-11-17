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

"""MotrixSim 阻尼最小二乘 IK 的 robot 层适配。

公开目标保持 Edge Pose6 ``[x, y, z, roll, pitch, yaw]``；本层转换为 MotrixSim Pose7 后调用
``DlsSolver``，并负责输入校验、多起点、软限位复核及稳定的失败契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real

import numpy as np

from .piper import PiperKinematics
from .transforms import pose6_to_pose7

_MAX_DAMPING = float(np.sqrt(np.finfo(np.float64).max))


@dataclass(frozen=True)
class IkResult:
    """单次 IK 求解结果；``ok=False`` 时 ``q`` 为 ``None``。"""

    ok: bool
    q: np.ndarray | None
    pos_err: float
    rot_err: float
    iterations: int
    reason: str = ""
    residual: float = float("inf")

    def describe(self) -> str:
        state = "ok" if self.ok else f"failed({self.reason})"
        return (
            f"ik {state}: residual={self.residual:.4g} pos_err={self.pos_err:.4f}m "
            f"rot_err={self.rot_err:.4f}rad iters={self.iterations}"
        )


def _pose_errors(kinematics: PiperKinematics, q: np.ndarray, target_pose7: np.ndarray) -> tuple[float, float]:
    current = kinematics.pose7(q)
    pos_err = float(np.linalg.norm(target_pose7[:3] - current[:3]))
    quaternion_dot = abs(float(np.dot(target_pose7[3:], current[3:])))
    rot_err = 2.0 * float(np.arccos(np.clip(quaternion_dot, -1.0, 1.0)))
    return pos_err, rot_err


def _normalize_config(
    pos_tol: float,
    rot_tol: float,
    max_iters: int,
    damping: float,
    step_limit: float,
) -> tuple[float, float, int, float, float] | None:
    scalar_config = (pos_tol, rot_tol, damping, step_limit)
    if (
        type(max_iters) is not int
        or max_iters <= 0
        or not all(type(value) is not bool and isinstance(value, Real) for value in scalar_config)
    ):
        return None
    try:
        pos_tol, rot_tol, damping, step_limit = (float(value) for value in scalar_config)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not all(isfinite(value) for value in (pos_tol, rot_tol, damping, step_limit))
        or pos_tol < 0.0
        or rot_tol < 0.0
        or damping <= 0.0
        or damping > _MAX_DAMPING
        or step_limit <= 0.0
    ):
        return None
    return pos_tol, rot_tol, max_iters, damping, step_limit


def _normalize_seeds(seed: object, fallback_seeds: object, dof: int) -> tuple[np.ndarray, ...] | None:
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
    step_limit: float = 0.2,
    fallback_seeds: tuple[np.ndarray, ...] = (),
) -> IkResult:
    """解 Pose6 目标；``step_limit`` 映射为 MotrixSim DLS 的单步尺度。"""
    try:
        target_pose7 = pose6_to_pose7(target_pose)
    except (TypeError, ValueError, OverflowError):
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")

    config = _normalize_config(pos_tol, rot_tol, max_iters, damping, step_limit)
    if config is None:
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_config")
    pos_tol, rot_tol, max_iters, damping, step_limit = config

    starts = _normalize_seeds(seed, fallback_seeds, kinematics.DOF)
    if starts is None:
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_seed")

    best = IkResult(False, None, float("inf"), float("inf"), 0, "bad_seed")
    for start in starts:
        q = kinematics.clip_joints(start)
        initial_pos_err, initial_rot_err = _pose_errors(kinematics, q, target_pose7)
        if initial_pos_err <= pos_tol and initial_rot_err <= rot_tol:
            return IkResult(True, q.copy(), initial_pos_err, initial_rot_err, 0, "", 0.0)

        tolerance = max(min(pos_tol, rot_tol), np.finfo(np.float64).eps)
        residual = float("inf")
        try:
            solved = kinematics.solve_dls(
                target_pose7,
                q,
                max_iters=max_iters,
                step_size=step_limit,
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

        pos_err, rot_err = _pose_errors(kinematics, candidate, target_pose7)
        if reason != "joint_limit" and pos_err <= pos_tol and rot_err <= rot_tol:
            return IkResult(True, candidate.copy(), pos_err, rot_err, iterations, "", residual)
        if (pos_err, rot_err) < (best.pos_err, best.rot_err):
            best = IkResult(False, None, pos_err, rot_err, iterations, reason, residual)
    return best
