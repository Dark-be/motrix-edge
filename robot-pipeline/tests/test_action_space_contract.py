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

"""Robot HTTP 动作布局与遥操作模式契约。"""

from fastapi.testclient import TestClient
from robot.base_robot import BaseRobot
from server.contract_server import create_app

from motrix_edge.adapter.http_contract import TELEOP_MODES


class _Robot:
    ready = True
    teleop_enabled = False
    teleop_mode = None
    name = "test"
    SHM_NAME = "unused_action_space_contract"
    IMAGES = {}

    @staticmethod
    def action_dim():
        return 2


class _Env:
    def __init__(self):
        self.robot = _Robot()
        self.rollouts = []
        self.executes = []
        self.on_frame = None

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {
            "ready": True,
            "teleop_enabled": self.robot.teleop_enabled,
            "teleop_mode": self.robot.teleop_mode,
        }

    def robot_rollout(self, action, *, layout=None, arms=None, wait=False):
        self.rollouts.append(action)

    def robot_execute(self, action, *, layout=None, arms=None, wait=False):
        self.executes.append(action)


def test_teleop_modes_match_edge_contract():
    """robot 层保持契约无关，但两侧模式集合必须同步演进。"""
    assert set(BaseRobot.TELEOP_MODES) == set(TELEOP_MODES)


def test_legacy_action_space_field_is_rejected_instead_of_silently_ignored():
    """v6 契约改用 layout；旧 action_space 字段必须 422，不能静默按 joint 执行。"""
    env = _Env()
    with TestClient(create_app(env)) as client:
        rollout = client.post("/v1/rollout", json={"action": [1, 2], "action_space": "cartesian_pose"})
        execute = client.post("/v1/execute", json={"action": [1, 2], "action_space": "cartesian_pose"})

    assert rollout.status_code == 422
    assert execute.status_code == 422
    assert rollout.json()["detail"][0]["type"] == "extra_forbidden"
    assert execute.json()["detail"][0]["type"] == "extra_forbidden"
    assert env.rollouts == []
    assert env.executes == []


def test_joint_layout_remains_backward_compatible_without_field():
    env = _Env()
    with TestClient(create_app(env)) as client:
        response = client.post("/v1/rollout", json={"action": [1, 2]})

    assert response.status_code == 200
    assert env.rollouts == [[1.0, 2.0]]


def test_health_reports_authoritative_teleop_state():
    env = _Env()
    env.robot.teleop_enabled = True
    env.robot.teleop_mode = "delta"

    with TestClient(create_app(env)) as client:
        response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["mode"] == "delta"
