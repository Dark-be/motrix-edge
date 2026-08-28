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

"""格式契约 —— 与推理节点约定的消息结构与编解码规则。

契约 = wire 上 msgpack 消息的 schema。当前契约为 openpi 兼容格式，使 openpi 服务端
无需改动即可对接；同一契约层可扩展其它策略（act / 自研），只需保证「观测进、动作出」。

消息约定（client → server，每步一次）：
  {"observations/qpos": ndarray, "observations/images/<name>": ndarray | bytes}

响应约定（server → client）：
  {"action": ndarray}            # [horizon, dim] 动作块 或 [dim] 单步
  {"error": <str>}                # 出错时以该键返回
"""

import cv2
import numpy as np

KEY_OBS_QPOS = "observations/qpos"
KEY_OBS_IMAGE_PREFIX = "observations/images/"
KEY_ACTION = "action"
KEY_ERROR = "error"

IMAGE_JPEG = "jpeg"
IMAGE_UINT8 = "uint8"


def to_rgb_uint8(image) -> np.ndarray:
    """jpeg bytes 或 ndarray → uint8 [h, w, 3] RGB ndarray。"""
    if isinstance(image, (bytes, bytearray)):
        bgr = cv2.imdecode(np.frombuffer(bytes(image), dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("Failed to decode image as JPEG")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    return arr.astype(np.uint8)


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


def encode_image(image, image_size, image_format=IMAGE_JPEG):
    """统一图像编码：解码（如需）→ 等比缩放补零到 image_size → jpeg bytes 或 uint8。

    image_format: "jpeg"（默认，省带宽）或 "uint8"。
    """
    arr = to_rgb_uint8(image)
    arr = resize_with_pad(arr, image_size[0], image_size[1])
    if image_format == IMAGE_JPEG:
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("Failed to encode image as JPEG")
        return buf.tobytes()
    elif image_format == IMAGE_UINT8:
        return arr
    raise ValueError("Invalid image format")


def build_observation(qpos, images: dict, image_size, image_format=IMAGE_JPEG) -> dict:
    """按格式契约组装观测消息。

    Args:
        qpos: 关节/状态向量（ndarray）。
        images: {name: ndarray | bytes} 图像字典，key 不带前缀。
    Returns:
        {"observations/qpos": ndarray, "observations/images/<name>": ...}
    """
    obs = {KEY_OBS_QPOS: np.asarray(qpos)}
    for name, img in images.items():
        obs[f"{KEY_OBS_IMAGE_PREFIX}{name}"] = encode_image(img, image_size, image_format)
    return obs


def extract_action(response: dict) -> np.ndarray:
    """从服务端响应中抽取动作（契约：响应含 ``"action"`` 键）。

    Raises:
        RuntimeError: 响应含 ``"error"`` 键（服务端错误）。
        KeyError: 响应缺少 ``"action"`` 键。
    """
    if KEY_ERROR in response:
        raise RuntimeError(f"Error in inference response: {response[KEY_ERROR]}")
    if KEY_ACTION not in response:
        raise KeyError(f"Response missing '{KEY_ACTION}' key, got: {list(response.keys())}")
    return np.asarray(response[KEY_ACTION])
