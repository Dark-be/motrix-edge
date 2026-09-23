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

"""rpent.wire —— RPent RPC 的 numpy 线上编解码（``__ndarray__`` / ``__npscalar__`` tag）。

与 RPent ``rpent/utils/rpc/http_rpc.py`` 对称；``POST /call`` 的收发都经这里。
"""

from __future__ import annotations

import base64
from typing import Any

import numpy as np

# ---- 线上编解码（numpy tag）--------------------------------------------------

_NDARRAY_TAG = "__ndarray__"
_NPSCALAR_TAG = "__npscalar__"


def to_wire(value: Any) -> Any:
    """任意结果 → JSON 可序列化的线上形态（ndarray / 标量打 RPent 约定的 tag）。

    RPent 侧 ``_from_json`` 会把 tag 还原成 ndarray / NumPy 标量；未打 tag 的容器原样返回。
    ``bytes`` 按 uint8 一维数组传输（RPent 无 bytes 编码，这样至少可被正确还原）。
    """
    if isinstance(value, np.ndarray):
        array = np.ascontiguousarray(value)
        return {
            _NDARRAY_TAG: base64.b64encode(array.tobytes()).decode("ascii"),
            "dtype": str(array.dtype),
            "shape": list(array.shape),
        }
    if isinstance(value, np.generic):  # NumPy 标量：保留 dtype（RPent 会还原为同 dtype 标量）
        item = value.item()
        if value.dtype.kind in "biuf" and isinstance(item, (bool, int, float)):
            return {_NPSCALAR_TAG: item, "dtype": str(value.dtype)}
        return item
    if isinstance(value, (bytes, bytearray)):
        return to_wire(np.frombuffer(bytes(value), dtype=np.uint8))
    if isinstance(value, dict):
        return {str(key): to_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(item) for item in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return str(value)  # 其他对象（枚举 / datetime 等）：转字符串，保证响应始终可序列化


def from_wire(value: Any) -> Any:
    """线上形态 → Python 值（解析 ``args`` / ``kwargs``；与 RPent ``_from_json`` 同规则）。"""
    if isinstance(value, dict):
        if _NDARRAY_TAG in value and set(value) <= {_NDARRAY_TAG, "dtype", "shape"}:
            raw = base64.b64decode(value[_NDARRAY_TAG])
            array = np.frombuffer(raw, dtype=value.get("dtype"))
            # frombuffer 返回只读视图（base 是 bytes）：拷贝一份，行为与 pickle 往返一致
            return array.reshape(value.get("shape", (-1,))).copy()
        if _NPSCALAR_TAG in value and set(value) <= {_NPSCALAR_TAG, "dtype"}:
            return np.dtype(value["dtype"]).type(value[_NPSCALAR_TAG])
        return {key: from_wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_wire(item) for item in value]
    return value


__all__ = ["from_wire", "to_wire"]
