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

"""传输层连接目标测试 —— 目标由 host / port 在 connect 时现算（端点随会话固化）。

覆盖：ws URI、gRPC target 的现算结果、host 传完整 ws URL 时不重复拼端口（不触碰网络：
只构造传输对象，不 connect）。
"""

from motrix_edge.transport import AsyncInferenceGrpcTransport, WsTransport


def test_ws_transport_target_from_host_port():
    """WsTransport：``_target()`` 由构造参数现算（改端点请重建传输 / 重进会话）。"""
    transport = WsTransport(host="127.0.0.1", port=8000)
    assert transport.endpoint == {"host": "127.0.0.1", "port": 8000}
    assert transport._target() == "ws://127.0.0.1:8000"


def test_ws_transport_accepts_full_url():
    """host 传完整 ws URL（含端口）时不重复拼端口（与构造语义一致）。"""
    transport = WsTransport(host="ws://127.0.0.1:8000")
    assert transport._target() == "ws://127.0.0.1:8000"


def test_grpc_transport_target_from_host_port():
    """AsyncInferenceGrpcTransport：target 由 host / port 现算。"""
    transport = AsyncInferenceGrpcTransport(host="10.0.0.9", port=9090)
    assert transport.endpoint == {"host": "10.0.0.9", "port": 9090}
    assert transport._target() == "10.0.0.9:9090"
