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

"""robot.calibration —— 手眼标定与外参（**纯 numpy + 仅标定板检测用 OpenCV**）。

分工（见 ``wiki/design/robot_pipeline_frames.md``「分层与归属」）：

-   :mod:`~robot.calibration.solver`：点集配准 / ``AX = XB`` / 探针交替最小二乘（纯 numpy，可离线自测）；
-   :mod:`~robot.calibration.board`：ChArUco 板构造 / 检测 / ``solvePnP``（**唯一的 OpenCV 依赖点**；
    模块级只依赖 numpy，``cv2`` 在函数内惰性导入 → 没装 OpenCV 的机器仍能用求解器与产物读写）；
-   :mod:`~robot.calibration.samples`：采集样本 JSON 的结构与读写（采集与解算之间的唯一接口）；
-   :mod:`~robot.calibration.store`：产物 ``<根>/config/calibration/frames.json`` 读写（机器人进程与
      标定脚本共用）。

帧模型（``FrameSet`` / 变换数学）在 ``motrix_edge.geometry``——Edge 运行期要用同一份，故不在这里另写。
"""

from __future__ import annotations

from robot.calibration.board import BoardSpec, detect, object_points, pose_from_corners, render
from robot.calibration.external import (
    KIND_FIXED_BOARD_BRIDGE,
    KIND_HAND_EYE,
    TOOL,
    ExternalDataError,
    ExternalSession,
    board_spec_from_manifest,
    import_frames,
    inherit_wrist_camera,
    load_intrinsics,
    symmetric_anchor,
)
from robot.calibration.pipeline import (
    MIN_POINTS_CONDITION,
    MIN_ROTATION_SPREAD_DEG,
    MIN_TOUCHES,
    MIN_VIEWS,
    solve_frames,
)
from robot.calibration.samples import CameraView, ProbeTouch, Samples, as_pose_list
from robot.calibration.solver import (
    HandEyeResult,
    ProbeResult,
    average_transforms,
    fit_transform,
    self_test,
    solve_hand_eye,
    solve_probe,
)
from robot.calibration.store import (
    FRAMES_RELATIVE,
    camera_extrinsics,
    frames_path,
    load_frames,
    reset_cache,
    save_frames,
)

__all__ = [
    "FRAMES_RELATIVE",
    "KIND_FIXED_BOARD_BRIDGE",
    "KIND_HAND_EYE",
    "MIN_POINTS_CONDITION",
    "MIN_ROTATION_SPREAD_DEG",
    "MIN_TOUCHES",
    "MIN_VIEWS",
    "TOOL",
    "BoardSpec",
    "CameraView",
    "ExternalDataError",
    "ExternalSession",
    "HandEyeResult",
    "ProbeResult",
    "ProbeTouch",
    "Samples",
    "as_pose_list",
    "average_transforms",
    "board_spec_from_manifest",
    "camera_extrinsics",
    "detect",
    "fit_transform",
    "frames_path",
    "import_frames",
    "inherit_wrist_camera",
    "load_frames",
    "load_intrinsics",
    "object_points",
    "pose_from_corners",
    "render",
    "reset_cache",
    "save_frames",
    "self_test",
    "solve_frames",
    "solve_hand_eye",
    "solve_probe",
    "symmetric_anchor",
]
