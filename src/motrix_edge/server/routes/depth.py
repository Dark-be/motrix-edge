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

"""routes.depth —— ``/v1/depth`` 像素深度查询的 HTTP 映射（只读，须持租约）。

与 ``/v1/preview`` 同族：独立于会话、读 ``node.frame_manager`` 的最新观测缓存、受 Edge 租约
约束；业务语义在 ``server/depth.py``（``DepthService``），本模块只做入参 / 出参映射。
未注入服务 → 501。
"""

from fastapi import APIRouter, Header

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services


def build_router(services: Services) -> APIRouter:
    """构建深度查询路由（``/v1/depth``）。"""
    router = APIRouter()
    depth = services.depth

    @router.get("/v1/depth")
    def depth_query(
        camera: str,
        u: float = 0.5,
        v: float = 0.5,
        x_lease_id: str | None = Header(default=None),
    ):
        """查某个像素的深度（米）：``u`` / ``v`` 是**归一化**坐标（相对源分辨率，缺省 0.5 = 中心）。

        深度图取最新观测帧（与 ``/v1/preview`` / WebRTC 同源，且**已对齐到彩色图**，故与
        彩色图同一像素网格）；``depth_raw == 0`` = 该像素无有效深度（``valid: false``）。
        回执带 ``intrinsics``（彩色内参）与 ``depth_scale``——「像素 → 机器人坐标」的反投影输入。
        须持租约（``X-Lease-Id``）；相机无深度 / 未知 → 404。
        """
        if depth is None:
            raise ServiceError("depth not enabled", code=ErrorCode.NOT_IMPLEMENTED)
        return depth.depth(camera=camera, u=u, v=v, lease_id=x_lease_id)

    return router


__all__ = ["build_router"]
