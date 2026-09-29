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

"""重力补偿的**标定期纯逻辑**（不碰 SDK、不碰硬件，只依赖 numpy）。

设计见 ``wiki/design/robot_pipeline_impedance.md``：重力项只对每连杆的「质量 + 一阶质量矩」
（``π_i = (m_i, m_i·c_i)``，4 个参数）线性，所以标定的产出是一小组系数而不是查表；本模块负责
**取数阶段**的那一半——位形规划、判稳、窗口统计、摩擦估计与样本存取（回归器 ``Y(q)`` 与
``GravityModel`` 见实施计划阶段 1）。

为什么悬停不需要额外控制器：MIT 位置环本身就会停住。以 30 Hz 持续重发同一目标时，臂自动停在
``q_ss = q_des - τ_g(q_ss) / k_p``，所以**采样配对的是「实测 q」+「实测 τ」**——稳态点不是指令
点（这正是要量的东西）。

⚠️ 本模块**不 import 现场依赖**（pyAgxArm / 控制器），以便在没有 SDK 的环境里被单测直接导入。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

#: ``t_ff`` 的固件硬限幅：V188 为 12-bit、**全关节 ±16 N·m**（旧版驱动是 1–3 轴 ±32 / 4–6 轴 ±8）。
T_FF_LIMIT_NM = 16.0
#: 标准重力加速度（m/s²）与其基坐标向量（z 轴向上）。
GRAVITY = 9.80665
GRAVITY_VECTOR = np.array([0.0, 0.0, -GRAVITY], dtype=np.float64)

#: 判稳缺省阈值：速度上限（rad/s）、单拍位移上限（rad）、单拍力矩变化上限（N·m）、连续拍数。
DEFAULT_SETTLE = {"vel": 0.02, "dq": 0.002, "dtau": 0.05, "ticks": 10}
#: 稳态窗口缺省拍数（取均值 / 标准差的样本数）。
DEFAULT_WINDOW = 15
#: 正反两遍配对时允许的关节角距离（rad）：超过就认为不是同一个位形，不配对。
DEFAULT_MATCH_DISTANCE = 0.02


def within_limits(q, limits) -> bool:
    """``q`` 是否落在逐关节软限位内（``limits`` 为 ``(DOF, 2)`` 的 ``[lo, hi]``；``None`` 不检查）。"""
    if limits is None:
        return True
    array = np.asarray(q, dtype=np.float64).reshape(-1)
    table = np.asarray(limits, dtype=np.float64)
    return bool(np.all(array >= table[:, 0]) and np.all(array <= table[:, 1]))


def plan_waypoints(q_home, *, span=0.3, limits=None, combined=0, seed=0):
    """规划标定位形：**逐关节 ±span**（一次只动一个关节，风险最小）+ 可选 ``combined`` 个多关节组合。

    多关节组合用固定种子的伪随机偏移（±``span/2``）生成——确定性，便于重跑与比对。

    Returns:
        ``(points, dropped)``：``points`` = ``[(label, q), ...]``；``dropped`` = 因越界被丢弃的
        ``label`` 列表（现场据此收窄 ``span`` 或改基准位形）。
    """
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
    """判稳：连续 ``ticks`` 拍满足「速度小、位移小、力矩变化小」→ 认为已停稳。

    判据用**逐关节取最大**（任一关节没停稳就不算稳）；一旦判稳就**保持**（``stable = True``），
    之后 ``reset()`` 才复位。判稳前的每一拍都要继续重发目标（``move_mit`` 是直通无平滑）。
    """

    def __init__(self, params=None):
        merged = {**DEFAULT_SETTLE, **(params or {})}
        self.vel = float(merged["vel"])
        self.dq = float(merged["dq"])
        self.dtau = float(merged["dtau"])
        self.ticks = int(merged["ticks"])
        self.reset()

    def reset(self) -> None:
        """复位（换位形时调用）：清掉历史与稳定标志。"""
        self._streak = 0
        self._prev: tuple[np.ndarray, np.ndarray] | None = None
        self.stable = False

    def update(self, q, vel, tau) -> bool:
        """喂入一拍读数，返回「当前是否已判稳」。"""
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
    """稳态窗口统计：``q`` / ``tau`` 的均值与标准差 + 速度峰值。

    Args:
        records: 每项含 ``q`` / ``vel`` / ``tau``（``np.ndarray[DOF]``）的序列。

    Raises:
        ValueError: 空窗口（现场意味着读数一直失败，应当丢弃该点而不是当成 0）。
    """
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
    """正反两遍样本按关节角最近配对 → 每关节 ``|Δτ| / 2``（库仑摩擦幅值估计）。

    Args:
        forward / backward: 每项含 ``q`` / ``tau`` 的样本序列（两个方向各一遍）。
        max_distance: 配对的关节角距离上限（rad）；超距的**不配对**（记为未匹配）。

    Returns:
        ``{"paired": 配对数, "per_joint": 每关节估计, "mean": 均值, "max": 最大值}``；
        没有可配对的样本时 ``per_joint`` 为全 0、``paired`` 为 0。
    """
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
    """样本汇总（现场判读用）：最大 ``|τ|`` 与是否超限幅、每关节行程、力矩标准差。

    Args:
        samples: 每项含 ``q`` / ``tau`` / ``tau_std``（后两者可选）与 ``label`` / ``direction``。
        friction: :func:`friction_from_passes` 的结果（可选，原样带出）。
    """
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


def save_samples(path, payload) -> Path:
    """把样本载荷写成 JSON（``np.ndarray`` 一律转成列表）；返回落盘路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_samples(path) -> dict:
    """读回 :func:`save_samples` 写的 JSON（数组按 ``samples`` / ``summary`` 的结构还原）。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _as_vector(value) -> np.ndarray:
    return np.asarray(value, dtype=np.float64).reshape(-1)


def _to_jsonable(value):
    """递归把 numpy 标量 / 数组转成 JSON 可序列化的形式（dict / list / ndarray 三种容器）。"""
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value
