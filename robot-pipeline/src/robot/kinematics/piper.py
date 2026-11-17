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

FK、逐连杆变换、雅可比和 IK 都来自同一份由 AgileX 官方 URDF 转换的 MJCF。对 robot 层仍保持
``Pose6 = xyz+rpy``、4×4 FK 和「线速度在前」的既有接口；Pose7 与 MotrixSim 原生雅可比顺序只在
本适配层内部出现。
"""

from __future__ import annotations

import sysconfig
import threading
from dataclasses import dataclass
from pathlib import Path

import motrixsim
import numpy as np
from motrixsim import ik

from .transforms import make_transform, pose7_to_pose6, rpy_to_matrix

_MODEL_RELATIVE_PATH = Path("assets/robots/piper/mjcf/arm/piper.xml")
_SOURCE_MODEL_PATH = Path(__file__).resolve().parents[3] / _MODEL_RELATIVE_PATH
_INSTALLED_MODEL_PATH = Path(sysconfig.get_path("data")) / "share" / "robot-pipeline" / _MODEL_RELATIVE_PATH
PIPER_MODEL_PATH = _SOURCE_MODEL_PATH if _SOURCE_MODEL_PATH.is_file() else _INSTALLED_MODEL_PATH

# 该表不参与运行时 FK；保留为既有重力标定文件的模型兼容元数据。MJCF 的关节帧与该表同源。
PIPER_DH: tuple[tuple[float, float, float, float], ...] = (
    (0.0, 0.0, 0.123, 0.0),
    (-np.pi / 2, 0.0, 0.0, -172.22 * np.pi / 180.0),
    (0.0, 0.28503, 0.0, -102.78 * np.pi / 180.0),
    (np.pi / 2, -0.021984, 0.25075, 0.0),
    (-np.pi / 2, 0.0, 0.0, 0.0),
    (np.pi / 2, 0.0, 0.091, 0.0),
)

# 与当前控制器安全边界保持一致；J6 继续使用本仓更保守的 ±100°，不随后端切换放宽。
PIPER_MODEL_LIMITS: np.ndarray = np.array(
    [
        [-2.617994, 2.617994],
        [0.0, 3.141593],
        [-2.967060, 0.0],
        [-1.745330, 1.745330],
        [-1.221730, 1.221730],
        [-1.745300, 1.745300],
    ],
    dtype=np.float64,
)
JOINT_LIMIT_MARGIN = 0.001


def _shrink_limits(limits: np.ndarray, margin: float) -> np.ndarray:
    lo = np.where(limits[:, 0] == 0.0, limits[:, 0], limits[:, 0] + margin)
    hi = np.where(limits[:, 1] == 0.0, limits[:, 1], limits[:, 1] - margin)
    return np.column_stack([lo, hi])


PIPER_MODEL_LIMITS.setflags(write=False)
PIPER_JOINT_LIMITS: np.ndarray = _shrink_limits(PIPER_MODEL_LIMITS, JOINT_LIMIT_MARGIN)
PIPER_JOINT_LIMITS.setflags(write=False)


@dataclass(frozen=True)
class DHLink:
    """重力标定文件使用的历史 DH 兼容元数据；运行时运动学由 MJCF 计算。"""

    alpha: float
    a: float
    d: float
    theta_offset: float


class PiperKinematics:
    """线程安全的 MotrixSim Piper 适配器，保持 robot 层既有 Pose6 接口。"""

    DOF = 6

    def __init__(
        self,
        links: tuple[tuple[float, float, float, float], ...] | None = None,
        joint_limits: np.ndarray | None = None,
    ):
        rows = PIPER_DH if links is None else links
        self.links = tuple(DHLink(*row) for row in rows)
        if len(self.links) != self.DOF:
            raise ValueError(f"Piper kinematics expects {self.DOF} DH rows, got {len(self.links)}")
        if not np.allclose(np.asarray(rows, dtype=np.float64), np.asarray(PIPER_DH, dtype=np.float64)):
            raise ValueError("custom DH links are unsupported by the MotrixSim MJCF backend")

        limits = PIPER_JOINT_LIMITS if joint_limits is None else np.asarray(joint_limits, dtype=np.float64)
        if limits.shape != (self.DOF, 2):
            raise ValueError(f"joint limits must have shape ({self.DOF}, 2), got {limits.shape}")
        if not np.all(np.isfinite(limits)):
            raise ValueError("joint limits must contain finite values")
        if np.any(limits[:, 0] > limits[:, 1]):
            raise ValueError("joint limits must satisfy min <= max")
        self.joint_limits = limits.copy()
        self.joint_limits.setflags(write=False)

        if not PIPER_MODEL_PATH.is_file():
            raise FileNotFoundError(f"Piper MJCF not found: {PIPER_MODEL_PATH}")
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

    def prefix_transforms(self, q: np.ndarray) -> list[np.ndarray]:
        """返回 ``[I, T_base_link1, ..., T_base_link6]``，全部由 MotrixSim MJCF 正解生成。"""
        data = self._set_state(q)
        poses = np.asarray(self._model.get_link_poses(data), dtype=np.float64)
        rotations = np.asarray(self._model.get_link_rotation_mats(data), dtype=np.float64)
        if poses.shape != (self.DOF, 7) or rotations.shape != (self.DOF, 3, 3):
            raise RuntimeError(
                f"Piper MJCF link transforms have unexpected shapes: poses={poses.shape}, rotations={rotations.shape}"
            )
        transforms = [np.eye(4, dtype=np.float64)]
        transforms.extend(make_transform(rotations[index], poses[index, :3]) for index in range(self.DOF))
        return transforms

    def fk(self, q: np.ndarray) -> np.ndarray:
        """基座到法兰 ``link6`` 的 4×4 齐次变换。"""
        return self.prefix_transforms(q)[-1]

    def pose7(self, q: np.ndarray) -> np.ndarray:
        """MotrixSim 原生 Pose7：``[x, y, z, qx, qy, qz, qw]``。"""
        return np.asarray(self._chain.get_end_effector_pose(self._set_state(q)), dtype=np.float64)

    def pose(self, q: np.ndarray) -> np.ndarray:
        """robot 层 Pose6：``[x, y, z, roll, pitch, yaw]``（米 / 弧度）。"""
        return pose7_to_pose6(self.pose7(q))

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        """基座系几何雅可比，保持公开约定：前三行线速度，后三行角速度。"""
        native = np.asarray(self._chain.get_end_effector_jac(self._set_state(q)), dtype=np.float64)
        if native.shape != (6, self.DOF):
            raise RuntimeError(f"Piper MJCF Jacobian has unexpected shape {native.shape}")
        return np.vstack([native[3:, :], native[:3, :]])

    def solve_dls(
        self,
        target_pose7: np.ndarray,
        seed: np.ndarray,
        *,
        max_iters: int,
        step_size: float,
        tolerance: float,
        damping: float,
    ) -> np.ndarray:
        """调用 MotrixSim DLS；返回 ``[iterations, residual, q...]``。"""
        data = self._set_state(seed)
        solver = ik.DlsSolver(
            max_iter=max_iters,
            step_size=step_size,
            tolerance=tolerance,
            damping=damping,
            adaptive_damping=False,
        )
        return np.asarray(solver.solve(self._chain, data, target_pose7), dtype=np.float64).reshape(-1)

    @staticmethod
    def pose_matrix(pose: np.ndarray) -> np.ndarray:
        """``[x, y, z, roll, pitch, yaw]`` → 4×4 齐次变换。"""
        values = np.asarray(pose, dtype=np.float64).reshape(-1)
        if values.shape != (6,) or not np.all(np.isfinite(values)):
            raise ValueError("pose must contain exactly six finite values")
        return make_transform(rpy_to_matrix(values[3:]), values[:3])

    def clip_joints(self, q: np.ndarray) -> np.ndarray:
        return np.clip(np.asarray(q, dtype=np.float64), self.joint_limits[:, 0], self.joint_limits[:, 1])

    def within_limits(self, q: np.ndarray, tol: float = 1e-9) -> bool:
        values = np.asarray(q, dtype=np.float64)
        return bool(
            values.shape == (self.DOF,)
            and np.all(np.isfinite(values))
            and np.all(values >= self.joint_limits[:, 0] - tol)
            and np.all(values <= self.joint_limits[:, 1] + tol)
        )

    def clamp_margin(self, q: np.ndarray) -> float:
        values = np.asarray(q, dtype=np.float64)
        return float(np.min(np.minimum(values - self.joint_limits[:, 0], self.joint_limits[:, 1] - values)))
