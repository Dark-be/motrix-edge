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

"""WsTransport.request / close 的限时与断连语义（假 socket，不触网）。

覆盖：request 把 ``request_timeout`` 传给 ``recv``；超时 / 连接异常 / 服务端错误文本 →
关闭连接（``connected`` 置 False，上层据此重连）并抛出原异常；close 幂等且容忍半开连接。
"""

import msgpack
import pytest

from motrix_edge.transport.ws import WsTransport


class _FakeSocket:
    """假 websocket：记录 send / recv 调用参数，按脚本返回或抛异常。"""

    def __init__(self, response=None, recv_error=None, send_error=None, close_error=None):
        self.response = response
        self.recv_error = recv_error
        self.send_error = send_error
        self.close_error = close_error
        self.recv_timeouts: list = []
        self.sent: list = []
        self.close_calls = 0

    def send(self, data):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def recv(self, timeout=None):
        self.recv_timeouts.append(timeout)
        if self.recv_error is not None:
            raise self.recv_error
        return self.response

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _connected_transport(sock) -> WsTransport:
    """构造已连接（``_ws`` 非空）的传输：只测 request / close 语义，不触网。"""
    transport = WsTransport(host="127.0.0.1", port=8000, request_timeout=1.5)
    transport._ws = sock  # connected 属性基于 _ws 是否为空
    return transport


def test_request_passes_request_timeout_and_decodes_response():
    """正常往返：recv 带 request_timeout，返回解包后的 dict，连接保留。"""
    sock = _FakeSocket(response=msgpack.packb({"actions": [[1.0, 2.0]]}))
    transport = _connected_transport(sock)

    assert transport.request({"state": [0.0]}) == {"actions": [[1.0, 2.0]]}
    assert sock.recv_timeouts == [1.5]
    assert len(sock.sent) == 1
    assert transport.connected is True


def test_request_timeout_closes_connection():
    """超时 → 关闭连接（服务端可能迟到回包，复用连接会造成响应错位）并抛出原异常。"""
    sock = _FakeSocket(recv_error=TimeoutError("timed out"))
    transport = _connected_transport(sock)

    with pytest.raises(TimeoutError):
        transport.request({"state": [0.0]})
    assert transport.connected is False  # 状态可信：上层 session 会重连
    assert sock.close_calls == 1


def test_request_send_failure_closes_connection():
    """发送即失败（服务端已断开）→ 关闭连接，不误报仍在线。"""
    sock = _FakeSocket(send_error=ConnectionResetError("broken pipe"))
    transport = _connected_transport(sock)

    with pytest.raises(ConnectionResetError):
        transport.request({"state": [0.0]})
    assert transport.connected is False


def test_request_server_error_closes_connection():
    """服务端以文本回包表示错误：抛 RuntimeError，并同步关闭连接。

    openpi 官方服务端把 traceback 作为文本帧发出后**随即关闭**连接（
    ``websocket_policy_server._handler``）——这里跟着关，避免下次请求先撞「连接已断」、
    以及 ``connected`` 误报在线。
    """
    sock = _FakeSocket(response="boom")
    transport = _connected_transport(sock)

    with pytest.raises(RuntimeError, match="inference server"):
        transport.request({"state": [0.0]})
    assert transport.connected is False
    assert sock.close_calls == 1


def test_close_is_idempotent_and_tolerates_broken_socket():
    """close：幂等；socket 已异常断开时不抛异常，状态仍置为未连接。"""
    sock = _FakeSocket()
    transport = _connected_transport(sock)
    transport.close()
    assert transport.connected is False
    assert sock.close_calls == 1
    transport.close()  # 幂等：已关闭不再触碰 socket
    assert sock.close_calls == 1

    transport._ws = _FakeSocket(close_error=RuntimeError("already closed"))
    transport.close()  # 半开 / 已断连：不抛异常
    assert transport.connected is False
