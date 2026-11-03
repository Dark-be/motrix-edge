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

"""Piper 六轴运动学（**Modified DH / Craig 约定**，纯 numpy）。

变换定义：

.. code-block:: text

    (i-1)T_i = Rx(alpha_{i-1}) · Tx(a_{i-1}) · Rz(theta_i) · Tz(d_i),  theta_i = q_i + theta_offset_i

末杆 ``link6`` 即法兰——与 SDK 的 ``get_flange_pose()`` 同一基准（现场标定对照用，不在运行时链路），
故 ``pose(q)`` 与 ``observations/pose`` 可直接比较（同一套 ``xyz + rpy`` 与 rpy 约定，见
``transforms.py``）。

**读 / 写约定**：关节 ``q`` 为 6 维弧度，顺序 ``j1..j6``，与 ``get_joint_angles()`` 一致；限位取
pyAgxArm 的机型 preset（SDK 的 ``set_joint_limits_enabled(True)`` 按同一份裁）再内缩
``JOINT_LIMIT_MARGIN``（见 ``PIPER_JOINT_LIMITS``），硬件保护仍由 SDK 负责。DH 与限位都可整体替换
（子类 / 依赖注入），现场按机型核对。

**限位只有这一份**：IK 返回的解与 ``PiperController.set_joint`` 下发前的裁切共用本表（控制器
构造时注入），所以「解算出来的」与「下发的」永远在同一限位内——SDK 不会收到越界值（它会给越界值
报错并打印）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .transforms import make_transform, matrix_to_rpy, rpy_to_matrix

# ---- Modified DH 参数（[alpha, a, d, theta_offset]；官方教程参数，现场用 verify_cartesian.py 核对）----
PIPER_DH: tuple[tuple[float, float, float, float], ...] = (
    (0.0, 0.0, 0.123, 0.0),  # j1
    (-np.pi / 2, 0.0, 0.0, -172.22 * np.pi / 180.0),  # j2
    (0.0, 0.28503, 0.0, -102.78 * np.pi / 180.0),  # j3
    (np.pi / 2, -0.021984, 0.25075, 0.0),  # j4
    (-np.pi / 2, 0.0, 0.0, 0.0),  # j5
    (np.pi / 2, 0.0, 0.091, 0.0),  # j6
)

# ---- 关节限位（弧度，[min, max]）----------------------------------------------------
# 机型限位取自 pyAgxArm 的 piper preset（``api/constants.py::ROBOT_JOINT_LIMIT_PRESET_RAD``）——
# SDK 的 ``set_joint_limits_enabled(True)`` 按同一份裁；j6 保留本仓更保守的 ±100°（SDK 允许 120°，
# 不借机放大）。生效软限位 = 机型限位内缩 ``JOINT_LIMIT_MARGIN``。
PIPER_MODEL_LIMITS: np.ndarray = np.array(
    [
        [-2.617994, 2.617994],  # j1 ±150°
        [0.0, 3.141593],  # j2 0 ~ 180°
        [-2.967060, 0.0],  # j3 -170° ~ 0
        [-1.745330, 1.745330],  # j4 ±100°
        [-1.221730, 1.221730],  # j5 ±70°
        [-1.745300, 1.745300],  # j6 ±100°
    ],
    dtype=np.float64,
)

#: 下发余量（rad）：软限位比机型限位再内缩这么多——边界值经 float32 舍入后可能比限位大一个 ulp，
#: 被按严格比较判越界（下发被拒 / 报错）。0 边界不内缩：0 在 float32 里精确，且厂商 home 就在 0 上。
JOINT_LIMIT_MARGIN = 0.001


def _shrink_limits(limits: np.ndarray, margin: float) -> np.ndarray:
    """机型限位 → 生效软限位（非 0 边界各向内缩 ``margin``）。"""
    lo = np.where(limits[:, 0] == 0.0, limits[:, 0], limits[:, 0] + margin)
    hi = np.where(limits[:, 1] == 0.0, limits[:, 1], limits[:, 1] - margin)
    return np.column_stack([lo, hi])


#: 生效软限位（IK 与下发前裁切共用这一份）
PIPER_JOINT_LIMITS: np.ndarray = _shrink_limits(PIPER_MODEL_LIMITS, JOINT_LIMIT_MARGIN)


@dataclass(frozen=True)
class DHLink:
    """Modified DH 单连杆参数（alpha/a 描述 ``i-1 → i`` 的固定部分，d/theta_offset 属关节 i）。"""

    alpha: float
    a: float
    d: float
    theta_offset: float


class PiperKinematics:
    """Piper 六轴运动学：正解（``fk`` / ``pose``）、基座系几何雅可比（``jacobian``）与限位处理。

    纯函数式（无内部状态），可安全跨线程复用（控制线程内解算，见设计文档「解算时机」）。
    """

    DOF = 6

    def __init__(
        self,
        links: tuple[tuple[float, float, float, float], ...] | None = None,
        joint_limits: np.ndarray | None = None,
    ):
        self.links = tuple(DHLink(*row) for row in (links if links is not None else PIPER_DH))
        if len(self.links) != self.DOF:
            raise ValueError(f"Piper kinematics expects {self.DOF} DH rows, got {len(self.links)}")
        limits = PIPER_JOINT_LIMITS if joint_limits is None else joint_limits
        limits = np.asarray(limits, dtype=np.float64)
        if limits.shape != (self.DOF, 2):
            raise ValueError(f"joint limits must have shape ({self.DOF}, 2), got {limits.shape}")
        if np.any(limits[:, 0] > limits[:, 1]):
            raise ValueError("joint limits must satisfy min <= max")
        self.joint_limits = limits

    # ---- 正向运动学 ----------------------------------------------------------------
    @staticmethod
    def _dh_matrix(link: DHLink, theta: float) -> np.ndarray:
        """``Rx(alpha)·Tx(a)·Rz(theta)·Tz(d)``（Modified DH 的齐次矩阵形式）。"""
        ca, sa = float(np.cos(link.alpha)), float(np.sin(link.alpha))
        ct, st = float(np.cos(theta)), float(np.sin(theta))
        return np.array(
            [
                [ct, -st, 0.0, link.a],
                [st * ca, ct * ca, -sa, -link.d * sa],
                [st * sa, ct * sa, ca, link.d * ca],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )

    def prefix_transforms(self, q: np.ndarray) -> list[np.ndarray]:
        """``[T_0_0(=I), T_0_1, ..., T_0_6]``——**共 7 个**：``result[i]`` 是关节 i 的原点系。

        雅可比用 ``result[i-1]`` 取关节 i 的轴，正解取最后一个元素。
        """
        q = np.asarray(q, dtype=np.float64).reshape(-1)
        if q.shape[0] != self.DOF:
            raise ValueError(f"joint vector must have {self.DOF} entries, got {q.shape[0]}")
        transforms = [np.eye(4, dtype=np.float64)]
        acc = transforms[0]
        for index, link in enumerate(self.links):
            acc = acc @ self._dh_matrix(link, float(q[index]) + link.theta_offset)
            transforms.append(acc)
        return transforms

    def fk(self, q: np.ndarray) -> np.ndarray:
        """正解：基座 → 法兰的 4×4 齐次变换。"""
        return self.prefix_transforms(q)[self.DOF]

    def pose(self, q: np.ndarray) -> np.ndarray:
        """基座 → 法兰位姿 ``[x, y, z, roll, pitch, yaw]``（米 / 弧度，与 edge 契约同约定）。"""
        transform = self.fk(q)
        return np.concatenate([transform[:3, 3], matrix_to_rpy(transform[:3, :3])])

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        """基座系几何雅可比（6×6，行 0-2 线速度 / 行 3-5 角速度）——与 ``pose`` 误差同系。

        关节 i 的转轴 = ``T_0_i`` 的 z 轴、轴上一点 = ``T_0_i`` 的原点（本实现约定；由
        ``robot-pipeline/tests/test_piper_kinematics.py`` 的有限差分用例逐列钉住——若改动 ``_dh_matrix``，
        该用例会立刻报错）。
        """
        transforms = self.prefix_transforms(q)
        end_position = transforms[self.DOF][:3, 3]
        jacobian = np.zeros((6, self.DOF), dtype=np.float64)
        for index in range(self.DOF):
            frame = transforms[index + 1]  # T_0_i（关节 i 的坐标系）
            axis = frame[:3, 2]  # 关节 i 的转轴（基座系）
            origin = frame[:3, 3]
            jacobian[:3, index] = np.cross(axis, end_position - origin)
            jacobian[3:, index] = axis
        return jacobian

    # ---- 位姿 ↔ 变换（求解器内部使用；也便于把观测位姿直接当目标）--------------------
    @staticmethod
    def pose_matrix(pose: np.ndarray) -> np.ndarray:
        """``[x, y, z, roll, pitch, yaw]`` → 4×4 齐次变换。"""
        pose = np.asarray(pose, dtype=np.float64).reshape(-1)
        if pose.shape[0] < 6:
            raise ValueError(f"pose must have 6 entries, got {pose.shape[0]}")
        return make_transform(rpy_to_matrix(pose[3:6]), pose[:3])

    # ---- 限位 ---------------------------------------------------------------------
    def clip_joints(self, q: np.ndarray) -> np.ndarray:
        """把关节向量裁切到软限位内（逐关节）。"""
        return np.clip(np.asarray(q, dtype=np.float64), self.joint_limits[:, 0], self.joint_limits[:, 1])

    def within_limits(self, q: np.ndarray, tol: float = 1e-9) -> bool:
        """关节向量是否在软限位内（含 ``tol`` 容差）。"""
        q = np.asarray(q, dtype=np.float64)
        return bool(np.all(q >= self.joint_limits[:, 0] - tol) and np.all(q <= self.joint_limits[:, 1] + tol))

    def clamp_margin(self, q: np.ndarray) -> float:
        """距最近限位的余量（rad，负数 = 已越界）——失败诊断 / 日志用。"""
        q = np.asarray(q, dtype=np.float64)
        return float(np.min(np.minimum(q - self.joint_limits[:, 0], self.joint_limits[:, 1] - q)))
