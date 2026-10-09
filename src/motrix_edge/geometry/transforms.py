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

"""geometry/transforms —— 4×4 齐次变换的纯 numpy 实现（无硬件、无 OpenCV、无 Robotics 依赖）。

本模块是 Edge 侧**坐标系表达**的单点：``rpy`` ↔ 旋转矩阵、变换的组合 / 求逆、点变换，以及标定产物
里「16 个浮点数按行主序平铺」的解析与校验。

约定（与 ``robot/kinematics/transforms.py`` **一致**——两处实现由 ``tests/test_geometry.py`` 的
交叉用例钉住，防止漂移）：

-   ``rpy = [roll, pitch, yaw]``，弧度，``R = Rz(yaw)·Ry(pitch)·Rx(roll)``；
-   ``pitch`` 取主值 ``[-π/2, π/2]``、``roll`` / ``yaw`` 取 ``(-π, π]``；万向锁（``|pitch| = π/2``）
    时把自由度全给 ``roll``（``yaw = 0``），保证往返可逆。

为什么要有一份 Edge 侧实现：Edge 运行期要把「像素 + 深度」反投影到 ``world``（腕相机还要乘**同拍**的
``observations/pose``），而 ``robot/kinematics`` 在 robot-pipeline 包内（Edge 不反向依赖它）。
标定工具（robot-pipeline 侧）反过来复用本模块 → 帧模型只有一份。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

#: 旋转矩阵正交性校验容差（``RRᵀ = I`` / ``det = +1``）：标定产物与手写变换都过这道校验。
ROTATION_TOLERANCE = 1e-6

#: 单位变换（世界帧恒等对齐）。
IDENTITY: np.ndarray = np.eye(4, dtype=np.float64)


def is_rotation(rotation, tolerance: float = ROTATION_TOLERANCE) -> bool:
    """是否是合法旋转矩阵（正交 + 右手系）：``RRᵀ ≈ I`` 且 ``det ≈ +1``。"""
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        return False
    return bool(
        np.allclose(matrix @ matrix.T, np.eye(3), atol=tolerance)
        and abs(float(np.linalg.det(matrix)) - 1.0) <= tolerance
    )


def rpy_to_matrix(rpy) -> np.ndarray:
    """``[roll, pitch, yaw]`` → 旋转矩阵（``Rz(yaw)·Ry(pitch)·Rx(roll)``）。"""
    values = np.asarray(rpy, dtype=np.float64).reshape(-1)
    if values.size < 3:
        raise ValueError(f"rpy must have 3 entries, got {values.size}")
    roll, pitch, yaw = (float(values[0]), float(values[1]), float(values[2]))
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


def matrix_to_rpy(rotation) -> np.ndarray:
    """旋转矩阵 → ``[roll, pitch, yaw]``（``rpy_to_matrix`` 的逆；万向锁时 ``yaw = 0``）。"""
    matrix = np.asarray(rotation, dtype=np.float64)
    if matrix.shape != (3, 3):
        raise ValueError(f"rotation must be 3x3, got {matrix.shape}")
    sin_pitch = float(np.clip(-matrix[2, 0], -1.0, 1.0))
    pitch = float(np.arcsin(sin_pitch))
    cos_pitch = float(np.sqrt(max(0.0, 1.0 - sin_pitch * sin_pitch)))
    if cos_pitch > 1e-12:
        roll = float(np.arctan2(matrix[2, 1], matrix[2, 2]))
        yaw = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
        return np.array([roll, pitch, yaw], dtype=np.float64)
    # 万向锁：roll / yaw 只剩一个自由度，约定 yaw = 0，全部给 roll
    sign = 1.0 if sin_pitch >= 0.0 else -1.0
    roll = float(np.arctan2(sign * matrix[0, 1], sign * matrix[0, 2]))
    return np.array([roll, pitch, 0.0], dtype=np.float64)


def make_transform(rotation, translation) -> np.ndarray:
    """旋转 + 平移 → 4×4 齐次变换。"""
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = np.asarray(rotation, dtype=np.float64).reshape(3, 3)
    transform[:3, 3] = np.asarray(translation, dtype=np.float64).reshape(3)
    return transform


def pose_to_transform(pose) -> np.ndarray:
    """``[x, y, z, roll, pitch, yaw]`` → 4×4 齐次变换（与 ``observations/pose`` 同约定）。"""
    values = np.asarray(pose, dtype=np.float64).reshape(-1)
    if values.size < 6:
        raise ValueError(f"pose must have 6 entries, got {values.size}")
    return make_transform(rpy_to_matrix(values[3:6]), values[:3])


def transform_to_pose(transform) -> np.ndarray:
    """4×4 齐次变换 → ``[x, y, z, roll, pitch, yaw]``（``pose_to_transform`` 的逆）。"""
    matrix = as_transform(transform)
    return np.concatenate([matrix[:3, 3], matrix_to_rpy(matrix[:3, :3])])


def invert_transform(transform) -> np.ndarray:
    """求逆（用旋转转置，不做通用矩阵求逆——数值上更稳、更快）。"""
    matrix = as_transform(transform)
    inverse = np.eye(4, dtype=np.float64)
    inverse[:3, :3] = matrix[:3, :3].T
    inverse[:3, 3] = -matrix[:3, :3].T @ matrix[:3, 3]
    return inverse


def compose(*transforms) -> np.ndarray:
    """按**左到右**顺序组合（``compose(A, B) = A · B``：先施加 B，再施加 A）。

    ``compose()`` = 单位变换。链式意义即「坐标系逐级换到下一级」：
    ``compose(T_world_base, FK(q), T_flange_cam)`` = ``T_world_cam``。
    """
    result = IDENTITY.copy()
    for transform in transforms:
        result = result @ as_transform(transform)
    return result


def transform_points(transform, points) -> np.ndarray:
    """点变换：``(N, 3)`` / ``(3,)`` → 同形状（齐次提升内部完成）。"""
    matrix = as_transform(transform)
    array = np.asarray(points, dtype=np.float64)
    single = array.ndim == 1
    block = np.atleast_2d(array)
    if block.shape[1] != 3:
        raise ValueError(f"points must be (N, 3), got {block.shape}")
    moved = block @ matrix[:3, :3].T + matrix[:3, 3]
    return moved[0] if single else moved


def as_transform(values, *, name: str = "transform") -> np.ndarray:
    """任意形状的 4×4 / 16 数（**行主序**）→ 4×4 齐次变换；非法 → ``ValueError``。

    16 数平铺是标定产物（JSON）里的存储形状；这里同时校验旋转部分正交、最后一行 ``[0,0,0,1]``
    ——**半份 / 手改坏的产物不如没有**，宁可在这里报错。
    """
    if isinstance(values, np.ndarray) and values.shape == (4, 4):
        matrix = np.array(values, dtype=np.float64)
    else:
        flat = np.asarray(values, dtype=np.float64).reshape(-1)
        if flat.size != 16:
            raise ValueError(f"{name}: expected 4x4 or 16 numbers, got {flat.size}")
        matrix = flat.reshape(4, 4)
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name}: contains non-finite values")
    if not is_rotation(matrix[:3, :3]):
        raise ValueError(f"{name}: rotation part is not a valid rotation (orthonormal, det = +1)")
    if not np.allclose(matrix[3], np.array([0.0, 0.0, 0.0, 1.0]), atol=ROTATION_TOLERANCE):
        raise ValueError(f"{name}: last row must be [0, 0, 0, 1]")
    return matrix


def flatten_transform(transform) -> list[float]:
    """4×4 齐次变换 → 16 个浮点数（**行主序**平铺；标定产物的存储形状）。"""
    return [float(value) for value in as_transform(transform).reshape(-1)]


def as_pose_list(pose: Sequence[float] | None) -> list[float] | None:
    """``[xyz, rpy]`` → 浮点列表（缺值 → ``None``；回执/上报用，避免 numpy 标量漏到 JSON）。"""
    if pose is None:
        return None
    values = np.asarray(pose, dtype=np.float64).reshape(-1)
    return None if values.size < 6 else [float(value) for value in values[:6]]


__all__ = [
    "IDENTITY",
    "ROTATION_TOLERANCE",
    "as_pose_list",
    "as_transform",
    "compose",
    "flatten_transform",
    "invert_transform",
    "is_rotation",
    "make_transform",
    "matrix_to_rpy",
    "pose_to_transform",
    "rpy_to_matrix",
    "transform_points",
    "transform_to_pose",
]
