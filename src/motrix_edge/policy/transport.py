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

import websockets.sync.client

from motrix_edge.policy import msgpack_numpy


class MsgpackTransport:
    """通用 msgpack-over-websocket 传输层。

    与推理节点的对话模型（一问一答，阻塞式）：
      connect():  建立连接，等待服务端下发首条 metadata（msgpack）
      request():  发送 msgpack(payload)，阻塞接收并解析响应；服务端以文本回包表示错误
      close():    关闭连接

    借鉴 openpi-client 的 WebsocketClientPolicy，但解耦出通用的「传输 + 序列化」，
    上层策略只需约定 payload / response 的格式契约（见 contract.py）。
    """

    def __init__(self, host="0.0.0.0", port=None, api_key=None, connect_timeout=5.0):
        self._uri = host if str(host).startswith("ws") else f"ws://{host}"
        if port is not None:
            self._uri += f":{port}"
        self._api_key = api_key
        self._connect_timeout = connect_timeout  # 单次连接尝试超时（open + metadata 接收）
        self._packer = msgpack_numpy.Packer()
        self._ws = None
        self._server_metadata = None

    def connect(self):
        """连接推理节点（单次尝试限时）。

        连接 / 接收 metadata 超时 → 抛异常并清理半开连接，**不再无限重试**——重试由上层
        session 驱动（推理节点未就绪时任务线程可被打断退出，避免阻塞命令回执）。
        """
        headers = {"Authorization": f"Api-Key {self._api_key}"} if self._api_key else None
        try:
            self._ws = websockets.sync.client.connect(
                self._uri,
                compression=None,
                max_size=None,
                additional_headers=headers,
                open_timeout=self._connect_timeout,
            )
            self._server_metadata = msgpack_numpy.unpackb(self._ws.recv(timeout=self._connect_timeout))
        except Exception:
            self.close()  # 释放半开连接（幂等）
            raise

    @property
    def server_metadata(self):
        return self._server_metadata

    def request(self, payload: dict) -> dict:
        """发送 payload 并阻塞等待响应。响应为 str 时视为服务端错误。"""
        self._ws.send(self._packer.pack(payload))
        response = self._ws.recv()
        if isinstance(response, str):
            raise RuntimeError(f"Error in inference server:\n{response}")
        return msgpack_numpy.unpackb(response)

    def close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None
