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

"""WsTransport —— 通用 msgpack-over-websocket 传输（一问一答，阻塞式）。

与推理节点的对话模型：
  connect():  建立连接，等待服务端下发首条 metadata（msgpack）
  request():  发送 msgpack(payload)，阻塞接收并解析响应；服务端以文本回包表示错误
  close():    关闭连接

借鉴 openpi-client 的 WebsocketClientPolicy，但解耦出通用的「传输 + 序列化」，
上层策略只需约定 payload / response 的格式契约（见 policy/contract.py）。
"""

import websockets.sync.client

from motrix_edge.transport import msgpack_numpy
from motrix_edge.transport.base import BaseTransport


class WsTransport(BaseTransport):
    """msgpack-over-websocket 传输（一问一答）。"""

    def __init__(self, host="0.0.0.0", port=None, api_key=None, connect_timeout=5.0, request_timeout=60.0):
        super().__init__(
            host=host,
            port=port,
            api_key=api_key,
            connect_timeout=connect_timeout,  # 单次连接尝试超时（open + metadata 接收）
            request_timeout=request_timeout,  # 单次 request 等待响应超时（防无限阻塞）
        )
        self._packer = msgpack_numpy.Packer()
        self._ws = None

    def _target(self) -> str:
        """按 host / port **现算** websocket URI（端点变更后无需重建缓存）。"""
        host = self.config.get("host", "0.0.0.0")
        port = self.config.get("port")
        uri = host if str(host).startswith("ws") else f"ws://{host}"
        return uri if port is None else f"{uri}:{port}"

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def connect(self):
        """连接推理节点（单次尝试限时）。

        连接 / 接收 metadata 超时 → 抛异常并清理半开连接，**不再无限重试**——重试由上层
        session 驱动（推理节点未就绪时任务线程可被打断退出，避免阻塞命令回执）。
        """
        api_key = self.config.get("api_key")
        timeout = self.config.get("connect_timeout", 5.0)
        headers = {"Authorization": f"Api-Key {api_key}"} if api_key else None
        try:
            self._ws = websockets.sync.client.connect(
                self._target(),
                compression=None,
                max_size=None,
                additional_headers=headers,
                open_timeout=timeout,
            )
            self.server_metadata = msgpack_numpy.unpackb(self._ws.recv(timeout=timeout))
        except Exception:
            self.close()  # 释放半开连接（幂等）
            raise

    def request(self, payload: dict) -> dict:
        """发送 payload 并阻塞等待响应（**单次请求限时**）；文本响应视为服务端错误。

        - 超时 / 连接异常 → 关闭连接（``connected`` 置 False）后抛出原异常：超时后服务端仍可能
          回包，同一连接复用会造成「响应错位」（本次响应被下次 request 读到）；
        - 文本响应 → openpi 官方服务端把 traceback 作为文本帧发出后**随即关闭**连接（
          ``websocket_policy_server._handler``），故此路径同样关闭：否则下一次请求会先撞上
          「连接已断」，还会让 ``connected`` 误报在线。
        """
        try:
            self._ws.send(self._packer.pack(payload))
            response = self._ws.recv(timeout=self.config.get("request_timeout", 60.0))
        except Exception:
            self.close()  # 连接状态不可信：置为未连接，交由上层重连
            raise
        if isinstance(response, str):
            self.close()  # 服务端发完错误文本即断开：这里同步状态（重连由上层 session 驱动）
            raise RuntimeError(f"Error in inference server:\n{response}")
        return msgpack_numpy.unpackb(response)

    def close(self):
        """关闭连接（幂等；半开 / 已断连场景不抛异常，状态始终置为未连接）。"""
        ws, self._ws = self._ws, None
        self.server_metadata = {}
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 连接已异常断开：忽略（状态已重置）
                pass
