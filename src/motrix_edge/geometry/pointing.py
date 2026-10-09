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

"""pointing —— 朝向（``look_at``）与**末端系增量**的换算。

纯 numpy、无硬件、无 OpenCV；``rpy`` 约定与 :mod:`motrix_edge.geometry.transforms` **同一套**
（``R = Rz(yaw)·Ry(pitch)·Rx(roll)``、万向锁 ``yaw = 0``）。这里回答两个问题：

1. **朝向**：把末端某一根轴（缺省法兰 ``+z`` = 工具 / 探针伸出方向）转到给定方向矢量，且
   **不引入 roll**（``roll ≡ 0``）；解析解只对 ``x`` / ``z`` 轴存在，``y`` 轴在 ``roll = 0`` 时
   恒在水平面内 → 只能指向与末端同高的点，故直接报错而不是给一个歪头解（见 :func:`pointing_rpy`）；
2. **末端系增量 → 基座系增量**：机器人侧的 ``pose_delta`` 是**基座系**的 ``rpy`` chart 相加
   （基准 = ``FK(关节段目标)``，见 ``wiki/design/robot_pipeline_cartesian.md``），所以「沿末端自身
   三轴挪 / 绕末端自身三轴转」必须在上位换算：``d_base = R_cur · d_ego``、
   ``ΔR_base = R_cur · ΔR_ego · R_curᵀ``（小步长下与 rpy 相加一致）。

⚠️ 「末端系的三根轴分别叫什么」是**装配事实**（现场必须小步验证），故由 :class:`EgoAxes` 显式
声明并随回执回显——不写死在调用点。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from motrix_edge.geometry.transforms import matrix_to_rpy, rpy_to_matrix

#: 合法的轴向写法：``"+x"`` / ``"-z"`` 之类（大小写不敏感）。
AXIS_SPECS = tuple(f"{sign}{name}" for name in ("x", "y", "z") for sign in ("+", "-"))

#: 缺省装配约定：工具伸出方向 = 法兰 ``+z``（``j6`` 的转轴就是它，夹爪 / 探针只能沿它伸出；
#: DH 的 ``d6 = 0.091`` 也沿 ``z``），另外两根横轴取 ``x`` / ``y``。
DEFAULT_FORWARD = "+z"
DEFAULT_LEFT = "+x"
DEFAULT_UP = "+y"

#: ``look_at`` 的 roll 约定：恒为 0（见 :func:`pointing_rpy`）。
ROLL_FREE = 0.0


def parse_axis(spec: str) -> tuple[int, float]:
    """``"+z"`` / ``"-x"`` → ``(轴索引, 符号)``；非法 → ``ValueError``（消息带原值）。"""
    text = str(spec or "").strip().lower()
    if len(text) != 2 or text[0] not in "+-" or text[1] not in "xyz":
        raise ValueError(f"axis spec must look like '+z' / '-x', got {spec!r}")
    return ("xyz".index(text[1]), 1.0 if text[0] == "+" else -1.0)


def axis_vector(spec: str) -> np.ndarray:
    """该轴向在**局部系**下的单位矢量（如 ``"-y"`` → ``[0, -1, 0]``）。"""
    index, sign = parse_axis(spec)
    vector = np.zeros(3, dtype=np.float64)
    vector[index] = sign
    return vector


@dataclass(frozen=True)
class EgoAxes:
    """末端系三轴的**装配约定**：``forward``（工具伸出方向）/ ``left`` / ``up``。

    现场必须小步验证（发 1 cm 看实际方向）：三根轴要互不相同，否则配置明显写错 → 直接报错
    （宁可不干活，也不按一个含糊的映射动机械臂）。
    """

    forward: str = DEFAULT_FORWARD
    left: str = DEFAULT_LEFT
    up: str = DEFAULT_UP

    @classmethod
    def from_mapping(cls, raw: Mapping | None) -> EgoAxes:
        """``{"forward": "+z", "left": "+x", "up": "+y"}`` → :class:`EgoAxes`（缺键取缺省）。"""
        block = dict(raw or {})
        axes = cls(
            forward=str(block.get("forward", DEFAULT_FORWARD)),
            left=str(block.get("left", DEFAULT_LEFT)),
            up=str(block.get("up", DEFAULT_UP)),
        )
        indices = [parse_axis(axes.forward)[0], parse_axis(axes.left)[0], parse_axis(axes.up)[0]]
        if len(set(indices)) != 3:
            raise ValueError(
                f"ego_axes must map forward / left / up onto three different axes, got "
                f"{axes.as_dict()}——三根轴重了，命令会指向同一个方向"
            )
        return axes

    def as_dict(self) -> dict[str, str]:
        return {"forward": self.forward, "left": self.left, "up": self.up}

    def delta(self, *, forward: float = 0.0, left: float = 0.0, up: float = 0.0) -> np.ndarray:
        """三个带符号分量 → **末端系**下的三矢量（米 / 弧度，取决于怎么用）。"""
        vector = np.zeros(3, dtype=np.float64)
        for spec, value in ((self.forward, forward), (self.left, left), (self.up, up)):
            vector += float(value) * axis_vector(spec)
        return vector


def _unit_direction(direction: Sequence[float]) -> np.ndarray:
    vector = np.asarray(direction, dtype=np.float64).reshape(-1)
    if vector.size < 3 or not np.all(np.isfinite(vector[:3])):
        raise ValueError(f"direction must be 3 finite numbers, got {direction!r}")
    norm = float(np.linalg.norm(vector[:3]))
    if norm < 1e-12:
        raise ValueError("direction must be non-zero")
    return vector[:3] / norm


def pointing_rpy(axis: str, direction: Sequence[float], *, roll: float = ROLL_FREE) -> np.ndarray:
    """把 ``axis`` 轴指向 ``direction`` 的 ``[roll, pitch, yaw]``（**roll 恒为 0**）。

    为什么只解 roll-free：``roll ≡ 0`` 时 ``R = Rz(yaw)·Ry(pitch)``，
    ``R·e_y = (-sin yaw, cos yaw, 0)`` **恒在水平面内**——所以

    - 指向轴 ``x`` / ``z``：闭式可解（``pitch`` 由方向矢量的 ``z`` 分量定、``yaw`` 由 ``xy`` 分量定），
      对任意方向都成立：
      ``z``：``cos pitch = ±u_z``、``yaw = atan2(±u_y, ±u_x)``；
      ``x``：``sin pitch = ∓u_z``、``yaw = atan2(±u_y, ±u_x)``；
    - 指向轴 ``y``：只有 ``u_z = 0``（与末端**同高**的方向）才有解，否则抛 ``ValueError``
      ——调用方据此回 ``unsupported``，而不是悄悄给一个歪头的姿态。

    方向与指向轴反平行（``u = ∓z``）时 ``yaw`` 退化 → 取 0（结果只差一个绕自身轴的旋转，物理上
    等价于「不拧腕」）。
    """
    if not math.isclose(float(roll), ROLL_FREE, abs_tol=1e-12):
        raise ValueError(f"pointing only solves the roll-free convention (roll must be {ROLL_FREE}), got {roll!r}")
    index, sign = parse_axis(axis)
    unit = _unit_direction(direction)
    if index == 1:
        if abs(float(unit[2])) > 1e-9:
            raise ValueError(
                "the y axis stays horizontal when roll is 0: a roll-free pose can only point at a "
                "direction level with the end effector (use '+z' as the pointing axis)"
            )
        return np.array([ROLL_FREE, 0.0, math.atan2(-sign * unit[0], sign * unit[1])], dtype=np.float64)
    if index == 2:  # 指向轴 = z：R·(s·e_z) = (s·cos y·sin p, s·sin y·sin p, s·cos p)
        pitch = math.acos(float(np.clip(sign * unit[2], -1.0, 1.0)))
        degenerate = abs(math.sin(pitch)) < 1e-12
    else:  # 指向轴 = x：R·(s·e_x) = (s·cos y·cos p, s·sin y·cos p, -s·sin p)
        pitch = math.asin(float(np.clip(-sign * unit[2], -1.0, 1.0)))
        degenerate = abs(math.cos(pitch)) < 1e-12
    yaw = 0.0 if degenerate else math.atan2(sign * float(unit[1]), sign * float(unit[0]))
    return np.array([ROLL_FREE, pitch, yaw], dtype=np.float64)


def rotate_vector(rpy: Sequence[float], vector: Sequence[float]) -> np.ndarray:
    """把**末端系**下的矢量换算到**基座系**：``R(rpy) · v``。"""
    return rpy_to_matrix(rpy) @ np.asarray(vector, dtype=np.float64).reshape(3)


def base_delta_from_ego(rpy: Sequence[float], delta: Sequence[float]) -> np.ndarray:
    """末端系平移增量 → 基座系平移增量（``d_base = R_cur · d_ego``）。"""
    return rotate_vector(rpy, delta)


def base_rotation_delta_from_ego(rpy: Sequence[float], delta_rpy: Sequence[float]) -> np.ndarray:
    """末端系旋转增量 → 机器人侧要的**基座系 ``rpy`` chart 增量**。

    机器人把 ``pose_delta`` 的 rpy **逐分量相加**（``target_rpy += Δrpy``，基准 = 关节段目标的正
    解），而 rpy 是 chart：相加 ≠ 旋转复合（目标 ``pitch`` 不为 0 时差得很多，实测 ``pitch=45°``
    绕末端 ``z`` 转 ``20°`` 会偏 **10.7°**；``pitch=89°`` 偏 **28°**）。

    所以这里不下发“基座系旋转增量”的 rpy 三元组，而是直接给出**能凑出目标姿态的 chart 差**：
    ``Δrpy = wrap(rpy(R_target · ΔR_ego) - rpy_target)``——机器人相加后得到的姿态与
    ``R_target · ΔR_ego`` **精确一致**（``pitch`` 接近 ``±90°`` 时 chart 增量会变大：那里是
    gimbal 邻近，但相加结果仍然精确；要大角度改姿态请用 ``look_at`` / 绝对 ``pose``）。
    """
    target = rpy_to_matrix(rpy)
    composed = matrix_to_rpy(target @ rpy_to_matrix(delta_rpy))
    delta = composed - np.asarray(rpy, dtype=np.float64).reshape(3)[:3]
    return (delta + math.pi) % (2.0 * math.pi) - math.pi  # wrap 到 (-π, π]：差值可能跨分支


def turned_deg(rpy_from: Sequence[float], rpy_to: Sequence[float]) -> float:
    """两个姿态之间的**转角**（度）——``look_at`` 回执用（判断是否大角度甩动）。"""
    delta = np.asarray(rpy_from, dtype=np.float64).reshape(3)[:3]
    target = np.asarray(rpy_to, dtype=np.float64).reshape(3)[:3]
    relative = rpy_to_matrix(delta).T @ rpy_to_matrix(target)
    trace = float(np.trace(relative))
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))


__all__ = [
    "AXIS_SPECS",
    "DEFAULT_FORWARD",
    "DEFAULT_LEFT",
    "DEFAULT_UP",
    "ROLL_FREE",
    "EgoAxes",
    "axis_vector",
    "base_delta_from_ego",
    "base_rotation_delta_from_ego",
    "parse_axis",
    "pointing_rpy",
    "rotate_vector",
    "turned_deg",
]
