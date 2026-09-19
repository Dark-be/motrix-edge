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

"""通用观测键与图像原语（**各策略客户端共用**）。

只提供两样东西：

- edge 观测键：``observations/qpos`` / ``observations/images/<name>``（与 robot-pipeline /
  adapter 的 ``get_observation()`` 输出一致）；策略客户端直接按这些键读观测；
- 图像原语：解码 + dtype 归一（``to_rgb_uint8``）、等比补零缩放（``resize_with_pad`` = letterbox）。

**策略自己的 wire 契约与预处理参数在各策略目录内**（每个策略独立，互不牵连）：

- openpi 官方 flat 契约（``state`` / ``images`` / ``prompt`` → ``actions``）与它的 letterbox
  预处理 → ``motrix_edge.policy.openpi.contract``；
- lerobot AsyncInference gRPC 的观测 / features 组装 → ``motrix_edge.policy.lerobot_act.client``
  （wire 见 ``motrix_edge.transport.grpc`` 与 vendored ``lerobot.transport``）。
"""

import cv2
import numpy as np

KEY_OBS_QPOS = "observations/qpos"
KEY_OBS_IMAGE_PREFIX = "observations/images/"


def to_rgb_uint8(image) -> np.ndarray:
    """jpeg bytes 或 ndarray → uint8 ``[h, w, 3]`` RGB ndarray。

    浮点图按约定归一，**不直接静默截断**（``astype`` 会把 1.0 变成 1，图像几乎全黑）：
    取值落在 ``[0, 1]`` 视为归一化图像（×255），否则按 ``[0, 255]`` 四舍五入；
    最后统一裁剪到 ``[0, 255]``（越界值不环绕）。
    """
    if isinstance(image, (bytes, bytearray)):
        bgr = cv2.imdecode(np.frombuffer(bytes(image), dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("Failed to decode image as JPEG")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    arr = np.asarray(image)
    if arr.ndim == 2:  # 灰度 → 复制成 3 通道
        arr = np.stack([arr] * 3, axis=-1)
    if np.issubdtype(arr.dtype, np.floating):
        scale = 255.0 if arr.size and float(arr.max()) <= 1.0 else 1.0
        arr = np.rint(arr * scale)
    return np.clip(arr, 0, 255).astype(np.uint8)


def resize_with_pad(image: np.ndarray, height: int, width: int, method=cv2.INTER_LINEAR) -> np.ndarray:
    """等比缩放 + 居中补零到 (height, width)，复刻 openpi 的 tf.image.resize_with_pad。

    Args:
        image: [h, w, c] 或 [h, w] 的 uint8 图像（RGB）。
        height / width: 目标尺寸。
        method: cv2 插值方式（默认 cv2.INTER_LINEAR）。
    Returns:
        缩放补零后的 uint8 图像。
    """
    src_h, src_w = image.shape[:2]
    if src_h == height and src_w == width:
        return image

    ratio = max(src_w / width, src_h / height)
    resized_h = max(1, int(round(src_h / ratio)))
    resized_w = max(1, int(round(src_w / ratio)))
    resized = cv2.resize(image, (resized_w, resized_h), interpolation=method)

    top = (height - resized_h) // 2
    bottom = height - resized_h - top
    left = (width - resized_w) // 2
    right = width - resized_w - left
    return cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=0)
