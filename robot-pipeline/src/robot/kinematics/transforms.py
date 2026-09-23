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

"""姿态与旋转的数学工具（**纯 numpy**）——求解器与 edge 位姿契约之间的唯一换算层。

位姿约定（与 ``observations/pose`` / ``get_flange_pose()`` 同一套，详见
``wiki/design/robot_pipeline_cartesian.md``）：

- ``rpy`` = ``[roll, pitch, yaw]``，弧度，旋转矩阵 ``R = Rz(yaw) · Ry(pitch) · Rx(roll)``；
- ``pitch`` 取主值 ``[-π/2, π/2]``，``roll`` / ``yaw`` 取 ``(-π, π]``；
- 万向锁（``|pitch| = π/2``）时把自由度全部给 ``roll``（``yaw = 0``），保证往返可逆。
"""

from __future__ import annotations

import numpy as np

_EPS = 1e-12


def skew(v: np.ndarray) -> np.ndarray:
    """向量 → 反对称矩阵（``skew(v) @ w == cross(v, w)``）。"""
    x, y, z = (float(v[0]), float(v[1]), float(v[2]))
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=np.float64)


def vee(m: np.ndarray) -> np.ndarray:
    """反对称矩阵 → 向量（``skew`` 的逆）。"""
    return np.array([m[2, 1] - m[1, 2], m[0, 2] - m[2, 0], m[1, 0] - m[0, 1]], dtype=np.float64) / 2.0


def wrap_angles(angles: np.ndarray) -> np.ndarray:
    """角度 wrap 到 ``[-π, π)``（与 ``π`` 等价的 ``-π`` 保留：二者旋转等价）。

    用于「增量叠加在 rpy chart 上」之后（``pose_delta``）：``rpy + Δrpy`` 可能超出主值区间，
    wrap 回主值再送解算，保证 ``observations/pose_target`` 的取值区间稳定。
    """
    return (np.asarray(angles, dtype=np.float64) + np.pi) % (2.0 * np.pi) - np.pi


def rpy_to_matrix(rpy: np.ndarray) -> np.ndarray:
    """``[roll, pitch, yaw]`` → 旋转矩阵（``Rz(yaw)·Ry(pitch)·Rx(roll)``）。"""
    roll, pitch, yaw = (float(rpy[0]), float(rpy[1]), float(rpy[2]))
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ],
        dtype=np.float64,
    )


def matrix_to_rpy(rot: np.ndarray) -> np.ndarray:
    """旋转矩阵 → ``[roll, pitch, yaw]``（``rpy_to_matrix`` 的逆；万向锁时 ``yaw = 0``）。"""
    rot = np.asarray(rot, dtype=np.float64)
    sin_pitch = float(np.clip(-rot[2, 0], -1.0, 1.0))
    pitch = float(np.arcsin(sin_pitch))
    cos_pitch = float(np.sqrt(max(0.0, 1.0 - sin_pitch * sin_pitch)))
    if cos_pitch > _EPS:
        roll = float(np.arctan2(rot[2, 1], rot[2, 2]))
        yaw = float(np.arctan2(rot[1, 0], rot[0, 0]))
        return np.array([roll, pitch, yaw], dtype=np.float64)
    # 万向锁：roll / yaw 只剩一个自由度，约定 yaw = 0，全部给 roll
    sign = 1.0 if sin_pitch >= 0.0 else -1.0
    roll = float(np.arctan2(sign * rot[0, 1], sign * rot[0, 2]))
    return np.array([roll, pitch, 0.0], dtype=np.float64)


def log3(rot: np.ndarray) -> np.ndarray:
    """旋转矩阵 → 旋转向量（``so(3)`` 对数映射；小角度走一阶近似，避免除零）。

    ``vee(R - R^T) = 2·sin(θ)·axis``，故小角度时 ``vee(R - R^T)/2 ≈ θ·axis``——**不要**用
    ``R^T - R``（符号会反，IK 的更新方向随之反向）。
    """
    rot = np.asarray(rot, dtype=np.float64)
    cos_theta = float(np.clip((np.trace(rot) - 1.0) / 2.0, -1.0, 1.0))
    theta = float(np.arccos(cos_theta))
    if theta < 1e-8:
        return vee(rot - rot.T)  # 小角度：theta·axis ≈ vee(R - R^T)
    return (theta / (2.0 * np.sin(theta))) * vee(rot - rot.T)


def make_transform(rot: np.ndarray, translation: np.ndarray) -> np.ndarray:
    """旋转 + 平移 → 4×4 齐次变换矩阵。"""
    out = np.eye(4, dtype=np.float64)
    out[:3, :3] = np.asarray(rot, dtype=np.float64)
    out[:3, 3] = np.asarray(translation, dtype=np.float64).reshape(3)
    return out
