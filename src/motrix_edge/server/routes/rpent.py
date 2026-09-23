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

"""routes.rpent —— ``POST /call`` 与 ``GET /v1/rpent`` 的 HTTP 映射（RPent 兼容 RPC 面）。

**刻意不走统一错误处理器**：RPent 的 ``HttpRpcClient`` 契约要求响应**始终 HTTP 200**，
失败在 body 里用 ``{"ok": false, "error", "kind"}`` 表达，故此处自行把异常转成信封。
"""

import traceback

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services
from motrix_edge.server.rpent import RpentError, from_wire, to_wire


def build_router(services: Services) -> APIRouter:
    """构建 RPent 兼容的 RPC 面路由。"""
    router = APIRouter()
    rpent = services.rpent

    @router.post("/call")
    def rpent_call(payload: dict = Body(...)):
        """**RPent 兼容的 RPC 面**：``{method, args, kwargs, session_id}`` → ``{ok, result}``。

        响应**始终 HTTP 200**（失败在 body 里用 ``ok=false`` 表达），与 RPent ``HttpRpcClient``
        的契约一致；numpy 经 ``__ndarray__`` / ``__npscalar__`` tag 传输。方法集见
        ``GET /v1/rpent``；未注入 RpentService 时也回 ``ok=false`` 信封（不是 501，因为
        RPent 只读 body 的 ``error``）。免费链：见 ``RpentService.LEASE_FREE_METHODS``。
        """
        if rpent is None:
            return JSONResponse({"ok": False, "error": "rpent service not enabled", "kind": "unavailable"})
        try:
            result = rpent.call(
                payload.get("method"),
                args=tuple(from_wire(item) for item in (payload.get("args") or ())),
                kwargs={key: from_wire(item) for key, item in (payload.get("kwargs") or {}).items()},
                session_id=payload.get("session_id"),
            )
        except RpentError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "kind": exc.kind, "traceback": ""})
        except Exception as exc:  # noqa: BLE001 兜底：内部异常也转信封（不向 agent 吐 500 页面）
            return JSONResponse(
                {"ok": False, "error": str(exc), "kind": "internal", "traceback": traceback.format_exc()}
            )
        return JSONResponse({"ok": True, "result": to_wire(result)})

    @router.get("/v1/rpent")
    def rpent_info():
        """RPent 面的自省：已实现方法 + 租约解析状态（只读，免租约）。"""
        if rpent is None:
            raise ServiceError("rpent service not enabled", code=ErrorCode.NOT_IMPLEMENTED)
        return {"enabled": True, "endpoint": "/call", "methods": rpent.methods, "lease": rpent.lease_status()}

    return router
