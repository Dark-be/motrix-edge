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

"""从**合成样本**跑完整个解算链（``robot.calibration.pipeline.solve_frames``）→ 复现真值。

这是「上真机之前」最关键的一条用例：它把 ``--collect`` 该产出的样本结构、PnP、手眼 ``AX = XB``、
探针求解、`world` 锚定**串起来**跑一遍。硬件（相机 / 机械臂）不参与——真值是我们自己造的，所以
能精确断言误差量级；任何「约定写错」（``T_A_B`` 方向、行/列主序、rpy 顺序）都会在这里暴露。

对应的现场流程见 ``wiki/design/robot_pipeline_frames.md``「标定流程」。
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

from motrix_edge.geometry import (
    IDENTITY,
    compose,
    invert_transform,
    pose_to_transform,
    transform_points,
    transform_to_pose,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


@pytest.fixture(scope="module")
def calibration():
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.calibration")


@pytest.fixture(scope="module")
def cv2():
    return pytest.importorskip("cv2", reason="板投影 / PnP 需要 OpenCV")


def _synthetic_samples(calibration, cv2):
    """造一份「真机该采出来的」样本：两臂 + 一路固定相机 + 两路腕相机 + 探针触碰。

    真值（解算必须复现）：
    ``T_world_base(right)``、``T_world_cam(cam_head)``、``T_flange_cam(两路腕相机)``、探针尖。
    """
    board_module = importlib.import_module("robot.calibration.board")
    samples_module = importlib.import_module("robot.calibration.samples")
    spec = board_module.BoardSpec()
    points = board_module.object_points(spec)
    rng = np.random.default_rng(11)

    world_base = {"left": IDENTITY, "right": pose_to_transform([0.31, -0.02, 0.0, 0.0, 0.0, 0.12])}
    world_board = pose_to_transform([-0.25, 0.05, 0.02, 0.1, -0.15, 0.05])
    # 固定相机摆在**板前方** 0.9 m 并对准板（现实里相机装机身、看工作区）。合成数据也必须物理
    # 可行：「板在相机后方」的位姿会让 PnP 解出**镜像解**（重投影同样完美，外参完全错）。
    world_cam_head = compose(world_board, pose_to_transform([0.0, 0.0, 0.9, np.pi, 0.0, 0.0]))
    flange_cam = {
        "cam_left_wrist": pose_to_transform([0.04, -0.02, 0.08, 0.0, -0.35, 0.1]),
        "cam_right_wrist": pose_to_transform([0.05, 0.01, 0.07, 0.1, -0.30, -0.05]),
    }
    probe_tip = np.array([0.0, 0.0, 0.125])
    intrinsics = {
        name: {"fx": 605.0, "fy": 604.0, "cx": 320.0, "cy": 240.0}
        for name in ("cam_head", "cam_left_wrist", "cam_right_wrist")
    }
    matrix = np.array([[605.0, 0.0, 320.0], [0.0, 604.0, 240.0], [0.0, 0.0, 1.0]])
    distortion = np.zeros(5)

    samples = samples_module.Samples(
        board=spec,
        arms=["left", "right"],
        wrist_cameras={"cam_left_wrist": "left", "cam_right_wrist": "right"},
        world_arm="left",
        robot="synthetic",
    )
    samples.intrinsics = intrinsics

    def _project_unused(camera_world):
        camera_from_board = compose(invert_transform(camera_world), world_board)
        projected = cv2.projectPoints(
            points, cv2.Rodrigues(camera_from_board[:3, :3])[0], camera_from_board[:3, 3], matrix, distortion
        )[0].reshape(-1, 2)
        return camera_from_board, projected

    # 固定相机：**板不动**（标定全程板不动），多帧只用于平均降噪 → 加极小的像素噪声
    fixed_from_board = compose(invert_transform(world_cam_head), world_board)
    for _ in range(8):
        projected = cv2.projectPoints(
            points, cv2.Rodrigues(fixed_from_board[:3, :3])[0], fixed_from_board[:3, 3], matrix, distortion
        )[0].reshape(-1, 2)
        samples.views.append(
            samples_module.CameraView(
                camera="cam_head",
                corners=projected + rng.normal(0.0, 0.02, size=projected.shape),
                ids=np.arange(points.shape[0]),
                arm=None,
            )
        )

    # 腕相机：板不动、臂摆位姿（姿态要铺开 → 朝向差异 ≥ 20°）。
    # 只留「板真的在相机前方且在画面内」的位姿：随机姿态可能把板甩到相机后面，此时 PnP 会解出
    # 镜像解（重投影同样为 0），它是**数据问题**而不是求解问题——真机上靠「板在视野里」自然避免。
    for camera, arm in samples.wrist_cameras.items():
        accepted = 0
        while accepted < 8:
            base_from_flange = pose_to_transform(
                np.concatenate([rng.uniform(-0.3, 0.3, size=3), rng.uniform(-0.8, 0.8, size=3)])
            )
            world_cam = compose(world_base[arm], base_from_flange, flange_cam[camera])
            camera_from_board = compose(invert_transform(world_cam), world_board)
            board_in_camera = transform_points(camera_from_board, points)
            projected = cv2.projectPoints(
                points, cv2.Rodrigues(camera_from_board[:3, :3])[0], camera_from_board[:3, 3], matrix, distortion
            )[0].reshape(-1, 2)
            in_view = (board_in_camera[:, 2].min() > 0.05) and bool(
                np.all(
                    (projected[:, 0] > 0) & (projected[:, 0] < 640) & (projected[:, 1] > 0) & (projected[:, 1] < 480)
                )
            )
            if not in_view:
                continue
            samples.views.append(
                samples_module.CameraView(
                    camera=camera,
                    corners=projected,
                    ids=np.arange(points.shape[0]),
                    arm=arm,
                    pose=transform_to_pose(base_from_flange).tolist(),
                )
            )
            accepted += 1

    # 探针触碰：让探针尖正好落在板上的已知角点上（反推所需法兰平移）。
    # 角点要**在平面内铺开**（不同行 / 不同列）——共线时旋转解不可靠（见非共线度判据）。
    corner_ids = [0, 2, 3, 5, 6, 11]
    for arm in samples.arms:
        for corner_id in corner_ids:
            base_from_flange = pose_to_transform(
                np.concatenate([rng.uniform(-0.2, 0.2, size=3), rng.uniform(-0.7, 0.7, size=3)])
            )
            # T_world_base · FK · t_probe = T_world_board · p_board → 平移由等式反推
            point = points[corner_id]
            rotation = base_from_flange[:3, :3]
            desired_flange = compose(invert_transform(world_base[arm]), world_board, IDENTITY) @ np.append(point, 1.0)
            translation = desired_flange[:3] - rotation @ probe_tip
            base_from_flange[:3, 3] = translation
            samples.touches.append(
                samples_module.ProbeTouch(
                    arm=arm,
                    pose=transform_to_pose(base_from_flange).tolist(),
                    point=[float(value) for value in point],
                    label=f"corner_{corner_id}",
                )
            )

    truth = {
        "world_base": world_base,
        "world_cam_head": world_cam_head,
        "flange_cam": flange_cam,
        "world_board": world_board,
        "probe_tip": probe_tip,
    }
    return samples, truth


def test_solve_frames_reproduces_ground_truth(calibration, cv2):
    """合成样本 → ``solve_frames`` → 复现真值（误差在数值精度量级）。"""
    samples, truth = _synthetic_samples(calibration, cv2)
    samples.validate()
    frames = calibration.solve_frames(samples)

    assert frames.world == "left_base"
    # 左臂 = world（恒等）；右臂安装外参必须被解出来
    assert np.allclose(frames.arms["left"], truth["world_base"]["left"], atol=1e-6)
    assert np.allclose(frames.arms["right"], truth["world_base"]["right"], atol=1e-4)
    # 固定相机：T_world_cam（这一路注入了 0.02 px 像素噪声 → 误差在亚毫米量级，故容差放宽到 1 mm）
    assert np.allclose(frames.camera("cam_head").transform, truth["world_cam_head"], atol=1e-3)
    assert frames.camera("cam_head").mount == "fixed"
    # 腕相机：T_flange_cam（随动，运行期再乘 FK）
    for camera, expected in truth["flange_cam"].items():
        assert np.allclose(frames.camera(camera).transform, expected, atol=1e-4)
        assert frames.camera(camera).mount == "wrist"
    # 探针尖（法兰系）
    for arm in ("left", "right"):
        assert np.allclose(frames.probe_tip(arm), truth["probe_tip"], atol=1e-6)


def test_solve_frames_consistent_pixel_to_world(calibration, cv2):
    """同一物理点被三台相机各自反投影 → ``world`` 坐标互相接近（这就是验收口径）。"""
    samples, truth = _synthetic_samples(calibration, cv2)
    frames = calibration.solve_frames(samples)
    intrinsics = dict(fx=605.0, fy=604.0, cx=320.0, cy=240.0)
    # 一个工作区里的物理点：取板上某个角点，反算出它投影到各相机时的像素与深度
    target = transform_points(truth["world_board"], np.asarray([0.0, 0.0, 0.0]))
    poses = {arm: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for arm in ("left", "right")}
    points = []
    for camera, world_cam in (
        ("cam_head", truth["world_cam_head"]),
        ("cam_left_wrist", compose(truth["world_base"]["left"], IDENTITY, truth["flange_cam"]["cam_left_wrist"])),
        ("cam_right_wrist", compose(truth["world_base"]["right"], IDENTITY, truth["flange_cam"]["cam_right_wrist"])),
    ):
        camera_from_world = invert_transform(world_cam)
        xyz_camera = transform_points(camera_from_world, target)
        u_px = intrinsics["fx"] * xyz_camera[0] / xyz_camera[2] + intrinsics["cx"]
        v_px = intrinsics["fy"] * xyz_camera[1] / xyz_camera[2] + intrinsics["cy"]
        _, xyz_world = frames.pixel_to_world(camera, u_px, v_px, float(xyz_camera[2]), pose_by_arm=poses, **intrinsics)
        points.append(xyz_world)
    spread = max(
        float(np.linalg.norm(points[i] - points[j])) for i in range(len(points)) for j in range(i + 1, len(points))
    )
    assert spread < 5e-3, f"三台相机对同一物理点的一致性误差过大：{spread}"


def test_solve_frames_rejects_insufficient_touches(calibration, cv2):
    """触碰点不足 → 明确报错（不产出半份产物）。"""
    samples, _truth = _synthetic_samples(calibration, cv2)
    samples.touches = [touch for touch in samples.touches if touch.arm != "right"]
    with pytest.raises(ValueError, match="探针触碰不足"):
        calibration.solve_frames(samples)
