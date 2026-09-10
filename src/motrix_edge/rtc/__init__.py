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

"""rtc 子包 —— 策略无关的实时动作块管理器（Real-Time Chunking）。

策略（``policy.infer_chunk``）只负责**拿到一次推理的原始动作块**；本子包统一负责：

  - **块长上限 H**（一次推理只取块的前 H 步，如 10 步 = 10Hz × 1s）；
  - **三元切分**：``prefix``（前置段 P，推理期间**已被执行** → 跳过）/ 执行段（E）/ ``suffix``
    （后缀段 S，与下一块执行段重叠融合）；
  - **时序平滑**（重叠步加权平均）、**预取时机**与**绝对步号推进**。

设计见 wiki/design/motrix_edge_rtc.md。
"""

from motrix_edge.rtc.base import AGGREGATE_FUNCTIONS, ActionChunk, ChunkSlice, as_action_chunk, get_aggregate_fn
from motrix_edge.rtc.manager import DEFAULT_RTC_CONFIG, RTCManager, validate_config, validate_params


def build_rtc(policy, config: dict | None = None, control_hz: float | None = None) -> RTCManager:
    """工厂：为一个策略客户端构造 RTCManager（推理会话进入时调用）。

    ``config`` = ``policy.rtc`` 配置段（缺省用 ``DEFAULT_RTC_CONFIG``）；非法参数会抛
    ``ValueError``（由调用方回执 rejected）。``control_hz`` = 会话控制频率（Hz），仅用于把
    **实测推理耗时**折算成步数上报（供人工设定前置段 P）。
    """
    return RTCManager(policy=policy, config=config, control_hz=control_hz)


__all__ = [
    "AGGREGATE_FUNCTIONS",
    "ActionChunk",
    "ChunkSlice",
    "DEFAULT_RTC_CONFIG",
    "RTCManager",
    "as_action_chunk",
    "build_rtc",
    "get_aggregate_fn",
    "validate_config",
    "validate_params",
]
