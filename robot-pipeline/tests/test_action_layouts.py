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

"""``/v1/rollout`` 的 ``layout`` / ``arms`` 契约在 env 层的表现（HTTP 线程侧，不启动控制 / 观测线程）。

钉住三件事：

- 通过校验后按 ``layout`` / ``arms`` 入队（动作 + 布局 + 臂名，逐臂「关节 6 + 夹爪 1」）；
- 参数非法 → ``ValueError``（server 映射 422），且**先于**遥操作判定——与 ``robot_rollout``
  的判定顺序一致；
- 遥操作（人工接管）中 → ``TakeoverActiveError``（server 映射 409），**不入队**。

控制线程侧的权威判定（``BaseRobot.plan_layout``）与「未选臂不补 home」的写入语义见
``test_state_layout.py``。
"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"


class _FakeCollector:
    """最小 collector：只接住 ``BaseEnv`` 构造期的注入（绕过 mcap 依赖）。

    与 ``test_capture_head_skip.py`` 里的桩**同表面**：``BaseEnv`` 在 ``env.base_env``
    导入时就绑定了 ``collector.get_collector``，哪个测试文件先导入就决定了整个会话用哪份桩，
    两份桩必须彼此兼容。
    """

    def __init__(self, cfg):
        self.cfg = dict(cfg or {})
        self.meta = dict(self.cfg.get("meta") or {})
        self.save_dir = Path(self.cfg.get("save_dir") or ".")
        self.calls: list[str] = []
        self.control_layout: dict = {}  # set_control_layout 注入的采集元信息（control_mode）

    def start(self):
        self.calls.append("start")

    def collect(self, observation):
        self.calls.append("collect")

    def finish(self):
        self.calls.append("finish")

    def cancel(self):
        self.calls.append("cancel")

    def set_meta(self, meta):
        self.meta.update(meta or {})

    def set_control_layout(self, layout):
        self.control_layout = dict(layout or {})


class _FakeRobot:
    """帧头跳过之外只用到：名字 / 布局回调 + 本次要验的判定（布局校验 + 运行位开关）。"""

    name = "fake_robot"
    ADAPTER_TYPE = "test_robot"

    def __init__(self):
        self.teleop_enabled = False
        self.pose_frame = "flange"  # 位姿系（set_tcp 后变 "tcp"）
        self.gravity_enabled = False  # 重力前馈运行位（set_gravity 后生效）
        self.gravity_supported = False  # 机型是否支持重力前馈（模拟「无控制器支持」）
        self.gravity_calls: list[tuple[bool, float | None]] = []
        self.tcp_calls: list[tuple[bool, object]] = []

    def state_layout(self):
        return {"state_space": "joint"}

    def control_layout(self):
        return {"mode": "mit+gravity" if self.gravity_enabled else "mit"}

    def set_tcp(self, enabled, offset=None):
        """同步切换（与真实 ``BaseRobot.set_tcp`` 同形）：先校验（只认 3 个有限数）再动运行位。"""
        if offset is not None:
            values = np.asarray(offset, dtype=np.float64).reshape(-1)
            if values.shape[0] != 3 or not np.all(np.isfinite(values)):
                raise ValueError("tcp offset must be 3 finite numbers")
        self.pose_frame = "tcp" if enabled else "flange"
        self.tcp_calls.append((bool(enabled), offset))

    def set_gravity(self, enabled, alpha=None):
        """同步切换（与真实 ``BaseRobot.set_gravity`` 同形）：不支持 → ``ValueError``。"""
        if not self.gravity_supported:
            raise ValueError("fake_robot: no controller supports gravity compensation")
        self.gravity_enabled = bool(enabled)
        self.gravity_calls.append((bool(enabled), alpha))
        return ["left_arm"]

    def plan_layout(self, action, layout=None, arms=None):
        """替身版校验：只认「右臂 + joint+gripper + 7 维」（与真实 ``BaseRobot.plan_layout`` 同形）。"""
        if layout != "joint+gripper" or list(arms or []) != ["right"]:
            raise ValueError("arms must be a nonempty unique subset of ['left', 'right']")
        values = np.asarray(action, dtype=np.float64)
        if values.shape != (7,) or not np.all(np.isfinite(values)):
            raise ValueError("action dim mismatch")
        return types.SimpleNamespace(
            layout="joint+gripper",
            segments=(("joint", 6), ("gripper", 1)),
            scope=["right"],
            blocks=values.reshape(1, 7),
        )


@pytest.fixture()
def env_module():
    """导入 ``env.base_env``：先打桩 collector 包（宿主机不装 mcap），**用完恢复 sys.modules**。"""
    stub = types.ModuleType("collector")
    stub.get_collector = lambda cfg: _FakeCollector(cfg if isinstance(cfg, dict) else {})
    previous = sys.modules.get("collector")
    sys.modules["collector"] = stub
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    try:
        yield importlib.import_module("env.base_env")
    finally:
        if previous is None:
            sys.modules.pop("collector", None)
        else:
            sys.modules["collector"] = previous


def _env(env_module):
    robot = _FakeRobot()
    return env_module.BaseEnv(robot, {"type": "act_mcap", "save_dir": "/tmp/action-layouts"}), robot


def test_enqueues_layout_rollout_command(env_module):
    """``layout="joint+gripper"`` + ``arms`` 原样入队（未选臂由机器人保持）。"""
    env, _ = _env(env_module)
    action = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]

    env.robot_rollout(action, layout="joint+gripper", arms=["right"])

    cmd, payload = env.commands.get_nowait()
    assert cmd == "rollout"
    sent_action, layout, arms = payload
    assert sent_action == action
    assert layout == "joint+gripper"
    assert arms == ["right"]


def test_validation_error_precedes_teleop_refusal(env_module):
    """坏动作 + 遥操作中：**先**报参数错误（422 优先于 409）——与 ``robot_rollout`` 同序。"""
    env, robot = _env(env_module)
    robot.teleop_enabled = True

    with pytest.raises(ValueError, match="action dim"):
        env.robot_rollout([0.0] * 6, layout="joint+gripper", arms=["right"])
    assert env.commands.empty()  # 未入队


def test_robot_set_tcp_and_gravity_switch(env_module):
    """运行位开关（tcp / gravity）都**直接同步切换**：不入队，失败立即抛（server 回 422）。

    不入队是为了把「偏移非法 / 机型不支持」直接回给调用方——经队列执行的话回执早就发完了。
    """
    env, robot = _env(env_module)
    env.robot_set_tcp(True, [0.0, 0.0, 0.1])
    with pytest.raises(ValueError, match="3 finite numbers"):
        env.robot_set_tcp(True, [0.0, 0.1])
    assert robot.tcp_calls == [(True, [0.0, 0.0, 0.1])]  # 非法那条未生效
    assert robot.pose_frame == "tcp"  # 同步已生效
    assert env.commands.empty()  # tcp 也不入队

    with pytest.raises(ValueError, match="no controller supports"):
        env.robot_set_gravity(True, 0.5)  # 不支持 → 立即抛（server 回 422）
    assert robot.gravity_calls == []
    robot.gravity_supported = True
    env.robot_set_gravity(True, 0.5)
    assert robot.gravity_calls == [(True, 0.5)]  # 同步已生效
    assert env.commands.empty()


def test_gravity_switch_reinjects_control_layout(env_module):
    """重力开关生效后**立刻重注入**控制模式：否则后续 episode 的 ``control_mode`` 漏报。

    ``control_mode`` 是采集 JSON 的元信息（下游据此判断这段数据带不带重力前馈），只在
    ``connect()`` 注入一次的话，运行期开关后写出的数据会一直报旧模式。
    """
    env, robot = _env(env_module)
    assert env._collector.control_layout == {"mode": "mit"}  # 构造期基线（无前馈）
    robot.gravity_supported = True
    env.robot_set_gravity(True, 0.5)

    assert robot.gravity_calls == [(True, 0.5)]
    assert env._collector.control_layout == {"mode": "mit+gravity"}


def test_teleop_refuses_layout_rollout(env_module):
    """遥操作（人工接管）中 → ``TakeoverActiveError``（409），且**不入队**。"""
    env, robot = _env(env_module)
    robot.teleop_enabled = True

    with pytest.raises(env_module.TakeoverActiveError):
        env.robot_rollout([0.0] * 7, layout="joint+gripper", arms=["right"])
    assert env.commands.empty()


def test_http_action_request_rejects_legacy_action_space_key(env_module):
    """HTTP 侧同样 fail-loud：``ActionRequest`` 只认 ``layout``，旧键 → 校验错（不静默按 joint 解释）。"""
    models = importlib.import_module("server.contract_server")

    with pytest.raises(ValidationError):
        models.ActionRequest(action=[0.0] * 7, action_space="pose")
    assert models.ActionRequest(action=[0.0] * 7, layout="pose").layout == "pose"  # 新键正常


def test_tracked_motion_is_idempotent_and_confirmed_by_control_thread(env_module):
    env, robot = _env(env_module)
    calls = []
    robot.rollout = lambda action, **kwargs: calls.append(action) or True
    kwargs = dict(layout="joint+gripper", arms=["right"], command_id="motion-1")
    queued = env.robot_rollout([0.0] * 7, **kwargs)
    assert queued["execution_status"] == "queued"
    assert env.robot_rollout([0.0] * 7, **kwargs) == queued
    assert env.commands.qsize() == 1
    env._drain_commands()
    applied = env.motion_command("motion-1")
    assert applied["execution_status"] == "applied" and applied["motion_revision"] == 1
    assert env.robot_rollout([0.0] * 7, **kwargs) == applied
    assert env.commands.empty() and len(calls) == 1
    with pytest.raises(ValueError, match="reused"):
        env.robot_rollout([0.1] * 7, **kwargs)
    assert env.motion_command("missing") is None


@pytest.mark.parametrize(
    "outcome,status",
    [(False, "rejected"), (ValueError("IK failed"), "rejected"), (RuntimeError("driver failed"), "failed")],
)
def test_control_thread_failure_is_not_an_applied_motion(env_module, outcome, status):
    env, robot = _env(env_module)

    def rollout(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    robot.rollout = rollout
    env.robot_rollout([0.0] * 7, layout="joint+gripper", arms=["right"], command_id="failed-1")
    env._drain_commands()
    receipt = env.motion_command("failed-1")
    assert receipt["execution_status"] == status
    assert receipt["motion_revision"] == (1 if status == "failed" else 0)
    assert receipt["error"]


def test_control_snapshot_freezes_without_new_sampling_and_reset_is_tracked(env_module, monkeypatch):
    env, robot = _env(env_module)
    now = [10.0]
    monkeypatch.setattr(env_module.time, "monotonic", lambda: now[0])
    robot.seq = 0
    robot.step = lambda: None
    robot.reset = lambda: None

    def sample():
        robot.seq += 1
        return {"value": robot.seq}

    robot.sample_state = sample
    robot.standard_observation = lambda state: {"observations/qpos": [state["value"]]}
    env.robot_reset(command_id="reset-1")
    env.step_control()
    first = env.motion_snapshot()
    assert first["sample_seq"] == 1 and first["motion_revision"] == 1
    assert env.motion_command("reset-1")["execution_status"] == "applied"
    now[0] += 2
    assert env.motion_snapshot()["sample_age_s"] == 2
    assert env.motion_snapshot()["sample_seq"] == 1
    env.step_control()
    assert env.motion_snapshot()["sample_seq"] == 2 and env.motion_snapshot()["sample_age_s"] == 0


def test_motion_http_receipts_distinguish_queue_and_application(env_module):
    from fastapi.testclient import TestClient

    server = importlib.import_module("server.contract_server")
    env, robot = _env(env_module)
    robot.ready = True
    robot.rollout = lambda *args, **kwargs: True
    robot.reset = lambda: None
    client = TestClient(server.create_app(env))  # no lifespan/hardware threads
    body = {"action": [0.0] * 7, "layout": "joint+gripper", "arms": ["right"], "command_id": "http-1"}
    assert client.post("/v1/rollout", json=body).json()["execution_status"] == "queued"
    assert client.get("/v1/motion/commands/http-1").json()["execution_status"] == "queued"
    env._drain_commands()
    receipt = client.get("/v1/motion/commands/http-1").json()
    assert receipt["execution_status"] == "applied"
    assert client.post("/v1/rollout", json=body).json()["motion_revision"] == receipt["motion_revision"]
    assert env.commands.empty()
    assert client.get("/v1/motion/commands/missing").status_code == 404
    assert client.get("/v1/motion/state").status_code == 503
    assert client.post("/v1/reset", json={"command_id": "reset-http"}).json()["execution_status"] == "queued"
    assert client.post("/v1/execute", json=body).status_code == 422
    client.close()


def test_motion_receipt_capacity_preserves_pending_commands(env_module):
    env, robot = _env(env_module)
    robot.rollout = lambda *args, **kwargs: True
    kwargs = dict(layout="joint+gripper", arms=["right"])
    for index in range(256):
        env.robot_rollout([0.0] * 7, command_id=f"cmd-{index}", **kwargs)
    with pytest.raises(ValueError, match="capacity"):
        env.robot_rollout([0.0] * 7, command_id="extra", **kwargs)
    assert env.commands.qsize() == 256
    env._drain_commands()
    env.robot_rollout([0.0] * 7, command_id="extra", **kwargs)
    assert env.motion_command("cmd-0") is None
    assert env.motion_command("cmd-1")["execution_status"] == "applied"
