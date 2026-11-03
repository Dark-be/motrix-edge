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

"""geometry —— 坐标系表达的单点（齐次变换 / 统一帧模型 / 像素反投影）。

纯 numpy、无硬件、无 OpenCV：Edge 运行期（``GET /v1/depth`` 的坐标换算）与 robot-pipeline 的标定工具
（求解器 / 脚本）**共用同一份帧模型**，避免两端各写一套约定。

-   :mod:`~motrix_edge.geometry.transforms`：``rpy`` ↔ 矩阵、组合 / 求逆 / 点变换、16 数平铺解析；
-   :mod:`~motrix_edge.geometry.extrinsics`：``FrameSet``（``world`` = 左臂基座）+ 产物读写与校验；
-   :mod:`~motrix_edge.geometry.deproject`：像素 + 深度 → 相机系坐标。

设计与约定见 ``wiki/design/robot_pipeline_frames.md``。
"""

from __future__ import annotations

from motrix_edge.geometry.deproject import pixel_to_camera
from motrix_edge.geometry.extrinsics import (
    MOUNT_FIXED,
    MOUNT_WRIST,
    MOUNTS,
    RPY_ORDER,
    WORLD_ALIAS,
    WORLD_ARM,
    CameraExtrinsics,
    FrameError,
    FrameSet,
)
from motrix_edge.geometry.pointing import (
    AXIS_SPECS,
    ROLL_FREE,
    EgoAxes,
    axis_vector,
    base_delta_from_ego,
    base_rotation_delta_from_ego,
    chart_increment,
    parse_axis,
    pointing_rpy,
    rotate_vector,
    turned_deg,
)
from motrix_edge.geometry.transforms import (
    IDENTITY,
    as_transform,
    compose,
    flatten_transform,
    invert_transform,
    is_rotation,
    make_transform,
    matrix_to_rpy,
    pose_to_transform,
    rpy_to_matrix,
    transform_points,
    transform_to_pose,
)

__all__ = [
    "AXIS_SPECS",
    "IDENTITY",
    "MOUNTS",
    "MOUNT_FIXED",
    "MOUNT_WRIST",
    "ROLL_FREE",
    "RPY_ORDER",
    "WORLD_ALIAS",
    "WORLD_ARM",
    "CameraExtrinsics",
    "EgoAxes",
    "FrameError",
    "FrameSet",
    "as_transform",
    "axis_vector",
    "base_delta_from_ego",
    "base_rotation_delta_from_ego",
    "chart_increment",
    "compose",
    "flatten_transform",
    "invert_transform",
    "is_rotation",
    "make_transform",
    "matrix_to_rpy",
    "parse_axis",
    "pixel_to_camera",
    "pointing_rpy",
    "pose_to_transform",
    "rotate_vector",
    "rpy_to_matrix",
    "transform_points",
    "transform_to_pose",
    "turned_deg",
]
