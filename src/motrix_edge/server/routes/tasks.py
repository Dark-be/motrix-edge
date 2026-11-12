# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

"""Read-only task status from the currently bound adapter."""

from fastapi import APIRouter

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services


def build_router(services: Services) -> APIRouter:
    router = APIRouter()
    node = services.node

    @router.get("/v1/task/status")
    def task_status():
        if node is None:
            raise ServiceError("node not initialized", code=ErrorCode.NOT_IMPLEMENTED)
        adapter = getattr(node, "adapter", None)
        read_status = None if adapter is None else getattr(adapter, "task_status", None)
        snapshot = read_status() if callable(read_status) else None
        return snapshot or {
            "available": False,
            "task": None,
            "provider": None,
            "status": "unavailable",
            "progress": [],
            "success": False,
            "reason": "adapter not bound" if adapter is None else "bound adapter does not expose task status",
        }

    return router
