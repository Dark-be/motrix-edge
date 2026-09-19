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

"""openpi 官方 wire 契约（**openpi 策略客户端专用**）。

讲这套 wire 的服务端：openpi 官方 ``WebsocketPolicyServer``（``scripts/serve_policy.py``），
以及本仓对接的 openpi piper 分支（后者在 ``policy_metadata`` 里额外声明 action_horizon /
cameras 等；见 ``motrix_edge.policy.openpi.client``）。

消息（client → server，每步一次）：``{"state": ndarray, "images": {<相机名>: uint8 RGB [h,w,c]},
"prompt": str?}``；响应：``{"actions": [horizon, dim]}``，并可能带 ``state`` / ``policy_timing`` /
``server_timing`` 等附加键（忽略）；出错时 ``{"error": <str>}``。

通用观测键与图像原语（解码 / 归一 / letterbox）在 ``motrix_edge.policy.contract``。
"""

import numpy as np

from motrix_edge.policy.contract import resize_with_pad, to_rgb_uint8

OPENPI_KEY_STATE = "state"
OPENPI_KEY_IMAGES = "images"
OPENPI_KEY_PROMPT = "prompt"
OPENPI_KEY_ACTIONS = "actions"  # 官方响应键：[horizon, dim] 动作块
OPENPI_KEY_ERROR = "error"  # 服务端错误：{"error": <str>}


def prepare_openpi_image(image, image_size) -> np.ndarray:
    """把 edge 观测图像（JPEG bytes 或 ndarray）转为 openpi 服务端需要的 uint8 RGB [h, w, c]。

    解码（如需）→ 等比缩放补零（letterbox）到 ``image_size``——与官方服务端 / 训练管线的
    ``image_tools.resize_with_pad`` 同语义，服务端再缩时为 no-op（省带宽且不变形）→ 返回
    **uint8 数组**（官方服务端只收数组、不收 JPEG bytes）。官方对 CHW / HWC 均接受，这里统一
    HWC。
    """
    arr = to_rgb_uint8(image)
    return resize_with_pad(arr, image_size[0], image_size[1])


def build_openpi_observation(state, images: dict, prompt=None) -> dict:
    """按 openpi 官方 flat 契约组装观测消息。

    Args:
        state: 低维状态（机器人 qpos；客户端传原始值，服务端按 norm_stats 归一化）。
        images: {相机名: uint8 RGB [h, w, c]}（已用 ``prepare_openpi_image`` 预处理）。
        prompt: 可选文本指令；每次请求可换。None = 不发（服务端用 default_prompt 兜底）。
    Returns:
        {"state": ndarray, "images": {...}, "prompt": <str>?}
    """
    obs = {OPENPI_KEY_STATE: np.asarray(state), OPENPI_KEY_IMAGES: dict(images)}
    if prompt is not None:
        obs[OPENPI_KEY_PROMPT] = prompt
    return obs


def extract_action_response(response: dict) -> np.ndarray:
    """从响应中抽取动作块（官方 ``"actions"``，``[horizon, dim]``）。

    Raises:
        RuntimeError: 响应含 ``"error"`` 键（服务端错误）。
        KeyError: 响应缺 ``"actions"`` 键。
    """
    if OPENPI_KEY_ERROR in response:
        raise RuntimeError(f"Error in inference response: {response[OPENPI_KEY_ERROR]}")
    if OPENPI_KEY_ACTIONS not in response:
        raise KeyError(f"Response missing '{OPENPI_KEY_ACTIONS}' key, got: {list(response.keys())}")
    return np.asarray(response[OPENPI_KEY_ACTIONS])
