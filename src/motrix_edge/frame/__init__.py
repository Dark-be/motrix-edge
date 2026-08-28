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

"""frame 子包 —— Edge 侧观测帧缓存管理（FrameManager）。

单点管理「最新观测帧」（观测图像 jpeg + qpos，线程安全）：session 每帧写入，preview /
WebRTC 推流读取。观测图像在 Edge 侧降采样为 ``DEFAULT_IMAGE_SIZE`` JPEG，方便内网传输。
"""

import threading

import numpy as np

from motrix_edge.adapter.base import CAMERA_PREFIX

# Edge 侧观测缓存图像尺寸 (width, height)：observe 之后降采样保存，方便内网传输 / WebRTC 推流
DEFAULT_IMAGE_SIZE = (320, 240)

# 预览 / 推流缓存 JPEG 质量：预览分辨率低，80 足够且编码显著更快（提升观测源频率）
_PREVIEW_JPEG_QUALITY = 80


def cache_observation(obs: dict, image_size: tuple[int, int] = DEFAULT_IMAGE_SIZE) -> dict:
    """整理 adapter 观测为 Edge 侧缓存帧：摄像头帧降采样为 ``image_size`` JPEG。

    obs 由 ``adapter.observe()`` 返回（图像为 JPEG，如 640x480）；qpos / action 原样透传。
    """
    cached: dict = {}
    for key, value in obs.items():
        if key.startswith(CAMERA_PREFIX) and isinstance(value, (bytes, bytearray)):
            cached[key] = _resize_image_jpeg(bytes(value), image_size)
        else:
            cached[key] = value
    return cached


def _resize_image_jpeg(jpeg_bytes: bytes, size: tuple[int, int], quality: int = _PREVIEW_JPEG_QUALITY) -> bytes:
    """JPEG bytes 缩放到 size 后重新编码为 JPEG（Edge 侧缓存降采样）。

    ``quality`` 控制 imencode 质量（默认 80）：预览 / 推流用，质量低 → 编码更快、
    体积更小（观测源 ``FrameManager.update`` 热路径，提速显著）。
    """
    import cv2

    arr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return jpeg_bytes
    resized = cv2.resize(arr, (size[0], size[1]), interpolation=cv2.INTER_LINEAR)  # (width, height)
    ok, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else jpeg_bytes


class FrameManager:
    """Edge 侧观测帧缓存管理器（线程安全，单活跃最新帧）。

    - ``update(obs)``：写入最新观测（``adapter.observe()`` 返回；图像降采样为 jpeg）。
    - ``latest()``：读取最新观测帧（供 preview / WebRTC 推流）；无帧返回 None。
    - ``clear()``：清空缓存（会话结束 / 退出时）。
    """

    def __init__(self, image_size: tuple[int, int] = DEFAULT_IMAGE_SIZE):
        self._image_size = image_size
        self._latest: dict | None = None
        self._lock = threading.Lock()

    @property
    def image_size(self) -> tuple[int, int]:
        """Edge 侧缓存图像尺寸（预览 / WebRTC 推流分辨率）。"""
        return self._image_size

    def update(self, obs: dict) -> None:
        """写入最新观测帧（图像降采样为 Edge 侧尺寸 jpeg）。"""
        with self._lock:
            self._latest = cache_observation(obs, self._image_size)

    def latest(self) -> dict | None:
        """读取最新观测帧（线程安全；无帧返回 None）。"""
        with self._lock:
            return self._latest

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._latest = None


__all__ = ["DEFAULT_IMAGE_SIZE", "FrameManager", "cache_observation"]
