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

"""rpent.settle —— 到位等待（arrival wait）：参数、判定、回执。

写原语（``move_delta`` / ``rotate_delta`` / ``set_gripper`` / ``recover_joint_posture``）下发后
edge **阻塞到机器人到位**再回执：``rollout`` 只负责“设目标”，由机器人侧循环限速靠近，立即返回会让
RPent 紧接着的 ``dump_state`` 看到尚未动的那一帧 → planner 以为命令无效、重试或振荡。

- :class:`SettleConfig`：部署配置（``server.rpent.settle``）+ 逐次覆盖（``settle=false`` /
  ``settle={"timeout_s": 60}``）→ 生效参数（值域已钳制）；
- :func:`wait_for_reached`：轮询观测误差，直到落进容差 / 停滞 / 超时 → 回执；
- :func:`unreached`：不适用场景（``dry_run`` / 关闭 / 机器人不提供位姿）的 ``reached: null`` 回执。

**容差按 MIT 实际稳态误差标定**：底层只有 P/D、**无重力 / 力矩前馈**，“设定什么关节就是什么
关节”并不成立（关节停在 ``τ_gravity / kp`` 附近的平衡点）；容差小于稳态误差时 ``reached`` 永远
不成立（每个原语都走满 ``stall_s`` / ``timeout_s``）。故**部署值**（``edge.yml``，当前 位置 5cm /
姿态 0.4rad ≈ 23°）是把静态误差先**盖住**的有意放大——宁可能判出“到位”，也别让 agent 每次都
等到超时；等补上前馈、或按 ``final_err`` 实测平台收敛后再收紧（见
``wiki/design/robot_pipeline_cartesian.md``）。

**回执字段**（RPent 按它判成败，勿改名）：``reached`` / ``final_err``（主误差）/ ``final_err_m``
（位置，米；仅位姿）/ ``final_err_rad``（姿态或关节，弧度）/ ``elapsed_s`` / ``settle_timeout_s``
/ ``settle_pos_tol`` / ``settle_rot_tol``，外加标志 ``stalled`` / ``timeout``。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from motrix_edge.adapter.base import ActionSpace

from .errors import RpentError

# 观测误差三元组：``(主误差, 位置误差, 姿态或关节误差)``——主误差只用于回执展示与停滞追踪。
Errors = tuple[float, float | None, float | None]


@dataclass(frozen=True)
class SettleConfig:
    """到位等待参数（字段名与 ``server.rpent.settle`` 一致）。"""

    enabled: bool = True
    # 容差：**部署值在 edge.yml**（当前 5cm / 0.4rad，按 MIT 静态误差有意放宽）；
    # 下面这两个只是“配置里没写”时的兜底，且故意更紧——宁可判不出到位，也不误报到位。
    pos_tol: float = 0.01  # 米（位置）
    rot_tol: float = 0.05  # 弧度（姿态 / 关节 / 夹爪）
    timeout_s: float = 5.0
    max_timeout_s: float = 90.0  # 单次等待上限（RPent 客户端 HTTP 预算是 120s）
    stall_s: float = 1.0  # 主误差多久没改善就判停滞
    stall_eps: float = 1e-4  # 视为“有改善”的最小增量（小于编码器噪声 → 见模块 docstring）
    poll_s: float = 0.02
    target_wait_s: float = 1.0  # 增量下发后等「命令落地」的上限（见 service._pose_target_reference）

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None = None) -> SettleConfig:
        """部署配置 → 生效参数（缺键取缺省；越界值在 :meth:`clamped` 里钳制）。"""
        raw = dict(raw or {})
        unknown = [key for key in raw if key not in cls.__dataclass_fields__]
        if unknown:
            raise ValueError(f"unknown settle key(s) {unknown} (available: {sorted(cls.__dataclass_fields__)})")
        return cls(**{key: raw[key] for key in raw}).clamped()

    def clamped(self) -> SettleConfig:
        """值域钳制：负等待 / 忙等 / 超出 HTTP 预算的等待都没有意义。"""
        return replace(
            self,
            enabled=bool(self.enabled),
            pos_tol=max(float(self.pos_tol), 0.0),
            rot_tol=max(float(self.rot_tol), 0.0),
            timeout_s=min(max(float(self.timeout_s), 0.0), max(float(self.max_timeout_s), 0.0)),
            stall_s=max(float(self.stall_s), 0.0),
            stall_eps=max(float(self.stall_eps), 0.0),
            poll_s=max(float(self.poll_s), 0.001),
            target_wait_s=max(float(self.target_wait_s), 0.0),
        )

    def with_override(self, override: Any = None) -> SettleConfig:
        """逐次覆盖：``None`` / ``True`` = 不变；``False`` = 关闭；字典 = 只改指定项。

        长行程 / 慢原语由调用方自己给足时间（``settle={"timeout_s": 60}``），不必改部署配置。
        未知键直接报错（配置笔误要看得见，不静默忽略）。
        """
        if override is None or override is True:
            return self
        if override is False:
            return replace(self, enabled=False)
        if not isinstance(override, Mapping):
            raise RpentError(
                f"settle must be a bool or a dict of overrides, got {type(override).__name__}",
                kind="invalid_params",
            )
        unknown = [key for key in override if key not in self.__dataclass_fields__]
        if unknown:
            raise RpentError(
                f"settle override has unknown key(s) {unknown} (available: {sorted(self.__dataclass_fields__)})",
                kind="invalid_params",
            )
        return replace(self, **{key: override[key] for key in override}).clamped()

    def within(self, pos_err: float | None, rot_err: float | None, space: ActionSpace) -> bool:
        """是否落进生效容差（**分项判**：位置用米、姿态 / 关节用弧度，不混进同一阈值）。

        ``pose``：位置 ≤ ``pos_tol`` 且姿态 ≤ ``rot_tol``；``joint``：逐关节 / 夹爪误差 ≤
        ``rot_tol``——夹爪夹住物体时到不了目标开合度（回 ``stalled``），文档已约定「夹爪的
        stalled = 接触」而不是失败。
        """
        if space is ActionSpace.POSE:
            return pos_err is not None and rot_err is not None and pos_err <= self.pos_tol and rot_err <= self.rot_tol
        return rot_err is not None and rot_err <= self.rot_tol


def unreached(reason: str, **extra: Any) -> dict:
    """不适用 / 未下发的回执：``reached: null`` + 原因（不谎报到位）。"""
    return {"reached": None, "reason": reason, **extra}


def receipt(config: SettleConfig, reached: bool, errors: Errors | None, started: float, **flags: Any) -> dict:
    """settle 回执（字段见模块 docstring）。

    连同生效容差一起回，让 agent 能区分两种失败：``final_err`` 接近容差（还没到位，可补一个小
    增量）vs ``stalled``（位姿不再变化 → 禁止同向重发，必须重观测 / restage）。
    """
    main, pos_err, rot_err = errors if errors is not None else (None, None, None)
    return {
        "reached": bool(reached),
        "final_err": None if main is None else float(main),
        "final_err_m": None if pos_err is None else float(pos_err),  # 仅 pose 空间
        "final_err_rad": None if rot_err is None else float(rot_err),  # 姿态 / 关节
        "elapsed_s": round(time.monotonic() - started, 4),
        "settle_timeout_s": float(config.timeout_s),
        "settle_pos_tol": config.pos_tol,
        "settle_rot_tol": config.rot_tol,
        **{key: True for key in flags},
    }


def wait_for_reached(probe: Callable[[], Errors | None], *, space: ActionSpace, config: SettleConfig) -> dict:
    """轮询 ``probe()`` 直到到位 / 停滞 / 超时，返回回执。

    ``probe()`` 返回 ``None`` = 本拍无观测帧（瞬态无帧不是失败，继续等）。停滞判定：主误差在
    ``stall_s`` 内没改善 → ``reached: false`` + ``stalled: true``（撞到东西 / 力不足；MIT 下也
    常见于「已到稳态误差平台」——两者从位置观测上不可区分，回执同时给 ``final_err`` 与生效容差，
    由 agent 自己判）。
    """
    started = time.monotonic()
    deadline = started + config.timeout_s
    best = float("inf")
    best_at = started
    last: Errors | None = None
    while True:
        errors = probe()
        if errors is not None:
            last = errors
            main, pos_err, rot_err = errors
            if config.within(pos_err, rot_err, space):
                return receipt(config, True, errors, started)
            if main < best - config.stall_eps:
                best, best_at = main, time.monotonic()
        now = time.monotonic()
        if now >= deadline:
            return receipt(config, False, last, started, timeout=True)
        if now - best_at >= config.stall_s:
            return receipt(config, False, last, started, stalled=True)
        time.sleep(config.poll_s)


__all__ = ["Errors", "SettleConfig", "receipt", "unreached", "wait_for_reached"]
