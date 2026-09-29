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

"""重力补偿：标定（取数 + 拟合）与运行期前馈（不碰 SDK、不碰硬件，只依赖 numpy + 运动学）。

设计见 ``wiki/design/robot_pipeline_impedance.md``。重力项只对每连杆的「质量 + 一阶质量矩」
（``π_k = (m_k, m_k·c_k)``，4 个参数）线性，所以标定的产出是一小组系数而不是查表：

1. **回归器** :func:`gravity_regressor`：``τ_g(q) = Y(q)·π``，`Y` 由 DH 正解推出（与 FK / IK /
   限幅共用同一份运动学，不引入第二个模型文件）；
2. **取数** :func:`plan_waypoints` / :class:`SettleDetector` / :func:`window_stats`（现场脚本
   ``scripts/verify_gravity.py`` 用）；
3. **拟合** :func:`fit_gravity`：样本 → `π̂` + 条件数 / 残差 RMS（脚本 ``scripts/fit_gravity.py``）；
4. **运行期** :class:`GravityModel` / :class:`GravityCompensator`：``t_ff = α·τ̂_g(q_meas)``，
   带限幅与「任何异常 → 本拍 0」的降级。

为什么悬停不需要额外控制器：MIT 位置环本身就会停住。以 30 Hz 持续重发同一目标时，臂自动停在
``q_ss = q_des - τ_g(q_ss) / k_p``，所以**采样配对的是「实测 q」+「实测 τ」**——稳态点不是指令
点（这正是要量的东西）。

标定期间**不要**开重力前馈（否则采到的是「残差力矩」而不是 τ_g，拟合出来会是一片 0）。

⚠️ 本模块**不 import 现场依赖**（pyAgxArm / 控制器）：运动学是惰性导入，以便在没有 SDK 的环境
里被单测直接按路径加载。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
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

#: 重力模型的参数布局：按**连杆**分组，每连杆 4 个 ``π_k = [m_k, m_k·c_k]``。
#: 连杆 k（0-based，k = 0..5）的坐标系 = ``prefix_transforms(q)[k+1]``——与
#: :meth:`PiperKinematics.jacobian` 取关节轴 / 原点是**同一约定**，所以回归器与雅可比可互校。
#: 惯量张量不进重力项（只出现在动能里），故这里没有它。
PI_PER_LINK = 4
#: 参数总数（6 连杆 × 4）。
N_PARAMS = PI_PER_LINK * 6
#: 缺省参数文件（相对配置目录，见 ``config.resolve_config_file``）：标定产物按机型 / 负载覆盖它。
DEFAULT_PARAMS_RELATIVE = "gravity/piper_6dof.json"
#: 缺省前馈比例 ``α``（标定完成后即全量前馈；现场首跑的逐档验收见 ``verify_gravity.py --alpha``）。
DEFAULT_ALPHA = 1.0
#: 参数文件标识（防止把别的 JSON 当参数装载）。
PARAMS_KIND = "piper_gravity_link_params"

_DEFAULT_KINEMATICS = None


def default_kinematics():
    """本模块缺省使用的运动学（惰性单例）：与 FK / IK / 软限位共用同一份 DH。

    惰性是为了让本模块能被**按文件路径**导入（单测不需要 ``robot`` 包可导入，也不需要硬件 SDK）。
    """
    global _DEFAULT_KINEMATICS
    if _DEFAULT_KINEMATICS is None:
        from robot.kinematics import PiperKinematics

        _DEFAULT_KINEMATICS = PiperKinematics()
    return _DEFAULT_KINEMATICS


def gravity_regressor(q, kinematics=None) -> np.ndarray:
    """重力回归量 ``Y(q) ∈ ℝ^{6×24}``：``τ_g(q) = Y(q)·π``（``π`` 的排布见 :data:`PI_PER_LINK`）。

    ``Y[j, 4k : 4k+4]`` 是**连杆 k 的 4 个参数**对关节 j 的重力力矩贡献系数；``k < j`` 处为 0
    （关节 j 不驱动近端连杆）。推导（``g`` 为重力加速度向量，``a_j`` / ``o_j`` 为关节 j 的转轴
    与轴上一点，``R_k`` / ``o_k`` 为连杆 k 的坐标系，全部在基座系）：

        τ_g,j = −Σ_{k≥j} [ m_k·(g × a_j)·(o_k − o_j) + (R_kᵀ (g × a_j))·(m_k c_k) ]

    Raises:
        ValueError: ``q`` 维度与运动学不符（由 ``prefix_transforms`` 报出）。
    """
    kinematics = default_kinematics() if kinematics is None else kinematics
    transforms = kinematics.prefix_transforms(q)
    rotations = [transform[:3, :3] for transform in transforms]
    origins = [transform[:3, 3] for transform in transforms]
    regressor = np.zeros((kinematics.DOF, N_PARAMS), dtype=np.float64)
    for joint in range(kinematics.DOF):
        axis = rotations[joint + 1][:, 2]  # 关节 j 的转轴（与 jacobian() 同一取法）
        wrench = np.cross(GRAVITY_VECTOR, axis)
        for link in range(joint, kinematics.DOF):
            column = PI_PER_LINK * link
            regressor[joint, column] = -float(wrench @ (origins[link + 1] - origins[joint + 1]))
            regressor[joint, column + 1 : column + PI_PER_LINK] = -rotations[link + 1].T @ wrench
    return regressor


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


# ---- 重力模型 τ̂_g(q) = Y(q)·π （阶段 1：拟合产物 → 运行期求值）-------------------------


class GravityModel:
    """重力模型：``τ̂_g(q) = Y(q)·π``（每连杆 4 个参数；**全 0 = 占位**，输出恒为 0）。

    ``π`` 的物理含义是「每连杆的质量 + 一阶质量矩」，**不是**硬件真值——它只在标定采样域内
    保证能重现力矩（超出采样域的外推才是风险，故参数文件里同时记下 DH 与采样信息）。

    ``is_placeholder`` **只看数值**（全 0 即占位）：手改参数文件却忘改标记时，行为仍与数值一致。
    """

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
        """按关节角求重力前馈力矩（N·m，6 维）——**输入必须是与力矩同拍的实测 q**。"""
        return gravity_regressor(q, self.kinematics) @ self.params.reshape(-1)

    def describe(self) -> str:
        """人读描述（日志 / 报告用）。

        **不报「质量合计」**：秩不足时最小二乘给的是最小范数解，参数值本身不是物理质量，
        报出去只会被误当实测值（有意义的是拟合报告里的秩 / 条件数 / 残差）。
        """
        if self.is_placeholder:
            return "占位参数（全 0）→ t_ff 恒为 0"
        return f"非占位参数（来源 {self.source or '内存'}）"


@dataclass(frozen=True)
class GravityFit:
    """拟合结果：模型 + 质量指标（秩 / 条件数 / 残差）——现场据此判断「数据够不够」。"""

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
        """方程数或秩不足以定出全部参数：解不唯一，只能作为「先跑起来」的临时参数。"""
        return self.n_equations < self.n_params or self.rank < self.n_params

    def report(self) -> str:
        """多行文本报告（脚本打印 / 写进参数文件）。"""
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
    """由标定样本拟合 ``π̂``：``min‖Y(q)π − τ‖²``（可选岭正则），并给出质量指标。

    Args:
        samples: 每项含实测 ``q`` / ``tau``（``scripts/verify_gravity.py --sweep`` 的样本 JSON
            里的 ``samples`` 字段直接可用；多余字段忽略）。
        kinematics: 回归器用的运动学（缺省与 FK / IK 同一份）。
        ridge: 岭系数 ``λ``（与 ``YᵀY`` 同量纲）；``0`` = 普通最小二乘。病态（位形激励不足）时用
            小量（如 ``1e-3``）抑制参数爆炸；代价是略偏。

    Raises:
        ValueError: 没有样本。
    """
    pairs = [(_as_vector(item["q"]), _as_vector(item["tau"])) for item in samples]
    if not pairs:
        raise ValueError("fit_gravity requires at least one sample")
    kinematics = default_kinematics() if kinematics is None else kinematics
    regressors = np.stack([gravity_regressor(q, kinematics) for q, _ in pairs])  # (n, 6, 24)
    torques = np.stack([tau for _, tau in pairs])  # (n, 6)
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


# ---- 参数文件（标定产物）-----------------------------------------------------------------


def dh_rows(kinematics=None) -> list[list[float]]:
    """当前运动学的 DH 行（参数文件里一并存下：换 DH 后旧参数必须重新标定）。"""
    kinematics = default_kinematics() if kinematics is None else kinematics
    return [[float(link.alpha), float(link.a), float(link.d), float(link.theta_offset)] for link in kinematics.links]


def save_gravity_params(path, model, *, extra=None) -> Path:
    """把重力参数写成 JSON（含 DH 与布局说明）；返回落盘路径。

    Args:
        path: 目标文件（如配置目录下的 ``gravity/piper_6dof.json``）。
        model: :class:`GravityModel`。
        extra: 额外字段（如 ``fit`` 报告 / 机型 / 负载）。
    """
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
    """读回参数文件 → :class:`GravityModel`。

    Raises:
        FileNotFoundError: 文件不存在（现场提示：先标定再填）。
        ValueError: 文件不是重力参数 / 维度不符 / **DH 与当前运动学不一致**（用另一套 DH 标定的
            参数装载后会算错力矩，宁可不装）。
    """
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


# ---- 配置解析（每臂一份；纯函数，便于单测）-----------------------------------------------


def resolve_arm_config(section, arm) -> dict:
    """把 ``robot.gravity`` 配置归约成**某个臂**的实际配置：

    ``{...公共键..., arms: {<臂>: {...}}}`` → ``{...公共键..., ...该臂覆盖...}``。

    ``params`` 可以写成字符串（各臂共用）或 ``{<臂>: 路径, default: 路径}`` 字典（**每臂一份**：
    双臂的装配与现场误差不同，不能共用一套参数）。
    """
    section = dict(section or {})
    overrides = dict((section.pop("arms", None) or {}).get(arm) or {})
    merged = {**section, **overrides}
    paths = merged.get("params")
    if isinstance(paths, dict):
        merged["params"] = paths.get(arm) or paths.get("default")
    return merged


# ---- 运行期前馈（限幅 + 异常降级）--------------------------------------------------------


class GravityCompensator:
    """运行期重力前馈：``t_ff = clip(α · τ̂_g(q_meas), ±limit)``。

    **硬约定：一律降级、绷不中断控制**——停用 / 占位 / 读数不可用 / 非有限 / 模型异常，都只记一条
    原因并把本拍前馈置 0（退回纯位置环）。占位参数（全 0）直接短路，连读数都不取。

    ``last_note`` 是「当前状态」而不是历史：每次求值都会刷新（正常时为 ``None``），控制器只在
    它**变化**时告警一条（30 Hz 不会刷屏）。
    """

    def __init__(self, model, *, alpha=DEFAULT_ALPHA, limit=T_FF_LIMIT_NM, enabled=True):
        if not np.isfinite(alpha):
            raise ValueError(f"alpha must be finite, got {alpha}")
        self.model = model
        self.alpha = float(alpha)
        # 固件硬限幅是天花板（V188 全关节 ±16 N·m）：配置只能收紧，不能放宽。
        self.limit = float(min(abs(limit), T_FF_LIMIT_NM))
        self.enabled = bool(enabled)
        self.degrades = 0
        self.clips = 0
        self.last_note: str | None = None
        self.last_torque: np.ndarray | None = None

    @property
    def active(self) -> bool:
        """是否真会产生非零前馈（停用 / 占位 / ``α=0`` 都不是）。"""
        return bool(self.enabled and self.alpha != 0.0 and not self.model.is_placeholder)

    def torque(self, q_meas) -> np.ndarray:
        """按实测关节角求本拍前馈力矩（N·m，6 维；任何异常 → 全 0）。"""
        if not self.active:
            return self._set(np.zeros(self.model.DOF, dtype=np.float64), None)
        q = np.asarray(q_meas, dtype=np.float64).reshape(-1)
        if q.size != self.model.DOF or not np.all(np.isfinite(q)):
            return self._degrade(f"关节角不可用（dim={q.size}）")
        try:
            predicted = self.model.torque(q)
        except Exception as exc:  # noqa: BLE001 模型异常不致死：本拍退回纯位置环
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
        """外部取数失败（如电机状态读不到）时调用：记原因并返回本拍全 0 前馈。"""
        return self._degrade(str(reason))

    def status(self) -> dict:
        """可观测状态（健康 / 现场验收）：是否生效、α、限幅、当前降级原因、最近一次前馈。"""
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


def save_samples(path, payload) -> Path:
    """把样本载荷写成 JSON（``np.ndarray`` 一律转成列表）；返回落盘路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_to_jsonable(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def load_samples(path) -> dict:
    """读回 :func:`save_samples` 写的 JSON（数组仍是列表，交给调用方按需转 numpy）。"""
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
