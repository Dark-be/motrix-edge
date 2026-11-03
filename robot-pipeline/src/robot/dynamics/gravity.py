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

"""重力动力学模型与运行期前馈。

本模块只依赖 NumPy 和共享运动学，不碰控制器或硬件 SDK。标定位形、判稳和拟合流程位于
``robot.calibration.gravity``；运行期 Controller 只依赖本模块。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

T_FF_LIMIT_NM = 16.0
GRAVITY = 9.80665
GRAVITY_VECTOR = np.array([0.0, 0.0, -GRAVITY], dtype=np.float64)
PI_PER_LINK = 4
N_PARAMS = PI_PER_LINK * 6
DEFAULT_PARAMS_RELATIVE = "gravity/piper_6dof.json"
DEFAULT_ALPHA = 1.0
PARAMS_KIND = "piper_gravity_link_params"

_DEFAULT_KINEMATICS = None


def default_kinematics():
    """返回与 FK、IK、软限位共用的惰性运动学单例。"""
    global _DEFAULT_KINEMATICS
    if _DEFAULT_KINEMATICS is None:
        from robot.kinematics import PiperKinematics

        _DEFAULT_KINEMATICS = PiperKinematics()
    return _DEFAULT_KINEMATICS


def gravity_regressor(q, kinematics=None) -> np.ndarray:
    """计算 ``τ_g(q) = Y(q)·π`` 的重力回归矩阵 ``Y``。"""
    kinematics = default_kinematics() if kinematics is None else kinematics
    transforms = kinematics.prefix_transforms(q)
    rotations = [transform[:3, :3] for transform in transforms]
    origins = [transform[:3, 3] for transform in transforms]
    regressor = np.zeros((kinematics.DOF, N_PARAMS), dtype=np.float64)
    for joint in range(kinematics.DOF):
        axis = rotations[joint + 1][:, 2]
        wrench = np.cross(GRAVITY_VECTOR, axis)
        for link in range(joint, kinematics.DOF):
            column = PI_PER_LINK * link
            regressor[joint, column] = -float(wrench @ (origins[link + 1] - origins[joint + 1]))
            regressor[joint, column + 1 : column + PI_PER_LINK] = -rotations[link + 1].T @ wrench
    return regressor


class GravityModel:
    """重力模型 ``τ̂_g(q) = Y(q)·π``；全零参数表示不产生前馈的占位模型。"""

    DOF = 6

    def __init__(self, link_params, *, kinematics=None, source=None):
        params = np.asarray(link_params, dtype=np.float64)
        if params.shape != (self.DOF, PI_PER_LINK):
            raise ValueError(f"link_params must have shape ({self.DOF}, {PI_PER_LINK}), got {params.shape}")
        if not np.all(np.isfinite(params)):
            raise ValueError("link_params contains non-finite values")
        self.params = params.copy()
        self.kinematics = default_kinematics() if kinematics is None else kinematics
        self.source = None if source is None else str(source)
        self.is_placeholder = bool(np.all(self.params == 0.0))

    def torque(self, q) -> np.ndarray:
        """按同拍实测关节角计算重力力矩。"""
        return gravity_regressor(q, self.kinematics) @ self.params.reshape(-1)

    def describe(self) -> str:
        """返回用于运行日志的模型描述。"""
        if self.is_placeholder:
            return "占位参数（全 0）→ t_ff 恒为 0"
        return f"非占位参数（来源 {self.source or '内存'}）"


def dh_rows(kinematics=None) -> list[list[float]]:
    """返回用于参数兼容性校验的当前 DH 表。"""
    kinematics = default_kinematics() if kinematics is None else kinematics
    return [[float(link.alpha), float(link.a), float(link.d), float(link.theta_offset)] for link in kinematics.links]


def save_gravity_params(path, model, *, extra=None) -> Path:
    """保存重力参数及其运动学兼容信息。"""
    payload = {
        "version": 1,
        "kind": PARAMS_KIND,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "placeholder": bool(model.is_placeholder),
        "dof": int(model.DOF),
        "layout": "row k = link k+1: [m (kg), m*cx, m*cy, m*cz (kg*m)] in link frame T_0_{k+1}",
        "gravity": float(GRAVITY),
        "dh": dh_rows(model.kinematics),
        "link_params": model.params.tolist(),
    }
    payload.update(_to_jsonable(extra or {}))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_gravity_params(path, kinematics=None) -> GravityModel:
    """加载重力参数，并拒绝维度或 DH 与当前运动学不一致的产物。"""
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"重力参数文件不存在：{source}（先跑 scripts/fit_gravity.py）")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("kind") != PARAMS_KIND:
        raise ValueError(f"{source}: 不是重力参数文件（kind != {PARAMS_KIND!r}）")
    kinematics = default_kinematics() if kinematics is None else kinematics
    if int(payload.get("dof", 0)) != int(kinematics.DOF):
        raise ValueError(f"{source}: dof {payload.get('dof')} != 运动学 {kinematics.DOF}")
    stored_dh = payload.get("dh")
    if stored_dh is not None:
        current = np.asarray(dh_rows(kinematics), dtype=np.float64)
        if not np.allclose(np.asarray(stored_dh, dtype=np.float64), current):
            raise ValueError(f"{source}: DH 与当前运动学不一致（参数是用另一套 DH 标定的，装载会算错）")
    params = payload.get("link_params")
    if params is None:
        raise ValueError(f"{source}: 缺少 link_params")
    return GravityModel(params, kinematics=kinematics, source=source)


def resolve_arm_config(section, arm) -> dict:
    """把公共 ``robot.gravity`` 配置与指定臂的覆盖合并。"""
    section = dict(section or {})
    overrides = dict((section.pop("arms", None) or {}).get(arm) or {})
    merged = {**section, **overrides}
    paths = merged.get("params")
    if isinstance(paths, dict):
        merged["params"] = paths.get(arm) or paths.get("default")
    return merged


class GravityCompensator:
    """运行期重力前馈，包含比例、限幅和异常降级。"""

    def __init__(self, model, *, alpha=DEFAULT_ALPHA, limit=T_FF_LIMIT_NM, enabled=True):
        if not np.isfinite(alpha):
            raise ValueError(f"alpha must be finite, got {alpha}")
        self.model = model
        self.alpha = float(alpha)
        self.limit = float(min(abs(limit), T_FF_LIMIT_NM))
        self.enabled = bool(enabled)
        self.degrades = 0
        self.clips = 0
        self.last_note: str | None = None
        self.last_torque: np.ndarray | None = None

    @property
    def active(self) -> bool:
        """模型当前是否会产生非零前馈。"""
        return bool(self.enabled and self.alpha != 0.0 and not self.model.is_placeholder)

    def torque(self, q_meas) -> np.ndarray:
        """计算并限幅本拍前馈；任意异常降级为全零。"""
        if not self.active:
            return self._set(np.zeros(self.model.DOF, dtype=np.float64), None)
        q = np.asarray(q_meas, dtype=np.float64).reshape(-1)
        if q.size != self.model.DOF or not np.all(np.isfinite(q)):
            return self._degrade(f"关节角不可用（dim={q.size}）")
        try:
            predicted = self.model.torque(q)
        except Exception as exc:  # noqa: BLE001 控制拍必须退回纯位置环
            return self._degrade(f"模型求值失败：{exc}")
        if not np.all(np.isfinite(predicted)):
            return self._degrade("模型输出非有限值")
        tau = self.alpha * predicted
        clipped = np.clip(tau, -self.limit, self.limit)
        if np.array_equal(clipped, tau):
            return self._set(clipped, None)
        self.clips += 1
        return self._set(clipped, f"前馈限幅（±{self.limit:g} N·m）：该关节未完全补偿")

    def note_failure(self, reason) -> np.ndarray:
        """记录外部取数失败并返回全零前馈。"""
        return self._degrade(str(reason))

    def status(self) -> dict:
        """返回健康检查和现场验收所需的运行状态。"""
        return {
            "active": self.active,
            "enabled": self.enabled,
            "alpha": self.alpha,
            "limit_nm": self.limit,
            "placeholder": bool(self.model.is_placeholder),
            "source": self.model.source,
            "degrades": self.degrades,
            "clips": self.clips,
            "note": self.last_note,
            "torque_ff": None if self.last_torque is None else self.last_torque.tolist(),
        }

    def _set(self, tau: np.ndarray, note: str | None) -> np.ndarray:
        self.last_torque = tau
        self.last_note = note
        return tau

    def _degrade(self, reason: str | None = None) -> np.ndarray:
        if reason is not None:
            self.degrades += 1
            reason = f"{reason} → 本拍 t_ff = 0（退回纯位置环）"
        return self._set(np.zeros(self.model.DOF, dtype=np.float64), reason)


def _to_jsonable(value):
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value
