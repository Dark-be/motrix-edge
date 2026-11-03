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

"""calibration/board —— 标定板的几何 / 检测 / 位姿（**唯一的 OpenCV 依赖点**）。

流程里的「相机 ↔ 板」一步：板放在工作台上**固定不动**，每台相机拍若干帧，检出 ChArUco 角点后用
``cv2.solvePnP``（内参已知）给出 ``T_cam_board``——**单帧即得 6 自由度**，不需要深度。

-   板系（``board``）：以板平面为 ``z = 0``，原点在第一个棋盘格角点，``x`` / ``y`` 沿格线（米）；
-   ``cv2.aruco`` 自 OpenCV 4.7 起在主模块里（``opencv-python`` 即含），但**不假设**调用环境一定装了
    OpenCV：本模块只在函数内 ``import cv2``，没装时给出可读报错；
-   内参用 ``GET /v1/cameras`` 的**出厂值**（针孔 + **零畸变**）。RealSense 彩色图的畸变系数很小但
    非零，忽略它是本方案已知的精度上限之一（见 ``wiki/design/robot_pipeline_frames.md``「未做」）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from motrix_edge.geometry import transform_points

#: 常用 5×4 ChArUco（``square_length`` 是格子边长、``marker_length`` 是内部 ArUco 码边长，**米**）。
DEFAULT_SQUARES_X = 5
DEFAULT_SQUARES_Y = 4
DEFAULT_SQUARE_LENGTH = 0.04
DEFAULT_MARKER_LENGTH = 0.02
DEFAULT_DICTIONARY = "DICT_5X5_50"


@dataclass(frozen=True)
class BoardSpec:
    """标定板规格（打印时量准，**单位米**）——采集 / 解算两侧必须一致，故写进样本 JSON。"""

    squares_x: int = DEFAULT_SQUARES_X
    squares_y: int = DEFAULT_SQUARES_Y
    square_length: float = DEFAULT_SQUARE_LENGTH
    marker_length: float = DEFAULT_MARKER_LENGTH
    dictionary: str = DEFAULT_DICTIONARY

    def as_dict(self) -> dict:
        return {
            "squares_x": int(self.squares_x),
            "squares_y": int(self.squares_y),
            "square_length": float(self.square_length),
            "marker_length": float(self.marker_length),
            "dictionary": str(self.dictionary),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> BoardSpec:
        return cls(
            squares_x=int(payload.get("squares_x", DEFAULT_SQUARES_X)),
            squares_y=int(payload.get("squares_y", DEFAULT_SQUARES_Y)),
            square_length=float(payload.get("square_length", DEFAULT_SQUARE_LENGTH)),
            marker_length=float(payload.get("marker_length", DEFAULT_MARKER_LENGTH)),
            dictionary=str(payload.get("dictionary", DEFAULT_DICTIONARY)),
        )


def _cv2():
    """惰性导入 OpenCV（未安装时给出可读提示，而不是模块导入期就 ImportError）。"""
    try:
        import cv2  # noqa: PLC0415 按需导入：本模块是唯一的 OpenCV 依赖点
        import cv2.aruco as aruco  # noqa: PLC0415

        return cv2, aruco
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError("opencv (opencv-python-headless) is required for board detection") from exc


def make_board(spec: BoardSpec):
    """构造 OpenCV 的 ChArUco 板对象。"""
    _, aruco = _cv2()
    dictionary_id = getattr(aruco, str(spec.dictionary), None)
    if dictionary_id is None:
        raise ValueError(f"unknown aruco dictionary {spec.dictionary!r}")
    return aruco.CharucoBoard(
        (int(spec.squares_x), int(spec.squares_y)),
        float(spec.square_length),
        float(spec.marker_length),
        aruco.getPredefinedDictionary(dictionary_id),
    )


def object_points(spec: BoardSpec) -> np.ndarray:
    """板系下的全部棋盘格角点 ``(N, 3)``（米）——``ids`` 就是这里的下标。"""
    board = make_board(spec)
    return np.asarray(board.getChessboardCorners(), dtype=np.float64).reshape(-1, 3)


def detect(image, spec: BoardSpec) -> tuple[np.ndarray, np.ndarray]:
    """检测一帧图像里的 ChArUco 角点 → ``(corners (N, 2) float64, ids (N,))``。

    输入可以是彩色（BGR）或灰度图；检不到 → ``(空数组, 空数组)``（调用方据此跳过该帧，不报错）。
    """
    cv2, aruco = _cv2()
    frame = np.asarray(image)
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    corners, ids, _, _ = aruco.CharucoDetector(make_board(spec)).detectBoard(gray)
    if corners is None or ids is None or len(ids) == 0:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0,), dtype=np.int32)
    return np.asarray(corners, dtype=np.float64).reshape(-1, 2), np.asarray(ids, dtype=np.int32).reshape(-1)


def pose_from_corners(
    corners: np.ndarray,
    ids: np.ndarray,
    spec: BoardSpec,
    intrinsics: dict,
    *,
    refine: bool = True,
) -> tuple[np.ndarray, float]:
    """角点 + 内参 → ``(T_cam_board, rms_px)``（``T_cam_board`` 把板点映射到相机帧）。

    ``rms_px`` = 重投影残差（像素）——**判读要用**：> 1 px 通常是角点检出不准（板倾斜过大 / 模糊 /
    曝光过度）或内参不对。

    ⚠️ **平面靶的两义性**：PnP 只会返回「板在相机前方」的解；若角点本身来自「板在相机后方」
    （镜像 / 反射 / 非物理的合成数据），解出来的会是**镜像位姿**——重投影误差同样完美，外参却完全
    错。现场板总在视野内，故这种数据不会出现；这里用「板必须在相机前方」做一道兜底检查。
    """
    cv2, _ = _cv2()
    points = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    indices = np.asarray(ids, dtype=np.int64).reshape(-1)
    if points.shape[0] < 4:
        raise ValueError(f"at least 4 detected corners required, got {points.shape[0]}")
    world = object_points(spec)[indices]
    camera_matrix = np.array(
        [
            [float(intrinsics["fx"]), 0.0, float(intrinsics["cx"])],
            [0.0, float(intrinsics["fy"]), float(intrinsics["cy"])],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    distortion = np.zeros(5, dtype=np.float64)
    ok, rvec, tvec = cv2.solvePnP(
        world, points.reshape(-1, 1, 2), camera_matrix, distortion, flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not ok:
        raise ValueError("solvePnP failed (degenerate corner layout?)")
    if refine:
        rvec, tvec = cv2.solvePnPRefineLM(world, points.reshape(-1, 1, 2), camera_matrix, distortion, rvec, tvec)
    rotation = cv2.Rodrigues(rvec)[0]
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = np.asarray(tvec, dtype=np.float64).reshape(3)
    # 平面靶的 PnP 有**镜像解**（重投影误差同样为 0）：板落到相机后方时会解出那一个 → 直接拦掉，
    # 否则它会以「残差完美」的姿态污染后续手眼标定（真机上表现为「某几帧就是不对」）
    if float(np.min(transform_points(transform, object_points(spec))[:, 2])) <= 0.0:
        raise ValueError("board is behind the camera (PnP mirror solution): keep the board inside the view")
    projected = cv2.projectPoints(world, rvec, tvec, camera_matrix, distortion)[0].reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((projected - points) ** 2, axis=1))))
    return transform, rms


def render(spec: BoardSpec, size_px: int = 900, margin: int = 40) -> np.ndarray:
    """渲染板图（打印用 / 离线自测用）——灰度输出（OpenCV ``generateImage`` 就是灰度）。"""
    cv2, _ = _cv2()
    image = make_board(spec).generateImage((int(size_px), int(size_px)), marginSize=int(margin))
    return cv2.cvtColor(np.asarray(image, dtype=np.uint8), cv2.COLOR_GRAY2BGR)


__all__ = [
    "DEFAULT_DICTIONARY",
    "DEFAULT_MARKER_LENGTH",
    "DEFAULT_SQUARE_LENGTH",
    "DEFAULT_SQUARES_X",
    "DEFAULT_SQUARES_Y",
    "BoardSpec",
    "detect",
    "make_board",
    "object_points",
    "pose_from_corners",
    "render",
]
