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

"""routes.commands —— ``POST /v1/commands`` 的 HTTP 映射（受控命令 / capability 通道）。

capability 命名 ``<scope>/<verb>``，由命令词派生（``robot execute`` → ``robot/execute``）；
旧拼写保留一版（回执带 ``deprecated=true``）。命令语义在 ``CommandService`` /
``utils/commands.py``，此处只做请求体 → 命令 → 回执的映射。
"""

from fastapi import APIRouter, Request

from motrix_edge.identity import new_correlation_id
from motrix_edge.server.deps import Services
from motrix_edge.server.schemas import CommandRequest, CommandResponse


def build_router(services: Services) -> APIRouter:
    """构建受控命令路由。"""
    router = APIRouter()
    commands = services.commands

    @router.post("/v1/commands")
    def command(req: CommandRequest, request: Request):
        """受控命令：须持有有效租约（``lease_id``）；``capability=robot/estop`` → 全局急停。

        ``capability`` 命名 ``<scope>/<verb>``（如 ``robot/execute`` / ``capture/episode/start``），
        由命令词（``robot execute``）派生；旧拼写仍可用，但回执带 ``deprecated=true``。

        回执状态直接反映命令执行结果（``ok`` / ``rejected`` / ``error``）；``push`` 型命令
        （robot/estop、node/reset）无回执通道 → ``accepted``。未注入 CommandService 时保持骨架。
        """
        corr = getattr(request.state, "correlation_id", None) or new_correlation_id()
        if commands is None:  # 骨架：无 CommandService → accepted（具体执行 / 校验留待注入）
            return CommandResponse(
                command_id=req.command_id,
                status="accepted",
                idempotency_key=req.idempotency_key,
                correlation_id=corr,
            )
        result = commands.execute(
            command_id=req.command_id,
            lease_id=req.lease_id,
            capability=req.capability,
            params=req.params,
        )
        return CommandResponse(
            command_id=req.command_id,
            status=result.get("status", "accepted"),
            idempotency_key=req.idempotency_key,
            correlation_id=corr,
            executed=result.get("executed"),
            error=result.get("error"),
            data=result.get("data"),
            code=result.get("code"),
            deprecated=result.get("deprecated", False),
        )

    return router
