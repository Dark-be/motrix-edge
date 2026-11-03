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

"""transport 包 —— 推理策略的通用传输层（与 lerobot / 具体策略解耦）。

按「传输方式」承载并抽象，策略客户端只依赖本包提供的传输对象：

- ``WsTransport``（msgpack-over-websocket）：openpi / 通用一问一答策略使用。

``lerobot-remote``（zenoh）**不走本包**：它的传输（``ZenohTransport``）与协议一起由
vendored ``src/lerobot`` 提供，策略客户端直接使用，避免在 edge 侧再抽象一层 zenoh 语义。

传输层不关心消息格式：序列化契约（``contract``）与策略语义由上层策略客户端负责。
"""

from .base import BaseTransport
from .ws import WsTransport

__all__ = ["BaseTransport", "WsTransport"]
