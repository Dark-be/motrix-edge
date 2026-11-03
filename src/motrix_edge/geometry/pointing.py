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

#: 缺省装配约定（**2026-10-09 dual piper 真机小步实测**）：
#: - ``forward = "+z"``：夹爪 / 探针只能沿 ``j6`` 转轴（法兰 ``+z``）伸出，DH 的 ``d6 = 0.091``
#:   也沿 ``z``——实测在这一姿态下 ``+z`` 就是**向前**（工具伸出方向）；
#: - ``up = "-x"``：实测法兰 ``+x`` 是**向下**，所以“上”取它的反向；
#: - ``left = "+y"``：右手系自洽（``forward × left = up`` ⟺ ``z × y = -x``）。
#: 三根轴是**装配事实**，换机型 / 换支架后必须重验（下面“上机 1 分钟验证”），现场可用
#: ``server.rpent.ego_axes`` 覆盖。
DEFAULT_FORWARD = "+z"
DEFAULT_LEFT = "+y"
DEFAULT_UP = "-x"

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
        """``{"forward": "+z", "left": "+y", "up": "-x"}`` → :class:`EgoAxes`（缺键取缺省）。"""
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

    def basis(self) -> np.ndarray:
        """三个语义轴在**法兰系**下的列向量（``d_flange = basis @ d_semantic``）。

        分量顺序 = ``(forward, left, up)`` = RPC 里的 ``delta_xyz`` / ``delta_rpy`` 的 x / y / z。
        0 位（关节全 0）时它正好把“前 / 左 / 上”搬到基座系：dual piper 实测法兰 ``+z`` = 基座
        ``+x``（前）、法兰 ``-x`` = 基座 ``+z``（上）、法兰 ``+y`` = 基座 ``+y``（左）。
        """
        return np.column_stack([axis_vector(self.forward), axis_vector(self.left), axis_vector(self.up)])

    def delta(self, *, forward: float = 0.0, left: float = 0.0, up: float = 0.0) -> np.ndarray:
        """三个带符号分量 → **法兰系**下的三矢量（米 / 弧度，取决于怎么用）。"""
        return self.basis() @ np.array([float(forward), float(left), float(up)], dtype=np.float64)


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


def chart_increment(rpy: Sequence[float], delta_matrix) -> np.ndarray:
    """**基座系** chart 增量：``wrap(rpy(R_cur · ΔR) - rpy_cur)``。

    机器人把 ``pose_delta`` 的 rpy **逐分量相加**，而 rpy 是 chart：相加 ≠ 旋转复合（目标
    ``pitch`` 不为 0 时差得很多，实测 ``pitch=45°`` 绕末端 z 转 20° 偏 **10.7°**）。给出“能凑出目标
    姿态的 chart 差”后，机器人相加的结果与 ``R_cur · ΔR`` **精确一致**。

    ``ΔR`` 是**法兰系**下的旋转矩阵（语义系的旋转先用 ``EgoAxes.basis()`` 换基：
    ``ΔR_flange = M · ΔR_semantic · Mᵀ``——转轴像矢量一样换基）。
    """
    composed = matrix_to_rpy(rpy_to_matrix(rpy) @ np.asarray(delta_matrix, dtype=np.float64))
    delta = composed - np.asarray(rpy, dtype=np.float64).reshape(3)[:3]
    return (delta + math.pi) % (2.0 * math.pi) - math.pi  # wrap 到 (-π, π]：差值可能跨分支


def base_rotation_delta_from_ego(rpy: Sequence[float], delta_rpy: Sequence[float]) -> np.ndarray:
    """**法兰系** rpy 增量 → 机器人侧要的**基座系 chart 增量**（语义系请先换基，见 ``chart_increment``）。"""
    return chart_increment(rpy, rpy_to_matrix(delta_rpy))


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
    "chart_increment",
    "parse_axis",
    "pointing_rpy",
    "rotate_vector",
    "turned_deg",
]
