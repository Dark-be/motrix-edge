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

"""AsyncInferenceGrpcTransport —— lerobot AsyncInference gRPC 传输（流式策略）。

只负责连接管理与 channel / stub 暴露：Ready / SendPolicyInstructions /
SendObservations / GetActions 的 **wire 语义由上层策略客户端（policy/act）组合**，
wire 数据类与分块工具来自 vendored ``lerobot.transport``（``src/lerobot``）。

grpc / pb2 延迟导入：导入本模块不依赖 grpc，仅 ``connect()`` 时才加载。
"""

from motrix_edge.transport.base import BaseTransport


class AsyncInferenceGrpcTransport(BaseTransport):
    """AsyncInference 服务（lerobot 官方 policy_server）的 gRPC 客户端连接。"""

    def __init__(self, host="127.0.0.1", port=None, connect_timeout=5.0):
        super().__init__(host=host, port=port, connect_timeout=connect_timeout)
        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._channel = None
        self.stub = None  # AsyncInferenceStub（connect 后可用）

    @property
    def connected(self) -> bool:
        return self.stub is not None

    def connect(self):
        """建立 insecure channel 并等待 READY；暴露 ``self.stub``（AsyncInferenceStub）。

        连接 / 就绪超时 → 清理半开连接后抛异常（重试由上层 session 驱动）。
        """
        import grpc  # noqa: PLC0415 延迟导入（避免导入 motrix_edge.transport 时依赖 grpc）

        from lerobot.transport import services_pb2_grpc as pb2_grpc  # noqa: PLC0415
        from lerobot.transport.utils import grpc_channel_options  # noqa: PLC0415

        self.close()  # 幂等清理（重连场景）
        target = f"{self._host}:{self._port}"
        self._channel = grpc.insecure_channel(target, options=grpc_channel_options(initial_backoff="0.1s"))
        try:
            grpc.channel_ready_future(self._channel).result(timeout=self._connect_timeout)
        except Exception:
            self.close()
            raise
        self.stub = pb2_grpc.AsyncInferenceStub(self._channel)

    def close(self):
        """关闭 channel（幂等）。"""
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self.stub = None
