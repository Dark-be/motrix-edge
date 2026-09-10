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

"""BaseTransport —— 策略推理传输层基类（ws / grpc 通用最小接口）。

上层策略客户端只依赖 connect / close / server_metadata；具体消息语义（一问一答 /
流式）由各传输实现与策略客户端约定。与 RobotAdapter / BasePolicyClient 基类一致，
生命周期由会话驱动：connect（连接）→ 消息交换 → close（断开）。
"""


class BaseTransport:
    """传输层基类：connect / close / server_metadata / set_endpoint。子类实现具体媒介。"""

    def __init__(self, **kwargs) -> None:
        self.config: dict = kwargs or {}
        self.server_metadata: dict = {}

    @property
    def connected(self) -> bool:
        """是否已建立连接（子类覆盖）。"""
        return False

    @property
    def endpoint(self) -> dict:
        """当前目标端点（``{"host", "port"}``；来自构造参数）。"""
        return {"host": self.config.get("host"), "port": self.config.get("port")}

    def set_endpoint(self, host=None, port=None) -> dict:
        """更新目标端点（**仅未连接时**）：写入 ``config`` 并重建连接目标。

        已连接 → ``ValueError``（连接目标不能热改；由上层先断开或重建传输）。子类用
        ``_rebuild_target()`` 重建 URI / target（默认 no-op，无目标缓存的传输无需覆盖）。
        """
        if self.connected:
            raise ValueError("transport already connected: close it before changing endpoint")
        if host is not None:
            self.config["host"] = host
        if port is not None:
            self.config["port"] = port
        self._rebuild_target()
        return self.endpoint

    def _rebuild_target(self) -> None:
        """重建连接目标（URI / target）：在端点变更后调用。默认 no-op。"""
        return None

    def connect(self):
        """建立连接（初始化传输并读取服务端 metadata）；失败应清理半开连接后抛异常。"""
        raise NotImplementedError("Subclasses should implement this method.")

    def close(self):
        """关闭连接（幂等）。"""
        pass
