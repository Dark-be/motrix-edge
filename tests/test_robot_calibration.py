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

"""标定求解与产物的离线测试（**不需要硬件**；只有板检测那一组需要 OpenCV）。

钉住：① 求解器能用**已知真值的虚拟数据**复现外参（手眼 ``AX = XB`` / 探针触碰 / 点集配准）；
② 零空间解的标量倍率与 ``vec`` 约定（``c < 0`` 时不先归一化会解出一个完全错的结果——曾经的
真 bug）；③ ChArUco + ``solvePnP`` 全链路（合成投影 / 渲染图）；④ 样本 JSON 与产物
``frames.json`` 的读写与失败路径。设计见 ``wiki/design/robot_pipeline_frames.md``。
"""

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from motrix_edge.geometry import IDENTITY, compose, invert_transform, pose_to_transform, transform_points

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


@pytest.fixture(scope="module")
def calibration():
    """导入 robot-pipeline 的 ``robot.calibration``（纯 numpy；OpenCV 惰性导入）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.calibration")


@pytest.fixture(scope="module")
def store(calibration):
    return importlib.import_module("robot.calibration.store")


@pytest.fixture(scope="module")
def samples_module(calibration):
    return importlib.import_module("robot.calibration.samples")


@pytest.fixture(scope="module")
def board_module(calibration):
    return importlib.import_module("robot.calibration.board")


# ---- 求解器（虚拟数据自测）--------------------------------------------------


@pytest.mark.parametrize("seed", [0, 3, 7])
def test_self_test_recovers_ground_truth(calibration, seed):
    """完整求解链在虚拟数据上复现真值：手眼 / 探针 / 板外参 / 探针尖（误差 ~1e-9 以内）。"""
    report = calibration.self_test(seed)
    assert report["hand_eye_error"] < 1e-9
    assert report["hand_eye"]["rotation_rms_deg"] < 1e-4
    assert report["hand_eye"]["translation_rms_m"] < 1e-9
    assert report["hand_eye"]["pairs"] >= 10
    for arm in ("left", "right"):
        assert report["board_error"][arm] < 1e-9
        assert report["probe_tip_error"][arm] < 1e-9
        assert report["probe"][arm]["rms_m"] < 1e-9
        assert report["probe"][arm]["max_m"] < 1e-9


def test_solve_hand_eye_handles_negative_scalar_multiple(calibration):
    """回归：零空间向量的符号是任意的——``c < 0`` 时必须先按 ``det`` 归一化。

    ``det(c·R) = c³``，不先归一化就去投影「最近的旋转」在数学上**不唯一**（对 ``−R`` 处处等距），
    SVD 会返回一个完全错的解；这条用例用少量姿态提高撞上「负倍数」的概率。
    """
    flange_from_cam = pose_to_transform([0.041, -0.017, 0.083, 0.02, -0.35, 0.1])
    board_from_base = pose_to_transform([-0.25, 0.1, 0.02, 0.15, -0.2, 0.05])
    base_from_flange = []
    camera_from_board = []
    for index in range(3):
        pose = [0.1 * index, -0.05 * index, 0.02 * index, 0.2 * index, -0.3 * index, 0.5 * index]
        transform = pose_to_transform(pose)
        base_from_flange.append(transform)
        world_from_cam = compose(transform, flange_from_cam)
        camera_from_board.append(compose(invert_transform(world_from_cam), board_from_base))
    result = calibration.solve_hand_eye(base_from_flange, camera_from_board)
    assert np.allclose(result.transform, flange_from_cam, atol=1e-9)
    assert result.rotation_spread_deg > 20.0  # 姿态确实铺开了（否则该标定不可靠）


def test_solve_hand_eye_rejects_too_few_poses(calibration):
    """少于 2 个姿态无法定解 → 明确报错（不要返回一个「看起来像解」的东西）。"""
    with pytest.raises(ValueError, match="at least 2 poses"):
        calibration.solve_hand_eye([IDENTITY], [IDENTITY])


def test_solve_probe_needs_three_touches(calibration):
    """少于 3 次触碰无法定解（旋转 + 两个平移）→ 明确报错。"""
    with pytest.raises(ValueError, match="at least 3 touches"):
        calibration.solve_probe([IDENTITY, IDENTITY], [[0.0, 0.0, 0.0], [0.01, 0.0, 0.0]])


def test_solve_probe_reports_worst_touch(calibration):
    """``max_m`` 要能指出「哪一次触碰被碰歪了」——残差向量逐点可查。"""
    board_from_base = pose_to_transform([-0.2, 0.05, 0.0, 0.0, 0.0, 0.0])
    probe_tip = np.array([0.0, 0.0, 0.12])
    # 姿态 / 触碰点要**铺开**（不同位置 + 不同朝向）；近似共线时求解退化（下面单钉一条）
    poses = [
        pose_to_transform(pose)
        for pose in (
            [0.10, 0.02, 0.05, 0.30, -0.20, 0.40],
            [-0.12, 0.08, -0.03, -0.25, 0.35, -0.50],
            [0.05, -0.15, 0.10, 0.45, 0.15, 1.20],
            [0.16, 0.10, -0.08, -0.40, -0.30, 2.10],
            [-0.06, -0.10, 0.12, 0.10, 0.50, -1.60],
        )
    ]
    points = [transform_points(invert_transform(board_from_base), transform_points(pose, probe_tip)) for pose in poses]
    clean = calibration.solve_probe(poses, points)
    assert clean.rms_m < 1e-9 and clean.points_condition > 0.05
    # 第 3 个点被碰歪 2 mm（残差是**诊断量**：最小二乘会把单点扰动摊到所有点上，故残差 ≠ 扰动大小）
    points[2] = list(np.asarray(points[2]) + np.array([0.002, 0.0, 0.0]))
    result = calibration.solve_probe(poses, points)
    assert result.residuals_m.shape == (5,)
    assert int(np.argmax(result.residuals_m)) == 2
    assert result.max_m > 10 * max(clean.max_m, 1e-12)


def test_solve_probe_flags_collinear_points(calibration):
    """触碰点近似共线 → ``points_condition`` 接近 0（现场据此判「摆得太少，重采」）。

    姿态只沿一条线小幅平移时，触碰点几乎在直线上：残差可能看着很小，旋转解却不可靠。
    """
    probe_tip = np.array([0.0, 0.0, 0.12])
    poses = [pose_to_transform([0.02 * index, 0.0, 0.0, 0.0, 0.0, 0.0]) for index in range(5)]
    points = [transform_points(pose, probe_tip) for pose in poses]
    result = calibration.solve_probe(poses, points)
    assert result.points_condition < 0.05


def test_average_transforms_reports_spread(calibration):
    """多帧同一变换的平均：完全一致 → 离散度 0；加噪声后离散度如实上报（采样质量指标）。"""
    known = pose_to_transform([0.12, -0.05, 0.03, 0.2, -0.35, 1.1])
    clean, spread_deg, spread_m = calibration.average_transforms([known, known, known])
    assert np.allclose(clean, known, atol=1e-12) and spread_deg < 1e-9 and spread_m < 1e-12
    noisy = known.copy()
    noisy[:3, 3] = noisy[:3, 3] + np.array([0.01, 0.0, 0.0])
    mean, spread_deg, spread_m = calibration.average_transforms([known, noisy])
    assert spread_m == pytest.approx(0.005, abs=1e-6)
    assert np.allclose(mean[:3, :3], known[:3, :3], atol=1e-9)
    with pytest.raises(ValueError, match="at least one transform"):
        calibration.average_transforms([])


def test_fit_transform_matches_known_transform(calibration):
    """点集配准（Umeyama，无缩放）能复现已知刚体变换；点太少 → 报错。"""
    known = pose_to_transform([0.1, -0.2, 0.3, 0.2, -0.1, 0.4])
    source = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.0, 0.1, 0.0], [0.02, 0.03, 0.05]])
    fitted, rms = calibration.fit_transform(source, transform_points(known, source))
    assert np.allclose(fitted, known, atol=1e-12) and rms < 1e-12
    with pytest.raises(ValueError, match="at least 3 point pairs"):
        calibration.fit_transform(source[:2], source[:2])


# ---- 标定板（需要 OpenCV）----------------------------------------------------


@pytest.fixture(scope="module")
def cv2():
    return pytest.importorskip("cv2", reason="板检测需要 OpenCV（opencv-python-headless）")


def test_board_pnp_recovers_synthetic_pose(calibration, board_module, cv2):
    """合成投影 → ``solvePnP`` 复现 ``T_cam_board``（离线，不需要相机）。"""
    spec = board_module.BoardSpec()
    true = np.eye(4)
    true[:3, :3] = cv2.Rodrigues(np.array([0.2, -0.35, 0.1]))[0]
    true[:3, 3] = [0.03, -0.02, 0.6]
    intrinsics = {"fx": 600.0, "fy": 600.0, "cx": 450.0, "cy": 450.0}
    matrix = np.array([[600.0, 0.0, 450.0], [0.0, 600.0, 450.0], [0.0, 0.0, 1.0]])
    points = board_module.object_points(spec)
    projected = cv2.projectPoints(points, cv2.Rodrigues(true[:3, :3])[0], true[:3, 3], matrix, np.zeros(5))[0].reshape(
        -1, 2
    )
    solved, rms = board_module.pose_from_corners(projected, np.arange(points.shape[0]), spec, intrinsics)
    assert np.abs(solved - true).max() < 1e-6 and rms < 1e-6


def test_board_detection_on_rendered_image(calibration, board_module, cv2):
    """渲染板图 → 检测 → PnP：证明采集那一步的链路（角点 id ↔ 板几何）对得上。"""
    spec = board_module.BoardSpec()
    image = board_module.render(spec)
    corners, ids = board_module.detect(image, spec)
    assert corners.shape[0] == ids.shape[0] >= 4
    solved, rms = board_module.pose_from_corners(
        corners, ids, spec, {"fx": 600.0, "fy": 600.0, "cx": 450.0, "cy": 450.0}
    )
    assert rms < 1.0
    # 板正对相机且在其前方（渲染图的放置）
    assert solved[2, 3] > 0.0


def test_board_detect_returns_empty_when_nothing_found(calibration, board_module, cv2):
    """检不到板 → 返回空（调用方跳过该帧），不抛异常。"""
    corners, ids = board_module.detect(np.zeros((480, 640, 3), dtype=np.uint8), board_module.BoardSpec())
    assert corners.shape == (0, 2) and ids.shape == (0,)


# ---- 样本 JSON ---------------------------------------------------------------


def _sample_payload(samples_module) -> dict:
    return {
        "version": 1,
        "created_at": "2026-10-09T12:00:00",
        "robot": "dual_piper_001",
        "world_arm": "left",
        "arms": ["left", "right"],
        "wrist_cameras": {"cam_left_wrist": "left"},
        "board": samples_module.__dict__["BoardSpec"]().as_dict(),
        "intrinsics": {"cam_head": {"fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0}},
        "views": [
            {
                "camera": "cam_head",
                "arm": None,
                "pose": None,
                "ids": [0, 1, 2, 3],
                "corners": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]],
            }
        ],
        "touches": [{"arm": "left", "pose": [0.0] * 6, "point": [0.01, 0.02, 0.0], "label": "corner_0"}],
    }


def test_samples_roundtrip_and_summary(samples_module, tmp_path):
    """样本 JSON 往返 + 概况文字（现场先看「采够不够」再解算）。"""
    samples = samples_module.Samples.from_dict(_sample_payload(samples_module))
    path = samples.save(tmp_path / "samples.json")
    assert samples_module.Samples.load(path).as_dict() == samples.as_dict()
    summary = "\n".join(samples.summary())
    assert "cam_head" in summary and "探针触碰 1 点" in summary
    assert samples.cameras == ["cam_head"]


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda data: data.update(version=2), "unsupported samples version"),
        (lambda data: data.update(world_arm=""), "world_arm is required"),
        (lambda data: data.update(views=[]), "views is empty"),
        (lambda data: data.update(intrinsics={}), "intrinsics missing for camera"),
        (lambda data: data["views"][0].update(ids=[0, 1], corners=[[0.0, 0.0], [1.0, 1.0]]), "at least 4 corners"),
        (
            lambda data: data["intrinsics"].update(cam_left_wrist={"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0})
            or data["views"][0].update(camera="cam_left_wrist", arm="left", pose=None),
            "arm pose required",
        ),
        (lambda data: data["touches"][0].update(arm="middle"), "arm 'middle' not in"),
        (lambda data: data.update(wrist_cameras={"cam_left_wrist": "middle"}), "wrist_cameras: arms not in"),
    ],
)
def test_samples_validation_failures(samples_module, mutate, match):
    """结构校验：**哪一步缺数据就在哪一步报清楚**（不要留到解算时报数值错）。"""
    payload = _sample_payload(samples_module)
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        samples_module.Samples.from_dict(payload)


# ---- 产物读写（store）--------------------------------------------------------


def test_store_falls_back_to_packaged_artifact_then_local_wins(calibration, store):
    """本地无产物 → **只读回落**包内那份台位实测产物（带 tool / 时间，可追溯）；写出本地产物后
    本地优先（`effective_path()` 一比就能看出当前读的是哪一份）。"""
    import motrix_edge.geometry as geometry

    store.reset_cache()
    fallback = store.load_frames()
    assert fallback is not None, "包内应带一份实测产物——clone 后不改配置就能读坐标"
    assert store.effective_path() != store.frames_path(), "本地产物不存在时生效的应是包内那份"
    assert fallback.tool is not None and fallback.calibrated_at is not None, "包内产物要能追溯来源"
    assert sorted(fallback.cameras), "包内产物至少要有一台相机，否则回落没有意义"

    frames = geometry.FrameSet(
        arms={"left": IDENTITY},
        cameras={
            "cam_head": geometry.CameraExtrinsics("cam_head", geometry.MOUNT_FIXED, None, IDENTITY, 0.002),
            "cam_left_wrist": geometry.CameraExtrinsics(
                "cam_left_wrist", geometry.MOUNT_WRIST, "left", IDENTITY, 0.003
            ),
        },
    )
    path = store.save_frames(frames)
    assert path == store.frames_path() and path.exists()
    assert store.effective_path() == store.frames_path(), "本地产物一旦存在就压过包内那份"
    assert sorted(store.load_frames().cameras) == ["cam_head", "cam_left_wrist"]
    assert store.camera_extrinsics()["cam_left_wrist"]["mount"] == geometry.MOUNT_WRIST
    path.unlink(missing_ok=True)
    store.reset_cache()


def test_store_broken_payload_is_ignored_not_raised(store, capsys):
    """产物写坏 → 整份忽略 + 一条 WARNING（**不抛**）：机器人进程不该因此起不来。"""
    store.reset_cache()
    path = store.frames_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    store.reset_cache()
    assert store.load_frames() is None
    assert store.camera_extrinsics() == {}
    store.reset_cache()
    path.unlink(missing_ok=True)


def test_store_invalid_product_is_ignored(store):
    """版本不认识 / 旋转非法 → 同样忽略（而不是把半份产物用起来）。"""
    store.reset_cache()
    path = store.frames_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 99, "world": "left_base", "arms": {}, "cameras": {}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    store.reset_cache()
    assert store.load_frames() is None
    store.reset_cache()
    path.unlink(missing_ok=True)
