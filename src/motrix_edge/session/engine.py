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

"""会话的步进引擎 —— 「**本步动作从哪来**」的唯一抽象（见 design「会话分层」的引擎层）。

会话只依赖「``step(observation)`` + ``reset()``」这一个面：

- :class:`RtcEngine`：薄包装 ``RTCManager``（块队列 / 三元切分 / 时序平滑 / 异步预取），推理会话用；
  ``rtc.enabled=false`` 时由 manager 自身退化为「每步请求一次、只取块首步」。

引擎只管取步，不管命令、生命周期、录制与上行（那些归基座 / 功能 / 会话）。
"""

from typing import Protocol

import numpy as np


class Engine(Protocol):
    """步进引擎协议：``step`` 产出本步动作（``None`` = 本步无动作，会话跳过本步），``reset`` 归零。"""

    def step(self, observation) -> np.ndarray | None:
        """本步动作（物理空间）；``None`` = 本步无动作（空块 / 整块过期 / 队列暂无）。"""
        ...

    def reset(self) -> None:
        """归零内部状态（步号 / 块队列 / 预取）：会话开始或暂停恢复时调用。"""
        ...


class RtcEngine:
    """RTC 引擎：薄包装 ``RTCManager``（``step`` = ``infer``，``reset`` = ``reset``）。

    manager 本身仍是 RTC 参数（``infer rtc set``）与状态上报（``/v1/infers`` 的 ``rtc`` 字段）
    的作用对象，经 :attr:`rtc` 暴露给会话，故引擎这层只提供统一的取步面。
    """

    def __init__(self, rtc):
        self.rtc = rtc

    def step(self, observation) -> np.ndarray | None:
        """本步动作：交给 ``RTCManager.infer``（必要时预取 / 内联推理 / 弹队）。"""
        return self.rtc.infer(observation)

    def reset(self) -> None:
        """清空块队列、作废在途预取并步号归零（策略连接与工作线程不变）。"""
        self.rtc.reset()

    def status(self) -> dict:
        """运行状态（参数 / 步号 / 预取计数 / 最近一块）；直接转发 manager。"""
        return self.rtc.status()
