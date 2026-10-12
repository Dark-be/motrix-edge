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

"""末端 TCP（工具末端）帧换算 + ``robot.tcp`` 配置解析（纯 numpy，无硬件依赖）。

位姿（``pose`` / ``pose_delta`` 动作与 ``observations/pose`` / ``pose_target`` 观测）的原点可以
从**法兰**（缺省）切到**工具末端 TCP**：``T_base_tcp = T_base_flange · T_flange_tcp``，其中
``T_flange_tcp`` = TCP 在法兰系下的位姿，由配置给出。**本期只允许纯平移**（旋转 = 单位阵）：

.. code-block:: text

    xyz_tcp = xyz_flange + R(rpy_flange) · t          # 读：法兰位姿 → TCP 位姿
    xyz_flange = xyz_tcp − R(rpy_tcp) · t             # 写：TCP 位姿 → 法兰（解算前还原）
    rpy 两个方向都不变（纯平移不改变朝向）

「沿工具方向伸出的尖端」= ``[0, 0, L]``：法兰 ``+z`` 就是工具伸出方向（``j6`` 转轴 / DH 的
``d6``），与 ``look_at`` 的指向轴一致。

**为什么只允许纯平移**：TCP 的朝向若与法兰不同，RPent 的工具三轴（``ego_axes`` 的 ``forward`` /
``left`` / ``up``）与 ``look_at`` 的「法兰 ``+z`` 指向 target」会同时失真——它们都按法兰系写。
纯平移下朝向不变，这两处完全不受影响；配置里出现旋转键直接报错（不静默忽略）。

设计与适用范围见 ``wiki/design/robot_pipeline_tcp.md``：换算只发生在机器人基类的「位姿 ↔ 法兰」
边界，``PiperKinematics`` / ``PiperController`` 保持纯法兰（``scripts/verify_cartesian.py`` 要用
``fk(q)`` 对照 SDK 的 ``get_flange_pose()``，混入 TCP 会整体偏一个常量、失去判据）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from .transforms import rpy_to_matrix

#: 位姿每臂宽度（``xyz + rpy``）；与 ``BaseRobot.POSE_DIM_PER_ARM`` 一致。
PER_ARM = 6

#: 帧名（与 edge 契约 ``POSE_FRAME`` 同词表）。
FRAME_FLANGE = "flange"
FRAME_TCP = "tcp"

#: 配置段名（``robot.tcp``）与允许的键。
SECTION = "tcp"
_ALLOWED_KEYS = ("enabled", "offset", "arms")
_ARM_ALLOWED_KEYS = ("offset",)
#: 旋转键：本期**明确拒绝**（见模块 docstring「为什么只允许纯平移」）。
_ROTATION_KEYS = ("rpy", "rotation", "quaternion", "quat")


def _offset_vector(raw, where: str) -> np.ndarray:
    """``[x, y, z]``（米）→ ``(3,)``；形状 / 取值不合法 → ``ValueError``（不猜、不补 0）。"""
    values = np.asarray(raw, dtype=np.float64).reshape(-1)
    if values.shape[0] != 3:
        raise ValueError(f"{where} 必须是 3 个数（米），得到 {values.shape[0]} 个")
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{where} 含有非有限数：{values.tolist()}")
    return values.copy()


def resolve_tcp(section: Mapping | None, arms: Sequence[str]) -> dict[str, np.ndarray]:
    """``robot.tcp`` 配置段 → ``{臂名: offset[3]}``（关闭 / 缺段 → ``{}``，即恒等）。

    关闭（``enabled`` 非真）时**不做任何换算**、也不校验 ``offset``——「开关关闭行为与现状一致」
    是硬要求，配置里留着一份 offset 不生效是允许的。

    Raises:
        ValueError: 未知键 / 出现旋转键 / ``enabled: true`` 但缺 ``offset`` /
            ``offset`` 不是 3 个有限数 / ``arms`` 里出现未知臂名。
    """
    values = dict(section or {})
    if not values:
        return {}
    # 旋转键先判（它们是「有意的需求」，要给一句说得清的话），再判真正的未知键（拼写错误）
    rotation = [key for key in values if key in _ROTATION_KEYS]
    if rotation:
        raise ValueError(
            f"robot.{SECTION}.{rotation[0]} 不支持：本期 TCP 只允许纯平移（工具朝向与法兰一致），"
            "带姿态的 TCP 见 wiki/design/robot_pipeline_tcp.md「后续」"
        )
    unknown = [key for key in values if key not in _ALLOWED_KEYS]
    if unknown:
        raise ValueError(f"robot.{SECTION} 未知键 {unknown}（可用：{list(_ALLOWED_KEYS)}）")
    if not bool(values.get("enabled", False)):
        return {}
    if values.get("offset") is None:
        raise ValueError(f"robot.{SECTION}.enabled=true 时必须给 robot.{SECTION}.offset（3 个数，米）")

    common = _offset_vector(values["offset"], f"robot.{SECTION}.offset")
    overrides = dict(values.get("arms") or {})
    unknown_arms = [arm for arm in overrides if arm not in arms]
    if unknown_arms:
        raise ValueError(f"robot.{SECTION}.arms 未知臂 {unknown_arms}（可用：{list(arms)}）")
    resolved: dict[str, np.ndarray] = {}
    for arm in arms:
        arm_values = dict(overrides.get(arm) or {})
        unknown = [key for key in arm_values if key not in _ARM_ALLOWED_KEYS]
        if unknown:
            raise ValueError(f"robot.{SECTION}.arms.{arm} 未知键 {unknown}（可用：{list(_ARM_ALLOWED_KEYS)}）")
        offset = arm_values.get("offset")
        resolved[str(arm)] = (
            common.copy() if offset is None else _offset_vector(offset, f"robot.{SECTION}.arms.{arm}.offset")
        )
    return resolved


def to_tcp_block(pose, offset) -> np.ndarray:
    """单臂位姿（法兰系，``xyz + rpy``）→ TCP 系：``T_base_tcp = T_base_flange · T_flange_tcp``。"""
    values = np.asarray(pose, dtype=np.float64).reshape(-1)[:PER_ARM]
    if offset is None:
        return values.copy()
    tip = np.asarray(offset, dtype=np.float64).reshape(3)
    out = values.copy()
    out[:3] = values[:3] + rpy_to_matrix(values[3:PER_ARM]) @ tip
    return out


def from_tcp_block(pose, offset) -> np.ndarray:
    """单臂位姿（TCP 系，``xyz + rpy``）→ 法兰系：``T_base_flange = T_base_tcp · T_flange_tcp⁻¹``。"""
    values = np.asarray(pose, dtype=np.float64).reshape(-1)[:PER_ARM]
    if offset is None:
        return values.copy()
    tip = np.asarray(offset, dtype=np.float64).reshape(3)
    out = values.copy()
    out[:3] = values[:3] - rpy_to_matrix(values[3:PER_ARM]) @ tip
    return out


def to_tcp(pose_flat, offsets: Sequence | None, *, per_arm: int = PER_ARM) -> np.ndarray:
    """扁平位姿（**法兰系**）→ TCP 系（逐臂，顺序与 ``offsets`` 对齐）。

    ``offsets`` 缺省 / 全为 ``None`` → 原值返回（``enabled: false`` 时零开销、零数值扰动）。
    """
    values = np.asarray(pose_flat, dtype=np.float64).reshape(-1).copy()
    if offsets is None or all(offset is None for offset in offsets):
        return values
    for index, offset in enumerate(offsets):
        start = index * per_arm
        values[start : start + per_arm] = to_tcp_block(values[start : start + per_arm], offset)
    return values


def from_tcp(pose_flat, offsets: Sequence | None, *, per_arm: int = PER_ARM) -> np.ndarray:
    """扁平位姿（**TCP 系**）→ 法兰系（逐臂，顺序与 ``offsets`` 对齐）。"""
    values = np.asarray(pose_flat, dtype=np.float64).reshape(-1).copy()
    if offsets is None or all(offset is None for offset in offsets):
        return values
    for index, offset in enumerate(offsets):
        start = index * per_arm
        values[start : start + per_arm] = from_tcp_block(values[start : start + per_arm], offset)
    return values


__all__ = [
    "FRAME_FLANGE",
    "FRAME_TCP",
    "PER_ARM",
    "SECTION",
    "from_tcp",
    "from_tcp_block",
    "resolve_tcp",
    "to_tcp",
    "to_tcp_block",
]
