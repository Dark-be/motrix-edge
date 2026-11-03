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

"""calibration/solver —— 外参求解（**纯 numpy**，不碰硬件、不碰 OpenCV）。

三件事，都是标定流程里的「解算」步（采集由脚本负责，见
``wiki/design/robot_pipeline_frames.md``「标定流程」）：

1.  :func:`fit_transform`：**点集配准**（Umeyama，无缩放）——已知一组点在两个帧下的坐标，
    求它们之间的刚体变换；
2.  :func:`solve_hand_eye`：**手眼标定** $AX = XB$ → 腕相机的 ``T_flange_cam``
    （固定相机用 ``solvePnP`` 直接给 ``T_cam_board``，不需要这一步）；
3.  :func:`solve_probe`：**探针触碰** → ``T_base_board`` + 探针尖在法兰系的位置 ``t_probe``
    （探针本身**不需要预先标定**：它与臂外参一起解出来）。

⚠️ 为什么自己写而不是用 OpenCV：``cv2.calibrateHandEye`` / ``calibrateRobotWorldHandEye`` 在
OpenCV **5.0 已移除**（本机实测 AttributeError）；自写还有两个好处——纯 numpy **可离线单测**
（:func:`self_test` 用虚拟数据复现真值），且与 ``solve_ik`` / ``fit_gravity`` 同风格。

约定（与 :mod:`motrix_edge.geometry` 一致）：``T_A_B`` 表示「把 B 帧的点映射到 A 帧」的 4×4 齐次变换；
``rpy`` 不参与本模块的运算（只用旋转矩阵），避免约定混入求解。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from motrix_edge.geometry.transforms import (
    IDENTITY,
    as_transform,
    compose,
    invert_transform,
    pose_to_transform,
    transform_points,
)

#: ``solve_probe`` 的交替优化上限（块坐标下降，通常 5–10 次就收敛到机器精度）。
PROBE_MAX_ITERATIONS = 64
#: 交替优化的收敛阈值（米）：两次迭代的探针尖位置差小于它即停。
PROBE_TOLERANCE = 1e-12


@dataclass(frozen=True)
class HandEyeResult:
    """手眼标定结果：``T_flange_cam`` + 残差 + **姿态铺开度**（判读「这次标定能不能用」）。"""

    transform: np.ndarray
    rotation_rms_deg: float
    translation_rms_m: float
    pairs: int
    rotation_spread_deg: float

    def as_dict(self) -> dict:
        return {
            "pairs": int(self.pairs),
            "rotation_rms_deg": float(self.rotation_rms_deg),
            "translation_rms_m": float(self.translation_rms_m),
            "rotation_spread_deg": float(self.rotation_spread_deg),
        }


@dataclass(frozen=True)
class ProbeResult:
    """探针触碰结果：``T_base_board`` + 探针尖（法兰系）+ 残差。"""

    transform: np.ndarray
    probe_tip: np.ndarray
    rms_m: float
    max_m: float
    iterations: int
    points_condition: float
    residuals_m: np.ndarray = field(repr=False)

    def as_dict(self) -> dict:
        return {
            "iterations": int(self.iterations),
            "rms_m": float(self.rms_m),
            "max_m": float(self.max_m),
            "points_condition": float(self.points_condition),
        }


def average_transforms(transforms: Sequence) -> tuple[np.ndarray, float, float]:
    """多帧同一变换的平均 → ``(T, 旋转离散度°, 平移离散度 m)``。

    固定相机（``cam_head``）会在多帧里各得到一个 ``T_cam_board``：它们**应当相同**，差异就是采样
    质量。旋转部分按矩阵均值再做 SVD 投影（不能对 ``rpy`` 逐项取平均——过 ±π 会撕裂），平移取均值。

    离散度是**判读指标**：偏大说明某几帧板没固定好 / 图糊了 / 采样时人碰了相机——去查逐帧 RMS。
    """
    matrices = [as_transform(item) for item in transforms]
    if not matrices:
        raise ValueError("at least one transform required")
    rotations = np.array([item[:3, :3] for item in matrices], dtype=np.float64)
    translation = np.mean([item[:3, 3] for item in matrices], axis=0)
    mean = np.eye(4, dtype=np.float64)
    mean[:3, :3] = _project_rotation(np.mean(rotations, axis=0))
    mean[:3, 3] = translation
    spread = [_rotation_angle_deg(item[:3, :3].T @ mean[:3, :3]) for item in matrices]
    offsets = [float(np.linalg.norm(item[:3, 3] - mean[:3, 3])) for item in matrices]
    return mean, float(max(spread)), float(max(offsets))


def _project_rotation(matrix: np.ndarray) -> np.ndarray:
    """任意 3×3 → **最近的**合法旋转矩阵（SVD 投影，``det`` 强制 ``+1``）。"""
    u, _, vt = np.linalg.svd(np.asarray(matrix, dtype=np.float64))
    rotation = u @ vt
    if float(np.linalg.det(rotation)) < 0.0:
        u[:, -1] *= -1.0
        rotation = u @ vt
    return rotation


def _rotation_from_scale(raw: np.ndarray) -> np.ndarray:
    """齐次解（旋转矩阵的**标量倍数**）→ 合法旋转。

    ``det(c·R) = c³``，故先按 ``c = sign(det)·|det|^(1/3)`` 去尺度，再投影回 ``SO(3)``。
    **不先做这一步会踩一个很隐蔽的坑**：零空间向量的符号是任意的，``c < 0`` 时「最近的旋转」
    在数学上**不唯一**（Frobenius 距离对 ``−R`` 处处相同）→ SVD 投影会返回一个完全错的解
    （表现是「标定结果离谱」，且能在 :func:`self_test` 里复现）。
    """
    matrix = np.asarray(raw, dtype=np.float64).reshape(3, 3)
    determinant = float(np.linalg.det(matrix))
    if abs(determinant) < 1e-12:
        raise ValueError("rotation candidate is singular (degenerate motions?)")
    scale = np.copysign(abs(determinant) ** (1.0 / 3.0), determinant)
    return _project_rotation(matrix / scale)


def _apply(transform: np.ndarray, point) -> np.ndarray:
    """单个变换作用于单个点（``T · p``）。"""
    return transform_points(transform, point)


def _apply_all(transforms: Sequence[np.ndarray], point) -> np.ndarray:
    """同一批位姿各自作用于**同一个点** → ``(N, 3)``（各次触碰的探针尖位置）。"""
    return np.array([transform_points(item, point) for item in transforms], dtype=np.float64)


def _rotation_angle_deg(rotation: np.ndarray) -> float:
    """旋转矩阵 → 等价的转角（度）：``arccos((tr - 1) / 2)``。"""
    cosine = float(np.clip((np.trace(np.asarray(rotation, dtype=np.float64)) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _rotation_spread_deg(poses: Sequence[np.ndarray]) -> float:
    """姿态铺开度：任意两姿态之间相对转角的最大值（度）。

    ``AX = XB`` 靠**姿态旋转差异**定解：全都朝一个方向小幅度摆（< 20°）时手眼标定退化——残余
    看起来小，解却不准。这里只如实报一个数，现场据此判断要不要重采。
    """
    spread = 0.0
    for index, first in enumerate(poses):
        for second in poses[index + 1 :]:
            spread = max(spread, _rotation_angle_deg(first[:3, :3].T @ second[:3, :3]))
    return spread


def _points_condition(points: np.ndarray) -> float:
    """触碰点集的**非共线度**：中心化后**第二 / 第一**奇异值之比（0 = 完全共线）。

    探针法靠「已知板点 → 触碰位置」的刚体配准定解：点**近似共线**时旋转解不可靠（残差可能仍很
    小）。取第二 / 第一（而不是最小 / 最大）是有意的——标定板是**平面**，最小奇异值天然为 0，
    用它当判据会把每组合法数据都判成病态。现场要的是把触碰点在**平面内铺开**（不同角落 / 不同
    区域），这个比值就是判据。
    """
    centered = np.asarray(points, dtype=np.float64).reshape(-1, 3) - np.mean(points, axis=0)
    singular = np.linalg.svd(centered, compute_uv=False)
    if singular[0] <= 0.0:
        return 0.0
    return float(singular[1] / singular[0])


def fit_transform(source: Sequence, target: Sequence) -> tuple[np.ndarray, float]:
    """点集配准（Umeyama，**无缩放**）：求 ``T`` 使 ``T · source ≈ target``。

    ``source`` / ``target``：``(N, 3)``（N ≥ 3 且不共线）。返回 ``(T, rms)``，``rms`` = 逐点距离的
    均方根（米）——**必须看残差**：点太少 / 共线 / 有一两个点被碰过时，旋转会「凑」出一个解。
    """
    points = np.asarray(source, dtype=np.float64).reshape(-1, 3)
    wanted = np.asarray(target, dtype=np.float64).reshape(-1, 3)
    if points.shape != wanted.shape:
        raise ValueError(f"point sets must have the same shape, got {points.shape} vs {wanted.shape}")
    if points.shape[0] < 3:
        raise ValueError(f"at least 3 point pairs required, got {points.shape[0]}")
    center_source = points.mean(axis=0)
    center_target = wanted.mean(axis=0)
    covariance = (wanted - center_target).T @ (points - center_source)
    rotation = _project_rotation(covariance)
    translation = center_target - rotation @ center_source
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation
    residual = transform_points(transform, points) - wanted
    rms = float(np.sqrt(np.mean(np.sum(residual**2, axis=1))))
    return transform, rms


def solve_hand_eye(base_from_flange: Sequence, camera_from_board: Sequence) -> HandEyeResult:
    """手眼标定 $AX = XB$ → 腕相机外参 ``X = T_flange_cam``。

    输入（同一批姿态，长度 ≥ 2，**建议 ≥ 10 且旋转充分**）：

    - ``base_from_flange[i]`` = ``T_base_flange(q_i)``（机器人 FK，``observations/pose``）；
    - ``camera_from_board[i]`` = ``T_cam_board``（**把板点映射到相机**，由 PnP 给出；同一块
      **固定不动**的板，故多帧差异全部来自相机运动）。

    取第 ``0`` 个姿态为参考，逐姿态构造一对相对运动：

    .. math::

        A_i = T_{F_0 F_i} \\qquad B_i = T_{C_0 C_i} \\qquad A_i X = X B_i

    ``F`` = 法兰帧、``C`` = 相机帧（推导：``T_{F_i B} = X · T_{C_i B}``，把板帧消去即得上式）。
    ``rotation_spread_deg`` = 姿态铺开度（任意两姿态相对转角的最大值）：**要 ≥ 20–30°** 才可靠
    ——全朝一个方向小幅度摆时该标定退化（残差看上去很小，解却不准）。
    """
    flange = [as_transform(item, name=f"base_from_flange[{index}]") for index, item in enumerate(base_from_flange)]
    camera = [as_transform(item, name=f"camera_from_board[{index}]") for index, item in enumerate(camera_from_board)]
    if len(flange) != len(camera):
        raise ValueError(f"pose lists must have the same length, got {len(flange)} vs {len(camera)}")
    if len(flange) < 2:
        raise ValueError(f"at least 2 poses required, got {len(flange)}")

    motions: list[tuple[np.ndarray, np.ndarray]] = []
    for index in range(len(flange)):
        if index == 0:
            continue
        # 相对运动：参考姿态 ← 该姿态（T_{F_0 F_i} 与 T_{C_0 C_i}，约定见 docstring）
        motions.append(
            (
                compose(invert_transform(flange[0]), flange[index]),
                compose(camera[0], invert_transform(camera[index])),
            )
        )

    # 旋转：R_A R_X = R_X R_B 的 Kronecker 零空间（vec 为**列主序** → reshape(order="F")）
    rotations_kron = [
        np.kron(np.eye(3), motion_flange[:3, :3]) - np.kron(motion_camera[:3, :3].T, np.eye(3))
        for motion_flange, motion_camera in motions
    ]
    _, _, vt = np.linalg.svd(np.vstack(rotations_kron))
    # vec 为**列主序**（行主序解回来的是转置）；零空间向量带任意标量倍率 → 先去尺度再投影
    rotation = _rotation_from_scale(vt[-1].reshape(3, 3, order="F"))

    # 平移：(R_A − I) t_X = R_X t_B − t_A（把上一步的 R_X 代进去）
    left = np.vstack([motion_flange[:3, :3] - np.eye(3) for motion_flange, _ in motions])
    right = np.concatenate(
        [rotation @ motion_camera[:3, 3] - motion_flange[:3, 3] for motion_flange, motion_camera in motions], axis=0
    )
    translation, *_ = np.linalg.lstsq(left, right, rcond=None)

    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation

    rotation_errors = [
        _rotation_angle_deg(motion_flange[:3, :3] @ rotation @ motion_camera[:3, :3].T @ rotation.T)
        for motion_flange, motion_camera in motions
    ]
    translation_errors = [
        float(
            np.linalg.norm(
                motion_flange[:3, 3]
                + motion_flange[:3, :3] @ translation
                - rotation @ motion_camera[:3, 3]
                - translation
            )
        )
        for motion_flange, motion_camera in motions
    ]
    return HandEyeResult(
        transform=transform,
        rotation_rms_deg=float(np.sqrt(np.mean(np.square(rotation_errors)))),
        translation_rms_m=float(np.sqrt(np.mean(np.square(translation_errors)))),
        pairs=len(motions),
        rotation_spread_deg=_rotation_spread_deg(flange),
    )


def _motion_rotations(flange: Sequence[np.ndarray], camera: Sequence[np.ndarray]):
    """``AX = XB`` 里每对的 ``(R_A, R_B)``（与 :func:`solve_hand_eye` 同一取法，供残差报告）。"""
    for index in range(1, len(flange)):
        motion_flange = compose(invert_transform(flange[0]), flange[index])
        motion_camera = compose(camera[0], invert_transform(camera[index]))
        yield motion_flange[:3, :3], motion_camera[:3, :3]


def _motion_translations(flange: Sequence[np.ndarray], camera: Sequence[np.ndarray]):
    """``AX = XB`` 里每对的 ``(t_A, R_A, t_B)``（供残差报告）。"""
    for index in range(1, len(flange)):
        motion_flange = compose(invert_transform(flange[0]), flange[index])
        motion_camera = compose(camera[0], invert_transform(camera[index]))
        yield motion_flange[:3, 3], motion_flange[:3, :3], motion_camera[:3, 3]


def solve_probe(base_from_flange: Sequence, board_points: Sequence) -> ProbeResult:
    """探针触碰求解 ``(T_base_board, t_probe)``。

    每次触碰给出两条信息：**碰的是板上哪个点**（``board_points[i]``，板系坐标，板几何已知）与
    **当时的法兰位姿**（``base_from_flange[i]`` = ``FK(q)``）。同一个物理点被（恰好）同一个探针尖
    碰到，故：

    .. math::

        T_{base\\leftarrow board} \\cdot p_{board,i} = T_{base\\leftarrow flange,i} \\cdot t_{probe}

    未知量 = ``T_base_board``（6）+ 探针尖在法兰系的位置 ``t_probe``（3；**姿态无关**，只用位置）。
    解法：先用「线性最小二乘」初始化（上式对 15 个未知数（``R`` 的 9 项 + 两个平移）都是线性的），
    再交替优化（① 固定 ``t_probe`` → 点集配准求 ``T_base_board``；② 固定 ``T_base_board`` →
    ``t_probe`` 取各次触碰的均值）——块坐标下降，单调收敛且每次都是闭式解。

    返回的 ``rms_m`` / ``max_m`` 是**触碰残差**：峰值得看——某一次触碰点被碰歪（或探针尖松动）会在
    ``max_m`` 上暴露出来。``points_condition`` 是触碰点的**非共线度**（中心化后第二 / 第一奇异值之
    比）：接近 0 说明点在平面内近似排在一条线上（摆得太少）——此时残差可能看着很小，旋转解却不可
    靠，要把触碰点在平面内铺开重采。
    """
    flange = [as_transform(item, name=f"base_from_flange[{index}]") for index, item in enumerate(base_from_flange)]
    points = np.asarray(board_points, dtype=np.float64).reshape(-1, 3)
    if len(flange) != len(points):
        raise ValueError(f"poses and points must have the same length, got {len(flange)} vs {len(points)}")
    if len(flange) < 3:
        raise ValueError(f"at least 3 touches required, got {len(flange)}")

    # ① 线性初始化：M_i·(Y p̃_i) − t_probe = 0（M_i = inv(F_i)，Y = T_base_board）
    #    未知数顺序：[Y 的 3×4（行主序：每行 3 个旋转项 + 1 个平移项）, t_probe(3)]
    rows: list[np.ndarray] = []
    constants: list[np.ndarray] = []
    for transform, point in zip(flange, points, strict=True):
        inverse = invert_transform(transform)
        homogeneous = np.append(point, 1.0).reshape(1, -1)
        block = np.zeros((3, 15), dtype=np.float64)
        block[:, :12] = np.kron(inverse[:3, :3], homogeneous)
        block[:, 12:15] = -np.eye(3)
        rows.append(block)
        constants.append(-inverse[:3, 3])  # M_i 的平移项是常数 → 移到右侧
    solution, *_ = np.linalg.lstsq(np.vstack(rows), np.concatenate(constants), rcond=None)
    # 线性初始化的旋转 / 平移只用来给出初值（下一段两类闭式解会把它收敛到真值）
    probe_tip = solution[12:15]

    # ② 交替优化（块坐标下降：两步都是闭式最优解）
    iterations = 0
    for iterations in range(1, PROBE_MAX_ITERATIONS + 1):
        touched = _apply_all(flange, probe_tip)  # 各次触碰的探针尖位置（基座系）
        transform, _ = fit_transform(points, touched)
        previous = probe_tip
        probe_tip = np.mean(
            [
                _apply(invert_transform(item), _apply(transform, point))
                for item, point in zip(flange, points, strict=True)
            ],
            axis=0,
        )
        if float(np.linalg.norm(probe_tip - previous)) < PROBE_TOLERANCE:
            break

    touched = _apply_all(flange, probe_tip)
    residual = np.linalg.norm(transform_points(transform, points) - touched, axis=1)
    return ProbeResult(
        transform=transform,
        probe_tip=probe_tip,
        rms_m=float(np.sqrt(np.mean(residual**2))),
        max_m=float(np.max(residual)),
        iterations=iterations,
        points_condition=_points_condition(points),
        residuals_m=residual,
    )


def self_test(seed: int = 0) -> dict:
    """虚拟数据端到端自测：**已知真值** → 走完整求解链 → 复现真值（误差应 ~1e-9）。

    这一步专门拦「约定错」：``AX = XB`` 的 A/B 取法、``T_A_B`` 方向、行/列主序、rpy 顺序写错了，
    真机上表现和「标定误差大」一模一样。**上真机前先跑它**。
    """
    rng = np.random.default_rng(seed)
    world_from_base = {"left": IDENTITY.copy(), "right": pose_to_transform([0.31, -0.02, 0.0, 0.0, 0.0, 0.12])}
    board_from_base = pose_to_transform([-0.25, 0.1, 0.02, 0.15, -0.2, 0.05])  # 板在工作台上的随机位姿
    flange_from_cam = pose_to_transform([0.041, -0.017, 0.083, 0.02, -0.35, 0.1])
    probe_tip = np.array([0.0, 0.0, 0.12], dtype=np.float64)

    # 腕相机的「同一个板、多个姿态」数据
    base_from_flange = []
    camera_from_board = []
    for _ in range(12):
        pose = np.concatenate([rng.uniform(-0.35, 0.35, size=3), rng.uniform(-0.6, 0.6, size=3)])
        transform = pose_to_transform(pose)  # 只当「IK 解出来的一组姿态」，不必可达
        base_from_flange.append(transform)
        world_from_cam = compose(world_from_base["left"], transform, flange_from_cam)
        camera_from_board.append(compose(invert_transform(world_from_cam), board_from_base))
    hand_eye = solve_hand_eye(base_from_flange, camera_from_board)

    # 探针触碰（两个臂各碰若干点）
    touches: dict[str, list] = {}
    touched_points: dict[str, list] = {}
    for arm in ("left", "right"):
        poses = []
        points = []
        for _ in range(5):
            pose = np.concatenate([rng.uniform(-0.3, 0.3, size=3), rng.uniform(-0.5, 0.5, size=3)])
            transform = pose_to_transform(pose)
            poses.append(transform)
            # 反推「这次触碰碰到的板点」：把探针尖从法兰系经基座系换到板系
            world_from_flange = compose(world_from_base[arm], transform)
            touches_base = transform_points(world_from_flange, probe_tip)
            points.append(transform_points(invert_transform(board_from_base), touches_base))
        touches[arm] = poses
        touched_points[arm] = points

    probe = {arm: solve_probe(touches[arm], touched_points[arm]) for arm in touches}
    # 期望值：各臂**自己的**基座系下的板外参（探针法给的就是这个系）
    expected_board = {arm: compose(invert_transform(world_from_base[arm]), board_from_base) for arm in touches}
    return {
        "hand_eye_error": float(np.max(np.abs(hand_eye.transform - flange_from_cam))),
        "hand_eye": hand_eye.as_dict(),
        "probe": {arm: result.as_dict() for arm, result in probe.items()},
        "board_error": {arm: float(np.max(np.abs(probe[arm].transform - expected_board[arm]))) for arm in probe},
        "probe_tip_error": {arm: float(np.max(np.abs(probe[arm].probe_tip - probe_tip))) for arm in probe},
    }


__all__ = [
    "PROBE_MAX_ITERATIONS",
    "PROBE_TOLERANCE",
    "HandEyeResult",
    "ProbeResult",
    "fit_transform",
    "self_test",
    "solve_hand_eye",
    "solve_probe",
]
