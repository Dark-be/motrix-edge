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

"""rpent.node_view —— ``EdgeNode`` 的窄读接口（RPC 层唯一接触节点的地方）。

RPent 面只关心节点的几件事：绑定的 adapter（能力 / 臂 / home / 相机 / 位姿系）、观测缓存
（``frame_manager``）、实测控制频率、推理会话的 prompt、节点自描述。把这些读法收在一处，
``service`` 就不必到处 ``getattr(node, ...)``——依赖从「整个 ``EdgeNode``」收窄成这一个类，
读法（哪个属性、缺省怎么算）也只有一份。

**纯读**：本类不下发命令、不改节点状态（写路径一律走 ``CommandService``）。测试用的假节点
（``tests/test_rpent.py`` 的 ``FakeNode``）只要提供同名公开属性即可。
"""

from __future__ import annotations

from typing import Any

from motrix_edge.adapter.base import ActionSpace

from .coerce import space_value
from .errors import RpentError


class EdgeNodeView:
    """``EdgeNode`` 的窄读接口（构造时只保存引用，不触碰节点）。"""

    def __init__(self, node: Any):
        self._node = node

    # ---- 节点自描述 ---------------------------------------------------------

    @property
    def adapter_name(self) -> str | None:
        """绑定 adapter 的展示名（未绑定 → ``None``）。"""
        return getattr(self._node, "adapter_name", None)

    @property
    def adapter_type(self) -> str | None:
        """绑定 adapter 的类型（entry point 名；未绑定 → ``None``）。"""
        return getattr(self._node, "adapter_type", None)

    @property
    def state(self) -> str | None:
        """节点状态值（``NodeState``）；拿不到 → ``None``。"""
        return str(getattr(getattr(self._node, "state", None), "value", None))

    @property
    def prompt(self) -> str | None:
        """推理会话的任务语言描述（无会话 / 未设置 → ``None``）。"""
        prompt = getattr(getattr(self._node, "session", None), "prompt", None)
        return str(prompt) if prompt else None

    # ---- adapter ------------------------------------------------------------

    @property
    def adapter(self):
        """当前绑定的 adapter（未绑定 → ``None``）。"""
        return getattr(self._node, "adapter", None)

    def require_adapter(self):
        """已绑定 adapter；未绑定 → :class:`RpentError`（``kind=state``，不静默降级）。"""
        adapter = self.adapter
        if adapter is None:
            raise RpentError("no adapter bound to the node", kind="state")
        return adapter

    def require_adapter_with_arms(self) -> tuple[Any, list[str]]:
        """``(adapter, 启用臂)``——下发前的统一前置校验。"""
        return self.require_adapter(), self.arms

    @property
    def arms(self) -> list[str]:
        """启用臂名（``enabled_arms``，物理顺序）；未绑定 adapter → ``[]``。"""
        return [str(arm) for arm in (getattr(self.adapter, "enabled_arms", None) or [])]

    @property
    def all_arms(self) -> list[str]:
        """adapter 声明的全部臂名（``ARM_NAMES``，含未启用）；未绑定 → ``[]``。"""
        return [str(arm) for arm in (getattr(self.adapter, "ARM_NAMES", None) or [])]

    def dim_per_arm(self, space: ActionSpace) -> int:
        """adapter 为该动作空间声明的**每臂值维数**（joint 6 / pose 6 / gripper 1）；未声明 → 0。"""
        dims = dict(getattr(self.adapter, "ACTION_DIM_PER_ARM", None) or {})
        return int(dims.get(space.value, 0) or 0)

    @property
    def action_spaces(self) -> tuple:
        """adapter 声明的动作空间（``ACTION_SPACES``；未绑定 → ``()``）。"""
        return tuple(getattr(self.adapter, "ACTION_SPACES", None) or ())

    def supports(self, space: ActionSpace) -> bool:
        """adapter 是否声明了该动作空间（按值比较，兼容 ``str`` / ``ActionSpace`` 混用）。"""
        return space.value in {space_value(item) for item in self.action_spaces}

    def home(self, space: ActionSpace) -> list[float]:
        """``HOME[space]`` 关节 home 常量（未声明 → ``[]``）。

        位姿空间**有意不给 home**——编一个位姿比拒绝更危险，故这里只如实返回声明值。
        """
        return list((getattr(self.adapter, "HOME", None) or {}).get(space.value) or [])

    @property
    def pose_frame(self) -> str | None:
        """位姿坐标系名（``POSE_FRAME``）；机器人不提供位姿 → ``None``。"""
        if self.dim_per_arm(ActionSpace.POSE) <= 0:
            return None
        return str(getattr(self.adapter, "POSE_FRAME", "unknown"))

    def images(self) -> dict[str, tuple[int, int]]:
        """相机 → **原生分辨率**（``IMAGES``；未声明 → ``{}``）。"""
        return dict(getattr(self.adapter, "IMAGES", None) or {})

    @property
    def cameras(self) -> list[str]:
        """启用相机名（``adapter.images``）；未启用 / 未声明 → ``IMAGES`` 全量。"""
        enabled = [str(name) for name in (getattr(self.adapter, "images", None) or [])]
        return enabled or list(self.images())

    # ---- 观测缓存 / 实测频率 -------------------------------------------------

    def frame(self) -> dict:
        """观测缓存的最新一帧（``FrameManager.latest()``）；无缓存管理器 → ``RpentError``。

        ``FrameManager`` 是**唯一**的观测读入口（含 ``observations/*`` 与相机 JPEG）；原图
        （``adapter.observe()``）只在载荷确实要图时才单独取，避免每拍都碰机器人。
        """
        frame_manager = getattr(self._node, "frame_manager", None)
        if frame_manager is None:
            raise RpentError("frame manager not available", kind="state")
        return frame_manager.latest() or {}

    def image_size(self) -> tuple[int, int]:
        """预览缓存尺寸（``FrameManager.image_size``，WebRTC / 降采样用）；拿不到 → ``(0, 0)``。"""
        size = getattr(getattr(self._node, "frame_manager", None), "image_size", None)
        if isinstance(size, (tuple, list)) and len(size) == 2:
            return int(size[0]), int(size[1])
        return 0, 0

    def control_hz(self) -> float | None:
        """机器人上报的**实测**控制频率（``adapter_health.control_hz``）；未知 → ``None``。"""
        try:
            value = float(getattr(getattr(self._node, "adapter_health", None), "control_hz", None))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None


__all__ = ["EdgeNodeView"]
