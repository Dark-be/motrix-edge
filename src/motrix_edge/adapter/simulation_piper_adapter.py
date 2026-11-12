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

"""Single-arm simulation capabilities over the existing HTTP/SHM transport."""

import httpx2

from motrix_edge.adapter.base import ActionSpace, AdapterCapability, CaptureStatus
from motrix_edge.adapter.http_contract import PATH_TASK_STATUS
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter


class SimulationPiperAdapter(HttpShmAdapter):
    NAME = "simulation_piper"
    ADAPTER_TYPE = "simulation_piper"
    ROBOT_MODEL_ID = "simulated-piper"
    ROBOT_MODEL_VERSION = "0.1.0"
    ARM_NAMES = ("right",)
    DEFAULT_ENABLED_ARMS = ARM_NAMES
    ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.POSE, ActionSpace.GRIPPER)
    ACTION_DIM_PER_ARM = {"joint": 6, "pose": 6, "gripper": 1}
    POSE_DIM_PER_ARM = 6
    HOME = {"joint": [0.0, 0.1, 0.0, 0.0, 0.0, 0.0], "gripper": [1.0]}
    IMAGES = {"cam_head": (640, 480), "cam_right_wrist": (640, 480)}
    DEPTH_CAMERAS = ()
    CAPABILITIES = {
        AdapterCapability.CAPTURE: False,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }
    SDK_URL = "http://127.0.0.1:8090"
    HTTP_TIMEOUT = 3.0
    SHM_NAME = "simulation_piper_obs"

    def capture_status(self) -> CaptureStatus:
        return CaptureStatus(running=False)

    def task_status(self) -> dict | None:
        try:
            response = self._client().get(PATH_TASK_STATUS)
        except httpx2.TransportError:
            # Task status is an optional diagnostic surface.  The simulation process may exit
            # before Edge or Console stops polling, which must degrade to "unavailable" rather
            # than turn every poll into an application traceback.
            return None
        if response.status_code in (404, 503):
            return None
        response.raise_for_status()
        return response.json()
