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

"""calibration/pipeline —— 样本 → 外参产物（解算主体，**纯离线**：不碰硬件，只要 OpenCV）。

步骤（与设计文档「标定流程 · Step 2」一一对应）：

1. **相机 ↔ 板**：每帧 ChArUco 角点 + ``solvePnP`` → ``T_cam_board``。

   ⚠️ **板必须全程不动**（含探针触碰阶段）：探针触碰得到的是「这块板」的位姿，板一挪就全错。
   固定相机多帧只为了**平均降噪**（帧间离散 = 采样质量指标，偏大就会提醒）；腕相机靠多姿态差异
   定解（固定相机做不到，也不需要——PnP 单帧就是 6 自由度）；
2. **腕相机 ↔ 法兰**：多姿态同一块板 → $AX = XB$ → ``T_flange_cam``；
3. **臂 ↔ 板**：探针触碰 → ``T_base_board`` + 探针尖 ``t_probe``；
4. **换算到 ``world``**（= ``world_arm`` 的基座，缺省 ``left``）：
   ``T_world_board = T_base_world_arm_board``、``T_world_base_right = T_world_board · T_base_right_board⁻¹``、
   ``T_world_cam = T_world_board · T_cam_board``。

放在库里（而不是脚本里）是为了**能离线单测**：合成一份样本 → 解算 → 复现真值（见
``tests/test_robot_calibration.py``）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from motrix_edge.geometry import (
    MOUNT_FIXED,
    MOUNT_WRIST,
    WORLD_ALIAS,
    CameraExtrinsics,
    FrameSet,
    invert_transform,
    pose_to_transform,
)
from robot.calibration.board import pose_from_corners
from robot.calibration.samples import Samples
from robot.calibration.solver import average_transforms, solve_hand_eye, solve_probe

#: 每台相机建议的最少帧数（少于它解算仍会跑，但会提醒补采）。
MIN_VIEWS = 6
#: 每个臂建议的最少触碰点数（硬要求 ≥ 3）。
MIN_TOUCHES = 3
#: 手眼标定可靠所需的最小姿态铺开度（度）——不够会提醒重采。
MIN_ROTATION_SPREAD_DEG = 20.0
#: 触碰点共线度下限（低于它旋转解不可靠）。
MIN_POINTS_CONDITION = 0.05
#: 固定相机的帧间离散上限（米）：超了说明「板 / 相机中途动过」（板必须全程不动）。
MAX_FIXED_SPREAD_M = 0.005


class CameraViews:
    """一台相机的「看板」数据：帧均值变换 + 每帧的 ``(位姿, T_cam_board)`` + 重投影残差。"""

    def __init__(
        self,
        average: np.ndarray,
        spread_m: float,
        per_view: list[tuple[list[float] | None, np.ndarray]],
        rms_px: float,
    ):
        self.average = average
        self.spread_m = spread_m
        self.per_view = per_view
        self.rms_px = float(rms_px)

    @property
    def count(self) -> int:
        return len(self.per_view)


def camera_views(samples: Samples) -> dict[str, CameraViews]:
    """每帧 PnP → ``T_cam_board``（**只算一次**，固定相机与手眼共用）。"""
    result: dict[str, CameraViews] = {}
    for camera in samples.cameras:
        per_view: list[tuple[list[float] | None, np.ndarray]] = []
        rms_values: list[float] = []
        for view in samples.views_of(camera):
            transform, rms = pose_from_corners(view.corners, view.ids, samples.board, samples.intrinsics[camera])
            per_view.append((view.pose, transform))
            rms_values.append(rms)
        average, _spread_deg, spread_m = average_transforms([item for _, item in per_view])
        result[camera] = CameraViews(
            average=average,
            spread_m=spread_m,
            per_view=per_view,
            rms_px=float(np.mean(rms_values)),
        )
    return result


def solve_frames(
    samples: Samples, *, log: Callable[[str], None] | None = None, world_arm: str | None = None
) -> FrameSet:
    """样本 → :class:`FrameSet`（解算主体；判读出问题时**抛 ``ValueError``**，不产出半份产物）。"""
    say = log or (lambda _message: None)
    views = camera_views(samples)
    say("=== 相机 ↔ 板（PnP）===")
    for camera, item in views.items():
        say(
            f"  {camera}: {item.count} 帧 · 重投影 RMS 均值 {item.rms_px:.3f} px"
            f" · 帧间离散 {item.spread_m * 1000:.2f} mm"
        )
        if item.count < MIN_VIEWS:
            say(f"    ⚠️ 仅 {item.count} 帧（建议 ≥ {MIN_VIEWS}）：会跑，但精度无保证")
        if camera not in samples.wrist_cameras and item.spread_m > MAX_FIXED_SPREAD_M:
            say(
                f"    ⚠️ 固定相机的帧间离散偏大（{item.spread_m * 1000:.1f} mm）："
                "板 / 相机中途动过？板必须**全程不动**——挪板后探针那一步记的板位就对不上了"
            )

    world_arm = world_arm or samples.world_arm
    if world_arm not in samples.arms:
        raise ValueError(f"world_arm {world_arm!r} not in {samples.arms}")

    say("=== 臂 ↔ 板（探针）===")
    base_from_board: dict[str, np.ndarray] = {}
    probe_tips: dict[str, np.ndarray] = {}
    for arm in samples.arms:
        touches = samples.touches_of(arm)
        if len(touches) < MIN_TOUCHES:
            raise ValueError(f"{arm}: 探针触碰不足（{len(touches)} < {MIN_TOUCHES}）——无法定解，请补采")
        # 样本里存的是位姿 6 维（``observations/pose`` 的形状）；求解器吃 4×4（与手眼同一约定）
        result = solve_probe([pose_to_transform(touch.pose) for touch in touches], [touch.point for touch in touches])
        base_from_board[arm] = result.transform
        probe_tips[arm] = result.probe_tip
        say(
            f"  {arm}: {len(touches)} 点 · 残差 RMS {result.rms_m * 1000:.2f} mm / 峰值 {result.max_m * 1000:.2f} mm"
            f" · 共线度 {result.points_condition:.3f} · 迭代 {result.iterations}"
        )
        if result.points_condition < MIN_POINTS_CONDITION:
            say("    ⚠️ 触碰点近似共线（摆得太少）：旋转解不可靠，请在不同区域 / 高度重采")

    world_from_board = base_from_board[world_arm]
    # 统一一套公式：``T_world_base = T_world_board · T_base_arm_board⁻¹``——world 臂代进去自然得到
    # 恒等（它**就是** world，不该把板位姿当成它的底座外参）。
    arms = {arm: world_from_board @ invert_transform(base_from_board[arm]) for arm in base_from_board}

    say("=== 相机外参 ===")
    cameras: dict[str, CameraExtrinsics] = {}
    for camera, item in views.items():
        arm = samples.wrist_cameras.get(camera)
        if arm is None:
            cameras[camera] = CameraExtrinsics(
                name=camera,
                mount=MOUNT_FIXED,
                arm=None,
                # 链式方向：``T_world_cam = T_world_board · T_board_cam``（PnP 给的是 board←cam 的反向）
                transform=world_from_board @ invert_transform(item.average),
                rms_m=item.spread_m,  # 米制残差 = 帧间离散（采样质量）
            )
            say(f"  {camera}: fixed · T_world_cam = T_world_board · T_cam_board")
            continue
        if item.count < 2:
            raise ValueError(f"{camera}: 腕相机至少需要 2 个位姿（当前 {item.count}）——无法解手眼")
        hand_eye = solve_hand_eye(
            [pose_to_transform(pose) for pose, _ in item.per_view],
            [transform for _, transform in item.per_view],
        )
        cameras[camera] = CameraExtrinsics(
            name=camera, mount=MOUNT_WRIST, arm=arm, transform=hand_eye.transform, rms_m=hand_eye.translation_rms_m
        )
        say(
            f"  {camera}: wrist({arm}) · {item.count} 位姿 · 姿态铺开 {hand_eye.rotation_spread_deg:.1f}°"
            f" · 手眼残差 {hand_eye.rotation_rms_deg:.3f}° / {hand_eye.translation_rms_m * 1000:.2f} mm"
        )
        if hand_eye.rotation_spread_deg < MIN_ROTATION_SPREAD_DEG:
            say(f"    ⚠️ 姿态铺开不足（< {MIN_ROTATION_SPREAD_DEG:.0f}°）：手眼标定退化，请重采（朝向要分散）")

    return FrameSet(
        world=WORLD_ALIAS,
        arms=arms,
        cameras=cameras,
        probe_tips=probe_tips,
        calibrated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        tool="probe",
    )


__all__ = [
    "MAX_FIXED_SPREAD_M",
    "MIN_POINTS_CONDITION",
    "MIN_ROTATION_SPREAD_DEG",
    "MIN_TOUCHES",
    "MIN_VIEWS",
    "CameraViews",
    "camera_views",
    "solve_frames",
]
