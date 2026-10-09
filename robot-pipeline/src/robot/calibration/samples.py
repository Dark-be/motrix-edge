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

"""calibration/samples —— 标定采集样本（``frames_samples.json``）的结构与读写。

采集（碰硬件，``scripts/calibrate_extrinsics.py --collect``）与解算（纯离线，``--solve``）之间只通过
这个文件传递数据——把「取数」和「算数」分开，与重力前馈的 ``verify_gravity`` / ``fit_gravity`` 同一
套路（算数那半步在任意机器上都能跑、能重算）。

结构（``version: 1``）：

```json
{
    "version": 1,
    "created_at": "2026-10-09T12:00:00",
    "robot": "dual_piper_001",
    "world_arm": "left",
    "arms": ["left", "right"],
    "wrist_cameras": { "cam_left_wrist": "left", "cam_right_wrist": "right" },
    "board": {
        "squares_x": 5, "squares_y": 4, "square_length": 0.04,
        "marker_length": 0.02, "dictionary": "DICT_5X5_50"
    },
    "intrinsics": {
        "cam_head": { "fx": 605.1, "fy": 604.9, "cx": 320.5, "cy": 240.6 }
    },
    "views": [
        { "camera": "cam_head", "arm": null, "pose": null, "ids": [0, 1, 2], "corners": [[u, v], ...] },
        { "camera": "cam_left_wrist", "arm": "left", "pose": [x, y, z, r, p, yaw],
          "ids": [...], "corners": [[u, v], ...] }
    ],
    "touches": [
        { "arm": "left", "pose": [x, y, z, r, p, yaw], "point": [bx, by, bz], "label": "corner_3" }
    ]
}
```

-   ``views`` 存**原始角点**（不存解算结果）：内参 / 板规格变了可以重算，且能复核逐帧重投影残差；
-   ``pose`` 是承载相机的那条臂的 ``observations/pose``（``xyz + rpy``，**该臂基座系**）；固定相机
    为 ``null``；
-   ``touches.point`` 是被触碰的那一点在**板系**下的坐标（板几何已知 → 由角点 id 查得）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from robot.calibration.board import BoardSpec

#: 样本格式版本（读到不认识的版本直接拒绝——半份数据比没有更危险）。
SAMPLES_VERSION = 1

#: 逐帧至少要有这么多角点才参与解算（少于 4 个点 PnP 不唯一）。
MIN_CORNERS = 4


@dataclass
class CameraView:
    """一帧「相机看板」的观测。"""

    camera: str
    corners: np.ndarray  # (N, 2) 像素
    ids: np.ndarray  # (N,) 板角点 id
    arm: str | None = None  # 腕相机的承载臂（固定相机为 None）
    pose: list[float] | None = None  # 该臂位姿 [xyz, rpy]（固定相机为 None）

    def as_dict(self) -> dict:
        return {
            "camera": self.camera,
            "arm": self.arm,
            "pose": None if self.pose is None else [float(value) for value in self.pose],
            "ids": [int(value) for value in np.asarray(self.ids).reshape(-1)],
            "corners": [[float(u), float(v)] for u, v in np.asarray(self.corners).reshape(-1, 2)],
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> CameraView:
        corners = np.asarray(payload.get("corners"), dtype=np.float64).reshape(-1, 2)
        ids = np.asarray(payload.get("ids"), dtype=np.int64).reshape(-1)
        if corners.shape[0] != ids.shape[0]:
            raise ValueError(f"view {payload.get('camera')!r}: corners / ids length mismatch")
        pose = payload.get("pose")
        return cls(
            camera=str(payload.get("camera") or ""),
            corners=corners,
            ids=ids,
            arm=None if payload.get("arm") is None else str(payload["arm"]),
            pose=None if pose is None else [float(value) for value in np.asarray(pose, dtype=np.float64).reshape(-1)],
        )


@dataclass
class ProbeTouch:
    """一次「探针触碰板上已知点」的记录。"""

    arm: str
    pose: list[float]  # 该臂位姿 [xyz, rpy]
    point: list[float]  # 被触碰点在板系下的坐标（米）
    label: str = ""

    def as_dict(self) -> dict:
        return {
            "arm": self.arm,
            "pose": [float(value) for value in self.pose],
            "point": [float(value) for value in self.point],
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> ProbeTouch:
        pose = [float(value) for value in np.asarray(payload.get("pose"), dtype=np.float64).reshape(-1)]
        point = [float(value) for value in np.asarray(payload.get("point"), dtype=np.float64).reshape(-1)]
        if len(pose) < 6:
            raise ValueError(f"touch on arm {payload.get('arm')!r}: pose needs 6 entries, got {len(pose)}")
        if len(point) < 3:
            raise ValueError(f"touch on arm {payload.get('arm')!r}: point needs 3 entries, got {len(point)}")
        return cls(
            arm=str(payload.get("arm") or ""), pose=pose[:6], point=point[:3], label=str(payload.get("label") or "")
        )


@dataclass
class Samples:
    """一次标定采集的完整样本（解算的**唯一**输入）。"""

    board: BoardSpec = field(default_factory=BoardSpec)
    intrinsics: dict[str, dict] = field(default_factory=dict)
    views: list[CameraView] = field(default_factory=list)
    touches: list[ProbeTouch] = field(default_factory=list)
    arms: list[str] = field(default_factory=list)
    wrist_cameras: dict[str, str] = field(default_factory=dict)
    world_arm: str = "left"
    robot: str = ""
    created_at: str = ""

    # ---- 读写 ----------------------------------------------------------------
    def as_dict(self) -> dict:
        return {
            "version": SAMPLES_VERSION,
            "created_at": self.created_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "robot": self.robot,
            "world_arm": self.world_arm,
            "arms": list(self.arms),
            "wrist_cameras": dict(self.wrist_cameras),
            "board": self.board.as_dict(),
            "intrinsics": {
                name: {key: float(value) for key, value in block.items()} for name, block in self.intrinsics.items()
            },
            "views": [view.as_dict() for view in self.views],
            "touches": [touch.as_dict() for touch in self.touches],
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> Samples:
        if payload.get("version") != SAMPLES_VERSION:
            raise ValueError(f"unsupported samples version {payload.get('version')!r} (expected {SAMPLES_VERSION})")
        board = BoardSpec.from_dict(dict(payload.get("board") or {}))
        intrinsics = {
            str(name): {str(key): float(value) for key, value in dict(block or {}).items()}
            for name, block in (payload.get("intrinsics") or {}).items()
        }
        samples = cls(
            board=board,
            intrinsics=intrinsics,
            views=[CameraView.from_dict(item) for item in payload.get("views") or []],
            touches=[ProbeTouch.from_dict(item) for item in payload.get("touches") or []],
            arms=[str(arm) for arm in payload.get("arms") or []],
            wrist_cameras={str(name): str(arm) for name, arm in (payload.get("wrist_cameras") or {}).items()},
            world_arm=str(payload.get("world_arm") if payload.get("world_arm") is not None else "left"),
            robot=str(payload.get("robot") or ""),
            created_at=str(payload.get("created_at") or ""),
        )
        samples.validate()
        return samples

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> Samples:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)

    # ---- 校验 / 查询 ---------------------------------------------------------
    def validate(self) -> None:
        """结构校验（解算前必须过）：**哪一步缺数据就在哪一步报清楚**，不要留到解算时报数值错。"""
        if not self.world_arm:
            raise ValueError("world_arm is required (world frame = that arm's base)")
        if not self.views:
            raise ValueError("views is empty: run --collect first")
        cameras = {view.camera for view in self.views}
        for camera in cameras:
            if camera not in self.intrinsics:
                raise ValueError(f"intrinsics missing for camera {camera!r} (all: {sorted(self.intrinsics)})")
        for view in self.views:
            if len(view.ids) < MIN_CORNERS:
                raise ValueError(
                    f"view on {view.camera!r}: at least {MIN_CORNERS} corners required, got {len(view.ids)}"
                )
            if view.arm is not None and view.pose is None:
                raise ValueError(f"view on wrist camera {view.camera!r}: arm pose required")
            if view.arm is not None and view.arm not in self.arms:
                raise ValueError(f"view on {view.camera!r}: arm {view.arm!r} not in {self.arms}")
        for arm in {touch.arm for touch in self.touches}:
            if arm not in self.arms:
                raise ValueError(f"touch: arm {arm!r} not in {self.arms}")
        wrist_arms = set(self.wrist_cameras.values())
        unknown = wrist_arms - set(self.arms)
        if unknown:
            raise ValueError(f"wrist_cameras: arms not in {self.arms}: {sorted(unknown)}")

    def views_of(self, camera: str) -> list[CameraView]:
        return [view for view in self.views if view.camera == camera]

    @property
    def cameras(self) -> list[str]:
        """出现过的相机名（顺序 = 采集顺序，去重）。"""
        seen: list[str] = []
        for view in self.views:
            if view.camera not in seen:
                seen.append(view.camera)
        return seen

    def touches_of(self, arm: str) -> list[ProbeTouch]:
        return [touch for touch in self.touches if touch.arm == arm]

    def summary(self) -> list[str]:
        """人类可读的采集概况（脚本报告用）——**先把「够不够」看一眼再解算**。"""
        lines = [f"板 {self.board.squares_x}×{self.board.squares_y}（格子 {self.board.square_length} m）"]
        for camera in self.cameras:
            views = self.views_of(camera)
            corner_counts = [len(view.ids) for view in views]
            lines.append(
                f"  {camera}: {len(views)} 帧"
                + (f"（角点 {min(corner_counts)}–{max(corner_counts)}）" if corner_counts else "")
                + (f" · 承载臂 {self.wrist_cameras.get(camera, '?')}" if camera in self.wrist_cameras else " · 固定")
            )
        for arm in sorted({touch.arm for touch in self.touches}):
            lines.append(f"  {arm}: 探针触碰 {len(self.touches_of(arm))} 点")
        return lines


def as_pose_list(pose: Sequence[float] | None) -> list[float] | None:
    """``observations/pose`` 的每臂 6 维切片 → 浮点列表（缺值 → ``None``）。"""
    if pose is None:
        return None
    values = np.asarray(pose, dtype=np.float64).reshape(-1)
    return None if values.size < 6 else [float(value) for value in values[:6]]


__all__ = ["MIN_CORNERS", "SAMPLES_VERSION", "CameraView", "ProbeTouch", "Samples", "as_pose_list"]
