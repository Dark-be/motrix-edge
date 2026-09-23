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

"""server.routes —— HTTP 端点按域拆分，每个模块导出一个 ``build_router(services)``。

**只做 HTTP 映射**：解析入参（Pydantic 模型）→ 提交命令（``CommandService``）或读只读快照
（``server/status.py`` · ``state.py``）→ 返回 dict。业务语义在 session / node 与
``utils/commands.py``（命令注册表 / 参数解析）里；错误统一在 ``app.py`` 的错误处理器转成
响应——故 router 里没有 try/except。
"""

from fastapi import APIRouter

from motrix_edge.server.deps import Services
from motrix_edge.server.routes.captures import build_router as build_captures_router
from motrix_edge.server.routes.commands import build_router as build_commands_router
from motrix_edge.server.routes.health import build_router as build_health_router
from motrix_edge.server.routes.infers import build_router as build_infers_router
from motrix_edge.server.routes.leases import build_router as build_leases_router
from motrix_edge.server.routes.rpent import build_router as build_rpent_router
from motrix_edge.server.routes.uploads import build_router as build_uploads_router
from motrix_edge.server.routes.webrtc import build_router as build_webrtc_router


def build_routers(services: Services) -> list[APIRouter]:
    """按域构建全部 router（``create_app`` 逐个 ``include_router``）。"""
    return [
        build_health_router(services),  # /v1/health + /v1/adapters/*
        build_leases_router(services),  # /v1/leases/*
        build_commands_router(services),  # /v1/commands
        build_uploads_router(services),  # /v1/uploads/*
        build_captures_router(services),  # /v1/captures/* + /v1/preview
        build_infers_router(services),  # /v1/infers/*
        build_webrtc_router(services),  # /v1/webrtc/offer
        build_rpent_router(services),  # /call + /v1/rpent
    ]


__all__ = ["build_routers"]
