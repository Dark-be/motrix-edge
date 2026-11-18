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

"""Pure NumPy Piper FK/IK used by the simulation command boundary."""

from dataclasses import dataclass

import numpy as np

from motrix_edge.geometry.transforms import pose_to_transform, rpy_to_matrix, transform_to_pose

PIPER_DH = (
    (0.0, 0.0, 0.123, 0.0),
    (-np.pi / 2, 0.0, 0.0, -172.22 * np.pi / 180.0),
    (0.0, 0.28503, 0.0, -102.78 * np.pi / 180.0),
    (np.pi / 2, -0.021984, 0.25075, 0.0),
    (-np.pi / 2, 0.0, 0.0, 0.0),
    (np.pi / 2, 0.0, 0.091, 0.0),
)
PIPER_LIMITS = np.array(
    [
        [-2.616994, 2.616994],
        [0.0, 3.140593],
        [-2.966060, 0.0],
        [-1.744330, 1.744330],
        [-1.220730, 1.220730],
        [-1.744300, 1.744300],
    ],
    dtype=np.float64,
)


def _log3(rotation: np.ndarray) -> np.ndarray:
    cos_theta = float(np.clip((np.trace(rotation) - 1.0) / 2.0, -1.0, 1.0))
    theta = float(np.arccos(cos_theta))
    vee = (
        np.array([rotation[2, 1] - rotation[1, 2], rotation[0, 2] - rotation[2, 0], rotation[1, 0] - rotation[0, 1]])
        / 2.0
    )
    if theta < 1e-8:
        return vee
    return theta / np.sin(theta) * vee


@dataclass(frozen=True)
class IkResult:
    ok: bool
    q: np.ndarray | None
    pos_err: float
    rot_err: float
    iterations: int
    reason: str

    def describe(self) -> str:
        return (
            f"ik {'ok' if self.ok else 'failed(' + self.reason + ')'}: "
            f"pos_err={self.pos_err:.4f}m rot_err={self.rot_err:.4f}rad iters={self.iterations}"
        )


class PiperKinematics:
    def __init__(self, joint_limits: np.ndarray, tcp_offset=(0.0, 0.0, 0.13)):
        limits = np.asarray(joint_limits, dtype=np.float64)
        if limits.shape != (6, 2) or np.any(limits[:, 0] > limits[:, 1]):
            raise ValueError(f"invalid Piper joint limits: {limits.tolist()}")
        self.joint_limits = limits
        self.tcp_offset = np.asarray(tcp_offset, dtype=np.float64).reshape(-1)
        if self.tcp_offset.shape != (3,) or not np.all(np.isfinite(self.tcp_offset)):
            raise ValueError("Piper TCP offset must contain three finite values")

    @staticmethod
    def _link(alpha: float, a: float, d: float, theta: float) -> np.ndarray:
        ca, sa, ct, st = np.cos(alpha), np.sin(alpha), np.cos(theta), np.sin(theta)
        return np.array(
            [
                [ct, -st, 0.0, a],
                [st * ca, ct * ca, -sa, -d * sa],
                [st * sa, ct * sa, ca, d * ca],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    def prefixes(self, q: np.ndarray) -> list[np.ndarray]:
        values = np.asarray(q, dtype=np.float64).reshape(-1)
        if values.shape != (6,):
            raise ValueError("Piper joint vector must have 6 entries")
        result = [np.eye(4)]
        for index, (alpha, a, d, offset) in enumerate(PIPER_DH):
            result.append(result[-1] @ self._link(alpha, a, d, values[index] + offset))
        return result

    def fk(self, q: np.ndarray) -> np.ndarray:
        return self.prefixes(q)[-1]

    def pose(self, q: np.ndarray) -> np.ndarray:
        flange = transform_to_pose(self.fk(q))
        flange[:3] += rpy_to_matrix(flange[3:6]) @ self.tcp_offset
        return flange

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        prefixes = self.prefixes(q)
        end = prefixes[-1][:3, 3]
        jacobian = np.zeros((6, 6))
        for index in range(6):
            frame = prefixes[index + 1]
            axis, origin = frame[:3, 2], frame[:3, 3]
            jacobian[:3, index] = np.cross(axis, end - origin)
            jacobian[3:, index] = axis
        return jacobian

    def solve(self, target_pose: np.ndarray, seed: np.ndarray, fallback: np.ndarray) -> IkResult:
        target = np.asarray(target_pose, dtype=np.float64).reshape(-1)
        if target.shape != (6,) or not np.all(np.isfinite(target)):
            return IkResult(False, None, np.inf, np.inf, 0, "bad_input")
        flange_target = target.copy()
        flange_target[:3] -= rpy_to_matrix(target[3:6]) @ self.tcp_offset
        target_transform = pose_to_transform(flange_target)
        best = IkResult(False, None, np.inf, np.inf, 0, "max_iters")
        for start in (seed, fallback):
            q = np.clip(np.asarray(start, dtype=np.float64), self.joint_limits[:, 0], self.joint_limits[:, 1])
            for iteration in range(1, 201):
                current = self.fk(q)
                error = np.r_[
                    flange_target[:3] - current[:3, 3],
                    _log3(target_transform[:3, :3] @ current[:3, :3].T),
                ]
                pos_err, rot_err = float(np.linalg.norm(error[:3])), float(np.linalg.norm(error[3:]))
                if pos_err <= 1e-4 and rot_err <= 1e-3:
                    return IkResult(True, q.copy(), pos_err, rot_err, iteration, "")
                if pos_err + rot_err < best.pos_err + best.rot_err:
                    best = IkResult(False, None, pos_err, rot_err, iteration, "max_iters")
                jacobian = self.jacobian(q)
                lhs = jacobian @ jacobian.T + 1e-4 * np.eye(6)
                delta = jacobian.T @ np.linalg.solve(lhs, error)
                next_q = np.clip(q + np.clip(delta, -0.2, 0.2), self.joint_limits[:, 0], self.joint_limits[:, 1])
                if np.linalg.norm(next_q - q) < 1e-12:
                    best = IkResult(False, None, pos_err, rot_err, iteration, "stalled")
                    break
                q = next_q
        return best


def effective_limits(model_limits: np.ndarray) -> np.ndarray:
    """Use only configurations accepted by both the scene model and physical Piper contract."""
    model = np.asarray(model_limits, dtype=np.float64)
    limits = np.column_stack([np.maximum(model[:, 0], PIPER_LIMITS[:, 0]), np.minimum(model[:, 1], PIPER_LIMITS[:, 1])])
    if np.any(limits[:, 0] > limits[:, 1]):
        raise ValueError("scene joint limits do not overlap Piper limits")
    return limits
