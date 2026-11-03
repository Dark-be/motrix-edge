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

"""``session/recording`` 测试 —— 所有会话共用的回合录制状态（开轮 / 关轮 + ``episode_id``）。

不依赖 torch / 硬件（只用一个记录调用的 adapter 替身）。
"""

import time

import pytest

from motrix_edge.session.recording import EPISODE_ID_FORMAT, EpisodeRecorder


class _Adapter:
    """adapter 替身：只记录 ``start_capture`` / ``end_capture`` 的调用顺序。"""

    def __init__(self):
        self.calls: list[str] = []

    def start_capture(self):
        self.calls.append("start")

    def end_capture(self):
        self.calls.append("end")


def test_start_generates_episode_id_and_advances_seq():
    """开轮 → 生成 ``{毫秒}-{序号}`` 标识并进入录制态；关轮后保留标识、序号继续递增。"""
    adapter = _Adapter()
    recorder = EpisodeRecorder(adapter)
    assert recorder.recording is False
    assert recorder.episode_id is None
    assert recorder.episode_seq == 0

    first = recorder.start()
    millis, _, seq = first.partition("-")
    assert recorder.recording is True
    assert recorder.episode_id == first
    assert (millis.isdigit(), len(millis), seq) == (True, 13, "1")
    assert abs(int(millis) / 1000 - time.time()) < 5  # 时间戳口径 = 毫秒
    assert first == EPISODE_ID_FORMAT.format(millis=int(millis), seq=1)

    recorder.end()
    assert recorder.recording is False
    assert recorder.episode_id == first  # 关轮后保留「最近一轮」标识（回执 / 状态仍要引用）

    second = recorder.start()
    assert second != first and second.endswith("-2")
    assert recorder.episode_seq == 2
    assert adapter.calls == ["start", "end", "start"]


def test_end_without_recording_still_notifies_adapter():
    """``end`` 幂等：未在录时也照常通知一次（与三个会话既有语义一致，不静默跳过）。"""
    adapter = _Adapter()
    recorder = EpisodeRecorder(adapter)
    recorder.end()
    recorder.end()
    assert adapter.calls == ["end", "end"]
    assert recorder.recording is False


def test_missing_adapter_raises_runtime_error():
    """未绑定适配器就开轮 / 关轮 → 明确报错（构造期不报错：绑定可能晚于会话构造）。"""
    recorder = EpisodeRecorder(None)
    with pytest.raises(RuntimeError, match="has no adapter"):
        recorder.start()
    with pytest.raises(RuntimeError, match="has no adapter"):
        recorder.end()
