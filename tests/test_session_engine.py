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

"""步进引擎与共享取步口径的单测（S6）。

``chunk_step_action`` = 「块内取当前绝对步、整块过期则不给动作」的**唯一实现**（推理的
``rtc.enabled=false`` 退化路径与它后面的消费方共用）；``RtcEngine`` 是引擎层的实现之一，会话只依赖
``step`` / ``reset``。无网络无硬件可跑。
"""

import numpy as np

from motrix_edge.rtc.base import ActionChunk, chunk_step_action
from motrix_edge.session.engine import RtcEngine


class _FakePolicy:
    """策略替身：每次 ``infer_chunk`` 返回同一块，并记录请求步号。"""

    def __init__(self, chunk):
        self.chunk = chunk
        self.indexes: list[int | None] = []

    def infer_chunk(self, observation, index=None):
        self.indexes.append(index)
        return self.chunk


# ---- 共享取步口径（F11）----------------------------------------------------


def test_chunk_step_action_reads_current_step():
    """块首步早于当前步号 → 取块内对应步（异步预取期间控制环已经走过几步）。"""
    chunk = ActionChunk(actions=np.arange(6, dtype=float).reshape(3, 2), start_index=10)
    assert np.array_equal(chunk_step_action(chunk, 10), [0.0, 1.0])
    assert np.array_equal(chunk_step_action(chunk, 12), [4.0, 5.0])  # 块内最后一步


def test_chunk_step_action_drops_stale_chunk():
    """整块落在过去 → None（不拿过期动作驱动真机）；块首步领先当前步号 → 取首步。"""
    chunk = ActionChunk(actions=np.arange(6, dtype=float).reshape(3, 2), start_index=10)
    assert chunk_step_action(chunk, 13) is None  # 块尾都过去了
    assert np.array_equal(chunk_step_action(chunk, 8), [0.0, 1.0])  # 块还没到：取首步


def test_chunk_step_action_handles_missing_and_empty_chunk():
    """空块 / 未返回块 → None；策略未声明块首步 → 按请求步号对齐（与 as_action_chunk 同语义）。"""
    assert chunk_step_action(None, 0) is None
    assert chunk_step_action(ActionChunk(actions=np.zeros((0, 2))), 0) is None
    undeclared = ActionChunk(actions=np.array([[1.0, 2.0], [3.0, 4.0]]))  # start_index=None
    assert np.array_equal(chunk_step_action(undeclared, 7), [1.0, 2.0])


# ---- 引擎层 -----------------------------------------------------------------


def test_rtc_engine_delegates_to_manager():
    """``RtcEngine`` = 薄包装：``step`` / ``reset`` / ``status`` 都转给 manager，manager 原样可取。"""
    calls: list[str] = []

    class _Manager:
        def infer(self, observation):
            calls.append(f"infer:{observation}")
            return np.array([9.0])

        def reset(self) -> None:
            calls.append("reset")

        def status(self) -> dict:
            return {"step": 1}

    manager = _Manager()
    engine = RtcEngine(manager)
    assert engine.rtc is manager  # 会话仍直接持 manager（infer rtc set / rtc 状态上报）
    assert np.array_equal(engine.step("obs"), [9.0])
    engine.reset()
    assert engine.status() == {"step": 1}
    assert calls == ["infer:obs", "reset"]
