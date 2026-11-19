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

"""Run reproducible, process-isolated policy evaluation episodes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import yaml


class EvaluationError(RuntimeError):
    """One infrastructure or contract failure that invalidates an episode."""


class EvaluationPending(EvaluationError):
    """A transient transport failure while waiting for a process to become ready."""


@dataclass(frozen=True)
class EvaluationConfig:
    workspace: Path
    task: str
    policy: Path
    output: Path
    seed_start: int
    episodes: int
    edge_url: str = "http://127.0.0.1:8000"
    pipeline_host: str = "127.0.0.1"
    pipeline_port: int = 8090
    startup_timeout: float = 60.0
    warmup_timeout: float = 300.0
    episode_timeout: float = 60.0
    poll_interval: float = 0.1
    render: bool = True
    resume: bool = False
    task_python: Path | None = None


def load_policy(path: Path) -> tuple[str, dict[str, object], str]:
    """Load the same preset-shaped YAML used by Console and return its Edge payload."""
    source = path.read_bytes()
    payload = yaml.safe_load(source)
    if not isinstance(payload, dict) or set(payload) != {"preset", "policy"}:
        raise EvaluationError("policy YAML must contain exactly preset and policy objects")
    policy = payload["policy"]
    if not isinstance(policy, dict):
        raise EvaluationError("policy YAML policy must be an object")
    policy_type = policy.get("type")
    if not isinstance(policy_type, str) or not policy_type.strip():
        raise EvaluationError("policy YAML requires policy.type")
    config = {str(key): value for key, value in policy.items() if key != "type"}
    return policy_type.strip(), config, hashlib.sha256(source).hexdigest()


def _http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, object]:
    data = None if body is None else json.dumps(body).encode()
    request_headers = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise EvaluationError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise EvaluationPending(f"{method} {path} failed: {exc}") from exc
    except ValueError as exc:
        raise EvaluationError(f"{method} {path} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvaluationError(f"{method} {path} returned a non-object JSON response")
    return payload


def _wait(deadline: float, interval: float, probe, description: str) -> dict[str, object]:
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result is not None:
                return result
        except EvaluationPending as exc:
            last_error = str(exc)
        time.sleep(interval)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise EvaluationError(f"timed out waiting for {description}{suffix}")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _sample_scene(config: EvaluationConfig, episode: Path, seed: int) -> Path:
    task_python = config.task_python or config.workspace / ".venv/bin/python"
    if not task_python.is_file():
        raise EvaluationError(f"task Python does not exist: {task_python}")
    scene_dir = episode / "scene"
    command = [
        str(task_python),
        "-m",
        f"tasks.{config.task}",
        "sample-scene",
        "--seed",
        str(seed),
        "--output",
        str(scene_dir),
    ]
    completed = subprocess.run(command, cwd=config.workspace, text=True, capture_output=True, check=False)
    (episode / "scene-generator.log").write_text(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise EvaluationError(f"scene generator exited {completed.returncode}")
    scene = scene_dir / "scene.json"
    if not scene.is_file():
        raise EvaluationError("scene generator did not produce scene.json")
    return scene


def _start_pipeline(config: EvaluationConfig, episode: Path, scene: Path) -> tuple[subprocess.Popen, object]:
    pipeline_output = episode / "pipeline-output"
    log = (episode / "pipeline.log").open("w")
    command = [
        sys.executable,
        "-m",
        "motrix_simulation_pipeline",
        "--workspace",
        str(config.workspace),
        "--session",
        str(config.workspace / "tasks" / config.task / "session.toml"),
        "--scene",
        str(scene),
        "--task",
        config.task,
        "--output",
        str(pipeline_output),
        "--shm-name",
        f"simulation_eval_{os.getpid()}_{episode.name}",
        "--host",
        config.pipeline_host,
        "--port",
        str(config.pipeline_port),
    ]
    if not config.render:
        command.append("--no-render")
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[3], stdout=log, stderr=log)
    return process, log


def _pipeline_ready(config: EvaluationConfig, process: subprocess.Popen, log_path: Path) -> dict[str, object] | None:
    if process.poll() is not None:
        detail = log_path.read_text(errors="replace") if log_path.exists() else ""
        raise EvaluationError(f"pipeline exited {process.returncode}: {detail[-2000:]}")
    health = _http_json(f"http://{config.pipeline_host}:{config.pipeline_port}", "GET", "/v1/health", timeout=2)
    return health if health.get("ok") is True else None


def _install_lease(config: EvaluationConfig) -> str:
    status = _http_json(config.edge_url, "GET", "/v1/leases")
    if status.get("lease_id") is not None and status.get("leasable") is not True:
        raise EvaluationError(f"Edge already has lease {status['lease_id']}; refusing to take over")
    health = _http_json(config.edge_url, "GET", "/v1/health")
    identity = health.get("identity")
    edge_id = identity.get("X-Edge-Id") if isinstance(identity, dict) else None
    if not isinstance(edge_id, str) or not edge_id:
        raise EvaluationError("Edge health does not contain identity.X-Edge-Id")
    lease_id = f"ls_eval_{uuid4().hex}"
    ttl = config.startup_timeout + config.warmup_timeout + config.episode_timeout + 120
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    response = _http_json(
        config.edge_url,
        "POST",
        "/v1/leases",
        body={
            "lease_id": lease_id,
            "edge_id": edge_id,
            "holder_subject_id": "simulation-evaluator",
            "purpose": "rollout-evaluation",
            "state": "active",
            "expires_at": expires_at,
            "lease_version": 1,
            "ttl": ttl,
        },
    )
    if response.get("lease_id") != lease_id:
        raise EvaluationError("Edge returned a different lease ID")
    return lease_id


def _wait_edge_ready(config: EvaluationConfig, lease_id: str) -> dict[str, object]:
    status = _http_json(config.edge_url, "GET", "/v1/infers")
    if status.get("session_type") is not None:
        raise EvaluationError(f"Edge already has session {status['session_type']}; refusing to take over")
    if status.get("node_state") == "error":
        _http_json(
            config.edge_url,
            "POST",
            "/v1/commands",
            body={"command_id": f"eval-reset-{uuid4().hex}", "lease_id": lease_id, "capability": "node/reset"},
        )

    def probe() -> dict[str, object] | None:
        current = _http_json(config.edge_url, "GET", "/v1/infers")
        return current if current.get("node_state") == "ready" and current.get("session_type") is None else None

    return _wait(time.monotonic() + config.startup_timeout, config.poll_interval, probe, "Edge READY")


def _wait_observation_ready(
    config: EvaluationConfig,
    lease_id: str,
    policy_config: dict[str, object],
) -> dict[str, object]:
    camera_map = policy_config.get("camera_map")
    required_cameras = set(camera_map) if isinstance(camera_map, dict) else set()
    headers = {"X-Lease-Id": lease_id}

    def probe() -> dict[str, object] | None:
        preview = _http_json(config.edge_url, "GET", "/v1/preview", headers=headers, timeout=2)
        observation = preview.get("observation")
        if not isinstance(observation, dict):
            return None
        qpos = observation.get("qpos")
        images = observation.get("images")
        if not isinstance(qpos, list) or not qpos:
            return None
        available_cameras = set(images) if isinstance(images, list) else set()
        return preview if required_cameras <= available_cameras else None

    return _wait(time.monotonic() + config.startup_timeout, config.poll_interval, probe, "Edge observation")


def _run_inference(
    config: EvaluationConfig,
    lease_id: str,
    policy_type: str,
    policy_config: dict[str, object],
) -> tuple[str, dict[str, object]]:
    headers = {"X-Lease-Id": lease_id}
    _http_json(
        config.edge_url,
        "POST",
        "/v1/infers",
        body={"policy_type": policy_type, "config": policy_config},
        headers=headers,
    )
    _http_json(config.edge_url, "POST", "/v1/infers/connect", body={}, headers=headers)

    def warmed() -> dict[str, object] | None:
        status = _http_json(config.edge_url, "GET", "/v1/infers")
        if status.get("warmup_error"):
            raise EvaluationError(f"policy warmup failed: {status['warmup_error']}")
        policy_status = status.get("policy_config")
        values = policy_status.get("values") if isinstance(policy_status, dict) else None
        warmup_required = values.get("warmup_required", True) if isinstance(values, dict) else True
        ready = status.get("connected") is True and (warmup_required is False or status.get("warmed_up") is True)
        return status if ready else None

    _wait(time.monotonic() + config.warmup_timeout, config.poll_interval, warmed, "policy warmup")
    initial = _http_json(config.edge_url, "GET", "/v1/task/status")
    if initial.get("available") is not True:
        raise EvaluationError(f"task status provider unavailable for {config.task}")
    _http_json(
        config.edge_url,
        "POST",
        "/v1/infers/rollout",
        body={"mode": "continuous"},
        headers=headers,
    )
    deadline = time.monotonic() + config.episode_timeout
    final = initial
    while time.monotonic() < deadline:
        final = _http_json(config.edge_url, "GET", "/v1/task/status")
        if final.get("success") is True:
            return "success", final
        if final.get("status") == "failed":
            return "failed", final
        time.sleep(config.poll_interval)
    return "timeout", final


def _best_effort_cleanup(config: EvaluationConfig, lease_id: str | None) -> None:
    if lease_id is None:
        return
    headers = {"X-Lease-Id": lease_id}
    try:
        status = _http_json(config.edge_url, "GET", "/v1/infers", timeout=2)
        if status.get("continuous") is True:
            _http_json(config.edge_url, "POST", "/v1/infers/rollout/stop", body={}, headers=headers, timeout=5)
        if status.get("session_type") == "infer":
            _http_json(config.edge_url, "DELETE", f"/v1/infers?lease_id={lease_id}", timeout=10)
    except EvaluationError:
        pass
    try:
        _http_json(config.edge_url, "POST", f"/v1/leases/{lease_id}:revoke", body={}, timeout=5)
    except EvaluationError:
        pass


def _stop_pipeline(process: subprocess.Popen | None, log) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if log is not None:
        log.close()


def run_episode(
    config: EvaluationConfig,
    index: int,
    seed: int,
    policy_type: str,
    policy_config: dict[str, object],
    policy_digest: str,
) -> dict[str, object]:
    episode = config.output / "episodes" / f"{index:06d}-seed-{seed}"
    result_path = episode / "result.json"
    if result_path.is_file() and config.resume:
        result = json.loads(result_path.read_text())
        if isinstance(result, dict) and result.get("terminal") is True:
            return result
    if episode.exists():
        if not config.resume:
            raise EvaluationError(f"episode output already exists: {episode}")
        episode.rename(episode.with_name(f"{episode.name}.incomplete-{uuid4().hex[:8]}"))
    episode.mkdir(parents=True)
    started = time.monotonic()
    process = None
    log = None
    lease_id = None
    result: dict[str, object] = {
        "terminal": False,
        "index": index,
        "seed": seed,
        "task": config.task,
        "policy_type": policy_type,
        "policy_digest": policy_digest,
        "status": "infrastructure_error",
        "success": False,
    }
    try:
        scene = _sample_scene(config, episode, seed)
        process, log = _start_pipeline(config, episode, scene)
        _wait(
            time.monotonic() + config.startup_timeout,
            config.poll_interval,
            lambda: _pipeline_ready(config, process, episode / "pipeline.log"),
            "simulation pipeline",
        )
        lease_id = _install_lease(config)
        _wait_edge_ready(config, lease_id)
        _wait_observation_ready(config, lease_id, policy_config)
        status, task_status = _run_inference(config, lease_id, policy_type, policy_config)
        result.update(terminal=True, status=status, success=status == "success", task_status=task_status)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _best_effort_cleanup(config, lease_id)
        _stop_pipeline(process, log)
        result["elapsed_seconds"] = time.monotonic() - started
        _write_json(result_path, result)
    return result


def run_batch(config: EvaluationConfig) -> dict[str, object]:
    if config.episodes <= 0:
        raise EvaluationError("episodes must be positive")
    if not config.workspace.is_dir():
        raise EvaluationError(f"workspace does not exist: {config.workspace}")
    session = config.workspace / "tasks" / config.task / "session.toml"
    if not session.is_file():
        raise EvaluationError(f"task session does not exist: {session}")
    if config.output.exists() and not config.resume:
        raise EvaluationError(f"output already exists: {config.output}")
    policy_type, policy_config, policy_digest = load_policy(config.policy)
    config.output.mkdir(parents=True, exist_ok=config.resume)
    (config.output / "episodes").mkdir(exist_ok=True)
    results = [
        run_episode(config, index, config.seed_start + index, policy_type, policy_config, policy_digest)
        for index in range(config.episodes)
    ]
    with (config.output / "episodes.jsonl").open("w") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    successes = sum(result.get("success") is True for result in results)
    summary = {
        "task": config.task,
        "policy_type": policy_type,
        "policy_digest": policy_digest,
        "seed_start": config.seed_start,
        "episodes": len(results),
        "successes": successes,
        "success_rate": successes / len(results),
        "status_counts": {
            status: sum(result.get("status") == status for result in results)
            for status in sorted({str(result.get("status")) for result in results})
        },
    }
    _write_json(config.output / "summary.json", summary)
    return summary


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--episodes", type=int, required=True)
    parser.add_argument("--edge-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pipeline-host", default="127.0.0.1")
    parser.add_argument("--pipeline-port", type=int, default=8090)
    parser.add_argument("--startup-timeout", type=_positive_float, default=60.0)
    parser.add_argument("--warmup-timeout", type=_positive_float, default=300.0)
    parser.add_argument("--episode-timeout", type=_positive_float, default=60.0)
    parser.add_argument("--poll-interval", type=_positive_float, default=0.1)
    parser.add_argument("--task-python", type=Path)
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser


def _fail(parser: argparse.ArgumentParser, error: Exception) -> NoReturn:
    parser.error(str(error))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    config = EvaluationConfig(
        workspace=args.workspace.resolve(),
        task=args.task,
        policy=args.policy.resolve(),
        output=args.output.resolve(),
        seed_start=args.seed_start,
        episodes=args.episodes,
        edge_url=args.edge_url,
        pipeline_host=args.pipeline_host,
        pipeline_port=args.pipeline_port,
        startup_timeout=args.startup_timeout,
        warmup_timeout=args.warmup_timeout,
        episode_timeout=args.episode_timeout,
        poll_interval=args.poll_interval,
        render=not args.no_render,
        resume=args.resume,
        task_python=args.task_python.resolve() if args.task_python else None,
    )
    try:
        summary = run_batch(config)
    except (EvaluationError, OSError, ValueError, TypeError) as exc:
        _fail(parser, exc)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
