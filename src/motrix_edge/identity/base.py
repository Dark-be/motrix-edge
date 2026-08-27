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

"""Edge 本地设备身份声明（identity）。

权威在 MotrixConsole：注册、Published RobotModel / CapabilityVersion、租约权威状态
都在 Console 侧保存。Edge 只持有本地的「身份声明」引用，用于随请求上报与本地校验，
不产出、不裁决这些状态（见 wiki/design「后续阶段（M11/M12）」）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    """Edge 本地设备身份声明（引用，非权威）。

    - ``edge_id`` / ``edge_name`` / ``edge_version`` 均来自部署时 Console 下发的
      ``identity`` 配置，Edge 只读。
    - ``headers()`` 是预留的「发送接口」：把身份序列化为 HTTP 请求头 / 元数据，
      具体发送（真正的 HTTP 调用）由 HTTP 服务 / 客户端层实现。
    """

    edge_id: str
    edge_name: str
    edge_version: str

    def headers(self) -> dict[str, str]:
        """预留发送接口：序列化为请求头 / 元数据 dict（供 HTTP 层上报身份）。"""
        return {
            "X-Edge-Id": self.edge_id,
            "X-Edge-Name": self.edge_name,
            "X-Edge-Version": self.edge_version,
        }
