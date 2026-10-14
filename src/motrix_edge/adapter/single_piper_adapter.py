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

"""单臂 Piper（普通 / 双向力反馈）共享的 HTTP + SHM adapter 声明。"""

from motrix_edge.adapter.base import ActionSpace, AdapterCapability
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter


class SinglePiperAdapter(HttpShmAdapter):
    NAME = "single_piper"
    ADAPTER_TYPE = "single_piper"
    ROBOT_MODEL_ID = "single-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    ACTION_DIM_PER_ARM = {ActionSpace.JOINT.value: 6, ActionSpace.GRIPPER.value: 1}
    ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.GRIPPER)
    ARM_NAMES = ("arm_1",)
    HOME = {ActionSpace.JOINT.value: [0.0] * 6, ActionSpace.GRIPPER.value: [1.0]}
    DEFAULT_ENABLED_ARMS = ARM_NAMES
    IMAGES: dict[str, tuple[int, int]] = {}
    DEPTH_CAMERAS: tuple[str, ...] = ()
    CAPABILITIES = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: False,
    }
    SDK_URL = "http://127.0.0.1:8090"
    SHM_NAME = "single_piper_obs"
    HTTP_TIMEOUT = 3.0


class ForceSinglePiperAdapter(SinglePiperAdapter):
    NAME = "force_single_piper"
    ADAPTER_TYPE = "force_single_piper"
    ROBOT_MODEL_ID = "force-single-piper"
    SHM_NAME = "force_single_piper_obs"
