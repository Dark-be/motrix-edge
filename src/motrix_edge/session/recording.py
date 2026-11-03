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

"""session/recording —— 数据回合（episode）录制状态：**所有会话共用一份**。

一次「回合」在本项目里是三件事同一边界（见 [会话（session）](../../../wiki/design/motrix_edge_session.md)
的「会话分层」）：

1. **mcap 录制**：由机器人进程执行（``adapter.start_capture`` / ``end_capture``）；
2. **回合标识**：``episode_id``（``{毫秒}-{序号}``）+ 递增序号；写进回执、日志与本仓状态；
3. **训练数据边界**（仅 RL）：``rl/episode_buffer`` 的过渡缓冲以同一 ``episode_id`` 开轮/关轮。

本模块只负责 1 + 2（**无回执、无日志**：回执形状与日志口径归会话）；3 由 RL 会话在自己的钩子里额外
驱动。原先 capture / infer / rl 三处各写一份「start_capture + 回合号 + episode_id 拼接」，收敛到这里。

**不做多线程**（见 design「冻结的取舍」）：本对象只被会话循环线程读写，不加锁。
"""

from __future__ import annotations

import time

# 回合标识格式：``{毫秒时间戳}-{自增序号}``（跨会话一致；序号防同毫秒内两次开轮重名）
EPISODE_ID_FORMAT = "{millis}-{seq}"


class EpisodeRecorder:
    """回合录制状态机（开轮 / 关轮 + 回合标识）。

    Args:
        adapter: 机器人适配器（需有 ``start_capture`` / ``end_capture``）；缺省 ``None``
            （未注入适配器的会话在开轮时才报错，不在构造期报错——构造期 adapter 可能尚未绑定）。

    Raises:
        RuntimeError: 未注入适配器就调用 :meth:`start` / :meth:`end`。
    """

    def __init__(self, adapter=None):
        self._adapter = adapter
        self._recording = False
        self._seq = 0
        self._episode_id: str | None = None

    @property
    def recording(self) -> bool:
        """当前是否开启了一轮回合（``capture episode start`` 之后为 ``True``）。"""
        return bool(self._recording)

    @property
    def episode_id(self) -> str | None:
        """当前 / **最近一轮**回合的标识（关轮后保留，供回执、状态与过渡上报引用）。"""
        return self._episode_id

    @property
    def episode_seq(self) -> int:
        """已开启的回合数（自增序号；重连 / 重进会话不重置）。"""
        return int(self._seq)

    def start(self) -> str:
        """开一轮：通知机器人进程开录 + 生成新 ``episode_id``（返回该标识）。

        Raises:
            RuntimeError: 会话未绑定适配器。
        """
        adapter = self._require_adapter("start")
        adapter.start_capture()
        self._seq += 1
        self._episode_id = EPISODE_ID_FORMAT.format(millis=int(time.time() * 1000), seq=self._seq)
        self._recording = True
        return self._episode_id

    def end(self) -> None:
        """关一轮：通知机器人进程保存 episode（**幂等**——未在录时也照常通知一次，与既有语义一致）。

        Raises:
            RuntimeError: 会话未绑定适配器。
        """
        adapter = self._require_adapter("end")
        adapter.end_capture()
        self._recording = False

    def _require_adapter(self, action: str):
        if self._adapter is None:
            raise RuntimeError(f"cannot {action} an episode recording: session has no adapter")
        return self._adapter
