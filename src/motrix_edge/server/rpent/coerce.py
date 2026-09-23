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

"""rpent.coerce —— 观测 / 入参的规范化（RPC 面的输入适配层）。

进来有三种形态：wire 还原后的 ndarray / list（外部 agent 的参数）、观测缓存里的 JPEG bytes、
``ActionSpace`` 这类 ``str`` 混血枚举。出去只要三种：numpy 数组 / ``float`` / ``str``——本模块
负责这一步，并在不合规时抛 :class:`~motrix_edge.server.rpent.errors.RpentError`。纯函数、无状态。

**与 ``layout`` 的分工**：本模块**不做几何**（矩阵 / rpy / rot6d / 夹爪域换算在 ``layout``），
``layout`` **不做取值**（读观测键、解析调用形态在这里）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import cv2
import numpy as np

from motrix_edge.adapter.base import ActionSpace

from .errors import RpentError


def as_float_array(value: Any) -> np.ndarray | None:
    """观测值 → ``float32`` 一维数组；``None`` 原样返回（缺键与空值都表示“没有”）。"""
    if value is None:
        return None
    return np.asarray(value, dtype=np.float32).reshape(-1)


def as_action_matrix(actions: Any) -> np.ndarray:
    """动作块 → ``[N, dim]`` ``float32`` 矩阵（单帧 ``[dim]`` 也接受）。"""
    matrix = np.asarray(actions, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2:
        raise RpentError("actions must be [N, dim] or [dim]", kind="argument")
    return matrix


def split_arm_and_vector(
    args: tuple, arm: str | None, vector: Any, name: str, *, expected: int
) -> tuple[str | None, np.ndarray]:
    """解析 RPent 的两种调用形态：``(delta,)``（franka）与 ``(arm, delta)``（dual_franka）。

    显式关键字（``delta_xyz=`` / ``delta_rpy=``）优先于位置参数；``name`` 仅用于错误信息。
    """
    if args:
        if len(args) == 1:
            vector = args[0] if vector is None else vector
        elif len(args) >= 2:
            arm = args[0] if arm is None else arm
            vector = args[1] if vector is None else vector
        else:  # pragma: no cover - 防御分支
            raise RpentError(f"{name} takes at most 2 positional arguments", kind="argument")
    if vector is None:
        raise RpentError(f"{name} is required", kind="argument")
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    if array.size != expected:
        raise RpentError(f"{name} must have {expected} elements, got {array.size}", kind="argument")
    return (str(arm) if arm is not None else None), array


def decode_jpeg(value: Any) -> np.ndarray | None:
    """观测缓存里的图像（JPEG bytes）→ ``uint8 HWC RGB`` 数组；解不出 → ``None``。

    适配器出图时是 ``RGB → BGR → imencode``（见 ``HttpShmAdapter._encode_jpeg``），故解码后要转回
    RGB，否则 RPent 落盘的 PNG / 喂给 LLM 的图会红蓝颠倒。
    """
    if isinstance(value, np.ndarray):  # 已经是数组（未来 adapter 直出 raw）：原样使用
        return value if value.ndim == 3 else None
    if not isinstance(value, (bytes, bytearray)):
        return None
    array = cv2.imdecode(np.frombuffer(bytes(value), dtype=np.uint8), cv2.IMREAD_COLOR)
    if array is None:
        return None
    return cv2.cvtColor(array, cv2.COLOR_BGR2RGB)


def wrap_angles(angles: Any) -> np.ndarray:
    """角度差包装到 ``[-π, π]``（姿态误差不能用原始差值）。"""
    return (np.asarray(angles, dtype=np.float64) + np.pi) % (2.0 * np.pi) - np.pi


def space_value(space: Any) -> str:
    """动作空间 → 值字符串（``ActionSpace`` 是 ``str`` + ``Enum`` 混血，``str(x)`` 不是值）。"""
    return space.value if isinstance(space, ActionSpace) else str(space)


def seconds_until(expires_at: str | None) -> float | None:
    """ISO 时间串（北京时间 +08:00）→ 剩余秒数；不可解析 → ``None``（启动自检用）。"""
    if not expires_at:
        return None
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        return None
    return round((deadline - datetime.now(deadline.tzinfo)).total_seconds(), 1)


__all__ = [
    "as_action_matrix",
    "as_float_array",
    "decode_jpeg",
    "seconds_until",
    "space_value",
    "split_arm_and_vector",
    "wrap_angles",
]
