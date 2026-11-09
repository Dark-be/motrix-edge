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

"""Edge Pose6/RPY 与 MotrixSim Pose7 的控制器边界换算。"""

from __future__ import annotations

import numpy as np


def _pose7_values(pose: np.ndarray) -> np.ndarray:
    values = np.asarray(pose, dtype=np.float64).reshape(-1)
    if values.shape != (7,) or not np.all(np.isfinite(values)):
        raise ValueError("pose must contain exactly seven finite values")
    quaternion_norm = float(np.linalg.norm(values[3:]))
    if not np.isclose(quaternion_norm, 1.0, atol=1e-6, rtol=0.0):
        raise ValueError("pose quaternion must be normalized")
    values = values.copy()
    values[3:] /= quaternion_norm
    return values


def rpy_to_quat_xyzw(rpy: np.ndarray) -> np.ndarray:
    """Edge RPY → MotrixSim 四元数 ``[x, y, z, w]``。"""
    roll, pitch, yaw = np.asarray(rpy, dtype=np.float64) / 2.0
    sr, cr = np.sin(roll), np.cos(roll)
    sp, cp = np.sin(pitch), np.cos(pitch)
    sy, cy = np.sin(yaw), np.cos(yaw)
    return np.array(
        [
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
            cr * cp * cy + sr * sp * sy,
        ],
        dtype=np.float64,
    )


def quat_xyzw_to_rpy(quaternion: np.ndarray) -> np.ndarray:
    """MotrixSim 四元数 ``[x, y, z, w]`` → Edge RPY。"""
    x, y, z, w = np.asarray(quaternion, dtype=np.float64)
    sin_pitch = float(np.clip(2.0 * (w * y - z * x), -1.0, 1.0))
    if abs(sin_pitch) >= 1.0 - 1e-12:
        sign = 1.0 if sin_pitch >= 0.0 else -1.0
        roll = np.arctan2(sign * 2.0 * (x * y - z * w), sign * 2.0 * (x * z + y * w))
        return np.array([roll, np.copysign(np.pi / 2.0, sin_pitch), 0.0], dtype=np.float64)
    roll = np.arctan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = np.arcsin(sin_pitch)
    yaw = np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return np.array([roll, pitch, yaw], dtype=np.float64)


def pose6_to_pose7(pose: np.ndarray) -> np.ndarray:
    """Edge ``xyz+rpy`` Pose6 → MotrixSim ``xyz+xyzw`` Pose7。"""
    values = np.asarray(pose, dtype=np.float64).reshape(-1)
    if values.shape != (6,) or not np.all(np.isfinite(values)):
        raise ValueError("pose must contain exactly six finite values")
    return np.concatenate([values[:3], rpy_to_quat_xyzw(values[3:])])


def pose7_to_pose6(pose: np.ndarray) -> np.ndarray:
    """MotrixSim ``xyz+xyzw`` Pose7 → Edge ``xyz+rpy`` Pose6。"""
    values = _pose7_values(pose)
    return np.concatenate([values[:3], quat_xyzw_to_rpy(values[3:])])


def validate_pose7(pose: np.ndarray) -> np.ndarray:
    """校验并返回 MotrixSim Pose7。"""
    return _pose7_values(pose)
