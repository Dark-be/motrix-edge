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

"""rpent.layout —— 外部动作块布局 + 几何换算（RPent 侧格式 ↔ edge 契约）。

两件事：

- **对外布局声明**：``server.rpent.action_layout``（如 ``rpent/dual_franka``）声明外部
  agent 的每臂块形态（``xyz(3) + rot6d(6) + 夹爪(1)``），由 :func:`resolve_layout` 解析成臂顺序；
- **几何换算**：旋转矩阵 ↔ rot6d / rpy / 四元数、角度包装、夹爪域换算（``+1/-1`` ↔ ``[0, 1]``）
  ——纯函数、无状态，便于单测与外部对齐（RPent 侧同名实现见其 ``utils``）。

取值（读观测键 / 解析调用形态）与编解码助手在 :mod:`motrix_edge.server.rpent.coerce`；动作块的逐帧
转换在 ``service.py``（``_convert_layout``），因为它要用到启用臂与当前位姿。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import RpentError

# ---- 外部动作块布局 -----------------------------------------------------------

# 笛卡尔动作每臂维数（契约固定）：``xyz(3) + rpy(3) + 夹爪(1)``（IK 归机器人侧）
CARTESIAN_ACTION_DIM_PER_ARM = 7

# 每臂块固定为 ``xyz(3) + rot6d(6) + 夹爪(1)``（RPent dual-Franka 的 20 维 = 2 臂 × 10）
RPENT_BLOCK_DIM = 10
LAYOUT_RPENT_DUAL_FRANKA = "rpent/dual_franka"
_RPENT_LAYOUTS: dict[str, tuple[str, ...]] = {LAYOUT_RPENT_DUAL_FRANKA: ("left", "right")}


def resolve_layout(name: str) -> tuple[str, ...]:
    """布局名 → 臂顺序；未知布局 → :class:`RpentError`（配置笔误要看得见，不静默降级）。"""
    arms = _RPENT_LAYOUTS.get(str(name))
    if arms is None:
        raise RpentError(f"unknown action_layout {name!r} (available: {sorted(_RPENT_LAYOUTS)})", kind="unsupported")
    return arms


def layout_block_dim() -> int:
    """外部布局的**每臂块**宽度（``xyz(3) + rot6d(6) + 夹爪(1)`` = :data:`RPENT_BLOCK_DIM`）。"""
    return RPENT_BLOCK_DIM


def layout_frame_dim(name: str) -> int:
    """该布局**一帧**的维度（= 臂数 × 每臂块）——校验外部动作块形状用。"""
    return len(resolve_layout(name)) * layout_block_dim()


def matrix_to_rot6d(matrix: np.ndarray) -> np.ndarray:
    """3×3 旋转矩阵 → rot6d（前两列，列主序；与 RPent ``_matrix_to_rot6d`` 对称）。"""
    array = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    return np.concatenate([array[:, 0], array[:, 1]]).astype(np.float64)


def rot6d_to_matrix(rot6d: Any) -> np.ndarray:
    """rot6d → 3×3 旋转矩阵：第三列 = 前两列叉乘（先 Gram-Schmidt 正交化，容忍网络噪声）。"""
    array = np.asarray(rot6d, dtype=np.float64).reshape(-1)
    if array.size != 6:
        raise ValueError(f"rot6d must have 6 elements, got {array.size}")
    c0, c1 = array[:3], array[3:]
    n0 = float(np.linalg.norm(c0))
    if n0 < 1e-8:
        raise ValueError("degenerate rot6d: first column is zero")
    c0 = c0 / n0
    c1 = c1 - c0 * float(c0 @ c1)
    n1 = float(np.linalg.norm(c1))
    if n1 < 1e-8:
        raise ValueError("degenerate rot6d: columns are collinear")
    c1 = c1 / n1
    return np.stack([c0, c1, np.cross(c0, c1)], axis=1)  # 列 = 基向量


def matrix_to_rpy(matrix: np.ndarray) -> np.ndarray:
    """旋转矩阵 → ``[roll, pitch, yaw]``（弧度，约定 ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)``）。

    万向锁（``|pitch| = π/2``）时 roll 取 0、yaw 吸收剩余旋转（此时姿态本身不可全参数化）。
    """
    m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    sp = float(np.clip(-m[2, 0], -1.0, 1.0))
    pitch = math.asin(sp)
    if abs(m[2, 0]) < 1.0 - 1e-9:
        roll = math.atan2(m[2, 1], m[2, 2])
        yaw = math.atan2(m[1, 0], m[0, 0])
    else:
        roll = 0.0
        yaw = -math.atan2(m[0, 1], m[1, 1])
    return np.asarray([roll, pitch, yaw], dtype=np.float64)


def rpy_to_matrix(rpy: Any) -> np.ndarray:
    """``[roll, pitch, yaw]`` → 旋转矩阵（``Rz @ Ry @ Rx``，与 :func:`matrix_to_rpy` 互逆）。"""
    roll, pitch, yaw = (float(v) for v in np.asarray(rpy, dtype=np.float64).reshape(-1))
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    return rz @ ry @ rx


def matrix_to_quat(matrix: np.ndarray) -> np.ndarray:
    """旋转矩阵 → 四元数 ``[x, y, z, w]``（RPent 的 ``tcp_pose[3:]`` 直接喂 ``from_quat``）。"""
    m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        quat = np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s])
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        quat = np.array([0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s])
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        quat = np.array([(m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s])
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        quat = np.array([(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s])
    return quat / float(np.linalg.norm(quat))  # [x, y, z, w]


def rpent_gripper_to_edge(value: Any) -> float:
    """外部夹爪命令 ``+1 = 张开 / -1 = 闭合`` → edge ``[0, 1]``（超界先限幅）。"""
    return (float(np.clip(float(value), -1.0, 1.0)) + 1.0) / 2.0


__all__ = [
    "CARTESIAN_ACTION_DIM_PER_ARM",
    "LAYOUT_RPENT_DUAL_FRANKA",
    "layout_block_dim",
    "layout_frame_dim",
    "RPENT_BLOCK_DIM",
    "matrix_to_quat",
    "matrix_to_rpy",
    "matrix_to_rot6d",
    "resolve_layout",
    "rot6d_to_matrix",
    "rpent_gripper_to_edge",
    "rpy_to_matrix",
]
