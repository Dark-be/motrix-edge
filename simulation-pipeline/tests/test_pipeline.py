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

from motrix_edge.adapter import discover_adapter
from motrix_edge.adapter.base import KEY_ACTION, KEY_QPOS
from motrix_edge.adapter.simulation_piper_adapter import SimulationPiperAdapter


@pytest.mark.parametrize("scene", ["piper_three_buttons", "stack_cubes"])
def test_scene_control_and_video(tmp_path, scene):
    workspace = Path(
        os.environ.get("SIMULATION_TEST_WORKSPACE", str(Path(__file__).resolve().parents[3] / "manipulation-tasks"))
    )
    if not workspace.is_dir():
        pytest.skip("Set SIMULATION_TEST_WORKSPACE to a manipulation-tasks checkout")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    shm = f"simulation_test_{os.getpid()}_{scene}"
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
                "tasks/press_three_buttons/session.toml",
                "--scene",
                f"assets/scenes/{scene}/scene.json",
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
                assert discovery["type"] == "simulation_piper"
                adapter = discover_adapter(host="127.0.0.1", port=port)
                assert isinstance(adapter, SimulationPiperAdapter)
                assert adapter.health().ok
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
                for action, layout in (([2], "gripper"), ([99] * 6, "joint"), ([0] * 5, "joint"), ([0] * 6, "pose")):
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
