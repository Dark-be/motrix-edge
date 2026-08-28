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

import numpy as np


class ActionChunkBroker:
    """动作块逐帧下发：每次只取当前步动作，**块耗尽后才由调用方向推理端请求新块**。

    与 openpi-client 的 ActionChunkBroker 语义一致：
      - ``empty``：当前无可用动作块（True = 调用方需向推理端请求新块并 ``feed``）；
      - ``feed(chunk)``：存入新动作块（仅在 ``empty`` 时调用）；
      - ``step()``：消耗缓存块的当前步动作（**不触发网络请求**）。

    动作块第一维是**实际块长度**（通常为协商的 horizon，但允许末块更短或服务端返回
    更长块）；耗尽判断以 ``chunk.shape[0]`` 为准，不截断、不按协商值越界。
    若服务端返回单步动作（[dim]），则透传不切片。
    """

    def __init__(self, action_horizon: int):
        self._action_horizon = action_horizon  # 协商值（诊断 / 契约信息）；消费以实际块长为准
        self._cur_step = 0
        self._chunk = None

    @property
    def empty(self) -> bool:
        """当前无可用动作块（True = 调用方需向推理端请求新块并 ``feed``）。"""
        return self._chunk is None

    def feed(self, chunk) -> None:
        """存入新动作块（重置游标；长度可与协商 horizon 不同）。"""
        self._chunk = np.asarray(chunk)
        self._cur_step = 0

    def step(self) -> np.ndarray:
        """推进一步：返回缓存块的当前步动作（不触发网络请求）。

        单步动作（[dim]）透传不切片；块耗尽后清空缓存（下次 ``empty`` 为 True）。
        """
        if self._chunk is None:
            raise RuntimeError("ActionChunkBroker.step() called with no cached chunk (feed() first)")
        if self._chunk.ndim == 1:
            action = self._chunk
            self._chunk = None
            return action

        action = self._chunk[self._cur_step]
        self._cur_step += 1
        if self._cur_step >= self._chunk.shape[0]:
            self._chunk = None
        return action

    def reset(self):
        self._chunk = None
        self._cur_step = 0
