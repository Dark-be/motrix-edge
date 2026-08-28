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

"""循环节奏控制 —— 按目标频率对齐循环周期（避免固定 sleep 造成的频率漂移）。

会话（采集 / 推理）主循环每帧调用：``loop_start`` 取本轮循环开始时刻，
``pace(freq, loop_start)`` 按 ``1/freq`` 补齐剩余时间；单帧已超时则跳过 sleep
（可经 ``label`` 告警提示低于目标频率）。
"""

import time

from motrix_edge.utils.data_handler import debug_print


def pace(freq: float, loop_start: float, label: str | None = None, name: str | None = None) -> None:
    """按 ``freq``（Hz）对齐循环周期：``loop_start`` 后补足 ``1/freq``。

    - 本轮已耗时小于周期 → 睡到周期边界（保持频率稳定）。
    - 已超时且 ``label`` 非 None → 告警提示低于目标频率（``name`` 为日志前缀），不 sleep。
    """
    elapsed = time.monotonic() - loop_start
    wait = 1.0 / freq - elapsed
    if wait > 0:
        time.sleep(wait)
    elif label is not None:
        debug_print(
            name or label,
            f"{label} over limit: {elapsed:.3f}s > {1 / freq:.3f}s (below {freq} Hz)",
            "WARNING",
        )


__all__ = ["pace"]
