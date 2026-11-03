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

"""rpent.errors —— RPent RPC 面的错误类型（HTTP 层据此回 ``ok=false`` 信封）。"""

from __future__ import annotations

# ---- 错误 --------------------------------------------------------------------


class RpentError(Exception):
    """RPC 方法拒绝（租约 / 状态 / 参数 / 不支持）。

    ``kind`` 供调用方与日志分类（RPent 只读 ``error`` 文本，多带的字段会被忽略）：
    ``lease`` / ``state`` / ``argument`` / ``unsupported`` / ``unknown_method``。
    """

    def __init__(self, message: str, *, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


__all__ = ["RpentError"]
