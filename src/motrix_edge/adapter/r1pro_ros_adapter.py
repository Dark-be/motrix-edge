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

"""Galaxea R1 Pro ROS 2 adapter declarations.

ROS, DDS, interpolation and recording live in the external R1 Pro Robot Server.  Edge remains a
thin HTTP/SHM client: policy-rate requests update robot targets, while the Robot Server owns its
control-rate loop and records source ROS data locally.
"""

from motrix_edge.adapter.base import ActionSpace, AdapterCapability
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter


class R1ProRosAdapter(HttpShmAdapter):
    """R1 Pro Robot Server client with two 7-DoF arms and four RGB cameras."""

    NAME = "r1pro_ros"
    ADAPTER_TYPE = "r1pro_ros"

    ROBOT_MODEL_ID = "galaxea-r1-pro"
    ROBOT_MODEL_VERSION = "0.0.0"

    ARM_NAMES = ("left", "right")
    DEFAULT_ENABLED_ARMS = ARM_NAMES
    ACTION_DIM_PER_ARM = {
        ActionSpace.JOINT.value: 7,
        ActionSpace.JOINT_DELTA.value: 7,
        ActionSpace.GRIPPER.value: 1,
        ActionSpace.GRIPPER_DELTA.value: 1,
    }
    ACTION_SPACES = (
        ActionSpace.JOINT,
        ActionSpace.JOINT_DELTA,
        ActionSpace.GRIPPER,
        ActionSpace.GRIPPER_DELTA,
    )
    # R1 Pro home values are deployment/calibration facts. Do not invent them in Edge. Callers that
    # target a subset of arms must pass ``arms`` so the Robot Server can hold all unselected groups.
    HOME: dict[str, list[float]] = {}

    # ObsShmWriter v9 uses one image geometry for every RGB slot. The ROS Robot Server therefore
    # decodes and resizes all source camera streams before writing this ordered 640x480 layout.
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head_left": (640, 480),
        "cam_head_right": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }
    DEPTH_CAMERAS: tuple[str, ...] = ()

    CAPABILITIES: dict[AdapterCapability, bool] = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }

    SDK_URL = "http://127.0.0.1:8090"
    SHM_NAME = "r1pro_ros_obs"
    HTTP_TIMEOUT = 3.0
