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

"""机器人运动学：MotrixSim MJCF 正解 / 雅可比 / 逆解。

**位姿观测与位姿控制同源**：``PiperKinematics.pose()`` 提供 MotrixSim 原生 Pose7，
``solve_ik()`` 使用同一模型解算位姿目标，因此后续 robot 栈接入 ``observations/pose`` 时，
「读到的位姿」与「下发的目标」在同一模型、同一坐标系下闭合。SDK 的
``get_flange_pose()`` 仅作兼容读取，不参与运行时控制链路。

设计与约定见 ``wiki/design/robot_pipeline_cartesian.md``。
"""

from .ik import IkResult, normalize_ik_config, normalize_ik_seeds, solve_ik
from .piper import JOINT_LIMIT_MARGIN, PIPER_JOINT_LIMITS, PIPER_MODEL_PATH, PIPER_SDK_LIMITS, PiperKinematics
from .transforms import pose6_to_pose7, pose7_to_pose6, quat_xyzw_to_rpy, rpy_to_quat_xyzw, validate_pose7

__all__ = [
    "JOINT_LIMIT_MARGIN",
    "PIPER_JOINT_LIMITS",
    "PIPER_MODEL_PATH",
    "PIPER_SDK_LIMITS",
    "IkResult",
    "PiperKinematics",
    "normalize_ik_config",
    "normalize_ik_seeds",
    "pose6_to_pose7",
    "pose7_to_pose6",
    "quat_xyzw_to_rpy",
    "rpy_to_quat_xyzw",
    "solve_ik",
    "validate_pose7",
]
