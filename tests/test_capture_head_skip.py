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

"""采集「帧头跳过」：遥操作（示教 / 接管）录制时，主臂未出现有效移动前不记录、也不开 episode。

用例按 ``step_observe()`` 的顺序驱动观测线程的两个入口（``_drain_capture_commands`` →
``_update_capture``）；collector 打桩（宿主机未装 mcap，不碰真实落盘）。
"""

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_Q0 = np.array([0.1] * 12)
_G0 = np.array([1.0, 1.0])


class _FakeCollector:
    """最小 collector：只记调用序列（绕过 mcap 依赖）。"""

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
    """帧头跳过只用到：机器人名 + 遥操作开关 + 最近主臂读数。"""

    def __init__(self):
        self.name = "fake_robot"
        self.teleop_enabled = False
        self.sample: tuple[np.ndarray, np.ndarray] | None = None  # (关节, 夹爪)

    def teleop_master_sample(self):
        return self.sample if self.teleop_enabled else None


@pytest.fixture(scope="module")
def base_env_module():
    """导入 ``env.base_env``：先打桩 collector 包（宿主机未装 mcap）。"""
    stub = types.ModuleType("collector")
    stub.get_collector = lambda cfg: _FakeCollector(cfg if isinstance(cfg, dict) else {})
    sys.modules["collector"] = stub
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module("env.base_env")


def _build(base_env_module, **collector_cfg):
    robot = _FakeRobot()
    cfg = {"type": "act_mcap", "save_dir": "/tmp/head-skip", **collector_cfg}
    env = base_env_module.BaseEnv(robot, cfg)
    env.observation = {"qpos": [0.0] * 12}
    return env, robot, env._collector


def _tick(env):
    """一拍观测线程：drain 采集命令（生效 capturing / 武装闸门）→ 按 capturing 记录。"""
    env._drain_capture_commands()
    env._update_capture()


def test_records_from_first_frame_without_teleop(base_env_module):
    """非遥操作（如 rollout 录制）：不启用帧头跳过，首帧即记录。"""
    env, robot, collector = _build(base_env_module)
    robot.sample = (_Q0, _G0)  # 有主臂读数但遥操作未开启 → 不武装
    env.robot_capture_start()
    _tick(env)
    env.robot_capture_end()
    _tick(env)

    assert collector.calls == ["start", "collect", "finish"]
    assert env.capture_status()["head_skip"] is None


def test_skips_idle_head_until_master_moves(base_env_module):
    """遥操作录制：主臂不动 → 不记录（也不开 episode）；出现有效移动后开始记录，之后微小位移照常记录。"""
    env, robot, collector = _build(base_env_module)
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)
    env.robot_capture_start()

    _tick(env)  # 第 1 拍：与首帧完全一致
    robot.sample = (_Q0 + 0.01, _G0)  # 位移 0.01 < 阈值 0.05
    _tick(env)  # 第 2 拍：仍在跳过
    assert collector.calls == []  # 连 episode 都没开（文件此时不存在）
    assert env.capture_status()["head_skip"] == {"skipped": 2}

    robot.sample = (_Q0 + 0.4, _G0)  # 超过阈值 → 本拍即开始记录
    _tick(env)
    robot.sample = (_Q0 + 0.41, _G0)  # 解锁后的微小位移照常记录
    _tick(env)
    env.robot_capture_end()
    _tick(env)

    assert collector.calls == ["start", "collect", "collect", "finish"]
    assert env.capture_status()["head_skip"] is None


def test_gripper_motion_opens_gate(base_env_module):
    """只动夹爪（关节不动）也算有效移动。"""
    env, robot, collector = _build(base_env_module, skip_until_motion={"joint_eps": 0.05, "gripper_eps": 0.05})
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)
    env.robot_capture_start()
    _tick(env)
    assert collector.calls == []

    robot.sample = (_Q0, _G0 + np.array([0.2, 0.0]))
    _tick(env)

    assert collector.calls == ["start", "collect"]


def test_episode_without_motion_produces_no_file(base_env_module):
    """整轮都没移动 → 本轮不产出文件（与「同拍 start/end」一致），并清掉闸门状态。"""
    env, robot, collector = _build(base_env_module)
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)
    env.robot_capture_start()
    for _ in range(3):
        _tick(env)
    env.robot_capture_end()
    _tick(env)

    assert collector.calls == []  # 没有 start / collect / finish
    assert env.capture_status()["head_skip"] is None


def test_gate_rearms_for_next_episode(base_env_module):
    """跳过只作用于本轮帧头：下一轮 episode 重新武装（各自跳过自己的无位移帧）。"""
    env, robot, collector = _build(base_env_module)
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)

    env.robot_capture_start()  # 第 1 轮：跳过 1 帧后移动
    _tick(env)
    robot.sample = (_Q0 + 0.4, _G0)
    _tick(env)
    env.robot_capture_end()
    _tick(env)

    moved = _Q0 + 0.4
    robot.sample = (moved, _G0)  # 第 2 轮：基准重置为当前主臂读数 → 先跳过后记录
    env.robot_capture_start()
    _tick(env)
    assert env.capture_status()["head_skip"] == {"skipped": 1}
    robot.sample = (moved + 0.4, _G0)
    _tick(env)
    env.robot_capture_end()
    _tick(env)

    assert collector.calls == ["start", "collect", "finish", "start", "collect", "finish"]


def test_gate_disabled_by_config(base_env_module):
    """``collector.skip_until_motion.enabled: false`` → 按原行为首帧即记录。"""
    env, robot, collector = _build(base_env_module, skip_until_motion={"enabled": False})
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)
    env.robot_capture_start()
    _tick(env)

    assert collector.calls == ["start", "collect"]


def test_teleop_off_before_motion_starts_recording(base_env_module):
    """等主臂移动期间遥操作被关掉（不再有输入）→ 解锁开始记录，避免整轮空等。"""
    env, robot, collector = _build(base_env_module)
    robot.teleop_enabled = True
    robot.sample = (_Q0, _G0)
    env.robot_capture_start()
    _tick(env)
    assert collector.calls == []

    robot.teleop_enabled = False  # 人工交回模型（不再有主臂输入）
    _tick(env)

    assert collector.calls == ["start", "collect"]
