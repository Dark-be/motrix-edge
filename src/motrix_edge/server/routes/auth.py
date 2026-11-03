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

"""飞书登录 HTTP 映射。"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services


def build_router(services: Services) -> APIRouter:
    router = APIRouter()
    auth = services.auth

    def _auth():
        if auth is None:
            raise ServiceError("Feishu authentication is not enabled", code=ErrorCode.NOT_IMPLEMENTED)
        return auth

    @router.get("/v1/auth")
    def auth_status():
        return {"enabled": auth is not None, "provider": "feishu" if auth is not None else None}

    @router.get("/v1/auth/feishu/login")
    def login(return_to: str | None = None):
        return RedirectResponse(_auth().begin_login(return_to), status_code=307)

    @router.get("/v1/auth/feishu/callback")
    def callback(code: str | None = None, state: str | None = None):
        service = _auth()
        session_id, target = service.complete_login(code, state)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            service.config.cookie_name,
            session_id,
            max_age=int(service.config.session_ttl),
            httponly=True,
            secure=service.config.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/v1/auth/me")
    def me(request: Request):
        service = _auth()
        return service.me(request.cookies.get(service.config.cookie_name))

    @router.post("/v1/auth/logout")
    def logout(request: Request):
        service = _auth()
        service.logout(request.cookies.get(service.config.cookie_name))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(service.config.cookie_name, path="/")
        return response

    return router


__all__ = ["build_router"]
