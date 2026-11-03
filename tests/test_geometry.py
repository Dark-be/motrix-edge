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

"""统一坐标系（``motrix_edge.geometry``）的离线测试：纯 numpy，不需要硬件 / OpenCV。

钉住四件事：① 变换数学自洽（``rpy`` 往返、组合 / 求逆、点变换、16 数平铺校验）；
② 与 ``robot/kinematics/transforms.py`` 的 ``rpy`` **逐字同约定**（两处实现不许漂移）；
③ 帧模型（``FrameSet``）的读写与校验失败路径；④ 像素 → 相机系 → ``world`` 的换算
（含腕相机的同拍位姿合成）。设计见 ``wiki/design/robot_pipeline_frames.md``。
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
    FrameError,
    FrameSet,
    as_transform,
    compose,
    flatten_transform,
    invert_transform,
    is_rotation,
    matrix_to_rpy,
    pixel_to_camera,
    pose_to_transform,
    rpy_to_matrix,
    transform_points,
    transform_to_pose,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


@pytest.fixture(scope="module")
def kinematics_transforms():
    """robot-pipeline 的 ``robot.kinematics.transforms``（纯 numpy）——用于交叉一致性。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("robot.kinematics.transforms")


def _sample_poses() -> list:
    return [
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.12, -0.05, 0.03, 0.2, -0.35, 1.1],
        [-0.4, 0.31, 0.02, -1.2, 0.7, -2.4],
        [0.05, 0.05, 0.4, 0.0, np.pi / 2, 0.3],  # 万向锁
    ]


# ---- 变换数学 ----------------------------------------------------------------


@pytest.mark.parametrize("pose", _sample_poses())
def test_pose_transform_roundtrip(pose):
    """``pose → 矩阵 → pose`` 往返（万向锁时约定 ``yaw = 0``，故只比 xyz + 旋转矩阵）。"""
    transform = pose_to_transform(pose)
    back = transform_to_pose(transform)
    assert np.allclose(back[:3], pose[:3], atol=1e-12)
    assert np.allclose(rpy_to_matrix(back[3:]), rpy_to_matrix(pose[3:]), atol=1e-12)
    assert is_rotation(transform[:3, :3])


@pytest.mark.parametrize("pose", _sample_poses())
def test_rpy_matches_robot_kinematics_convention(pose, kinematics_transforms):
    """与 ``robot/kinematics`` 的 ``rpy`` 约定**逐字一致**（含万向锁的 ``yaw = 0``）。

    两处实现（edge 运行期 / robot 运动学）必须同约定，否则「观测里看到的位姿」与「坐标换算用的
    位姿」会差一个顺序，真机上表现为「明明到位了，坐标却偏一大截」。
    """
    rotation = rpy_to_matrix(pose[3:])
    assert np.allclose(rotation, kinematics_transforms.rpy_to_matrix(pose[3:]), atol=1e-12)
    ours = matrix_to_rpy(rotation)
    theirs = kinematics_transforms.matrix_to_rpy(rotation)
    assert np.allclose(ours, theirs, atol=1e-12)


def test_compose_and_invert():
    """``compose(A, B) = A·B``（先 B 后 A）；``invert`` 是逆。"""
    a = pose_to_transform([0.1, 0.2, 0.3, 0.1, 0.2, 0.3])
    b = pose_to_transform([-0.2, 0.05, 0.1, -0.3, 0.1, 0.6])
    assert np.allclose(compose(a, b), a @ b)
    assert np.allclose(compose(), IDENTITY)
    assert np.allclose(compose(a, invert_transform(a)), IDENTITY, atol=1e-12)
    # 点变换与「先 B 后 A」一致
    point = np.array([0.03, -0.04, 0.5])
    expected = a @ (b @ np.append(point, 1.0))
    assert np.allclose(np.append(transform_points(compose(a, b), point), 1.0), expected)


def test_as_transform_validates():
    """16 数平铺解析 + 校验：非正交旋转 / 非法末行 / 个数不对都要报错（不要半份产物生效）。"""
    assert np.allclose(as_transform(flatten_transform(IDENTITY)), IDENTITY)
    with pytest.raises(ValueError, match="16 numbers"):
        as_transform([1.0, 2.0, 3.0])
    scaled = np.diag([2.0, 1.0, 1.0, 1.0])  # 旋转部分被拉伸
    with pytest.raises(ValueError, match="not a valid rotation"):
        as_transform(scaled)
    bad_row = IDENTITY.copy()
    bad_row[3, 0] = 0.5
    with pytest.raises(ValueError, match="last row"):
        as_transform(bad_row)


def test_pixel_to_camera_requires_intrinsics():
    """内参缺失 / 损坏 → 明确报错（不算一个数出来）。"""
    with pytest.raises(ValueError, match="invalid focal length"):
        pixel_to_camera(320, 240, 1.0, fx=0.0, fy=600.0, cx=320.0, cy=240.0)


def test_pixel_to_camera_deprojection():
    """反投影公式：主点 → 光轴；偏离主点 → 按 ``z/f`` 放大。"""
    assert np.allclose(pixel_to_camera(320, 240, 1.5, fx=600, fy=600, cx=320, cy=240), [0.0, 0.0, 1.5])
    assert np.allclose(pixel_to_camera(620, 90, 2.0, fx=500, fy=400, cx=320, cy=240), [1.2, -0.75, 2.0])


# ---- 帧模型 ------------------------------------------------------------------


def _frameset_payload() -> dict:
    return {
        "version": 1,
        "world": "left_base",
        "rpy_order": "zyx",
        "calibrated_at": "2026-10-09T12:00:00",
        "tool": "probe",
        "arms": {
            "left": {"T_world_base": flatten_transform(IDENTITY)},
            "right": {"T_world_base": flatten_transform(pose_to_transform([0.31, -0.02, 0.0, 0.0, 0.0, 0.12]))},
        },
        "cameras": {
            "cam_head": {
                "mount": MOUNT_FIXED,
                "T_world_cam": flatten_transform(pose_to_transform([-0.2, 0.0, 1.1, 0.1, -0.2, 0.05])),
                "rms_m": 0.002,
            },
            "cam_left_wrist": {
                "mount": MOUNT_WRIST,
                "arm": "left",
                "T_flange_cam": flatten_transform(pose_to_transform([0.04, -0.02, 0.08, 0.0, -0.35, 0.1])),
                "rms_m": 0.003,
            },
        },
        "table": None,
    }


def test_frameset_roundtrip_and_frames():
    """产物往返 + 帧名：固定相机 = ``world`` 别名，腕相机 = ``flange_<arm>``。"""
    frames = FrameSet.from_payload(_frameset_payload())
    assert frames.world == "left_base"
    assert frames.identity_world("left") and not frames.identity_world("right")
    assert frames.camera_frame("cam_head") == "left_base"
    assert frames.camera_frame("cam_left_wrist") == "flange_left"
    assert FrameSet.from_payload(frames.to_payload()).to_payload() == frames.to_payload()


def test_frameset_rejects_broken_payloads():
    """校验失败路径：版本 / 旋转 / 腕相机承载臂 / 安装方式——**整份拒绝**，不做部分采纳。"""
    payload = _frameset_payload()
    for mutate, match in (
        (lambda data: data.update(version=99), "unsupported calibration version"),
        (lambda data: data.update(world=""), "missing 'world'"),
        (lambda data: data.update(rpy_order="xyz"), "unsupported rpy order"),
        (lambda data: data["arms"]["left"].update(T_world_base=[1.0] * 16), "not a valid rotation"),
        (lambda data: data["cameras"]["cam_left_wrist"].update(arm="nope"), "needs 'arm' declared"),
        (lambda data: data["cameras"]["cam_head"].update(mount="ceiling"), "expected one of"),
    ):
        broken = json.loads(json.dumps(payload))
        mutate(broken)
        with pytest.raises(FrameError, match=match):
            FrameSet.from_payload(broken)


def test_frameset_load_missing_file_is_not_calibrated(tmp_path):
    """缺文件 = 未标定（``None``）——不是错误，也不是「用包内占位值」。"""
    assert FrameSet.load_if_exists(tmp_path / "missing.json") is None
    frames = FrameSet.from_payload(_frameset_payload())
    path = frames.save(tmp_path / "calibration" / "frames.json")
    assert FrameSet.load_if_exists(path).world == "left_base"
    with pytest.raises(FrameError, match="unreadable"):
        FrameSet.load(tmp_path / "nope.json")


def test_frameset_unknown_camera_and_arm():
    """查询未知相机 / 未标定臂 → 明确报错（列出已知项，便于排障）。"""
    frames = FrameSet.from_payload(_frameset_payload())
    with pytest.raises(FrameError, match="no extrinsics"):
        frames.world_from_camera("cam_right_wrist")
    with pytest.raises(FrameError, match="no T_world_base"):
        frames.world_from_base("right_arm")


def test_wrist_camera_needs_same_frame_pose():
    """腕相机：给同拍位姿才能合成 ``world``；缺位姿 → 报错（不拿别的拍位姿凑）。"""
    frames = FrameSet.from_payload(_frameset_payload())
    with pytest.raises(FrameError, match="same-frame pose required"):
        frames.world_from_camera("cam_left_wrist")
    matrix = frames.world_from_camera("cam_left_wrist", {"left": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})
    item = frames.camera("cam_left_wrist")
    assert np.allclose(matrix, item.transform, atol=1e-12)  # 基座恒等 + FK 恒等 → 就是法兰→相机


def test_pixel_to_world_fixed_and_wrist():
    """像素 → ``world``：固定相机直接用外参；腕相机随位姿变（同一点在两拍得到不同世界坐标）。"""
    frames = FrameSet.from_payload(_frameset_payload())
    intrinsics = dict(fx=600.0, fy=600.0, cx=320.0, cy=240.0)
    xyz_camera, xyz_world = frames.pixel_to_world("cam_head", 420, 240, 0.5, **intrinsics)
    assert np.allclose(xyz_camera, [0.083333, 0.0, 0.5], atol=1e-6)
    expected = transform_points(frames.camera("cam_head").transform, xyz_camera)
    assert np.allclose(xyz_world, expected, atol=1e-12)

    pose_a, pose_b = [0.0] * 6, [0.0, 0.0, 0.0, 0.0, 0.0, 0.4]
    _, world_a = frames.pixel_to_world("cam_left_wrist", 320, 240, 0.6, pose_by_arm={"left": pose_a}, **intrinsics)
    _, world_b = frames.pixel_to_world("cam_left_wrist", 320, 240, 0.6, pose_by_arm={"left": pose_b}, **intrinsics)
    assert not np.allclose(world_a, world_b)
    # 只给了非承载臂的位姿 → 同样视为「缺位姿」
    with pytest.raises(FrameError, match="same-frame pose required"):
        frames.pixel_to_world("cam_left_wrist", 320, 240, 0.6, pose_by_arm={"right": pose_a}, **intrinsics)


def test_camera_extrinsics_source_mount():
    """``CameraExtrinsics`` 只保存「安装方式 + 常数变换」，源帧名由 ``FrameSet`` 给。"""
    frames = FrameSet.from_payload(_frameset_payload())
    fixed = frames.camera("cam_head")
    wrist = frames.camera("cam_left_wrist")
    assert fixed.mount == MOUNT_FIXED and fixed.arm is None and fixed.rms_m == pytest.approx(0.002)
    assert wrist.mount == MOUNT_WRIST and wrist.arm == "left"
    assert isinstance(wrist.transform, np.ndarray) and wrist.transform.shape == (4, 4)


def test_frameset_empty_is_usable():
    """空帧集合（未标定）不该炸——只是什么都查不到。"""
    frames = FrameSet()
    assert frames.cameras == {} and frames.to_payload()["world"] == "left_base"
    with pytest.raises(FrameError):
        frames.world_from_camera("cam_head")


def test_camera_extrinsics_constructor_keeps_identity():
    """``CameraExtrinsics`` 显式构造（脚本侧用）——变换原样保留。"""
    item = CameraExtrinsics(name="cam_head", mount=MOUNT_FIXED, arm=None, transform=IDENTITY, rms_m=None)
    assert np.allclose(item.transform, IDENTITY) and item.rms_m is None
