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

"""server/depth —— 像素深度查询（单点，独立于会话，与预览同源）。

从 ``node.frame_manager`` 的最新观测缓存里取**已对齐到彩色图**的深度图
（``observations/depth/<cam>``，只有接入深度相机的机器人有），按**归一化坐标**查出某个像素的
深度并换算成米；回执同时给出该相机的**彩色内参**与**深度比例**，并在**标定产物可用**时给出
该像素在**相机系**与**统一 ``world`` 帧**（= 左臂基座，见
``wiki/design/robot_pipeline_frames.md``）下的坐标（米）。

坐标为什么用归一化：``/v1/preview`` 与 WebRTC 推的是 Edge 侧**降采样**图
（``DEFAULT_IMAGE_SIZE``），调用方在预览里点到的像素与源分辨率不是同一网格；归一化后两边一致，
Edge 内部再换算成源像素（回执回显 ``u_px`` / ``v_px`` 供核对）。

设计取舍：读缓存而不向机器人进程发请求——深度是**观测**，Edge 已按观测频率缓存最新帧
（与 ``/v1/preview`` 同一份数据），查询不该打扰机器人的控制循环。
"""

from __future__ import annotations

import math

import numpy as np

from motrix_edge.adapter.base import DEPTH_PREFIX, KEY_POSE, depth_names_of
from motrix_edge.adapter.http_contract import (
    FIELD_CX,
    FIELD_CY,
    FIELD_DEPTH,
    FIELD_DEPTH_SCALE,
    FIELD_FRAME,
    FIELD_FX,
    FIELD_FY,
    FIELD_INTRINSICS,
    FIELD_WORLD,
    FIELD_XYZ_CAMERA,
    FIELD_XYZ_WORLD,
)
from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.geometry import MOUNT_FIXED, FrameError, transform_points
from motrix_edge.geometry import pixel_to_camera as deproject_pixel
from motrix_edge.lease import LeaseError, LeaseManager


class DepthError(ServiceError):
    """深度查询被拒绝（租约缺失 / 不匹配 / 过期 / 相机无深度 / 坐标非法 / 未注入）。"""


class DepthService:
    """像素深度查询服务：绑定 node（FrameManager 观测缓存 + adapter 相机元数据）+ Edge 级租约。

    与 ``PreviewService`` 同一套依赖与规则（只读、须持租约、不要求进入会话）——深度就是
    「预览里能看到的那张图」的深度，两者必须来自同一帧缓存，否则调用方会把两个时刻的数据
    混着用。
    """

    def __init__(self, node, leases: LeaseManager):
        self._node = node  # 正在运行的 EdgeNode（frame_manager / adapter 取自 node）
        # Edge 级租约（受控操作校验）：**必须注入共享实例**，自建实例永远无租约 → 静默 403 / 409
        self._leases = leases

    def depth(self, camera: str, u: float = 0.5, v: float = 0.5, lease_id: str | None = None) -> dict:
        """查 ``camera`` 上归一化坐标 ``(u, v)`` 处的深度（缺省 = 画面中心）。

        - 深度图取**最新观测帧**（FrameManager 缓存，与 ``/v1/preview`` 同源）；
        - ``u`` / ``v`` ∈ ``[0, 1]``（相对源分辨率），非数值 / 越界 → ``invalid_argument``；
        - 该像素无有效深度（原始值 ``0``）→ ``valid: false``、``depth_m: null``
          （RealSense 的 ``0`` 是「测不到」，不是「距离 0」）；
        - ``u`` / ``v`` 越界 → ``invalid_argument``（非数值由 HTTP 层校验，回 422）；
        - 相机名未知 / 未启用 / 该机型无深度 → ``not_found``；
        - 回执带 ``intrinsics``（彩色内参）与 ``depth_scale``：调用方自己做反投影就用它们；
        - 标定产物可用时另给坐标：``xyz_camera``（相机光学系）与 ``xyz_world``（统一 ``world`` 帧，
          = 左臂基座；腕相机用**同拍**位姿合成外参）。坐标是**纯增量**：无产物 / 缺内参 / 腕相机
          缺位姿 → 相应字段为 ``null``，**绝不报错**（深度查询本身不依赖外参）。
        """
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise DepthError(str(exc), exc.code) from exc
        camera = str(camera or "").strip()
        if not camera:
            raise DepthError("camera is required", code=ErrorCode.INVALID_ARGUMENT)
        frame_manager = getattr(self._node, "frame_manager", None)
        if frame_manager is None:
            raise DepthError("frame manager not available", code=ErrorCode.NOT_IMPLEMENTED)
        latest = frame_manager.latest() or {}
        depth = latest.get(f"{DEPTH_PREFIX}{camera}")
        if depth is None:
            available = sorted(depth_names_of(latest))
            raise DepthError(
                f"no depth for camera {camera!r} (available: {available})",
                code=ErrorCode.NOT_FOUND,
            )
        array = np.asarray(depth)
        height, width = int(array.shape[0]), int(array.shape[1])
        u_px = self._pixel(u, width, "u")
        v_px = self._pixel(v, height, "v")
        depth_raw = int(array[v_px, u_px])
        scale = self._depth_scale(camera)
        # 0 = 该像素**测不到**（不是「距离 0」）：不给 0.0 这种会被下游当真值的米数
        depth_m = (depth_raw * scale) if (depth_raw > 0 and scale is not None) else None
        intrinsics = self._intrinsics(camera)
        xyz_camera, xyz_world, frame, world = self._coordinates(camera, u_px, v_px, depth_m, intrinsics, latest)
        return {
            "camera": camera,
            "u": float(u),
            "v": float(v),
            "u_px": u_px,
            "v_px": v_px,
            "width": width,
            "height": height,
            "depth_raw": depth_raw,
            "depth_m": depth_m,
            "valid": depth_raw > 0,
            "depth_scale": scale,
            "intrinsics": intrinsics,
            FIELD_XYZ_CAMERA: xyz_camera,
            FIELD_XYZ_WORLD: xyz_world,
            FIELD_FRAME: frame,
            FIELD_WORLD: world,
        }

    @staticmethod
    def _pixel(value: float, size: int, name: str) -> int:
        """归一化坐标 → 源像素（越界 / 非有限值 → 400；``1.0`` 落在最后一个像素）。"""
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise DepthError(f"{name} must be a number in [0, 1]", code=ErrorCode.INVALID_ARGUMENT) from exc
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise DepthError(f"{name} must be in [0, 1] (got {value!r})", code=ErrorCode.INVALID_ARGUMENT)
        return min(int(number * size), size - 1)

    def _camera_meta(self, camera: str) -> dict:
        """该相机的进程上报元数据（adapter 缓存；未上报 / 未实现 → 空 dict）。"""
        adapter = getattr(self._node, "adapter", None)
        infos = getattr(adapter, "camera_infos", None)
        if not callable(infos):
            return {}
        return dict(infos().get(camera) or {})

    def _depth_scale(self, camera: str) -> float | None:
        """深度原始值 → 米的比例；元数据不可用 → None（回执里 ``depth_m`` 也为 null）。"""
        depth = self._camera_meta(camera).get(FIELD_DEPTH) or {}
        scale = depth.get(FIELD_DEPTH_SCALE)
        return None if scale is None else float(scale)

    def _intrinsics(self, camera: str) -> dict:
        """该相机的**彩色内参**（对齐后深度与彩图共用同一像素网格）。"""
        return dict(self._camera_meta(camera).get(FIELD_INTRINSICS) or {})

    def _frame_set(self):
        """标定外参（adapter 的 ``frame_set()``）：未标定 / 旧版进程 → ``None``。"""
        adapter = getattr(self._node, "adapter", None)
        getter = getattr(adapter, "frame_set", None)
        return getter() if callable(getter) else None

    def _coordinates(
        self,
        camera: str,
        u_px: int,
        v_px: int,
        depth_m: float | None,
        intrinsics: dict,
        latest: dict,
    ) -> tuple[list[float] | None, list[float] | None, str | None, str | None]:
        """像素 + 深度 → ``(xyz_camera, xyz_world, frame, world)``（米；不可用 → ``None``）。

        三段各自独立降级：

        1. ``xyz_camera`` 只靠内参 + 深度；
        2. ``xyz_world`` 另需标定外参（固定相机直接用；腕相机用**同拍** ``observations/pose`` 合成）；
        3. 腕相机缺位姿 / 该相机未标定 → 只丢 ``xyz_world``（``xyz_camera`` 照常给）。
        """
        if depth_m is None or any(key not in intrinsics for key in (FIELD_FX, FIELD_FY, FIELD_CX, FIELD_CY)):
            return None, None, None, None
        frames = self._frame_set()
        if frames is None:
            return None, None, None, None
        try:
            xyz_camera = deproject_pixel(
                u_px,
                v_px,
                depth_m,
                fx=float(intrinsics[FIELD_FX]),
                fy=float(intrinsics[FIELD_FY]),
                cx=float(intrinsics[FIELD_CX]),
                cy=float(intrinsics[FIELD_CY]),
            )
        except (ValueError, TypeError, KeyError):
            return None, None, None, None
        item = frames.cameras.get(camera)
        if item is None:
            return self._round(xyz_camera), None, None, None
        poses = self._pose_by_arm(latest)
        if item.mount != MOUNT_FIXED and item.arm not in poses:
            # 腕相机但这一拍没位姿（机器人不提供 / 形状不符）→ 只丢世界坐标，不报错
            return self._round(xyz_camera), None, None, None
        try:
            matrix = frames.world_from_camera(camera, poses)
        except (FrameError, ValueError):
            return self._round(xyz_camera), None, None, None
        xyz_world = transform_points(matrix, xyz_camera)
        return self._round(xyz_camera), self._round(xyz_world), "world", frames.world

    def _pose_by_arm(self, latest: dict) -> dict:
        """最新帧里**按臂名**的位姿（``observations/pose``，每臂 ``xyz + rpy``）——布局由 adapter 给。"""
        adapter = getattr(self._node, "adapter", None)
        splitter = getattr(adapter, "pose_by_arm", None)
        if not callable(splitter):
            return {}
        return splitter(latest.get(KEY_POSE))

    @staticmethod
    def _round(values) -> list[float]:
        """米制坐标保留 3 位小数（与日志 / 预览口径一致）。"""
        return [round(float(value), 3) for value in values]


__all__ = ["DepthError", "DepthService"]
