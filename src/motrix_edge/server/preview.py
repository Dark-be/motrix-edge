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

"""server/preview —— 观测预览服务（独立于 capture / infer 会话）。

预览是**只读观测展示**：从 ``node.frame_manager``（observe 循环每帧写入的缓存）
读取最新观测（qpos / action + 摄像头名列表），**不依赖任何会话**——无需进入采集 /
推理会话，预览随时可开。受控操作：须持有有效租约（``X-Lease-Id``）。

图像不内联：HTTP JSON 不承载二进制，图像（jpeg / raw）由 WebRTC（/v1/webrtc/offer）
推流到前端，这里只返回摄像头名列表。
"""

import numpy as np

from motrix_edge.adapter.base import KEY_ACTION, KEY_QPOS, image_names_of
from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.server.state import adapter_ref
from motrix_edge.session.base import SessionState


class PreviewError(Exception):
    """预览被拒绝（租约缺失 / 不匹配 / 过期 / FrameManager 未就绪）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class PreviewService:
    """观测预览服务：绑定 FrameManager（经 node）+ Edge 级租约校验。

    独立于采集 / 推理会话：直接读 ``node.frame_manager`` 观测缓存，不经过
    CaptureService / InferService。受控操作：须持有有效租约。
    """

    def __init__(self, node, leases: LeaseManager):
        self._node = node  # 正在运行的 EdgeNode（frame_manager 取自 node）
        # Edge 级租约（受控操作校验）：**必须注入共享实例**（__main__ / 测试传同一个
        # manager）——这里不自建，自建实例永远无租约，预览会被静默拒成 409 / 403。
        self._leases = leases

    def preview(self, lease_id: str | None = None) -> dict:
        """最新观测预览：adapter 身份 / observation（qpos / action + 摄像头名列表）。

        不要求会话 —— 无会话时返回当前观测缓存（可能为空），预览随时可开。
        """
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise PreviewError(str(exc), exc.status_code) from exc
        node = self._node
        frame_manager = getattr(node, "frame_manager", None)
        if frame_manager is None:
            raise PreviewError("frame manager not available", status_code=501)
        latest = frame_manager.latest() or {}
        session = getattr(node, "session", None)
        state = getattr(session, "state", SessionState.INIT)
        return {
            "state": state,
            "adapter": adapter_ref(node),  # 与 /v1/captures · /v1/infers 的 adapter 段同源
            "observation": {
                "qpos": self._to_float_list(latest.get(KEY_QPOS)),
                "action": self._to_float_list(latest.get(KEY_ACTION)),
                "images": image_names_of(latest),
            },
        }

    @staticmethod
    def _to_float_list(value) -> list | None:
        """ndarray / list → float list（预览用）。"""
        if value is None:
            return None
        return [float(v) for v in np.asarray(value).reshape(-1)]


__all__ = ["PreviewError", "PreviewService"]
