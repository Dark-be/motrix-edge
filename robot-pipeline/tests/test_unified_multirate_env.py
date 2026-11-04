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

"""Unified BaseEnv multirate control, state, per-camera and action production."""

import time
from pathlib import Path

import env.base_env as env_module
import numpy as np
from utils.observation_time import KEY_TIMESTAMPS_NS

from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_QPOS, KEY_QPOS_ERR


class Robot:
    name = "multirate-test"
    ADAPTER_TYPE = "test_robot"
    IMAGE_NAMES = ("head", "wrist")
    step_rad = 0.1
    teleop_enabled = False
    last_error = None
    ready = False

    def __init__(self):
        self.on_command = None
        self.motion_state = None
        self.seq = 0
        self.state_count = 0
        self.command_count = 0
        self.camera_count = {name: 0 for name in self.IMAGE_NAMES}
        self.disconnected = False

    def state_layout(self):
        return {"state_space": "joint"}

    def control_layout(self):
        return {"mode": "test"}

    def connect(self):
        self.ready = True

    def disconnect(self):
        self.ready = False
        self.disconnected = True

    def step(self):
        self.command_count += 1
        if self.on_command is not None:
            self.on_command(np.full(12, self.command_count), np.array([0.2, 0.8]), time.time_ns())

    def sample_state(self):
        self.state_count += 1
        self.seq += 1
        now = time.time_ns()
        values = np.full(14, self.state_count, dtype=float)
        self.motion_state = {
            KEY_QPOS: values,
            KEY_QPOS_ERR: np.zeros(14),
            KEY_ACTION: values,
            KEY_TIMESTAMPS_NS: {KEY_QPOS: now, KEY_ACTION: now},
        }
        return self.motion_state

    def standard_observation(self, state):
        return dict(state)

    def interleave_state(self, joints, gripper):
        joints = np.asarray(joints).reshape(2, 6)
        gripper = np.asarray(gripper).reshape(2, 1)
        return np.concatenate([np.concatenate((joint, grip)) for joint, grip in zip(joints, gripper, strict=True)])

    def get_observation_frame(self, name):
        self.camera_count[name] += 1
        now = time.time_ns()
        return {
            "color": np.full((2, 3, 3), self.camera_count[name], dtype=np.uint8),
            "depth": None,
            "timestamp_ns": now,
            "timestamps_ns": {"color": now},
            "timestamp_metadata": {},
        }

    def frames_observation(self, frames):
        timestamps = {}
        result = {KEY_TIMESTAMPS_NS: timestamps}
        for name, frame in frames.items():
            key = CAMERA_PREFIX + name
            result[key] = frame["color"]
            timestamps[key] = frame["timestamps_ns"]["color"]
        return result

    def build_observation(self, frames=None):
        if self.motion_state is None:
            return None
        return {**self.motion_state, **(frames or {})}

    def teleop_master_sample(self):
        return None


class Collector:
    def __init__(self, config):
        self.save_dir = Path(config["save_dir"])
        self.meta = {}
        self.events = []

    def set_robot_meta(self, **meta):
        self.meta.update(meta)

    def set_state_layout(self, layout):
        self.meta.update(layout)

    def set_control_layout(self, layout):
        self.meta["control"] = layout

    def set_meta(self, meta):
        self.meta.update(meta)

    def start(self):
        self.events.append("start")

    def collect_qpos(self, event):
        self.events.append(("state", event))

    def collect_action(self, event):
        self.events.append(("action", event))

    def collect_images(self, event):
        self.events.append(("image", event))

    def finish(self):
        self.events.append("finish")

    def cancel(self):
        self.events.append("cancel")


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_unified_env_records_independent_state_action_and_camera_streams(tmp_path, monkeypatch):
    robot = Robot()
    monkeypatch.setattr(env_module, "get_collector", lambda config: Collector(config))
    env = env_module.BaseEnv(
        robot,
        {"type": "act_mcap", "save_dir": str(tmp_path), "skip_until_motion": {"enabled": False}},
        {
            "enabled": True,
            "control_hz": 80,
            "follower_state_hz": 60,
            "observation_hz": 20,
            "reference_control_hz": 30,
            "max_joint_speed_rad_s": 2.0,
            "recording": {"queue_size": 32},
            "cameras": {
                "profiles": {
                    "head": {"read_hz": 25},
                    "wrist": {"read_hz": 15},
                }
            },
        },
    )
    env.on_frame = lambda observation: None

    env.start()
    try:
        wait_until(lambda: all(robot.camera_count.values()) and robot.state_count > 0 and robot.command_count > 0)
        env.robot_capture_start()
        wait_until(lambda: env._recorder.active)
        time.sleep(0.15)
        env.robot_capture_end()
        wait_until(lambda: not env._episode_open)
    finally:
        env.stop()

    summary = env._recorder.last_summary
    assert summary is not None and not summary["errors"]
    assert summary["accepted"].get("state", 0) > 0, (summary, env.last_error, robot.state_count, robot.command_count)
    assert summary["accepted"]["action"] > 0
    assert summary["accepted"]["image:head"] > summary["accepted"]["image:wrist"] > 0
    assert robot.step_rad == 2.0 / 80
    assert robot.disconnected
