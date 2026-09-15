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

"""rtc 基础类型 —— 原始动作块（``ActionChunk``）、三元切分步数（``split_lens``）与过渡策略表。

设计见 wiki/design/motrix_edge_rtc.md：
  - ``ActionChunk``：策略一次推理返回的**原始动作块**（[H, dim]，含首步绝对步号；策略未声明时
    由管理器按请求步号补齐）；
  - ``split_lens``：三段（prefix 过去已失效 / execution 实际执行 / suffix 过渡）的**步数**，
    只用于状态上报；实际入队按**绝对步号**过滤，不切数组；
  - **过渡策略表**：重叠步（同一绝对步号被本段与下一段同时覆盖）的融合方式——策略给出的是
    **下一段在该步的权重** ``alpha``，融合式 ``(1 - alpha) * 本段 + alpha * 下一段``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np

# 过渡策略表：``fn(pos, length) -> alpha``，``alpha`` = **下一段（新块）** 在该重叠步的权重
# （0 = 全用本段，1 = 全用下一段）；``pos`` = 该步在重叠窗口内的序号（0 = 离当前最近的一步，
# ``length - 1`` = 最远的一步），``length`` = 重叠步数。
#
# - **权重过渡**（固定搭配）：整段重叠区用同一组权重，与步号无关；
# - **连续过渡**（``continuous``）：按重叠区步号线性过渡——本段权重 1→0、下一段权重 0→1。
TRANSITION_FUNCTIONS: dict[str, Callable[[int, int], float]] = {
    "weighted_average": lambda pos, length: 0.7,  # 权重过渡 0.3 本段 + 0.7 下一段（默认，新决策占主导）
    "conservative": lambda pos, length: 0.3,  # 权重过渡 0.7 本段 + 0.3 下一段（保守，抑制跳变）
    "average": lambda pos, length: 0.5,  # 权重过渡 0.5 本段 + 0.5 下一段
    "latest_only": lambda pos, length: 1.0,  # 全取下一段（硬切换，不做过渡）
    "continuous": lambda pos, length: 1.0 if length <= 1 else pos / (length - 1),  # 连续过渡 0→1
}
# 默认过渡策略（配置键 ``aggregate_fn`` 沿用既有命名，值即「下一段权重曲线」）。
DEFAULT_AGGREGATE_FN = "weighted_average"


def get_aggregate_fn(name: str) -> Callable[[int, int], float]:
    """按名取过渡策略（下一段权重曲线 ``fn(pos, length) -> alpha``）；未注册 → ``ValueError``（回执 rejected）。"""
    if name not in TRANSITION_FUNCTIONS:
        raise ValueError(f"unknown aggregate_fn: {name!r} (available: {list(TRANSITION_FUNCTIONS)})")
    return TRANSITION_FUNCTIONS[name]


def split_lens(height: int, prefix_len: int, execution_len: int, suffix_len: int) -> dict:
    """三元切分的**步数**（按块长依次截断：``prefix + execution + suffix <= height``）。

    仅用于状态上报（``status().last_chunk.lens``）：块数据本身不按三段切开，入队时只按绝对步号过滤。
    """
    prefix = max(0, min(int(prefix_len), int(height)))
    execution = max(0, min(int(execution_len), int(height) - prefix))
    suffix = max(0, min(int(suffix_len), int(height) - prefix - execution))
    return {"prefix": prefix, "execution": execution, "suffix": suffix}


@dataclass
class ActionChunk:
    """策略一次推理返回的原始动作块（``[H, dim]``，或单步 ``[dim]`` → 规范化为 ``[1, dim]``）。

    ``start_index`` = 该块首步对应的**绝对步号**（策略侧已知则填；``None`` = 未声明，由管理器按
    请求步号补齐，见 ``as_action_chunk``）。RTCManager 据此把落在当前步号之前的块前部识别为
    ``prefix``（已失效）。
    """

    actions: np.ndarray
    start_index: int | None = None

    def __post_init__(self):
        arr = np.asarray(self.actions, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)  # 单步动作（[dim]）→ [1, dim]
        if arr.ndim != 2:
            raise ValueError(f"action chunk must be 2-D [H, dim] (or 1-D [dim]), got shape {arr.shape}")
        if not np.isfinite(arr).all():
            # NaN / Inf 下发到真机 = 位置或速度直接失控：整块拒绝，交由调用方计入 failed_chunks
            raise ValueError("action chunk contains non-finite values (NaN/Inf)")
        self.actions = arr
        self.start_index = None if self.start_index is None else int(self.start_index)

    @property
    def height(self) -> int:
        """块长 H（步数）。"""
        return int(self.actions.shape[0])

    @property
    def dim(self) -> int:
        """动作维度。"""
        return int(self.actions.shape[1])

    def head(self, steps: int) -> ActionChunk:
        """取前 ``steps`` 步（块长上限 H，至少 1 步）；``start_index`` 不变（首步绝对步号不动）。

        总是返回**新块 + 拷贝**：策略返回的数组常是它自己复用的 buffer（如网络层解码缓冲），
        入队动作与它共享内存会被后续推理覆写——拷贝一次（<= H 步）换掉这整类别名风险。
        """
        steps = max(1, min(int(steps), self.height))
        return ActionChunk(actions=self.actions[:steps].copy(), start_index=self.start_index)


def as_action_chunk(chunk, start_index: int = 0) -> ActionChunk | None:
    """把策略返回（``ActionChunk`` / ndarray / None）规范化为 ``ActionChunk``；None 透传。

    ``start_index`` = 调用方（管理器）**请求时的绝对步号**：策略已声明步号 → 以策略为准（它自知的
    块首步优先）；未声明（``None``）或直接返回 ndarray → 用请求步号补齐——否则块会被当成从 0 起，
    整块落进 ``prefix`` 被当作过期丢弃。
    """
    if chunk is None:
        return None
    if isinstance(chunk, ActionChunk):
        return chunk if chunk.start_index is not None else replace(chunk, start_index=int(start_index))
    return ActionChunk(actions=chunk, start_index=start_index)
