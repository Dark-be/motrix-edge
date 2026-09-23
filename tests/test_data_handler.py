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

"""展示用数值精度（``round_floats``）：日志 / HTTP 回执 / 网页统一 3 位小数。

只影响对外展示值，内部推理 / 控制链路仍用全精度（见 ``utils/data_handler.py``）。
"""

import numpy as np
import pytest

from motrix_edge.utils.data_handler import DISPLAY_FLOAT_DIGITS, round_floats


def test_round_floats_nested_and_arrays():
    """ndarray / list / dict 递归取整为可 JSON 化的值。"""
    assert round_floats(np.array([0.123456789, 1.987654321])) == [0.123, 1.988]
    assert round_floats([0.1, [0.23456789]]) == [0.1, [0.235]]
    assert round_floats({"a": np.array([0.00049]), "b": 3}) == {"a": [0.0], "b": 3}


def test_round_floats_passthrough_and_edge_cases():
    """非浮点原样返回；NaN / Inf 保持（inf 不能取整）；负零归零。"""
    assert round_floats(None) is None
    assert round_floats(True) is True  # bool 是 int 子类：不能被当成 1 输出
    assert round_floats(7) == 7
    assert round_floats("1.23456") == "1.23456"
    assert round_floats(-0.0004) == 0.0  # -0.0 → 0.0（日志里不出现 -0.0）
    assert round_floats(float("inf")) == float("inf")


def test_display_digits_default_is_three():
    """默认展示精度 = 3 位小数（改这里等于改全局日志 / 网页口径）。"""
    assert DISPLAY_FLOAT_DIGITS == 3
    with pytest.raises(TypeError):
        round_floats(np.array([1.23456]), "3")  # ndigits 必须是整数
