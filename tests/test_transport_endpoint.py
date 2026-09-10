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

"""传输层端点更新测试 —— ``set_endpoint`` 只允许未连接时调用，并重建连接目标。

覆盖：ws URI 重建、gRPC target 重建、已连接时拒绝（不触碰网络：只构造传输对象，不 connect）。
"""

import pytest

from motrix_edge.transport.grpc import AsyncInferenceGrpcTransport
from motrix_edge.transport.ws import WsTransport


def test_ws_transport_set_endpoint_rebuilds_uri():
    """WsTransport：改 host / port 后 URI 重建（下次 connect 用新地址）。"""
    transport = WsTransport(host="127.0.0.1", port=8000)
    assert transport.endpoint == {"host": "127.0.0.1", "port": 8000}
    assert transport._uri == "ws://127.0.0.1:8000"

    assert transport.set_endpoint(host="10.0.0.9") == {"host": "10.0.0.9", "port": 8000}
    assert transport._uri == "ws://10.0.0.9:8000"  # 只改 host：端口保持

    transport.set_endpoint(port=9000)
    assert transport._uri == "ws://10.0.0.9:9000"


def test_ws_transport_set_endpoint_accepts_full_url():
    """host 传完整 ws URL（含端口）时不重复拼端口（与构造语义一致）。"""
    transport = WsTransport(host="ws://127.0.0.1:8000")
    assert transport._uri == "ws://127.0.0.1:8000"


def test_grpc_transport_set_endpoint_rebuilds_target():
    """AsyncInferenceGrpcTransport：改 host / port 后 target 字段重建。"""
    transport = AsyncInferenceGrpcTransport(host="127.0.0.1", port=8080)
    transport.set_endpoint(host="10.0.0.9", port=9090)
    assert transport._host == "10.0.0.9"
    assert transport._port == 9090
    assert transport.endpoint == {"host": "10.0.0.9", "port": 9090}


def test_transport_set_endpoint_rejected_when_connected():
    """已连接时不能热改连接目标（否则与实际连接不一致）→ ValueError。"""
    transport = WsTransport(host="127.0.0.1", port=8000)
    transport._ws = object()  # 模拟已连接（connected 属性基于 _ws 是否为空）
    assert transport.connected is True
    with pytest.raises(ValueError, match="already connected"):
        transport.set_endpoint(host="10.0.0.9")
