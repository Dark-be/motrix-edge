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

"""Mock robot SDK 与真实 robot-pipeline HTTP 契约的回归测试。"""

import sys
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_robot_sdk import SimRobotCore, create_sdk_app  # noqa: E402


def test_rollout_rejects_unknown_action_space_before_execution():
    """未知动作空间不能静默按 joint 执行。"""
    core = SimRobotCore(action_dim=14, random_walk=False)

    with pytest.raises(ValueError, match="action space 'typo' not supported"):
        core.rollout([0.0] * 14, "typo")

    assert core.rollout_calls == 0
    assert core.rollout_spaces == []


def test_rollout_rejects_cartesian_until_real_robot_contract_supports_it():
    """mock 不得比真实 robot-pipeline 提前宣告 cartesian_pose 能力。"""
    core = SimRobotCore(action_dim=14, random_walk=False)

    with pytest.raises(ValueError, match="cartesian_pose.*not supported"):
        core.rollout([0.0] * 14, "cartesian_pose")

    assert core.rollout_calls == 0


def test_http_execute_and_rollout_share_action_space_rejection_order():
    """mock HTTP 与真实 server 一致：execute/rollout 皆拒绝非 joint，且 422 优先于 teleop 409。"""
    core = SimRobotCore(action_dim=14, images=["cam"], camera_size=(2, 2), random_walk=False)
    app = create_sdk_app(core, shm_name=f"test_robot_sdk_{uuid.uuid4().hex}", run_hz=100)
    action = [0.0] * 14

    with TestClient(app) as client:
        execute = client.post("/v1/execute", json={"action": action, "action_space": "cartesian_pose"})
        execute_empty_space = client.post("/v1/execute", json={"action": action, "action_space": ""})
        execute_bad_dim = client.post("/v1/execute", json={"action": action[:-1]})
        teleop_bad_mode = client.post("/v1/teleop", json={"enabled": True, "mode": "fast"})
        core.set_teleop(True, "delta")
        rollout_space = client.post("/v1/rollout", json={"action": action, "action_space": "cartesian_pose"})
        rollout_bad_dim = client.post("/v1/rollout", json={"action": action[:-1]})
        rollout_teleop = client.post("/v1/rollout", json={"action": action})

    assert execute.status_code == 422
    assert "available: ['joint']" in execute.json()["detail"]
    assert execute_empty_space.status_code == 422
    assert execute_bad_dim.status_code == 422
    assert teleop_bad_mode.status_code == 422
    assert rollout_space.status_code == 422
    assert rollout_space.json()["detail"] == execute.json()["detail"]
    assert rollout_bad_dim.status_code == 422
    assert rollout_teleop.status_code == 409
    assert core.executed == []
    assert core.rollout_calls == 0


def test_program_control_commands_end_teleop_and_action_observation_is_interpolated():
    """mock 状态机与真 robot 一致：程控抢回 teleop，action 上报限速插值值而非 target。"""
    core = SimRobotCore(action_dim=14, images=["cam"], camera_size=(2, 2), random_walk=False)
    target = np.full(14, 0.5)

    core.set_teleop(True, "delta")
    core.execute(target)
    assert core.teleop_enabled is False
    np.testing.assert_allclose(core.frame()["action"], np.zeros(14))
    core.step()
    np.testing.assert_allclose(core.frame()["action"], np.full(14, core.step_rad))

    core.set_teleop(True, "delta")
    core.reset()
    assert core.teleop_enabled is False

    core.set_teleop(True, "delta")
    core.safe_stop()
    assert core.teleop_enabled is False
    frame = core.frame()
    np.testing.assert_allclose(frame["action"], frame["observations/qpos"])


@pytest.mark.parametrize(
    ("action_dim", "has_pose"),
    [(14, True), (2, False)],
)
def test_discover_observation_keys_match_actual_shm_layout(action_dim, has_pose):
    """discover 始终声明 qpos/action，pose 则与 pose_dim/SHM v3 是否启用一致。"""
    core = SimRobotCore(action_dim=action_dim, images=["cam"], camera_size=(2, 2), random_walk=False)
    app = create_sdk_app(core, shm_name=f"test_robot_sdk_{uuid.uuid4().hex}", run_hz=100)

    with TestClient(app) as client:
        keys = client.post("/v1/discover").json()["robot"]["observation_keys"]

    assert "observations/qpos" in keys
    assert "action" in keys
    assert ("observations/pose" in keys) is has_pose
    assert "observations/images/cam" in keys
