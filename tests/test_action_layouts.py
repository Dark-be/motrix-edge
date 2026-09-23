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

_REPO_ROOT = Path(__file__).resolve().parents[1]
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

    def start(self):
        self.calls.append("start")

    def collect(self, observation):
        self.calls.append("collect")

    def finish(self):
        self.calls.append("finish")

    def set_meta(self, meta):
        self.meta.update(meta or {})


class _FakeRobot:
    """帧头跳过之外只用到：名字 / 布局回调 + 本次要验的两个判定。"""

    name = "fake_robot"
    ADAPTER_TYPE = "test_robot"

    def __init__(self):
        self.teleop_enabled = False

    def state_layout(self):
        return {"state_space": "joint"}

    def control_layout(self):
        return {"mode": "mit"}

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
