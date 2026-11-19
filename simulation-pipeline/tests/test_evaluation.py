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

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from motrix_simulation_pipeline.evaluation import EvaluationConfig, EvaluationError, load_policy, run_batch


def test_load_policy_returns_edge_infer_payload(tmp_path: Path):
    policy = tmp_path / "policy.yml"
    policy.write_text(
        """preset:
  id: switch
  label: switch
policy:
  type: lerobot-remote
  endpoint: tcp/127.0.0.1:7448
  deployment: switch-v1
  rtc:
    enabled: true
"""
    )

    policy_type, config, digest = load_policy(policy)

    assert policy_type == "lerobot-remote"
    assert config == {
        "endpoint": "tcp/127.0.0.1:7448",
        "deployment": "switch-v1",
        "rtc": {"enabled": True},
    }
    assert len(digest) == 64


@pytest.mark.parametrize(
    "source",
    [
        "policy: {type: lerobot-remote}\n",
        "preset: {}\npolicy: {}\n",
        "preset: {}\npolicy: []\n",
    ],
)
def test_load_policy_rejects_incomplete_contract(tmp_path: Path, source: str):
    policy = tmp_path / "policy.yml"
    policy.write_text(source)
    with pytest.raises(EvaluationError):
        load_policy(policy)


class EdgeHandler(BaseHTTPRequestHandler):
    lease_id: str | None = None
    session_type: str | None = None
    connected = False
    continuous = False
    preview_reads = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _read_json(self) -> dict[str, object]:
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size) or b"{}")
        assert isinstance(payload, dict)
        return payload

    def _send(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/v1/leases":
            self._send({"lease_id": type(self).lease_id, "leasable": type(self).lease_id is None})
        elif path == "/v1/health":
            self._send({"identity": {"X-Edge-Id": "edge-evaluation-test"}})
        elif path == "/v1/infers":
            self._send(
                {
                    "node_state": "ready" if type(self).session_type is None else "active",
                    "session_type": type(self).session_type,
                    "state": "ready",
                    "connected": type(self).connected,
                    "warmed_up": type(self).connected,
                    "warmup_error": None,
                    "continuous": type(self).continuous,
                    "policy_config": {"values": {"warmup_required": True}},
                }
            )
        elif path == "/v1/task/status":
            success = type(self).continuous
            self._send(
                {
                    "available": True,
                    "status": "success" if success else "pending",
                    "success": success,
                    "progress": [{"key": "rocker", "complete": success}],
                }
            )
        elif path == "/v1/preview":
            type(self).preview_reads += 1
            ready = type(self).preview_reads > 1
            self._send(
                {
                    "observation": {
                        "qpos": [0.0] * 7 if ready else None,
                        "images": ["cam_head"] if ready else [],
                    }
                }
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        body = self._read_json()
        if path == "/v1/leases":
            type(self).lease_id = str(body["lease_id"])
            self._send({"lease_id": type(self).lease_id})
        elif path == "/v1/infers":
            type(self).session_type = "infer"
            self._send({"status": "ok"})
        elif path == "/v1/infers/connect":
            type(self).connected = True
            self._send({"status": "ok"})
        elif path == "/v1/infers/rollout":
            type(self).continuous = body.get("mode") == "continuous"
            self._send({"status": "ok"})
        elif path == "/v1/infers/rollout/stop":
            type(self).continuous = False
            self._send({"status": "ok"})
        elif path.startswith("/v1/leases/") and path.endswith(":revoke"):
            type(self).lease_id = None
            self._send({"status": "ok"})
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:
        if urlsplit(self.path).path != "/v1/infers":
            self.send_error(404)
            return
        type(self).session_type = None
        type(self).connected = False
        type(self).continuous = False
        self._send({"status": "ok"})


@pytest.mark.parametrize("task", ["press_three_buttons", "turn_on_light_switch"])
def test_batch_runs_task_sampler_pipeline_and_edge_contract(tmp_path: Path, task: str):
    workspace = Path(__file__).resolve().parents[3] / "manipulation-tasks"
    task_python = workspace / ".venv/bin/python"
    session = workspace / "tasks" / task / "session.toml"
    if not workspace.is_dir() or not task_python.is_file() or not session.is_file():
        pytest.skip("requires the task in the adjacent manipulation-tasks workspace")
    sampler = subprocess.run(
        [str(task_python), "-m", f"tasks.{task}", "sample-scene", "--help"],
        cwd=workspace,
        capture_output=True,
        check=False,
    )
    if sampler.returncode != 0:
        pytest.skip("requires a manipulation-tasks revision with the sample-scene command")
    EdgeHandler.lease_id = None
    EdgeHandler.session_type = None
    EdgeHandler.connected = False
    EdgeHandler.continuous = False
    EdgeHandler.preview_reads = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), EdgeHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        pipeline_port = sock.getsockname()[1]
    policy = tmp_path / "policy.yml"
    policy.write_text(
        """preset: {id: test, label: test}
policy:
  type: lerobot-remote
  endpoint: tcp/127.0.0.1:7448
  deployment: test-v1
  camera_map:
    cam_head: observation.images.cam_head
"""
    )
    try:
        summary = run_batch(
            EvaluationConfig(
                workspace=workspace,
                task=task,
                policy=policy,
                output=tmp_path / "evaluation",
                seed_start=91,
                episodes=1,
                edge_url=f"http://127.0.0.1:{server.server_port}",
                pipeline_port=pipeline_port,
                startup_timeout=10,
                warmup_timeout=2,
                episode_timeout=2,
                poll_interval=0.02,
                render=False,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert summary["successes"] == 1
    assert summary["success_rate"] == 1
    result = json.loads((tmp_path / "evaluation/episodes/000000-seed-91/result.json").read_text())
    assert result["terminal"] is True
    assert result["status"] == "success"
    assert EdgeHandler.preview_reads >= 2
    assert (tmp_path / "evaluation/episodes/000000-seed-91/scene/scene.json").is_file()
