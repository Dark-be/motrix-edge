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

"""Piper 六轴 MotrixSim 运动学后端。

FK、雅可比和 IK 都来自同一份由 AgileX 官方 URDF 转换的 MJCF；生产代码不再维护或使用简化 DH。
"""

from __future__ import annotations

import sysconfig
import threading
from pathlib import Path

import motrixsim
import numpy as np
from motrixsim import ik

_MODEL_RELATIVE_PATH = Path("assets/robots/piper/mjcf/arm/piper.xml")
_SOURCE_MODEL_PATH = Path(__file__).resolve().parents[3] / _MODEL_RELATIVE_PATH
_INSTALLED_MODEL_PATH = Path(sysconfig.get_path("data")) / "share" / "robot-pipeline" / _MODEL_RELATIVE_PATH
PIPER_MODEL_PATH = _SOURCE_MODEL_PATH if _SOURCE_MODEL_PATH.is_file() else _INSTALLED_MODEL_PATH

# 真机限位取 SDK 的 Piper 弧度 preset。MJCF 的 J6 ±170° 是机械范围，但 SDK 当前只接受 ±120°。
PIPER_SDK_LIMITS: np.ndarray = np.array(
    [
        [-2.617994, 2.617994],
        [0.0, 3.141593],
        [-2.967060, 0.0],
        [-1.745330, 1.745330],
        [-1.221730, 1.221730],
        [-2.094396, 2.094396],
    ],
    dtype=np.float64,
)
JOINT_LIMIT_MARGIN = 0.001


def _shrink_limits(limits: np.ndarray, margin: float) -> np.ndarray:
    lo = np.where(limits[:, 0] == 0.0, limits[:, 0], limits[:, 0] + margin)
    hi = np.where(limits[:, 1] == 0.0, limits[:, 1], limits[:, 1] - margin)
    return np.column_stack([lo, hi])


PIPER_SDK_LIMITS.setflags(write=False)
PIPER_JOINT_LIMITS: np.ndarray = _shrink_limits(PIPER_SDK_LIMITS, JOINT_LIMIT_MARGIN)
PIPER_JOINT_LIMITS.setflags(write=False)


class PiperKinematics:
    """线程安全的 MotrixSim Piper FK/IK 适配器，保持现有控制器接口。"""

    DOF = 6

    def __init__(self, joint_limits: np.ndarray | None = None):
        limits = PIPER_JOINT_LIMITS if joint_limits is None else np.asarray(joint_limits, dtype=np.float64)
        if limits.shape != (self.DOF, 2):
            raise ValueError(f"joint limits must have shape ({self.DOF}, 2), got {limits.shape}")
        if not np.all(np.isfinite(limits)):
            raise ValueError("joint limits must contain finite values")
        if np.any(limits[:, 0] > limits[:, 1]):
            raise ValueError("joint limits must satisfy min <= max")
        self.joint_limits = limits.copy()
        self.joint_limits.setflags(write=False)
        self._model = motrixsim.load_model(str(PIPER_MODEL_PATH))
        self._chain = ik.IkChain(self._model, end_link="link6")
        if self._chain.num_dof_pos != self.DOF:
            raise RuntimeError(f"Piper MJCF must expose {self.DOF} DoF, got {self._chain.num_dof_pos}")
        self._thread_state = threading.local()

    def _data(self):
        data = getattr(self._thread_state, "data", None)
        if data is None:
            data = motrixsim.SceneData(self._model)
            self._thread_state.data = data
        return data

    def _set_state(self, q: np.ndarray):
        values = np.asarray(q, dtype=np.float64).reshape(-1)
        if values.shape != (self.DOF,):
            raise ValueError(f"joint vector must have {self.DOF} entries, got {values.shape[0]}")
        if not np.all(np.isfinite(values)):
            raise ValueError("joint vector must contain finite values")
        data = self._data()
        data.set_dof_pos(values, self._model)
        self._model.forward_kinematic(data)
        return data

    def pose(self, q: np.ndarray) -> np.ndarray:
        """MotrixSim 原生 Pose7：``[x, y, z, qx, qy, qz, qw]``。"""
        return np.asarray(self._chain.get_end_effector_pose(self._set_state(q)), dtype=np.float64)

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        """MotrixSim 原生雅可比：前三行为角速度，后三行为线速度。"""
        return np.asarray(self._chain.get_end_effector_jac(self._set_state(q)), dtype=np.float64)

    def solve_dls(
        self,
        target_pose: np.ndarray,
        seed: np.ndarray,
        *,
        max_iters: int,
        step_size: float,
        tolerance: float,
        damping: float,
    ) -> np.ndarray:
        data = self._set_state(seed)
        solver = ik.DlsSolver(
            max_iter=max_iters,
            step_size=step_size,
            tolerance=tolerance,
            damping=damping,
            adaptive_damping=False,
        )
        return np.asarray(solver.solve(self._chain, data, target_pose), dtype=np.float64).reshape(-1)

    def clip_joints(self, q: np.ndarray) -> np.ndarray:
        return np.clip(np.asarray(q, dtype=np.float64), self.joint_limits[:, 0], self.joint_limits[:, 1])

    def within_limits(self, q: np.ndarray, tol: float = 1e-9) -> bool:
        q = np.asarray(q, dtype=np.float64)
        return bool(np.all(q >= self.joint_limits[:, 0] - tol) and np.all(q <= self.joint_limits[:, 1] + tol))
