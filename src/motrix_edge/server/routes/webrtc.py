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

"""routes.webrtc —— ``POST /v1/webrtc/offer`` 的 HTTP 映射（WebRTC 推流信令）。

视频轨道从 FrameManager 取帧、aiortc 编码推流；HTTP 只承载 SDP 协商（图像不内联在 JSON
里）。受控操作：须持租约；未注入 WebRTCService → 501。
"""

from fastapi import APIRouter, Header

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.server.deps import Services
from motrix_edge.server.schemas import WebRTCOfferRequest


def build_router(services: Services) -> APIRouter:
    """构建 WebRTC 信令路由。"""
    router = APIRouter()
    webrtc = services.webrtc

    @router.post("/v1/webrtc/offer")
    def webrtc_offer(req: WebRTCOfferRequest, x_lease_id: str | None = Header(default=None)):
        """WebRTC 推流：接收网页 SDP offer，返回 Edge answer。受控操作：须持有有效租约。"""
        if webrtc is None:
            raise ServiceError("webrtc not enabled", code=ErrorCode.NOT_IMPLEMENTED)
        return webrtc.offer(lease_id=x_lease_id, sdp=req.sdp, sdp_type=req.type)

    return router
