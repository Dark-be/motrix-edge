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

"""R1 Pro ROS adapter declarations and inherited HTTP behavior."""

from types import SimpleNamespace

from motrix_edge.adapter import robot_adapters
from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_QPOS,
    KEY_QPOS_ERR,
    ActionSpace,
    AdapterCapability,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_ARMS,
    FIELD_LAYOUT,
    FIELD_META,
    PATH_CAPTURE_CANCEL,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_CAPTURE_SYNC,
    PATH_ROLLOUT,
)
from motrix_edge.adapter.r1pro_ros_adapter import R1ProRosAdapter


class _FakeHttp:
    def __init__(self):
        self.posts = []

    def post(self, path, json=None):
        self.posts.append((path, json))
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"status": "accepted"})


def _adapter() -> tuple[R1ProRosAdapter, _FakeHttp]:
    adapter = R1ProRosAdapter(name="Warehouse R1 Pro")
    http = _FakeHttp()
    adapter._http = http
    return adapter, http


def test_identity_layout_and_capabilities():
    adapter = R1ProRosAdapter(name="Warehouse R1 Pro")
    caps = adapter.capabilities

    assert (adapter.name, adapter.type) == ("Warehouse R1 Pro", "r1pro_ros")
    assert adapter.ARM_NAMES == ("left", "right")
    assert caps.action_dim == 14
    assert caps.action_dims == {
        "joint": 14,
        "joint_delta": 14,
        "gripper": 2,
        "gripper_delta": 2,
    }
    assert caps.action_spaces == ["joint", "joint_delta", "gripper", "gripper_delta"]
    assert caps.observation_keys == [
        KEY_QPOS,
        KEY_QPOS_ERR,
        KEY_ACTION,
        f"{CAMERA_PREFIX}cam_head_left",
        f"{CAMERA_PREFIX}cam_head_right",
        f"{CAMERA_PREFIX}cam_left_wrist",
        f"{CAMERA_PREFIX}cam_right_wrist",
    ]
    assert all(caps.supports(capability) for capability in AdapterCapability)
    assert adapter.SHM_NAME == "r1pro_ros_obs"


def test_all_rgb_cameras_use_uniform_vga_geometry_and_can_be_filtered():
    adapter = R1ProRosAdapter()

    assert list(adapter.IMAGES) == ["cam_head_left", "cam_head_right", "cam_left_wrist", "cam_right_wrist"]
    assert set(adapter.IMAGES.values()) == {(640, 480)}
    assert adapter.DEPTH_CAMERAS == ()

    adapter.configure(enabled_cameras=["cam_head_left", "cam_right_wrist"])
    assert adapter.images == ["cam_head_left", "cam_right_wrist"]
    assert adapter.capabilities.image_names == ["cam_head_left", "cam_right_wrist"]


def test_policy_target_is_forwarded_once_and_robot_server_owns_control_rate():
    adapter, http = _adapter()
    joint_target = [float(index) for index in range(14)]
    joint_gripper_target = [float(index) for index in range(16)]

    assert adapter.rollout(joint_target, layout=ActionSpace.JOINT.value) is True
    assert adapter.rollout(joint_gripper_target, layout="joint+gripper") is True
    assert http.posts == [
        (PATH_ROLLOUT, {FIELD_ACTION: joint_target, FIELD_LAYOUT: "joint"}),
        (PATH_ROLLOUT, {FIELD_ACTION: joint_gripper_target, FIELD_LAYOUT: "joint+gripper"}),
    ]


def test_explicit_arm_scope_does_not_require_invented_home_values():
    adapter, http = _adapter()
    right_arm_target = [0.1] * 7

    assert adapter.rollout(right_arm_target, layout="joint", arms=["right"]) is True
    assert http.posts == [
        (
            PATH_ROLLOUT,
            {FIELD_ACTION: right_arm_target, FIELD_LAYOUT: "joint", FIELD_ARMS: ["right"]},
        )
    ]


def test_capture_commands_only_control_robot_server_episode_lifecycle():
    adapter, http = _adapter()
    meta = {"operator": "tester", "task_name": "warehouse_sorting"}

    adapter.sync_capture_meta(meta)
    adapter.start_capture()
    adapter.end_capture()
    adapter.cancel_capture()

    assert http.posts == [
        (PATH_CAPTURE_SYNC, {FIELD_META: meta}),
        (PATH_CAPTURE_START, None),
        (PATH_CAPTURE_END, None),
        (PATH_CAPTURE_CANCEL, None),
    ]


def test_r1pro_ros_adapter_is_registered_as_entry_point():
    assert "r1pro_ros" in {adapter_type for adapter_type, _, _ in robot_adapters()}
