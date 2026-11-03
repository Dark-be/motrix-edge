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

"""重力模型标定：位形规划、判稳、样本统计与参数拟合。

本模块只处理标定流程和产物生成；运行期模型与前馈补偿位于
``robot.dynamics.gravity``。依赖方向保持为 calibration → dynamics。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from robot.dynamics.gravity import (
    N_PARAMS,
    PI_PER_LINK,
    T_FF_LIMIT_NM,
    GravityModel,
    default_kinematics,
    gravity_regressor,
)

DEFAULT_SETTLE = {"vel": 0.02, "dq": 0.002, "dtau": 0.05, "ticks": 10}
DEFAULT_WINDOW = 15
DEFAULT_MATCH_DISTANCE = 0.02


def within_limits(q, limits) -> bool:
    """判断关节向量是否落在逐关节软限位内；``None`` 表示不检查。"""
    if limits is None:
        return True
    array = np.asarray(q, dtype=np.float64).reshape(-1)
    table = np.asarray(limits, dtype=np.float64)
    return bool(np.all(array >= table[:, 0]) and np.all(array <= table[:, 1]))


def plan_waypoints(q_home, *, span=0.3, limits=None, combined=0, seed=0):
    """生成逐关节正反位形和可选的确定性多关节组合位形。"""
    home = np.asarray(q_home, dtype=np.float64).reshape(-1).copy()
    points: list[tuple[str, np.ndarray]] = []
    dropped: list[str] = []

    def _add(label: str, q: np.ndarray) -> None:
        if within_limits(q, limits):
            points.append((label, q))
        else:
            dropped.append(label)

    for joint in range(home.size):
        for sign in (+1, -1):
            q = home.copy()
            q[joint] += sign * span
            _add(f"j{joint + 1}{'+' if sign > 0 else '-'}", q)

    rng = np.random.default_rng(seed)
    for index in range(max(0, int(combined))):
        q = home + rng.uniform(-span / 2.0, span / 2.0, size=home.size)
        _add(f"combo{index + 1}", q)
    return points, dropped


class SettleDetector:
    """连续若干拍满足速度、位移和力矩变化阈值后判稳。"""

    def __init__(self, params=None):
        merged = {**DEFAULT_SETTLE, **(params or {})}
        self.vel = float(merged["vel"])
        self.dq = float(merged["dq"])
        self.dtau = float(merged["dtau"])
        self.ticks = int(merged["ticks"])
        self.reset()

    def reset(self) -> None:
        """清除历史状态，开始检测下一个位形。"""
        self._streak = 0
        self._prev: tuple[np.ndarray, np.ndarray] | None = None
        self.stable = False

    def update(self, q, vel, tau) -> bool:
        """输入一拍关节状态并返回当前是否稳定。"""
        if self.stable:
            return True
        state = (np.asarray(q, dtype=np.float64).reshape(-1), np.asarray(tau, dtype=np.float64).reshape(-1))
        velocity = np.asarray(vel, dtype=np.float64).reshape(-1)
        if self._prev is None:
            self._streak = 1 if float(np.max(np.abs(velocity))) < self.vel else 0
        else:
            moved = float(np.max(np.abs(state[0] - self._prev[0])))
            bumped = float(np.max(np.abs(state[1] - self._prev[1])))
            quiet = float(np.max(np.abs(velocity))) < self.vel and moved < self.dq and bumped < self.dtau
            self._streak = self._streak + 1 if quiet else 0
        self._prev = state
        self.stable = self._streak >= self.ticks
        return self.stable


def window_stats(records) -> dict:
    """计算稳态窗口的关节、力矩统计和速度峰值。"""
    if not records:
        raise ValueError("window_stats requires at least one record")
    q = np.stack([np.asarray(item["q"], dtype=np.float64).reshape(-1) for item in records])
    tau = np.stack([np.asarray(item["tau"], dtype=np.float64).reshape(-1) for item in records])
    vel = np.stack([np.asarray(item["vel"], dtype=np.float64).reshape(-1) for item in records])
    return {
        "q": q.mean(axis=0),
        "tau": tau.mean(axis=0),
        "tau_std": tau.std(axis=0),
        "vel_absmax": float(np.max(np.abs(vel))),
        "n": int(q.shape[0]),
    }


def friction_from_passes(forward, backward, *, max_distance=DEFAULT_MATCH_DISTANCE) -> dict:
    """将正反两遍的邻近位形配对，估计每关节库仑摩擦幅值。"""
    forward_items = [(_as_vector(item["q"]), _as_vector(item["tau"])) for item in forward]
    backward_items = [(_as_vector(item["q"]), _as_vector(item["tau"])) for item in backward]
    if not forward_items or not backward_items:
        dim = len(forward_items[0][0]) if forward_items else (len(backward_items[0][0]) if backward_items else 0)
        return {"paired": 0, "per_joint": np.zeros(dim), "mean": 0.0, "max": 0.0}

    dim = forward_items[0][0].size
    deltas: list[np.ndarray] = []
    used: set[int] = set()
    for q_forward, tau_forward in forward_items:
        distances = [
            float(np.linalg.norm(q_forward - q_backward)) if index not in used else np.inf
            for index, (q_backward, _) in enumerate(backward_items)
        ]
        best = int(np.argmin(distances))
        if not np.isfinite(distances[best]) or distances[best] > max_distance:
            continue
        used.add(best)
        deltas.append(np.abs(tau_forward - backward_items[best][1]) / 2.0)

    if not deltas:
        return {"paired": 0, "per_joint": np.zeros(dim), "mean": 0.0, "max": 0.0}
    per_joint = np.mean(np.stack(deltas), axis=0)
    return {
        "paired": len(deltas),
        "per_joint": per_joint,
        "mean": float(np.mean(per_joint)),
        "max": float(np.max(per_joint)),
    }


def summarize_samples(samples, *, friction=None) -> dict:
    """汇总采样范围、力矩峰值、噪声和限幅余量。"""
    if not samples:
        return {"n": 0, "exceeds_t_ff_limit": False, "friction": friction}
    q = np.stack([_as_vector(item["q"]) for item in samples])
    tau = np.stack([_as_vector(item["tau"]) for item in samples])
    stds = np.stack([_as_vector(item["tau_std"]) for item in samples if item.get("tau_std") is not None])
    abs_max = float(np.max(np.abs(tau)))
    flat_index = int(np.argmax(np.abs(tau)))
    return {
        "n": len(samples),
        "t_ff_limit_nm": T_FF_LIMIT_NM,
        "tau_abs_max": abs_max,
        "tau_abs_max_joint": flat_index % tau.shape[1] + 1,
        "exceeds_t_ff_limit": bool(abs_max > T_FF_LIMIT_NM),
        "tau_abs_max_headroom": float(T_FF_LIMIT_NM - abs_max),
        "q_spread": np.stack([q.min(axis=0), q.max(axis=0)]),
        "tau_std_max": float(np.max(stds)) if stds.size else None,
        "friction": friction,
    }


@dataclass(frozen=True)
class GravityFit:
    """重力参数拟合结果及其秩、条件数和残差指标。"""

    model: GravityModel
    n_samples: int
    n_equations: int
    n_params: int
    rank: int
    cond: float
    ridge: float
    residual_rms: float
    residual_max: float
    residual_rms_per_joint: np.ndarray

    @property
    def underdetermined(self) -> bool:
        """方程数或秩是否不足以唯一确定全部参数。"""
        return self.n_equations < self.n_params or self.rank < self.n_params

    def report(self) -> str:
        """生成供现场判读的多行拟合报告。"""
        ill_conditioned = "（病态：位形激励不足）" if self.cond > 1e6 else ""
        lines = [
            f"样本 {self.n_samples} 条 / 方程 {self.n_equations} 个 / 参数 {self.n_params} 个",
            f"秩 {self.rank} / 条件数 {self.cond:.3g}{ill_conditioned}",
            f"残差 RMS {self.residual_rms:.4f} N·m / 最大 {self.residual_max:.4f} N·m",
            "逐关节 RMS " + np.array2string(self.residual_rms_per_joint, precision=4, suppress_small=True),
        ]
        if self.underdetermined:
            lines.append("⚠️ 欠定（方程数 / 秩 < 参数数）：解只是最小范数解，超出采样域不可信")
        return "\n".join(lines)


def fit_gravity(samples, *, kinematics=None, ridge=0.0) -> GravityFit:
    """以最小二乘或岭回归从实测 ``q`` / ``tau`` 拟合重力参数。"""
    pairs = [(_as_vector(item["q"]), _as_vector(item["tau"])) for item in samples]
    if not pairs:
        raise ValueError("fit_gravity requires at least one sample")
    kinematics = default_kinematics() if kinematics is None else kinematics
    regressors = np.stack([gravity_regressor(q, kinematics) for q, _ in pairs])
    torques = np.stack([tau for _, tau in pairs])
    matrix = regressors.reshape(-1, N_PARAMS)
    target = torques.reshape(-1)
    if ridge > 0:
        gram = matrix.T @ matrix + float(ridge) * np.eye(N_PARAMS)
        params = np.linalg.solve(gram, matrix.T @ target)
    else:
        params = np.linalg.lstsq(matrix, target, rcond=None)[0]

    residual = (matrix @ params - target).reshape(torques.shape)
    singular = np.linalg.svd(matrix, compute_uv=False)
    cond = float(singular[0] / singular[-1]) if singular[-1] > 0 else float("inf")
    return GravityFit(
        model=GravityModel(params.reshape(kinematics.DOF, PI_PER_LINK), kinematics=kinematics),
        n_samples=len(pairs),
        n_equations=int(matrix.shape[0]),
        n_params=N_PARAMS,
        rank=int(np.linalg.matrix_rank(matrix)),
        cond=cond,
        ridge=float(ridge),
        residual_rms=float(np.sqrt(np.mean(residual**2))),
        residual_max=float(np.max(np.abs(residual))),
        residual_rms_per_joint=np.sqrt(np.mean(residual**2, axis=0)),
    )


def save_samples(path, payload) -> Path:
    """将采样载荷保存为 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_samples(path) -> dict:
    """读取 :func:`save_samples` 生成的 JSON。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_vector(value) -> np.ndarray:
    return np.asarray(value, dtype=np.float64).reshape(-1)


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
