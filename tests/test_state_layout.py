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

"""状态 / 目标向量的布局与**采集 JSON 的逐维自描述**（robot-pipeline 侧）。

钉住现行观测契约：

- ``observations/qpos``（状态）与 ``action``（目标）**同维同布局**——每臂「**值 + 夹爪**」交错
  （双臂 7 + 7 = 14、单臂 7）；夹爪**不再单独成观测键**，就是每臂块的末位；
- 逐维含义由 ``state_layout()`` 自描述（``{index, arm, kind, name}``），写进每轮 mcap 的同名 JSON
  元信息（``state_space`` / ``state_dims`` / ``action_space`` / ``action_dims``）；
- **整体切位姿只改 ``STATE_SPACE``**：值段每臂仍是 6 维、总维度不变，下游按 ``kind`` 自行解释
  （``joint`` / ``pose`` / ``gripper``），不必改代码。

硬件 SDK 只装在机器人端：与 ``test_dual_piper_robot.py`` 同款占位模块。
"""

import importlib
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_QPOS = [0.0] * 12  # joint 空间：每臂 6 关节角 × 2 臂
_GRIPPER = [1.0, 1.0]  # gripper 空间：每臂 1 夹爪 × 2 臂（1 = 张开）
# 硬件接线必填（robot.ports / robot.cameras）：离线测试给占位值（不连真机）
_DEVICES = {
    "ports": {"left_master": "can0", "right_master": "can1", "left": "can2", "right": "can3"},
    "cameras": {"cam_head": "cam-head", "cam_left_wrist": "cam-lw", "cam_right_wrist": "cam-rw"},
}


def _stub_hardware_sdks() -> None:
    """硬件 SDK 只装在机器人端：本机离线测试用占位模块（导入期只需这些名字存在）。"""
    if "pyAgxArm" not in sys.modules:
        sdk = types.ModuleType("pyAgxArm")
        sdk.AgxArmFactory = type("AgxArmFactory", (), {})
        sdk.ArmModel = type("ArmModel", (), {})
        sdk.PiperFW = type("PiperFW", (), {"V188": "V188"})
        sdk.create_agx_arm_config = lambda *args, **kwargs: {}
        sys.modules["pyAgxArm"] = sdk
    for name in ("pyrealsense2", "v4l2"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)


def _load(module_name: str):
    """从 robot-pipeline/src 导入模块（离线，占位硬件 SDK）。"""
    _stub_hardware_sdks()
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module(module_name)


class _FakeArm:
    """假 PiperController：只做关节 / 夹爪读写（布局测试不碰运动学）。"""

    def __init__(self, joint=None, gripper=1.0):
        self.joint = None if joint is None else np.asarray(joint, dtype=np.float64)
        self.gripper = None if gripper is None else float(gripper)

    def get_joint(self):
        return None if self.joint is None else self.joint.copy()

    def get_gripper(self):
        return None if self.gripper is None else self.gripper

    def set_joint(self, joint, torque_ff=None):
        self.joint = np.asarray(joint, dtype=np.float64).copy()

    def set_gripper(self, value):
        self.gripper = float(value)


@pytest.fixture(scope="module")
def dual_piper():
    """导入 robot-pipeline 的 DualPiperRobot。"""
    return _load("robot.dual_piper_robot")


@pytest.fixture(scope="module")
def collector():
    """导入 robot-pipeline 的 ActMcapCollector（mcap 依赖在 ``start()`` 里才导入）。"""
    return _load("collector.mcap_collector")


def _robot(module, *, left_joint=(0.0,) * 6, right_joint=(0.0,) * 6, grippers=(0.4, 0.6)):
    robot = module.DualPiperRobot(robot_config={"init_joint": _QPOS, "init_gripper": _GRIPPER, **_DEVICES})
    robot.controllers = {
        "left_arm": _FakeArm(left_joint, grippers[0]),
        "right_arm": _FakeArm(right_joint, grippers[1]),
        "left_master": _FakeArm(None),
        "right_master": _FakeArm(None),
    }
    return robot


# ---- 状态 / 目标向量：每臂「值 + 夹爪」------------------------------------------


def test_state_vector_is_per_arm_value_then_gripper(dual_piper):
    """状态向量 = 每臂「关节 6 + 夹爪 1」交错（左先右后），夹爪不再单独成键。"""
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10, grippers=(0.4, 0.6))
    state = robot.sample_qpos()

    assert state[robot.KEY_QPOS].tolist() == pytest.approx([0, 1, 2, 3, 4, 5, 0.4, 10, 11, 12, 13, 14, 15, 0.6])
    assert "observations/gripper" not in state  # 夹爪就在状态向量里，不再有独立键
    assert state[robot.KEY_POSE].shape == (12,)  # 位姿仍是每臂 6 维、**不交错**
    assert robot.state_vector_dim() == 14 and robot.state_value_dim_per_arm() == 6


def test_action_vector_matches_state_layout(dual_piper):
    """``action`` = 同维同布局的目标向量（关节目标 + 夹爪目标）；无指令时回退状态。"""
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10)
    targets = np.arange(12, dtype=np.float64) + 100

    robot.set_target_action(targets)  # 关节段目标（每臂 6 → 共 12）
    robot.set_target_gripper([0.1, 0.2])
    state = robot.sample_qpos()

    assert state[robot.KEY_ACTION].shape == state[robot.KEY_QPOS].shape == (14,)
    assert state[robot.KEY_ACTION].tolist() == pytest.approx(
        [100, 101, 102, 103, 104, 105, 0.1, 106, 107, 108, 109, 110, 111, 0.2]
    )
    # 交错 ↔ 分段可逆（展示 / 桥接用）
    values, gripper = robot.split_state(state[robot.KEY_ACTION])
    assert values.tolist() == pytest.approx(list(targets))
    assert gripper.tolist() == pytest.approx([0.1, 0.2])


# ---- 逐维自描述（采集 JSON）-----------------------------------------------------


# ---- 按臂子集的组合下发（layout="joint+gripper" + arms）-------------------------


def test_rollout_layout_writes_selected_arm_only(dual_piper):
    """layout="joint+gripper" + arms：只写所选臂的「关节 + 夹爪」，其余臂目标**原样保留**（不补 home）。"""
    robot = _robot(dual_piper)
    robot.set_target_action(np.arange(12, dtype=np.float64) + 1.0)  # 已知的关节段目标
    robot.set_target_gripper([0.2, 0.8])

    assert robot.rollout([10, 11, 12, 13, 14, 15, 0.5], layout="joint+gripper", arms=["right"]) is True

    target = robot.target_action
    assert target[:6].tolist() == pytest.approx(list(np.arange(6) + 1.0))  # 左臂关节未被覆盖
    assert target[6:12].tolist() == pytest.approx([10, 11, 12, 13, 14, 15])  # 右臂 = 模型关节
    assert target[12] == pytest.approx(0.2)  # 左夹爪未被覆盖
    assert target[13] == pytest.approx(0.5)  # 右夹爪 = 块末位


def test_rollout_layout_block_order_follows_arms_and_clips_gripper(dual_piper):
    """layout="joint+gripper" + arms：块序 = ``arms`` 顺序；夹爪越界钳到 [0, 1]（同 gripper 空间）。"""
    robot = _robot(dual_piper)
    action = [1, 2, 3, 4, 5, 6, 1.5, 7, 8, 9, 10, 11, 12, -0.5]

    assert robot.rollout(action, layout="joint+gripper", arms=["left", "right"]) is True
    assert robot.target_action.tolist() == pytest.approx([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 1.0, 0.0])


def test_rollout_layout_validates_before_touching_target(dual_piper):
    """layout + arms：臂名 / 维度 / 有限性非法 → ValueError，且**不改动目标**（校验只读）。"""
    robot = _robot(dual_piper)

    with pytest.raises(ValueError, match="unique subset"):
        robot.rollout([0.0] * 7, layout="joint+gripper", arms=[])  # 空 arms
    with pytest.raises(ValueError, match="unique subset"):
        robot.rollout([0.0] * 7, layout="joint+gripper", arms=["right", "right"])  # 重复臂
    with pytest.raises(ValueError, match="unique subset"):
        robot.rollout([0.0] * 7, layout="joint+gripper", arms=["arm_9"])  # 未知臂
    with pytest.raises(ValueError, match="action dim"):
        robot.rollout([0.0] * 6, layout="joint+gripper", arms=["right"])  # 单臂 = 6 关节 + 1 夹爪
    with pytest.raises(ValueError, match="non-finite"):
        robot.rollout([0.0] * 6 + [float("nan")], layout="joint+gripper", arms=["right"])  # 非有限

    assert robot.target_action is None  # 一条坏动作不会写目标


def test_rollout_layout_refuses_during_teleop(dual_piper):
    """layout + arms 的 rollout：遥操作（人工接管）中 → False（让位，不改目标）。"""
    robot = _robot(dual_piper)
    robot.teleop_enabled = True

    assert robot.rollout([0.0] * 7, layout="joint+gripper", arms=["right"]) is False
    assert robot.target_action is None


def test_state_layout_describes_every_dim(dual_piper):
    """``state_layout()`` 逐维给 {index, arm, kind, name}——下游按它解释每个下标。"""
    robot = _robot(dual_piper)
    layout = robot.state_layout()

    assert layout["state_space"] == "joint" and layout["action_space"] == "joint"
    assert len(layout["state_dims"]) == len(layout["action_dims"]) == 14
    assert layout["state_dims"][0] == {"index": 0, "arm": "left", "kind": "joint", "name": "j1"}
    assert layout["state_dims"][6] == {"index": 6, "arm": "left", "kind": "gripper", "name": "gripper"}
    assert layout["state_dims"][7] == {"index": 7, "arm": "right", "kind": "joint", "name": "j1"}
    assert layout["state_dims"][13] == {"index": 13, "arm": "right", "kind": "gripper", "name": "gripper"}
    assert [d["index"] for d in layout["state_dims"]] == list(range(14))


def test_state_layout_follows_state_space_switch(dual_piper, monkeypatch):
    """整体切位姿：只改 ``STATE_SPACE``——维度不变（仍 14），值段 kind 变 pose、名字变 x/y/z/rx/ry/rz。"""
    robot = _robot(dual_piper)
    monkeypatch.setattr(dual_piper.DualPiperRobot, "STATE_SPACE", "pose")

    layout = robot.state_layout()
    assert layout["state_space"] == "pose"
    assert [d["name"] for d in layout["state_dims"][:6]] == ["x", "y", "z", "rx", "ry", "rz"]
    assert all(d["kind"] == "pose" for d in layout["state_dims"][:6])
    assert len(layout["state_dims"]) == 14  # 维度不变：值 6 + 夹爪 1，每臂
    assert layout["action_space"] == "joint"  # action 恒为关节目标（底层始终关节控制）


def test_collector_writes_layout_into_episode_json(collector, tmp_path):
    """采集 JSON 元信息带上状态 / 动作布局（每轮 mcap 同名 ``{uuid}.json``）。"""
    layout = {
        "state_space": "joint",
        "state_dims": [{"index": 0, "arm": "left", "kind": "joint", "name": "j1"}],
        "action_space": "joint",
        "action_dims": [{"index": 0, "arm": "left", "kind": "joint", "name": "j1"}],
    }
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})
    instance.set_robot_meta(robot_name="dual_piper_001", robot_type="dual_piper")
    instance.set_state_layout(layout)

    assert instance.meta["state_space"] == "joint"  # /v1/capture/status 的 meta 同源
    episode = tmp_path / "abc.mcap"
    episode.write_bytes(b"not-a-real-mcap")  # 只验证元信息，不真写 mcap
    meta = json.loads(instance._write_meta_json(episode).read_text(encoding="utf-8"))

    assert meta["robot_name"] == "dual_piper_001"
    assert meta["state_space"] == "joint"
    assert meta["state_dims"] == layout["state_dims"]
    assert meta["action_space"] == "joint" and meta["action_dims"] == layout["action_dims"]


def test_collector_layout_defaults_to_empty(collector, tmp_path):
    """未注入布局（如旧机器人 / 测试替身）→ 键在位但为空，不编造维度。"""
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})

    assert instance.meta["state_space"] is None
    assert instance.meta["state_dims"] == []
    assert instance.meta["action_dims"] == []
