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

"""外部采集导入（``robot.calibration.external``）的测试。

钉住：① 外部样本（角点 + 位姿 + 板参 + 内参）能重算出腕相机与固定相机的外参——用**已知真值**
的虚拟外部会话，端到端复现；② 左臂锚定 = 「镜像位置 + 两底座同向」，且 ``plane="vertical"``
按「同底板 ⇒ 基座同高」把相机安装倾斜的 z 残差按公式消掉；③ 产物能通过 ``FrameSet`` 的校验
往返；④ 外部格式的失败路径（缺文件 / 类型不对 / 畸变非零 / 双相机会话缺 arm）都给出可读报错。

设计见 ``wiki/design/robot_pipeline_frames.md``「外部数据导入」。
"""

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from motrix_edge.geometry import (
    IDENTITY,
    MOUNT_FIXED,
    MOUNT_WRIST,
    CameraExtrinsics,
    FrameSet,
    invert_transform,
    transform_points,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"

#: 板参（现场那套 6×6 / 0.03 / 0.022 / DICT_5X5_100；**故意不等于**我们的缺省板参）
BOARD = {
    "board_type": "charuco",
    "dictionary": "DICT_5X5_100",
    "squares_x": 6,
    "squares_y": 6,
    "square_length_m": 0.03,
    "marker_length_m": 0.022,
    "legacy_pattern": False,
}
INTRINSICS = {"fx": 393.9, "fy": 393.6, "cx": 322.0, "cy": 240.8}

#: 真值：腕相机外参（``T_flange_cam``，取自现场数据，物理合理）
TRUE_WRIST_CAM = np.array(
    [
        [-0.027837882481, 0.938486689693, 0.344191495491, -0.076295247981],
        [-0.999560330694, -0.022618112136, -0.019172018831, 0.012843733157],
        [-0.010207722646, -0.344573873462, 0.938703706249, 0.038540217327],
        [0.0, 0.0, 0.0, 1.0],
    ]
)


@pytest.fixture(scope="module")
def external():
    """导入 ``robot.calibration.external``（纯 numpy + pyyaml；OpenCV 惰性导入）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.calibration.external")


@pytest.fixture(scope="module")
def board_module(external):
    return importlib.import_module("robot.calibration.board")


@pytest.fixture(scope="module")
def cv2():
    return pytest.importorskip("cv2", reason="PnP 需要 OpenCV（opencv-python-headless）")


# ---- 虚拟「外部采集」----------------------------------------------------------------


def _transform(rotation: np.ndarray, translation) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = np.asarray(rotation, dtype=np.float64)
    matrix[:3, 3] = np.asarray(translation, dtype=np.float64)
    return matrix


def _rotate(axis: str, angle: float) -> np.ndarray:
    """绕轴的 3×3 旋转（自建不求人：测试不该靠被测代码造数据）。"""
    cos, sin = float(np.cos(angle)), float(np.sin(angle))
    if axis == "x":
        return np.array([[1.0, 0.0, 0.0], [0.0, cos, -sin], [0.0, sin, cos]])
    if axis == "y":
        return np.array([[cos, 0.0, sin], [0.0, 1.0, 0.0], [-sin, 0.0, cos]])
    if axis == "z":
        return np.array([[cos, -sin, 0.0], [sin, cos, 0.0], [0.0, 0.0, 1.0]])
    raise ValueError(axis)


def _project(cam_from_object: np.ndarray, points: np.ndarray, intrinsics: dict) -> np.ndarray:
    """板点 → 像素（针孔、零畸变，与 ``pose_from_corners`` 的假设一致）。"""
    camera = transform_points(cam_from_object, points)
    if float(np.min(camera[:, 2])) <= 0.0:
        raise AssertionError("合成数据不物理：板点落到了相机后方")
    return np.stack(
        [
            intrinsics["fx"] * camera[:, 0] / camera[:, 2] + intrinsics["cx"],
            intrinsics["fy"] * camera[:, 1] / camera[:, 2] + intrinsics["cy"],
        ],
        axis=1,
    )


def _charuco(cam_from_board: np.ndarray, spec, intrinsics: dict) -> dict:
    from robot.calibration.board import object_points  # noqa: PLC0415 只在合成数据时需要

    points = np.asarray(object_points(spec), dtype=np.float64)
    corners = _project(cam_from_board, points, intrinsics)
    return {"ids": list(range(len(points))), "corners_xy": [[float(u), float(v)] for u, v in corners]}


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _intrinsics_yaml(path: Path, intrinsics: dict, *, distortion=(0.0,) * 5, image_geometry="rectified_color") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\n"
        "calibration_type: camera_intrinsics\n"
        "image_geometry: " + str(image_geometry) + "\n"
        "intrinsics:\n"
        "  camera_matrix:\n"
        f"  - [{intrinsics['fx']}, 0.0, {intrinsics['cx']}]\n"
        f"  - [0.0, {intrinsics['fy']}, {intrinsics['cy']}]\n"
        "  - [0.0, 0.0, 1.0]\n"
        "  distortion: [" + ", ".join(str(float(value)) for value in distortion) + "]\n",
        encoding="utf-8",
    )


class Truth:
    """虚拟现场的真值：右臂基座 = 参考系，head 相机 / 腕相机 / 板都摆在已知位置。"""

    def __init__(self, board_spec, *, camera_roll: float = 0.0) -> None:
        # head 相机：x 轴水平（``camera_roll`` 绕 y 拧一点 → x 轴离开水平面，用来验 plane 两种模式）
        self.head = _transform(_rotate("z", 0.3) @ _rotate("y", camera_roll) @ _rotate("x", np.pi), (0.07, 0.31, 0.68))
        self.wrist_cam = TRUE_WRIST_CAM.copy()
        self.board = _transform(_rotate("z", -0.05), (0.40, 0.05, -0.02))
        self.spec = board_spec
        # 左基座 = 「相对 head 相机镜像 + 同向」这个装配假设的**正解**（垂直对称面）
        self.left_origin = self.expected_left_origin()
        self.world_from_base_right = np.eye(4)
        self.world_from_base_right[:3, 3] = -self.left_origin

    def expected_left_origin(self) -> np.ndarray:
        """镜像公式的正解：对称面过相机光心、法向 = 相机横轴投影到水平面。"""
        base_from_head = invert_transform(self.head)
        lateral = np.asarray(base_from_head[:3, 0], dtype=np.float64)
        normal = np.array([lateral[0], lateral[1], 0.0])
        normal = normal / float(np.linalg.norm(normal))
        return 2.0 * float(np.dot(base_from_head[:3, 3], normal)) * normal

    def world_from_head(self) -> np.ndarray:
        return self.world_from_base_right @ self.head

    def wrist_flange_poses(self, count: int = 12, seed: int = 7):
        """构造一批法兰位姿：先取「板在相机前方 0.5 m」的 ``T_cam_board``，再反解法兰（``F = B · C⁻¹ · X⁻¹``）。"""
        rng = np.random.default_rng(seed)
        poses = []
        for _ in range(count):
            cam_from_board = np.eye(4)
            cam_from_board[:3, :3] = (
                _rotate("x", float(rng.uniform(-0.5, 0.5)))
                @ _rotate("y", float(rng.uniform(-0.5, 0.5)))
                @ _rotate("z", float(rng.uniform(-0.5, 0.5)))
            )
            cam_from_board[:3, 3] = [float(rng.uniform(-0.05, 0.05)), float(rng.uniform(-0.05, 0.05)), 0.5]
            # T_base_board = F · X · C  →  F = T_base_board · C⁻¹ · X⁻¹
            poses.append(self.board @ invert_transform(cam_from_board) @ invert_transform(self.wrist_cam))
        return poses


def _write_external(root: Path, truth: Truth, *, wrist_samples: int = 12) -> None:
    """按外部工具的目录布局把虚拟采集写到磁盘上。"""
    _intrinsics_yaml(root / "factory" / "right_wrist.yaml", INTRINSICS)
    _intrinsics_yaml(root / "factory" / "head.yaml", INTRINSICS)

    handeye_dir = root / "handeye_right" / "right_wrist" / "20260101T000000_000000Z"
    _dump(
        handeye_dir / "manifest.json",
        {
            "schema_version": 1,
            "session_type": "handeye",
            "camera": {"name": "right_wrist"},
            "arm": {"side": "right"},
            "board": BOARD,
        },
    )
    for index, flange in enumerate(truth.wrist_flange_poses(count=wrist_samples)):
        # 不变式：T_base_board = F · X · C  →  C = X⁻¹ · F⁻¹ · T_base_board
        cam_from_board = invert_transform(truth.wrist_cam) @ invert_transform(flange) @ truth.board
        _dump(
            handeye_dir / "samples" / f"sample_{index:04d}.json",
            {
                "schema_version": 1,
                "index": index,
                "charuco": _charuco(cam_from_board, truth.spec, INTRINSICS),
                "robot": {"T_base_gripper": flange.tolist()},
            },
        )

    bridge_dir = root / "head_via_wrist" / "right_wrist_to_head" / "20260101T000000_000000Z"
    _dump(
        bridge_dir / "manifest.json",
        {"schema_version": 1, "session_type": "head_via_wrist", "arm": {"side": "right"}, "board": BOARD},
    )
    head_from_board = invert_transform(truth.head) @ truth.board  # 相机固定 + 板固定 → 每帧相同
    charuco = _charuco(head_from_board, truth.spec, INTRINSICS)
    for index in range(3):
        _dump(
            bridge_dir / "samples" / f"sample_{index:04d}.json",
            {
                "schema_version": 1,
                "index": index,
                "cameras": {"head": {"charuco": charuco}},
                "robot": {"T_base_gripper": np.eye(4).tolist()},  # 固定相机这条链用不到位姿
            },
        )


# ---- 端到端 --------------------------------------------------------------------------


def test_import_recovers_camera_extrinsics_and_anchor(tmp_path, external, board_module, cv2):
    """已知真值的虚拟外部会话 → 用我们自己的链重算，腕相机 / 固定相机 / 左臂锚定都要复现。"""
    spec = external.board_spec_from_manifest({"board": BOARD})
    truth = Truth(spec)
    _write_external(tmp_path, truth)

    wrist_session = external.ExternalSession.load(
        tmp_path / "handeye_right" / "right_wrist" / "20260101T000000_000000Z"
    )
    bridge_session = external.ExternalSession.load(
        tmp_path / "head_via_wrist" / "right_wrist_to_head" / "20260101T000000_000000Z"
    )
    assert wrist_session.kind == external.KIND_HAND_EYE
    assert wrist_session.arm == "right"
    assert (wrist_session.board.squares_x, wrist_session.board.dictionary) == (6, "DICT_5X5_100")

    frames, report = external.import_frames(
        wrist_session=wrist_session,
        wrist_intrinsics=dict(INTRINSICS),
        fixed_session=bridge_session,
        fixed_intrinsics=dict(INTRINSICS),
    )

    # 1) 腕相机：X = T_flange_cam
    wrist = frames.cameras["cam_right_wrist"]
    assert wrist.mount == external.MOUNT_WRIST and wrist.arm == "right"
    assert np.allclose(wrist.transform, truth.wrist_cam, atol=1e-6)
    # 2) 固定相机：T_world_cam = T_world_base_right · T_base_head
    head = frames.cameras["cam_head"]
    assert head.mount == external.MOUNT_FIXED and head.arm is None
    assert np.allclose(head.transform, truth.world_from_head(), atol=1e-6)
    # 3) 左臂锚定：镜像 + 同向（旋转必须是单位阵）
    anchor = frames.arms["right"]
    assert np.allclose(anchor, truth.world_from_base_right, atol=1e-6)
    assert frames.arms["left"] is not None and np.allclose(frames.arms["left"], np.eye(4))
    assert np.allclose(frames.world_from_base("right"), truth.world_from_base_right, atol=1e-6)
    # 4) 帧名 / 溯源 / 不编造 probe_tip
    assert frames.world == external.WORLD_ALIAS
    assert frames.tool == external.TOOL
    assert frames.probe_tips == {}
    assert report.anchor.separation_m == pytest.approx(float(np.linalg.norm(truth.left_origin)), abs=1e-6)
    assert any("装配假设" in line for line in report.lines())


def test_imported_artifact_passes_frame_set_validation(tmp_path, external, board_module, cv2):
    """产物要能过 ``FrameSet`` 的整份校验（腕相机声明的臂必须在 ``arms`` 里）。"""
    spec = external.board_spec_from_manifest({"board": BOARD})
    truth = Truth(spec)
    _write_external(tmp_path, truth)
    frames, _ = external.import_frames(
        wrist_session=external.ExternalSession.load(
            tmp_path / "handeye_right" / "right_wrist" / "20260101T000000_000000Z"
        ),
        wrist_intrinsics=dict(INTRINSICS),
        fixed_session=external.ExternalSession.load(
            tmp_path / "head_via_wrist" / "right_wrist_to_head" / "20260101T000000_000000Z"
        ),
        fixed_intrinsics=dict(INTRINSICS),
    )
    path = frames.save(tmp_path / "frames.json")
    reloaded = FrameSet.load(path)
    assert np.allclose(reloaded.arms["right"], frames.arms["right"])
    assert np.allclose(reloaded.cameras["cam_head"].transform, frames.cameras["cam_head"].transform)
    assert reloaded.world == external.WORLD_ALIAS


def test_import_needs_fixed_session(tmp_path, external, board_module, cv2):
    """没有固定相机那一段就锚不上 world——必须明确报错，而不是产出一份看似正常的产物。"""
    spec = external.board_spec_from_manifest({"board": BOARD})
    truth = Truth(spec)
    _write_external(tmp_path, truth)
    with pytest.raises(external.ExternalDataError, match="固定相机|锚定"):
        external.import_frames(
            wrist_session=external.ExternalSession.load(
                tmp_path / "handeye_right" / "right_wrist" / "20260101T000000_000000Z"
            ),
            wrist_intrinsics=dict(INTRINSICS),
        )


def test_import_rejects_wrist_arm_as_world(tmp_path, external, board_module, cv2):
    """world 基准臂不能和腕相机所在臂相同（那样就没有「另一条臂」可锚）。"""
    spec = external.board_spec_from_manifest({"board": BOARD})
    truth = Truth(spec)
    _write_external(tmp_path, truth)
    with pytest.raises(external.ExternalDataError, match="world"):
        external.import_frames(
            wrist_session=external.ExternalSession.load(
                tmp_path / "handeye_right" / "right_wrist" / "20260101T000000_000000Z"
            ),
            wrist_intrinsics=dict(INTRINSICS),
            fixed_session=external.ExternalSession.load(
                tmp_path / "head_via_wrist" / "right_wrist_to_head" / "20260101T000000_000000Z"
            ),
            fixed_intrinsics=dict(INTRINSICS),
            world_arm="right",
        )


# ---- 锚定数学 ------------------------------------------------------------------------


def test_symmetric_anchor_is_a_vertical_mirror_of_the_base(external):
    """``plane="vertical"``：镜像前后同高（同底板），且关于对称面对称。"""
    base_from_head = _transform(_rotate("z", 0.25), (0.05, 0.30, 0.65))
    anchor = external.symmetric_anchor(base_from_head, plane="vertical")
    assert anchor.height_mm == pytest.approx(0.0, abs=1e-9)
    offset = -anchor.world_from_base[:3, 3]  # 左基座在「右基座系」里的位置
    assert offset[2] == pytest.approx(0.0, abs=1e-9)
    # 同向 ⇒ 旋转为单位阵；间距 = 2 × 到对称面的距离
    assert np.allclose(anchor.world_from_base[:3, :3], np.eye(3), atol=1e-12)
    assert anchor.separation_m == pytest.approx(2.0 * 0.05, abs=1e-9)


def test_plane_modes_differ_when_camera_lateral_axis_is_tilted(external):
    """相机横轴拧歪时：``camera`` 模式把倾斜漏进高度差，``vertical`` 模式按同高事实修正。"""
    base_from_head = _transform(_rotate("y", np.deg2rad(20.0)), (0.05, 0.30, 0.65))
    tilted = external.symmetric_anchor(base_from_head, plane="camera")
    vertical = external.symmetric_anchor(base_from_head, plane="vertical")
    assert tilted.tilt_deg == pytest.approx(20.0, abs=1e-6)
    assert abs(tilted.height_mm) > 1.0  # 倾斜漏进 z
    assert vertical.height_mm == pytest.approx(0.0, abs=1e-9)
    assert abs(vertical.separation_m - tilted.separation_m) > 1e-3


def test_symmetric_anchor_rejects_bad_arguments(external):
    base_from_head = _transform(_rotate("z", 0.1), (0.05, 0.30, 0.65))
    with pytest.raises(ValueError, match="axis"):
        external.symmetric_anchor(base_from_head, axis="z")
    with pytest.raises(ValueError, match="plane"):
        external.symmetric_anchor(base_from_head, plane="horizontal")
    # 相机横轴恰好竖直（横滚 90°）→ 推断不出对称面法向
    with pytest.raises(ValueError, match="水平面"):
        external.symmetric_anchor(_transform(_rotate("y", np.pi / 2), (0.0, 0.0, 0.6)))


# ---- 外部格式的失败路径 ---------------------------------------------------------------


def test_load_intrinsics_rejects_nonzero_distortion(tmp_path, external):
    path = tmp_path / "cam.yaml"
    _intrinsics_yaml(path, INTRINSICS, distortion=[-0.05, 0.0, 0.0, 0.0, 0.0])
    with pytest.raises(external.ExternalDataError, match="畸变"):
        external.load_intrinsics(path)


def test_load_intrinsics_reads_matrix(tmp_path, external):
    path = tmp_path / "cam.yaml"
    _intrinsics_yaml(path, INTRINSICS)
    assert external.load_intrinsics(path) == pytest.approx(INTRINSICS)


def test_board_spec_rejects_missing_or_wrong_type(external):
    with pytest.raises(external.ExternalDataError, match="board_type"):
        external.board_spec_from_manifest({"board": {"board_type": "chessboard"}})
    with pytest.raises(external.ExternalDataError, match="缺字段"):
        external.board_spec_from_manifest({"board": {"board_type": "charuco", "squares_x": 6}})


def test_session_load_rejects_unknown_type_and_missing_manifest(tmp_path, external):
    with pytest.raises(external.ExternalDataError, match="manifest"):
        external.ExternalSession.load(tmp_path / "nowhere")
    path = tmp_path / "session"
    _dump(path / "manifest.json", {"session_type": "something_else", "board": BOARD})
    with pytest.raises(external.ExternalDataError, match="session_type"):
        external.ExternalSession.load(path)
    _dump(path / "manifest.json", {"session_type": "handeye", "board": BOARD})
    with pytest.raises(external.ExternalDataError, match="arm"):
        external.ExternalSession.load(path)


def test_solve_wrist_rejects_bridge_session(tmp_path, external, board_module, cv2):
    """会话类型与求解入口不匹配时要直接报错（别把双相机样本当手眼样本喂进去）。"""
    spec = external.board_spec_from_manifest({"board": BOARD})
    truth = Truth(spec)
    _write_external(tmp_path, truth)
    bridge = external.ExternalSession.load(
        tmp_path / "head_via_wrist" / "right_wrist_to_head" / "20260101T000000_000000Z"
    )
    with pytest.raises(external.ExternalDataError, match="手眼"):
        external.solve_wrist(bridge, dict(INTRINSICS))
    with pytest.raises(external.ExternalDataError, match="wrist"):  # 键名写错 → 可读报错
        external.solve_fixed(bridge, dict(INTRINSICS), "wrist")


# ---- 左腕相机：从右腕继承（同件同向假设）---------------------------------------------


def test_inherit_wrist_camera_copies_transform_without_residual(external):
    """继承 = 照搬 ``T_flange_cam``、**不写 rms_m**（没有实测残差，宁缺勿假）+ 一句可打印的说明。"""
    frames = FrameSet(
        arms={"left": IDENTITY, "right": IDENTITY},
        cameras={
            "cam_right_wrist": CameraExtrinsics("cam_right_wrist", MOUNT_WRIST, "right", TRUE_WRIST_CAM, 0.005),
            "cam_head": CameraExtrinsics("cam_head", MOUNT_FIXED, None, IDENTITY, 0.002),
        },
    )
    note = external.inherit_wrist_camera(frames, source="cam_right_wrist", target="cam_left_wrist", arm="left")
    inherited = frames.cameras["cam_left_wrist"]
    assert (inherited.mount, inherited.arm) == (MOUNT_WRIST, "left")
    assert np.allclose(inherited.transform, TRUE_WRIST_CAM)
    assert inherited.rms_m is None
    assert "继承自 cam_right_wrist" in note and "同件同向" in note


@pytest.mark.parametrize(
    "source,target,arm,match",
    [
        ("cam_left_wrist", "cam_left_wrist", "left", "同名"),
        ("cam_head", "cam_x", "left", "不是已标定"),
        ("cam_right_wrist", "cam_x", "right", "另一条臂"),
    ],
)
def test_inherit_wrist_camera_rejects_bad_input(external, source: str, target: str, arm: str, match: str):
    """源不是腕相机 / 同臂继承 / 自己继承自己 → 明确报错（不静默产出一条假外参）。"""
    frames = FrameSet(
        arms={"left": IDENTITY, "right": IDENTITY},
        cameras={
            "cam_right_wrist": CameraExtrinsics("cam_right_wrist", MOUNT_WRIST, "right", TRUE_WRIST_CAM, 0.005),
            "cam_head": CameraExtrinsics("cam_head", MOUNT_FIXED, None, IDENTITY, 0.002),
        },
    )
    with pytest.raises(external.ExternalDataError, match=match):
        external.inherit_wrist_camera(frames, source=source, target=target, arm=arm)
