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

- ``WsTransport``（msgpack-over-websocket）：openpi / 通用一问一答策略使用；
- ``AsyncInferenceGrpcTransport``（lerobot AsyncInference gRPC）：act 等走
  lerobot 官方 ``policy_server`` 的流式策略使用（vendored ``src/lerobot`` 提供
  proto 与 wire 数据类，本包只做 channel/stub 封装与连接管理）。

传输层不关心消息格式：序列化契约（``contract``）与策略语义由上层策略客户端负责。
导入本包不触发 grpc / pb2 导入（各自延迟到使用时），避免纯 ws 场景缺依赖报错。
"""

from .base import BaseTransport
from .msgpack_numpy import Packer, Unpacker, packb, unpackb
from .ws import MsgpackTransport, WsTransport


def get_transport(kind: str, cfg: dict | None = None):
    """传输工厂：按 kind 实例化（ws / grpc）。

    kind: "ws" | "msgpack" → WsTransport；"grpc" | "async_inference" →
    AsyncInferenceGrpcTransport（后者延迟导入 grpc / pb2）。
    """
    cfg = cfg or {}
    if kind in ("ws", "msgpack"):
        return WsTransport(
            host=cfg.get("host", "0.0.0.0"),
            port=cfg.get("port"),
            api_key=cfg.get("api_key"),
            connect_timeout=cfg.get("connect_timeout", 5.0),
        )
    if kind in ("grpc", "async_inference"):
        from .grpc import AsyncInferenceGrpcTransport

        return AsyncInferenceGrpcTransport(
            host=cfg.get("host", "127.0.0.1"),
            port=cfg.get("port"),
            connect_timeout=cfg.get("connect_timeout", 5.0),
        )
    raise ValueError(f"Unknown transport kind '{kind}'. Available: ws/msgpack, grpc/async_inference")


__all__ = [
    "BaseTransport",
    "WsTransport",
    "MsgpackTransport",
    "Packer",
    "Unpacker",
    "packb",
    "unpackb",
    "get_transport",
]
