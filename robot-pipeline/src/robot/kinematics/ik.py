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

"""逆运动学求解器（阻尼最小二乘 / Levenberg–Marquardt，**纯 numpy**）。

求解目标：给定基座系目标位姿 ``[x, y, z, roll, pitch, yaw]`` 与种子关节角，求 ``q`` 使
``kinematics.pose(q)`` 落在容差内。误差与雅可比都在**基座系**（位置差 + ``log3`` 旋转向量），
与 ``PiperKinematics.jacobian()`` 同系，故可直接做 ``J^T (J J^T + λ²I)^{-1} e`` 迭代。

失败**不静默**：返回 ``IkResult(ok=False, reason=...)``，由调用方（robot 层）决定拒绝指令还是
保持原目标——绝不把「解不出来的位姿」当成关节角下发。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .piper import PiperKinematics
from .transforms import log3


@dataclass(frozen=True)
class IkResult:
    """单次 IK 求解结果（``ok=False`` 时 ``q`` 为 None，只有误差与原因可供诊断）。"""

    ok: bool
    q: np.ndarray | None
    pos_err: float  # 位置误差（米）
    rot_err: float  # 姿态误差（rad）
    iterations: int
    reason: str = ""  # 失败原因：max_iters / joint_limit / stalled / bad_input

    def describe(self) -> str:
        """一行诊断文本（日志 / HTTP detail 用）。"""
        state = "ok" if self.ok else f"failed({self.reason})"
        return f"ik {state}: pos_err={self.pos_err:.4f}m rot_err={self.rot_err:.4f}rad iters={self.iterations}"


def _pose_error(kinematics: PiperKinematics, q: np.ndarray, target: np.ndarray) -> np.ndarray:
    """基座系位姿误差 ``[Δp, log3(R_des R_cur^T)]``（6 维）。"""
    transform = kinematics.fk(q)
    pos_err = target[:3] - transform[:3, 3]
    rot_err = log3(PiperKinematics.pose_matrix(target)[:3, :3] @ transform[:3, :3].T)
    return np.concatenate([pos_err, rot_err])


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
    """解一个目标位姿（多起点：``seed`` → ``fallback_seeds``）。

    - ``seed``：首选起点（机器人侧传**当前关节读数**，解与现位形最近、运动最小）；
    - ``fallback_seeds``：``seed`` 不收敛时的备选起点（如「默认位形」/「上一解」）；
    - ``damping``：λ，抑制奇异位形附近的大步长；``step_limit``：单次迭代关节增量上限（rad）；
    - 返回**首个收敛解**；全部失败时返回误差最小的那次（``ok=False`` + ``reason``）。
    """
    target = np.asarray(target_pose, dtype=np.float64).reshape(-1)
    if target.shape[0] < 6 or not np.all(np.isfinite(target[:6])):
        return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")
    target = target[:6]

    best = IkResult(False, None, float("inf"), float("inf"), 0, "max_iters")
    for attempt, start in enumerate((seed, *fallback_seeds)):
        start = np.asarray(start, dtype=np.float64).reshape(-1)
        if start.shape[0] != kinematics.DOF or not np.all(np.isfinite(start)):
            continue  # 该起点不可用（读数缺失）：换下一个
        q = kinematics.clip_joints(start)
        # 起点已在限位外时，clip 会改变起点 → 保留最近一次可行动作，不做额外补偿
        for iteration in range(1, max_iters + 1):
            error = _pose_error(kinematics, q, target)
            pos_err, rot_err = float(np.linalg.norm(error[:3])), float(np.linalg.norm(error[3:]))
            if pos_err <= pos_tol and rot_err <= rot_tol:
                return IkResult(True, q.copy(), pos_err, rot_err, iteration, "")
            if pos_err < best.pos_err:
                best = IkResult(False, None, pos_err, rot_err, iteration, "max_iters")

            jacobian = kinematics.jacobian(q)
            # 阻尼最小二乘：dq = J^T (J J^T + λ²I)^{-1} e
            lhs = jacobian @ jacobian.T + (damping**2) * np.eye(6)
            try:
                dq = jacobian.T @ np.linalg.solve(lhs, error)
            except np.linalg.LinAlgError:  # 极端奇异：加大阻尼再试一次
                dq = jacobian.T @ np.linalg.solve(lhs + np.eye(6), error)
            dq = np.clip(dq, -step_limit, step_limit)
            next_q = kinematics.clip_joints(q + dq)
            progress = float(np.linalg.norm(next_q - q))
            q = next_q
            if progress < 1e-12:
                # 无进展：被限位卡住（越界）或已到可行域边界 → 换下一个起点
                reason = "joint_limit" if not kinematics.within_limits(q, tol=1e-6) else "stalled"
                best = IkResult(False, None, pos_err, rot_err, iteration, reason)
                break
        if best.reason == "max_iters" and attempt == 0:
            best = IkResult(False, None, best.pos_err, best.rot_err, best.iterations, "max_iters")
    return best
