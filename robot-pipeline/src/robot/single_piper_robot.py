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

"""左 Piper 主臂控制右从臂，复用双臂行为与机器档案的两路相机。"""

from robot.dual_piper_robot import DualPiperRobot


class SinglePiperRobot(DualPiperRobot):
    NAME = "single_piper"
    ADAPTER_TYPE = "single_piper"
    ROBOT_MODEL_ID = "single-piper"
    SHM_NAME = "single_piper_obs"

    ARM_NAMES = ("right",)
    PROFILE_ARM_NAMES = DualPiperRobot.ARM_NAMES
    ARM_CONTROLLERS = {"right": "right_arm"}
    MASTER_CONTROLLERS = {"right": "left_master"}
    PORT_ROLES = ("left_master", "right")
    PROFILE_PORT_ROLES = DualPiperRobot.PORT_ROLES
    PORT_KINDS = {role: "can" for role in PORT_ROLES}

    IMAGE_NAMES = ["cam_head", "cam_right_wrist"]
    IMAGES = {name: DualPiperRobot.IMAGES[name] for name in IMAGE_NAMES}
    CAMERA_KINDS = {name: "realsense" for name in IMAGE_NAMES}
    PROFILE_CAMERA_NAMES = tuple(DualPiperRobot.IMAGE_NAMES)
    DEPTH_CAMERAS = tuple(IMAGE_NAMES)
    PROFILE_DEPTH_CAMERAS = DualPiperRobot.DEPTH_CAMERAS
    WRIST_CAMERAS = {"cam_right_wrist": "right"}

    DEFAULT_INIT_JOINT = [0.0, 0.1, 0.0, 0.0, 0.0, 0.0]
    DEFAULT_INIT_GRIPPER = [1.0]

    def __init__(self, robot_config: dict | None = None):
        config = dict(robot_config or {})
        config.setdefault("init_joint", self.DEFAULT_INIT_JOINT)
        config.setdefault("init_gripper", self.DEFAULT_INIT_GRIPPER)
        super().__init__(config)
