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

"""server 子包 —— MotrixEdge HTTP API 服务。

FastAPI 应用工厂 ``create_app(base_cfg)``；默认运行（``motrix-edge``，加载 config/edge.yml）中
web 作为 node 的独立线程启动。
"""

from .app import CommandRequest, CommandResponse, create_app
from .command import CommandError, CommandService

__all__ = ["create_app", "CommandRequest", "CommandResponse", "CommandError", "CommandService"]
