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

"""右从臂 Piper：单臂动作布局与头部、右腕相机。"""

from motrix_edge.adapter.dual_piper_adapter import DualPiperAdapter


class SinglePiperAdapter(DualPiperAdapter):
    NAME = "single_piper"
    ADAPTER_TYPE = "single_piper"
    ROBOT_MODEL_ID = "single-piper"
    SHM_NAME = "single_piper_obs"
    ARM_NAMES = ("right",)
    DEFAULT_ENABLED_ARMS = ARM_NAMES
    HOME = {"joint": [0.0, 0.1, 0.0, 0.0, 0.0, 0.0], "gripper": [1.0]}
    IMAGES = {name: DualPiperAdapter.IMAGES[name] for name in ("cam_head", "cam_right_wrist")}
    DEPTH_CAMERAS = ("cam_head", "cam_right_wrist")
