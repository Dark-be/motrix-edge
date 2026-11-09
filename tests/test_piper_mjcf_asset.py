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

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

MODEL_DIR = Path(__file__).parents[1] / "robot-pipeline" / "assets" / "robots" / "piper" / "mjcf" / "arm"
MODEL_PATH = MODEL_DIR / "piper.xml"


def test_piper_mjcf_has_complete_six_axis_model_and_resources():
    root = ET.parse(MODEL_PATH).getroot()

    joints = root.findall("./worldbody//joint")
    assert [joint.get("name") for joint in joints] == [f"joint{index}" for index in range(1, 7)]

    meshes = root.findall("./asset/mesh")
    assert [mesh.get("name") for mesh in meshes] == ["base_link", *(f"link{index}" for index in range(1, 7))]
    for mesh in meshes:
        resource = mesh.get("file")
        assert resource is not None
        assert (MODEL_DIR / resource).is_file()


def test_piper_mjcf_keeps_third_party_attribution():
    assert (MODEL_DIR / "LICENSE").is_file()
    notices = (MODEL_DIR / "NOTICES").read_text(encoding="utf-8")
    assert "https://github.com/agilexrobotics/agx_arm_urdf.git" in notices
    assert "680fa925cd00db7e2628563d2edc5540963045de" in notices


def test_piper_mjcf_uses_latest_official_limits_and_inertials():
    root = ET.parse(MODEL_PATH).getroot()
    expected_limits = np.array(
        [
            [-2.6179938, 2.6179938],
            [0.0, 3.1415926],
            [-2.9670597, 0.0],
            [-1.7453292, 1.7453292],
            [-1.2217304, 1.2217304],
            [-2.9670597, 2.9670597],
        ]
    )
    joints = root.findall("./worldbody//joint")
    actuators = root.findall("./actuator/position")
    np.testing.assert_allclose([np.fromstring(joint.get("range", ""), sep=" ") for joint in joints], expected_limits)
    np.testing.assert_allclose(
        [np.fromstring(actuator.get("ctrlrange", ""), sep=" ") for actuator in actuators], expected_limits
    )

    inertials = root.findall("./worldbody//inertial")
    np.testing.assert_allclose(
        [float(inertial.get("mass", "nan")) for inertial in inertials], [0.71, 1.16, 0.5, 0.38, 0.383, 0.01655408133]
    )
    assert all(inertial.get("fullinertia") is not None for inertial in inertials)


def _quat_wxyz_to_matrix(quaternion: np.ndarray) -> np.ndarray:
    w, x, y, z = quaternion
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def _axis_z_rotation(angle: float) -> np.ndarray:
    cosine, sine = np.cos(angle), np.sin(angle)
    transform = np.eye(4)
    transform[:3, :3] = ((cosine, -sine, 0), (sine, cosine, 0), (0, 0, 1))
    return transform


def _mjcf_fk(joints: np.ndarray) -> np.ndarray:
    body = ET.parse(MODEL_PATH).getroot().find("./worldbody/body")
    transform = np.eye(4)
    for angle in joints:
        assert body is not None
        fixed = np.eye(4)
        fixed[:3, 3] = np.fromstring(body.get("pos", "0 0 0"), sep=" ")
        fixed[:3, :3] = _quat_wxyz_to_matrix(np.fromstring(body.get("quat", "1 0 0 0"), sep=" "))
        transform = transform @ fixed @ _axis_z_rotation(float(angle))
        body = body.find("./body")
    return transform


def test_piper_mjcf_matches_latest_official_urdf_frames():
    # 由固定版本 agx_arm_urdf 独立生成的法兰变换黄金值；测试中不再维护另一套 DH 实现。
    samples = (
        (
            np.zeros(6),
            [
                [0.087155742748, 0.0, 0.996194698092, 0.056135204443],
                [0.0, 1.0, 0.0, 0.0],
                [-0.996194698092, 0.0, 0.087155742748, 0.213178344954],
                [0.0, 0.0, 0.0, 1.0],
            ],
        ),
        (
            np.array([0.2, 0.5, -1.0, 0.3, -0.2, 0.4]),
            [
                [0.428200951341, -0.561336820982, 0.708198361111, 0.032377081682],
                [0.738584887304, 0.668950201413, 0.083654003342, 0.001137894741],
                [-0.520707508604, 0.487243882915, 0.70104000531, 0.513224290098],
                [0.0, 0.0, 0.0, 1.0],
            ],
        ),
        (
            np.array([-0.5, 1.0, -1.5, -0.7, 0.4, -1.0]),
            [
                [-0.694481014653, 0.176854652652, 0.697437131304, 0.130531826204],
                [-0.719290038154, -0.194672335391, -0.666876692385, -0.097383256252],
                [0.017831469346, -0.964792782761, 0.262406030861, 0.562527034552],
                [0.0, 0.0, 0.0, 1.0],
            ],
        ),
    )
    for joints, official_transform in samples:
        np.testing.assert_allclose(_mjcf_fk(joints), official_transform, atol=1e-11)
