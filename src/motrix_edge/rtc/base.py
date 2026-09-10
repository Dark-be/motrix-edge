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

"""rtc 基础类型 —— 原始动作块（``ActionChunk``）、三元切分结果（``ChunkSlice``）与重叠聚合函数。

设计见 wiki/design/motrix_edge_rtc.md：
  - ``ActionChunk``：策略一次推理返回的**原始动作块**（[H, dim]，含首步绝对步号）；
  - ``ChunkSlice``：按绝对步号把块切成 prefix（过去已失效）/ execution（实际执行）/ suffix（过渡）；
  - **聚合函数表**：重叠步（同一绝对步号出现多次）的融合方式，默认 ``weighted_average``
    （对齐 lerobot ``AGGREGATE_FUNCTIONS``）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 重叠聚合函数（对齐 lerobot 官方 robot_client 的 AGGREGATE_FUNCTIONS）。
# 默认 weighted_average：重叠步动作 = 0.3*旧块 + 0.7*新块（新决策更占主导）。
AGGREGATE_FUNCTIONS = {
    "weighted_average": lambda old, new: 0.3 * old + 0.7 * new,
    "latest_only": lambda old, new: new,
    "average": lambda old, new: 0.5 * old + 0.5 * new,
    "conservative": lambda old, new: 0.7 * old + 0.3 * new,
}
DEFAULT_AGGREGATE_FN = "weighted_average"


def get_aggregate_fn(name: str):
    """按名取重叠聚合函数；未注册 → ``ValueError``（调用方回执 rejected）。"""
    if name not in AGGREGATE_FUNCTIONS:
        raise ValueError(f"unknown aggregate_fn: {name!r} (available: {list(AGGREGATE_FUNCTIONS)})")
    return AGGREGATE_FUNCTIONS[name]


@dataclass
class ChunkSlice:
    """动作块的三元切分结果（按绝对步号连续切分）。

    - ``prefix``：过去时刻，已失效（丢弃 / 仅统计）；
    - ``execution``：本次实际执行段；
    - ``suffix``：后缀，为下一块做过渡（留在队列与下一块重叠融合）。
    """

    prefix: np.ndarray
    execution: np.ndarray
    suffix: np.ndarray

    @property
    def lens(self) -> dict:
        """三段步数（``{"prefix", "execution", "suffix"}``），供状态上报。"""
        return {
            "prefix": int(self.prefix.shape[0]),
            "execution": int(self.execution.shape[0]),
            "suffix": int(self.suffix.shape[0]),
        }


@dataclass
class ActionChunk:
    """策略一次推理返回的原始动作块（``[H, dim]``，或单步 ``[dim]`` → 规范化为 ``[1, dim]``）。

    ``start_index`` = 该块首步对应的**绝对步号**（策略侧已知则填，缺省 0）；RTCManager 据此把
    落在当前步号之前的块前部识别为 ``prefix``（已失效）。
    """

    actions: np.ndarray
    start_index: int = 0

    def __post_init__(self):
        arr = np.asarray(self.actions, dtype=np.float64)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)  # 单步动作（[dim]）→ [1, dim]
        if arr.ndim != 2:
            raise ValueError(f"action chunk must be 2-D [H, dim] (or 1-D [dim]), got shape {arr.shape}")
        self.actions = arr
        self.start_index = int(self.start_index)

    @property
    def height(self) -> int:
        """块长 H（步数）。"""
        return int(self.actions.shape[0])

    @property
    def dim(self) -> int:
        """动作维度。"""
        return int(self.actions.shape[1])

    def slice(self, prefix_len: int, execution_len: int, suffix_len: int) -> ChunkSlice:
        """按步数切三段（长度按实际块长截断：``prefix + execution + suffix <= H``）。"""
        prefix_len = max(0, min(int(prefix_len), self.height))
        execution_len = max(0, min(int(execution_len), self.height - prefix_len))
        suffix_len = max(0, min(int(suffix_len), self.height - prefix_len - execution_len))
        p = prefix_len
        e = p + execution_len
        s = e + suffix_len
        return ChunkSlice(self.actions[:p], self.actions[p:e], self.actions[e:s])

    def steps(self):
        """展开为 ``[(绝对步号, 动作), ...]``（含 prefix 段；调用方按当前步号过滤）。"""
        return [(self.start_index + i, self.actions[i]) for i in range(self.height)]


def as_action_chunk(chunk, start_index: int = 0) -> ActionChunk | None:
    """把策略返回（``ActionChunk`` / ndarray / None）规范化为 ``ActionChunk``；None 透传。"""
    if chunk is None:
        return None
    if isinstance(chunk, ActionChunk):
        return chunk
    return ActionChunk(actions=chunk, start_index=start_index)
