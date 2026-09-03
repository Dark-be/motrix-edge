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
    """传输层基类：connect / close / server_metadata。子类实现具体媒介。"""

    def __init__(self, **kwargs) -> None:
        self.config: dict = kwargs or {}
        self.server_metadata: dict = {}

    def connect(self):
        """建立连接（初始化传输并读取服务端 metadata）；失败应清理半开连接后抛异常。"""
        raise NotImplementedError("Subclasses should implement this method.")

    def close(self):
        """关闭连接（幂等）。"""
        pass
