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

"""机器人运动学（纯 numpy）：正解 / 雅可比 / 逆解。

**位姿观测与位姿控制同源**：``PiperKinematics.fk()`` 既产出 ``observations/pose``（机器人侧
位姿观测），又服务 ``solve_ik()``（位姿目标解算），因此「读到的位姿」与「下发的目标」在同一
模型、同一坐标系下闭合。SDK 的 ``get_flange_pose()`` 仅作**标定对照**（见
``robot-pipeline/scripts/verify_cartesian.py``），不参与运行时控制链路。

设计与约定见 ``wiki/design/robot_pipeline_cartesian.md``。
"""

from .ik import IkResult, solve_ik
from .piper import PIPER_DH, PIPER_JOINT_LIMITS, DHLink, PiperKinematics
from .transforms import log3, matrix_to_rpy, rpy_to_matrix, skew, vee, wrap_angles

__all__ = [
    "PIPER_DH",
    "PIPER_JOINT_LIMITS",
    "DHLink",
    "IkResult",
    "PiperKinematics",
    "log3",
    "matrix_to_rpy",
    "rpy_to_matrix",
    "skew",
    "solve_ik",
    "vee",
    "wrap_angles",
]
