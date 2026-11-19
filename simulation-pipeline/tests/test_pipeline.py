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

"""Real SDK scene, HTTP control and Edge shared-memory adapter integration."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import numpy as np
import pytest
from motrix_simulation_pipeline.piper_kinematics import PIPER_LIMITS, PiperKinematics

from motrix_edge.adapter import discover_adapter
from motrix_edge.adapter.base import KEY_ACTION, KEY_QPOS
from motrix_edge.adapter.simulation_piper_adapter import SimulationPiperAdapter
from motrix_edge.geometry.transforms import pose_to_transform, transform_to_pose


@pytest.mark.parametrize("task", ["press_three_buttons", "stack_color_blocks", "turn_on_light_switch"])
def test_scene_control_and_video(tmp_path, task):
    workspace = Path(
        os.environ.get("SIMULATION_TEST_WORKSPACE", str(Path(__file__).resolve().parents[3] / "manipulation-tasks"))
    )
    if not workspace.is_dir():
        pytest.skip("Set SIMULATION_TEST_WORKSPACE to a manipulation-tasks checkout")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    shm = f"simulation_test_{os.getpid()}_{task}"
    endpoint = f"http://127.0.0.1:{port}"
    adapter = None
    with (tmp_path / "server.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "motrix_simulation_pipeline",
                "--workspace",
                str(workspace),
                "--session",
                f"tasks/{task}/session.toml",
                "--scene",
                f"assets/scenes/{task}/scene.json",
                "--output",
                str(tmp_path / "run"),
                "--shm-name",
                shm,
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
        )
        try:
            with httpx2.Client(base_url=endpoint, timeout=3) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    try:
                        health = client.get("/v1/health").json()
                        assert health["detail"] in ("", "starting or stalled"), health
                        if health["ok"]:
                            break
                    except httpx2.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail((tmp_path / "server.log").read_text())
                discovery = client.post("/v1/discover").json()["robot"]
                assert discovery["capabilities"]["capture"] is False
                assert discovery["capabilities"]["task_status"] is (task != "stack_color_blocks")
                assert discovery["type"] == "simulation_piper"
                assert discovery["action_dims"]["pose"] == 6
                assert discovery["pose_frame"] == "tcp"
                assert discovery["pose_reference_frame"] == "world"
                assert discovery["tcp_offset"] == {"right": [0.0, 0.0, 0.13]}
                assert "pose+gripper" in {layout["name"] for layout in discovery["action_layouts"]}
                adapter = discover_adapter(host="127.0.0.1", port=port)
                assert isinstance(adapter, SimulationPiperAdapter)
                assert adapter.health().ok
                assert adapter.pose_frame() == "tcp"
                assert adapter.tcp_offset_by_arm()["right"] == pytest.approx([0.0, 0.0, 0.13])
                adapter_task_status = adapter.task_status()
                assert adapter_task_status is not None
                assert adapter_task_status["available"] is (task != "stack_color_blocks")
                task_status = client.get("/v1/task/status").json()
                assert task_status["available"] is (task != "stack_color_blocks")
                assert task_status["task"] == task
                assert task_status["success"] is False
                if task == "press_three_buttons":
                    assert task_status["expected_order"] == ["pink", "green", "blue"]
                    assert set(task_status["buttons"]) == {"pink", "green", "blue"}
                if task == "turn_on_light_switch":
                    assert task_status["criteria_version"] == "turn_on_light_switch_v2"
                    assert task_status["progress"][0]["key"] == "rocker"
                obs = None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    obs = adapter.observe()
                    if obs is not None:
                        import cv2

                        images = [
                            cv2.imdecode(
                                np.frombuffer(obs[f"observations/images/{name}"], dtype=np.uint8), cv2.IMREAD_COLOR
                            )
                            for name in ("cam_head", "cam_right_wrist")
                        ]
                        if all(image is not None and image.std() > 2 for image in images):
                            break
                    time.sleep(0.1)
                else:
                    pytest.fail("no valid two-camera observation: " + (tmp_path / "server.log").read_text())
                initial = np.asarray(obs[KEY_QPOS]).copy()
                target = np.asarray(obs[KEY_ACTION]).copy()
                target[0] += 0.08
                target[6] = 0.5
                result = adapter.submit_motion(target, command_id="move-1", layout="joint+gripper")
                assert result["execution_status"] == "queued"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    obs = adapter.observe()
                    if (
                        obs is not None
                        and abs(obs[KEY_QPOS][0] - target[0]) < 0.02
                        and abs(obs[KEY_QPOS][6] - 0.5) < 0.03
                    ):
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail(f"actual state did not reach target: {obs}")
                assert adapter.motion_command("move-1")["execution_status"] == "applied"
                assert adapter.motion_snapshot()["sample_age_s"] < 2
                kinematics = PiperKinematics(PIPER_LIMITS)
                pose_joint_target = np.asarray(obs[KEY_QPOS])[:6].copy()
                pose_joint_target[0] = np.clip(
                    pose_joint_target[0] + 0.03,
                    PIPER_LIMITS[0, 0] + 0.01,
                    PIPER_LIMITS[0, 1] - 0.01,
                )
                pose_target_base = kinematics.pose(pose_joint_target)
                world_from_base = np.asarray(health["world_from_base"], dtype=float).reshape(4, 4)
                pose_target = transform_to_pose(world_from_base @ pose_to_transform(pose_target_base))
                pose_result = adapter.submit_motion(
                    [*pose_target, 0.4], command_id="pose-1", layout="pose+gripper", arms=["right"]
                )
                resolved_pose_joints = np.asarray(pose_result["resolved_action"][:6])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    obs = adapter.observe()
                    if obs is not None and np.max(np.abs(np.asarray(obs[KEY_QPOS])[:6] - resolved_pose_joints)) < 0.03:
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail(f"pose target did not reach resolved joints: {obs}")
                same = client.post(
                    "/v1/execute", json={"action": target.tolist(), "layout": "joint+gripper", "command_id": "move-1"}
                )
                assert same.status_code == 200
                assert (
                    client.post(
                        "/v1/execute", json={"action": [0.8], "layout": "gripper", "command_id": "move-1"}
                    ).status_code
                    == 422
                )
                for action, layout in (([2], "gripper"), ([99] * 6, "joint"), ([0] * 5, "joint")):
                    assert client.post("/v1/rollout", json={"action": action, "layout": layout}).status_code == 422
                assert client.post("/v1/capture/start").status_code == 404
                stop = client.post("/v1/safe_stop").json()
                time.sleep(0.2)
                assert client.get("/v1/motion/commands/" + stop["command_id"]).json()["execution_status"] == "applied"
                adapter.submit_reset(command_id="reset-1")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    obs = adapter.observe()
                    if obs is not None and np.max(np.abs(np.asarray(obs[KEY_QPOS]) - initial)) < 0.04:
                        break
                    time.sleep(0.1)
                else:
                    pytest.fail("reset did not restore initial robot posture")
        finally:
            if adapter is not None:
                adapter.release()
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                pytest.fail("server failed to shut down")
        assert process.returncode in (0, -signal.SIGTERM), (tmp_path / "server.log").read_text()
        from multiprocessing.shared_memory import SharedMemory

        with pytest.raises(FileNotFoundError):
            SharedMemory(name=shm)
