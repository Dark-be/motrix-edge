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

"""geometry/extrinsics —— 统一坐标系模型（帧集合）+ 标定产物（``frames.json``）读写与校验。

**单一基底**：``world`` ≡ 左臂基座（``left`` 臂 ``fk()`` 的原点系）。左臂的位姿观测 / 位姿动作本来就
原生在这个系里（恒等，省一个外参），右臂与所有相机都锚到它。

帧与变换：

| 帧                    | 存储的变换                    | 来源                     |
| --------------------- | ----------------------------- | ------------------------ |
| ``world``             | （无，定义即基底）            | 机械臂 DH 模型           |
| ``base_<arm>``        | ``T_world_base``（每臂一个）   | 标定（左臂通常 = 恒等）  |
| ``flange_<arm>``      | ``FK(q)``（运行期给）          | 运动学（``observations/pose``） |
| ``cam:<name>``        | ``T_world_cam``（固定相机）或 ``T_flange_cam``（腕相机） | 标定        |

⚠️ **腕相机不给世界位姿**：它的世界位姿随臂动，静态产物里写死了必然过期 → 只存「法兰 → 相机」这个
常数，运行期按 ``T_world_base · FK(q) · T_flange_cam`` 合成（``FK(q)`` 必须是**同一拍**的位姿）。

产物由 robot-pipeline 读写（``robot.calibration.store``），Edge 侧只**消费**——外参经
``GET /v1/cameras`` 上报（与内参同通道），Edge 不读机器人配置。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.geometry.deproject import pixel_to_camera
from motrix_edge.geometry.transforms import (
    IDENTITY,
    as_transform,
    compose,
    flatten_transform,
    pose_to_transform,
    transform_points,
)

#: 产物格式版本（读到时校验；不认识的版本整份拒绝，不做「尽力而为」的兼容）。
FRAMES_VERSION = 1

#: ``mount`` 取值：固定相机（外参 = ``T_world_cam``）/ 腕相机（外参 = ``T_flange_cam``，随臂动）。
MOUNT_FIXED = "fixed"
MOUNT_WRIST = "wrist"
MOUNTS = (MOUNT_FIXED, MOUNT_WRIST)

#: rpy 约定标识（与 :mod:`motrix_edge.geometry.transforms` 一致：``R = Rz·Ry·Rx``）。
RPY_ORDER = "zyx"

#: ``world`` 所在的那条臂（布局里的臂名）——``world`` 的**定义**就是它的基座，故该臂在产物里
#: 恒等、也不需要 ``T_world_base``；其余臂必须靠产物换算（否则差一个基座间距）。
WORLD_ARM = "left"

#: ``world`` 的物理别名（左臂基座）——回执里与帧名一起给，避免「world 到底是谁」的歧义。
WORLD_ALIAS = f"{WORLD_ARM}_base"

# JSON 键名（产物 schema 单点；robot-pipeline 侧不另立一套）
KEY_VERSION = "version"
KEY_WORLD = "world"
KEY_RPY_ORDER = "rpy_order"
KEY_LENGTH_UNIT = "length_unit"
KEY_ANGLE_UNIT = "angle_unit"
KEY_CALIBRATED_AT = "calibrated_at"
KEY_TOOL = "tool"
KEY_ARMS = "arms"
KEY_CAMERAS = "cameras"
KEY_TABLE = "table"
KEY_T_WORLD_BASE = "T_world_base"
KEY_PROBE_TIP = "probe_tip"  # arms.<arm>：探针尖在**法兰系**的位置（米；可选，验收要用）
KEY_T_WORLD_CAM = "T_world_cam"
KEY_T_FLANGE_CAM = "T_flange_cam"
KEY_T_WORLD_TABLE = "T_world_table"
KEY_MOUNT = "mount"
KEY_ARM = "arm"
KEY_RMS_M = "rms_m"

LENGTH_UNIT = "m"
ANGLE_UNIT = "rad"


class FrameError(ServiceError):
    """帧集合不可用（产物缺失 / 非法 / 查询未知帧 / 腕相机缺位姿）。"""


@dataclass(frozen=True)
class CameraExtrinsics:
    """单个相机的外参：``mount`` 决定 ``transform`` 的**源帧**。

    - ``fixed``：``transform`` = ``T_world_cam``（常数，直接用）；
    - ``wrist``：``transform`` = ``T_flange_cam``（常数），运行期还要 ``arm`` 的 ``T_world_base``
      与同拍 ``FK(q)``。
    """

    name: str
    mount: str
    arm: str | None
    transform: np.ndarray
    rms_m: float | None = None

    @property
    def source_frame(self) -> str:
        """``transform`` 的源帧名（固定相机 = ``world`` 别名，腕相机 = ``flange_<arm>``）。"""
        return WORLD_ALIAS if self.mount == MOUNT_FIXED else f"flange_{self.arm}"


class FrameSet:
    """帧集合：``world`` + 各臂底座外参 + 各相机外参（标定产物在内存里的形态）。"""

    def __init__(
        self,
        *,
        world: str = WORLD_ALIAS,
        arms: Mapping[str, np.ndarray] | None = None,
        cameras: Mapping[str, CameraExtrinsics] | None = None,
        probe_tips: Mapping[str, Sequence[float]] | None = None,
        calibrated_at: str | None = None,
        tool: str | None = None,
    ):
        self.world = str(world or WORLD_ALIAS)
        self.arms: dict[str, np.ndarray] = dict(arms or {})
        self.cameras: dict[str, CameraExtrinsics] = dict(cameras or {})
        # 探针尖（法兰系，米）：标定的**副产品**，用于「反投影坐标 ↔ 臂夹持点」的绝对精度验收
        self.probe_tips: dict[str, np.ndarray] = {
            str(arm): np.asarray(tip, dtype=np.float64).reshape(3) for arm, tip in (probe_tips or {}).items()
        }
        self.calibrated_at = calibrated_at
        self.tool = tool

    # ---- 读取 ----------------------------------------------------------------
    @classmethod
    def from_payload(cls, payload: Mapping) -> FrameSet:
        """JSON 产物 → :class:`FrameSet`；任何不合法处 → :class:`FrameError`（**不做部分采纳**）。"""
        if not isinstance(payload, Mapping):
            raise FrameError(f"calibration payload must be a mapping, got {type(payload).__name__}")
        version = payload.get(KEY_VERSION)
        if version != FRAMES_VERSION:
            raise FrameError(f"unsupported calibration version {version!r} (expected {FRAMES_VERSION})")
        world = str(payload.get(KEY_WORLD) or "")
        if not world:
            raise FrameError(f"calibration payload is missing {KEY_WORLD!r}")
        if payload.get(KEY_RPY_ORDER) not in (None, RPY_ORDER):
            raise FrameError(f"unsupported rpy order {payload.get(KEY_RPY_ORDER)!r} (expected {RPY_ORDER!r})")

        arms: dict[str, np.ndarray] = {}
        probe_tips: dict[str, np.ndarray] = {}
        for arm, block in (payload.get(KEY_ARMS) or {}).items():
            name = str(arm)
            info = dict(block or {})
            values = info.get(KEY_T_WORLD_BASE)
            try:
                arms[name] = as_transform(values, name=f"arms.{name}.{KEY_T_WORLD_BASE}")
            except ValueError as exc:
                raise FrameError(str(exc), code=ErrorCode.INVALID_ARGUMENT) from exc
            tip = info.get(KEY_PROBE_TIP)
            if tip is not None:
                values = np.asarray(tip, dtype=np.float64).reshape(-1)
                if values.size < 3 or not np.all(np.isfinite(values[:3])):
                    raise FrameError(
                        f"arms.{name}.{KEY_PROBE_TIP}: expected 3 finite numbers", code=ErrorCode.INVALID_ARGUMENT
                    )
                probe_tips[name] = values[:3].copy()

        cameras: dict[str, CameraExtrinsics] = {}
        for camera, block in (payload.get(KEY_CAMERAS) or {}).items():
            name = str(camera)
            info = dict(block or {})
            mount = str(info.get(KEY_MOUNT) or "")
            if mount not in MOUNTS:
                raise FrameError(f"cameras.{name}.{KEY_MOUNT}: expected one of {MOUNTS}, got {mount!r}")
            arm = info.get(KEY_ARM)
            arm = None if arm is None else str(arm)
            key = KEY_T_WORLD_CAM if mount == MOUNT_FIXED else KEY_T_FLANGE_CAM
            try:
                transform = as_transform(info.get(key), name=f"cameras.{name}.{key}")
            except ValueError as exc:
                raise FrameError(str(exc), code=ErrorCode.INVALID_ARGUMENT) from exc
            if mount == MOUNT_WRIST and arm not in arms:
                raise FrameError(
                    f"cameras.{name}: wrist camera needs {KEY_ARM!r} declared in {KEY_ARMS!r} (got {arm!r})"
                )
            rms = info.get(KEY_RMS_M)
            cameras[name] = CameraExtrinsics(
                name=name,
                mount=mount,
                arm=arm,
                transform=transform,
                rms_m=None if rms is None else float(rms),
            )
        return cls(
            world=world,
            arms=arms,
            cameras=cameras,
            probe_tips=probe_tips,
            calibrated_at=None if payload.get(KEY_CALIBRATED_AT) is None else str(payload[KEY_CALIBRATED_AT]),
            tool=None if payload.get(KEY_TOOL) is None else str(payload[KEY_TOOL]),
        )

    @classmethod
    def load(cls, path: str | Path) -> FrameSet:
        """读产物文件（缺失 / 非 JSON / 非法 → :class:`FrameError`）。"""
        file = Path(path)
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            raise FrameError(f"calibration file unreadable: {file} ({exc})", code=ErrorCode.NOT_FOUND) from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FrameError(f"calibration file is not valid JSON: {file} ({exc})") from exc
        return cls.from_payload(payload)

    @classmethod
    def load_if_exists(cls, path: str | Path) -> FrameSet | None:
        """产物存在则解析（非法 → :class:`FrameError`），缺失 → ``None``（= 未标定，不是错误）。"""
        file = Path(path)
        if not file.exists():
            return None
        return cls.load(file)

    # ---- 写出 ----------------------------------------------------------------
    def to_payload(self) -> dict:
        """→ JSON 形状（与 :meth:`from_payload` 互逆；脚本 ``--install`` 用它落盘）。"""
        cameras: dict[str, dict] = {}
        for name, item in self.cameras.items():
            block: dict = {KEY_MOUNT: item.mount}
            if item.mount == MOUNT_WRIST:
                block[KEY_ARM] = item.arm
            key = KEY_T_WORLD_CAM if item.mount == MOUNT_FIXED else KEY_T_FLANGE_CAM
            block[key] = flatten_transform(item.transform)
            if item.rms_m is not None:
                block[KEY_RMS_M] = float(item.rms_m)
            cameras[name] = block
        return {
            KEY_VERSION: FRAMES_VERSION,
            KEY_WORLD: self.world,
            KEY_RPY_ORDER: RPY_ORDER,
            KEY_LENGTH_UNIT: LENGTH_UNIT,
            KEY_ANGLE_UNIT: ANGLE_UNIT,
            KEY_CALIBRATED_AT: self.calibrated_at,
            KEY_TOOL: self.tool,
            KEY_ARMS: {
                arm: {
                    KEY_T_WORLD_BASE: flatten_transform(T),
                    **(
                        {KEY_PROBE_TIP: [float(value) for value in self.probe_tips[arm]]}
                        if arm in self.probe_tips
                        else {}
                    ),
                }
                for arm, T in self.arms.items()
            },
            KEY_CAMERAS: cameras,
            KEY_TABLE: None,
        }

    def save(self, path: str | Path) -> Path:
        """写产物文件（父目录自动创建；UTF-8 + 缩进 2 便于人工核对与 diff）。"""
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(self.to_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return file

    # ---- 查询 ----------------------------------------------------------------
    def world_from_base(self, arm: str) -> np.ndarray:
        """``T_world_base``（未标定该臂 → :class:`FrameError`）。"""
        transform = self.arms.get(str(arm))
        if transform is None:
            raise FrameError(
                f"arm {arm!r} has no {KEY_T_WORLD_BASE} in calibration (known: {sorted(self.arms)})",
                code=ErrorCode.NOT_FOUND,
            )
        return transform

    def camera(self, camera: str) -> CameraExtrinsics:
        """取某相机的外参（未标定 / 未知相机 → :class:`FrameError`）。"""
        item = self.cameras.get(str(camera))
        if item is None:
            raise FrameError(
                f"camera {camera!r} has no extrinsics (calibrated: {sorted(self.cameras)})",
                code=ErrorCode.NOT_FOUND,
            )
        return item

    def world_from_camera(self, camera: str, pose_by_arm: Mapping[str, Sequence[float]] | None = None) -> np.ndarray:
        """``cam:<camera>`` → ``world`` 的变换。

        - 固定相机：直接用 ``T_world_cam``；
        - 腕相机：``T_world_base(arm) · pose_to_transform(pose_by_arm[arm]) · T_flange_cam``——位姿是
          **该臂基座系**下的 ``FK(q)``（``observations/pose`` 的每臂 6 维），必须是**同拍**数据；
          缺位姿 → :class:`FrameError`（不拿别的拍的位姿凑）。
        """
        item = self.camera(camera)
        if item.mount == MOUNT_FIXED:
            return item.transform.copy()
        pose = (pose_by_arm or {}).get(str(item.arm))
        if pose is None:
            raise FrameError(
                f"camera {camera!r} is wrist-mounted on arm {item.arm!r}: same-frame pose required",
                code=ErrorCode.NOT_FOUND,
            )
        return compose(self.world_from_base(str(item.arm)), pose_to_transform(pose), item.transform)

    def camera_frame(self, camera: str) -> str:
        """该相机外参的**源帧名**（固定 = ``world``，腕 = ``flange_<arm>``）。"""
        return self.camera(camera).source_frame

    def pixel_to_world(
        self,
        camera: str,
        u_px: float,
        v_px: float,
        depth_m: float,
        *,
        fx: float,
        fy: float,
        cx: float,
        cy: float,
        pose_by_arm: Mapping[str, Sequence[float]] | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """像素 + 深度 → ``(xyz_camera, xyz_world)``（米）——反投影 + 外参合成的唯一入口。"""
        xyz_camera = pixel_to_camera(u_px, v_px, depth_m, fx=fx, fy=fy, cx=cx, cy=cy)
        return xyz_camera, transform_points(self.world_from_camera(camera, pose_by_arm), xyz_camera)

    def probe_tip(self, arm: str) -> np.ndarray | None:
        """探针尖在**法兰系**的位置（米）；未标定该臂 → ``None``。

        用于验收：``T_world_base · FK(q) · t_probe`` 即「臂现在夹持的那一点」在 ``world`` 下的
        位置，可与深度查询出的坐标直接对比（绝对精度）。
        """
        tip = self.probe_tips.get(str(arm))
        return None if tip is None else tip.copy()

    def identity_world(self, arm: str) -> bool:
        """该臂底座是否就是 ``world``（左臂通常为真）——报告 / 排障用。"""
        transform = self.arms.get(str(arm))
        return transform is not None and bool(np.allclose(transform, IDENTITY, atol=1e-9))


__all__ = [
    "ANGLE_UNIT",
    "FRAMES_VERSION",
    "LENGTH_UNIT",
    "MOUNTS",
    "MOUNT_FIXED",
    "MOUNT_WRIST",
    "RPY_ORDER",
    "WORLD_ALIAS",
    "WORLD_ARM",
    "CameraExtrinsics",
    "FrameError",
    "FrameSet",
]
