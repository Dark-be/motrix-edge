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
- 控制模式由 ``control_layout()`` 自描述（``mit`` / ``mit+gravity`` / ``joint``，**主手不记录**），
  写进同一 JSON 的 ``control_mode``（聚合值，双臂不同时 ``mixed``）/ ``control``（逐控制器明细）——
  下游据此判断这段数据是否带重力前馈（``joint`` 通路不下发 ``t_ff``，前馈不生效）；
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

from motrix_edge.adapter.base import KEY_ACTION, KEY_POSE, KEY_POSE_ACTION, KEY_QPOS, KEY_QPOS_ERR

_REPO_ROOT = Path(__file__).resolve().parents[2]
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


@pytest.fixture(scope="module", params=("piper", "alicia"))
def dual_piper(request):
    """两种主手机型都必须满足相同的执行与数据布局契约。"""
    if request.param == "alicia":
        from test_machine_profile import _stub_hardware_sdks as stub_all_hardware

        stub_all_hardware()
        module = _load("robot.dual_alicia_piper_robot")
        return types.SimpleNamespace(DualPiperRobot=module.DualAliciaPiperRobot)
    return _load("robot.dual_piper_robot")


@pytest.fixture(scope="module")
def test_robot():
    """导入带可逆线性运动学的 TestRobot。"""
    return _load("robot.test_robot")


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


def test_dimensions_are_derived_from_arm_layout(dual_piper):
    single_piper = _load("robot.single_piper_robot")

    assert (dual_piper.DualPiperRobot.joint_dim(), dual_piper.DualPiperRobot.pose_dim()) == (12, 12)
    assert dual_piper.DualPiperRobot.gripper_dim() == 2
    assert (single_piper.SinglePiperRobot.joint_dim(), single_piper.SinglePiperRobot.pose_dim()) == (6, 0)
    assert single_piper.SinglePiperRobot.gripper_dim() == 1
    assert single_piper.SinglePiperRobot.ARM_NAMES == ("left",)
    assert not ({"QPOS", "POSE", "GRIPPER"} & vars(dual_piper.DualPiperRobot).keys())


def test_single_piper_selects_left_pair_from_dual_machine_profile():
    """同一机器档案可含双臂完整接线；单臂机型只装配左主手与左从臂。"""
    single_piper = _load("robot.single_piper_robot")
    robot = single_piper.SinglePiperRobot(
        robot_config={
            "init_joint": [0.0] * 6,
            "init_gripper": [1.0],
            "ports": {"left_master": "m_left", "right_master": "m_right", "left": "left", "right": "right"},
            "teleop": {"joint_offset": {"left": [0.1] * 6, "right": [0.2] * 6}},
        }
    )

    assert robot.ports == {"left_master": "m_left", "left": "left"}
    assert set(robot.controllers) == {"left_master", "left_arm"}
    assert robot.controllers["left_master"].name == "left_master"
    assert robot.controllers["left_arm"].name == "left"
    assert robot.state_arms() == ["left"]
    assert robot.teleop_joint_offset.tolist() == pytest.approx([0.1] * 6)


def test_single_piper_teleop_gripper_reads_left_master():
    single_piper = _load("robot.single_piper_robot")
    robot = single_piper.SinglePiperRobot(
        robot_config={
            "init_joint": [0.0] * 6,
            "init_gripper": [1.0],
            "ports": {"left_master": "m_left", "left": "left"},
        }
    )
    robot.controllers["left_master"] = _FakeArm(gripper=0.35)

    assert robot._read_teleop_gripper().tolist() == pytest.approx([0.35])


def test_robot_observation_uses_explicit_physical_keys(dual_piper):
    """robot 原始帧按 joint / gripper / pose 分键，不带 observations 前缀。"""
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10, grippers=(0.4, 0.6))
    state = robot.sample_state()

    assert state[robot.KEY_JOINT].tolist() == pytest.approx([0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15])
    assert state[robot.KEY_GRIPPER].tolist() == pytest.approx([0.4, 0.6])
    assert state[robot.KEY_POSE].shape == (12,)
    assert robot.state_vector_dim() == 14 and robot.state_value_dim_per_arm() == 6
    assert {robot.KEY_JOINT, robot.KEY_GRIPPER, robot.KEY_POSE}.issubset(state)
    assert all(not key.startswith("observations/") for key in state if key != robot.KEY_TIMESTAMP)


def test_standard_observation_packs_fused_contract_vectors(dual_piper):
    """``standard_observation()``：融合键（每臂「值 + 夹爪」）由 robot **单点**产出。

    采集器落盘与共享内存发布读的是**同一份**契约观测——回归点：融合曾经只在 server 层做过、且
    不回写观测，采集器拿到原始 ``joint`` / ``gripper`` → 每拍 ``KeyError: 'observations/qpos'``
    （日志刷 ``env observe step error``、episode 0 帧）。
    """
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10, grippers=(0.4, 0.6))
    robot.motion_state = robot.sample_state()
    robot.motion_state[f"{robot.CAMERA_PREFIX}cam_head"] = np.zeros((2, 2, 3), dtype=np.uint8)
    robot.motion_state[robot.KEY_TIMESTAMP] = 12.5

    obs = robot.standard_observation()

    assert obs[KEY_QPOS].tolist() == pytest.approx([0, 1, 2, 3, 4, 5, 0.4, 10, 11, 12, 13, 14, 15, 0.6])
    assert obs[KEY_ACTION].shape == (14,) and obs[KEY_QPOS_ERR].shape == (14,)
    assert obs[KEY_QPOS_ERR].tolist() == pytest.approx((obs[KEY_ACTION] - obs[KEY_QPOS]).tolist())
    # 位姿键按**契约名**（带 observations/ 前缀，与 robot 原始 pose / pose_action 不同名）
    assert obs[KEY_POSE].shape == (12,) and obs[KEY_POSE_ACTION].shape == (12,)
    assert obs[f"{robot.CAMERA_PREFIX}cam_head"].shape == (2, 2, 3)  # 本拍帧原样带走
    assert obs["timestamp"] == pytest.approx(12.5) and obs["seq"] == robot.seq
    assert robot.KEY_JOINT not in obs and robot.KEY_GRIPPER not in obs  # 原始键不外泄：消费方只认契约键


def test_standard_observation_without_first_sample(dual_piper):
    """控制线程还没采到第一拍 → 无观测（``None``），不是一份缺键的假观测。"""
    robot = _robot(dual_piper)

    assert robot.standard_observation() is None


def test_interleave_state_rejects_mismatched_blocks(dual_piper):
    """关节 / 夹爪长度与臂布局不符 → 立即报错（不拼出一条看似有效的状态向量）。"""
    robot = _robot(dual_piper)

    with pytest.raises(ValueError, match=r"cannot pack state"):
        robot.interleave_state(np.arange(5), [0.25, 0.75])
    with pytest.raises(ValueError, match=r"cannot pack state"):
        robot.interleave_state(np.arange(12), [0.25])


def test_collector_consumes_standard_observation(dual_piper, collector, tmp_path):
    """真采集器 + robot 契约观测：episode 要真的写进帧（回归：融在 server 层时这里是 0 帧）。"""
    pytest.importorskip("mcap_ros2", reason="robot-pipeline 侧依赖（mcap），宿主机 / 容器可能未装")

    from unittest.mock import Mock

    robot = _robot(dual_piper, grippers=(0.4, 0.6))
    robot.sensors = {
        name: Mock(
            pixel_format="raw", get_information=Mock(return_value={"color": np.zeros((480, 640, 3), dtype=np.uint8)})
        )
        for name in robot.IMAGE_NAMES
    }
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})
    instance.set_state_layout(robot.state_layout())
    instance.start()
    for _ in range(2):
        robot.motion_state = robot.sample_state()
        instance.collect(robot.standard_observation(robot.build_observation()))
    path = instance.finish()

    assert path is not None and path.exists()
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["frames"] == 2  # 每拍都真的落了帧（不是 0）
    assert meta["state_dims"] == robot.state_layout()["state_dims"]
    assert meta["action_dims"] == robot.state_layout()["action_dims"]
    from mcap.reader import make_reader

    with path.open("rb") as stream:
        messages = list(make_reader(stream).iter_messages())
    assert sum(channel.topic == "observations/qpos" for _, channel, _ in messages) == 2
    assert sum(channel.topic == "action" for _, channel, _ in messages) == 2
    for name in robot.IMAGE_NAMES:
        assert sum(channel.topic == f"observations/images/{name}" for _, channel, _ in messages) == 2


def test_robot_rejects_short_joint_block(dual_piper):
    """控制器少回关节时立即报错，不能拼出长度不完整但看似有效的整机向量。"""
    robot = _robot(dual_piper, left_joint=np.arange(5), right_joint=np.arange(6))

    with pytest.raises(RuntimeError, match=r"joint dim 5 < JOINT_DIM_PER_ARM 6"):
        robot.get_observation_joint()


def test_robot_action_keys_are_explicit(dual_piper):
    """robot 原始帧的 joint_action 与 gripper_action 分离。"""
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10)
    targets = np.arange(12, dtype=np.float64) + 100

    robot.set_joint_target(targets)  # 关节段目标（每臂 6 → 共 12）
    robot.set_gripper_target([0.1, 0.2])
    state = robot.sample_state()

    assert state[robot.KEY_JOINT_ACTION].shape == state[robot.KEY_JOINT].shape == (12,)
    assert state[robot.KEY_JOINT_ACTION].tolist() == pytest.approx(targets)
    assert state[robot.KEY_GRIPPER_ACTION].tolist() == pytest.approx([0.1, 0.2])
    assert state[robot.KEY_JOINT_ERROR].tolist() == pytest.approx(targets - state[robot.KEY_JOINT])
    assert state[robot.KEY_GRIPPER_ERROR].tolist() == pytest.approx(np.asarray([0.1, 0.2]) - state[robot.KEY_GRIPPER])


def test_gripper_target_can_run_without_joint_target(dual_piper):
    robot = _robot(dual_piper, left_joint=np.arange(6), right_joint=np.arange(6) + 10)
    robot.set_gripper_target([0.3, 0.4])

    robot.step()

    assert robot.controllers["left_arm"].joint.tolist() == pytest.approx(np.arange(6))
    assert robot.controllers["right_arm"].joint.tolist() == pytest.approx(np.arange(6) + 10)
    assert robot.controllers["left_arm"].gripper == pytest.approx(0.3)
    assert robot.controllers["right_arm"].gripper == pytest.approx(0.4)


# ---- 逐维自描述（采集 JSON）-----------------------------------------------------


# ---- 按臂子集的组合下发（layout="joint+gripper" + arms）-------------------------


def test_rollout_layout_writes_selected_arm_only(dual_piper):
    """layout="joint+gripper" + arms：只写所选臂的「关节 + 夹爪」，其余臂目标**原样保留**（不补 home）。"""
    robot = _robot(dual_piper)
    robot.set_joint_target(np.arange(12, dtype=np.float64) + 1.0)  # 已知的关节段目标
    robot.set_gripper_target([0.2, 0.8])

    assert robot.rollout([10, 11, 12, 13, 14, 15, 0.5], layout="joint+gripper", arms=["right"]) is True

    target = robot.joint_target
    assert target[:6].tolist() == pytest.approx(list(np.arange(6) + 1.0))  # 左臂关节未被覆盖
    assert target[6:12].tolist() == pytest.approx([10, 11, 12, 13, 14, 15])  # 右臂 = 模型关节
    assert robot.gripper_target.tolist() == pytest.approx([0.2, 0.5])


def test_rollout_layout_block_order_follows_arms_and_clips_gripper(dual_piper):
    """layout="joint+gripper" + arms：块序 = ``arms`` 顺序；夹爪越界钳到 [0, 1]（同 gripper 空间）。"""
    robot = _robot(dual_piper)
    action = [1, 2, 3, 4, 5, 6, 1.5, 7, 8, 9, 10, 11, 12, -0.5]

    assert robot.rollout(action, layout="joint+gripper", arms=["left", "right"]) is True
    assert robot.joint_target.tolist() == pytest.approx([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    assert robot.gripper_target.tolist() == pytest.approx([1.0, 0.0])


def test_delta_layouts_add_to_targets_for_selected_arm(dual_piper):
    """joint/gripper delta 以 target 为基准，且只修改选中的臂。"""
    robot = _robot(dual_piper)
    robot.set_joint_target(np.arange(12, dtype=np.float64))
    robot.set_gripper_target([0.2, 0.8])

    robot.apply_layout(
        [0.1, -0.2, 0.3, -0.4, 0.5, -0.6, 0.35],
        layout="joint_delta+gripper_delta",
        arms=["right"],
    )

    assert robot.joint_target[:6].tolist() == pytest.approx(np.arange(6))
    assert robot.joint_target[6:].tolist() == pytest.approx([6.1, 6.8, 8.3, 8.6, 10.5, 10.4])
    assert robot.gripper_target.tolist() == pytest.approx([0.2, 1.0])


def test_delta_layouts_fall_back_to_command(dual_piper):
    """尚无 target 时，delta 从本拍 command 起算，而不是从 home 或零起算。"""
    robot = _robot(dual_piper)
    robot.joint_command = np.arange(12, dtype=np.float64)
    robot.gripper_command = np.asarray([0.4, 0.6])

    robot.apply_layout(
        [0.5] * 6 + [-0.2],
        layout="joint_delta+gripper_delta",
        arms=["left"],
    )

    assert robot.joint_target[:6].tolist() == pytest.approx(np.arange(6) + 0.5)
    assert robot.joint_target[6:].tolist() == pytest.approx(np.arange(6, 12))
    assert robot.gripper_target.tolist() == pytest.approx([0.2, 0.6])


def test_layout_rejects_two_motion_or_two_gripper_segments(dual_piper):
    robot = _robot(dual_piper)

    with pytest.raises(ValueError, match="one motion segment"):
        robot.apply_layout([0.0] * 12, layout="joint+joint_delta", arms=["left"])
    with pytest.raises(ValueError, match="one gripper segment"):
        robot.apply_layout([0.0, 0.0], layout="gripper+gripper_delta", arms=["left"])

    assert robot.joint_target is None and robot.gripper_target is None


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

    assert robot.joint_target is None  # 一条坏动作不会写目标


def test_rollout_layout_refuses_during_teleop(dual_piper):
    """layout + arms 的 rollout：遥操作（人工接管）中 → False（让位，不改目标）。"""
    robot = _robot(dual_piper)
    robot.teleop_enabled = True

    assert robot.rollout([0.0] * 7, layout="joint+gripper", arms=["right"]) is False
    assert robot.joint_target is None


def test_pose_layout_writes_selected_arm_only(test_robot):
    """部分臂 pose：未选臂以目标 FK 补齐，不能把它的关节值误当成位姿。"""
    robot = test_robot.TestRobot({"init_joint": _QPOS, "init_gripper": _GRIPPER})
    initial = np.asarray([0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 0.3, 0.5, 0.7, 0.9, 1.1, 1.3])
    desired_right = np.asarray([0.4, 0.6, 0.8, 1.0, 1.2, 1.4])
    robot.set_joint_target(initial)
    robot.set_gripper_target([0.2, 0.8])
    right_pose = test_robot.TestArmController.joint_to_pose(desired_right)

    robot.apply_layout(right_pose, layout="pose", arms=["right"])

    assert robot.joint_target[:6].tolist() == pytest.approx(initial[:6])
    assert robot.joint_target[6:12].tolist() == pytest.approx(desired_right)
    assert robot.gripper_target.tolist() == pytest.approx([0.2, 0.8])


def test_pose_delta_layout_writes_selected_arm_only(test_robot):
    """部分臂 pose_delta：未选臂增量为 0，不能混入该臂现有的关节目标。"""
    robot = test_robot.TestRobot({"init_joint": _QPOS, "init_gripper": _GRIPPER})
    initial = np.asarray([0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 0.3, 0.5, 0.7, 0.9, 1.1, 1.3])
    delta = np.asarray([0.02, -0.04, 0.06, 0.1, -0.2, 0.3])
    robot.set_joint_target(initial)
    robot.set_gripper_target([0.2, 0.8])

    robot.apply_layout(delta, layout="pose_delta", arms=["right"])

    expected_right = initial[6:12] + np.asarray([0.1, -0.2, 0.3, 0.1, -0.2, 0.3])
    assert robot.joint_target[:6].tolist() == pytest.approx(initial[:6])
    assert robot.joint_target[6:12].tolist() == pytest.approx(expected_right)
    assert robot.gripper_target.tolist() == pytest.approx([0.2, 0.8])


def test_execute_solve_failure_has_no_state_side_effect(test_robot, monkeypatch):
    """首条命令解算失败：目标仍未初始化，execute 也不能提前退出遥操作。"""
    robot = test_robot.TestRobot({"init_joint": _QPOS, "init_gripper": _GRIPPER})
    assert robot.enable_teleop("absolute")

    def reject_target(*_args):
        raise test_robot.CartesianActionError("synthetic solve failure")

    monkeypatch.setattr(robot, "_prepare_target", reject_target)
    with pytest.raises(test_robot.CartesianActionError, match="synthetic solve failure"):
        robot.execute([0.0] * 12, layout="pose")

    assert robot.joint_target is None
    assert robot.teleop_enabled is True


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


def test_state_layout_is_always_joint(dual_piper):
    """qpos / action 布局固定为关节，不再允许切换为 pose。"""
    robot = _robot(dual_piper)
    layout = robot.state_layout()
    assert layout["state_space"] == layout["action_space"] == "joint"
    assert {d["kind"] for d in layout["state_dims"]} == {"joint", "gripper"}


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
    runtime_context = {"log_path": "/rpent/runs/42/run.log", "model": "rpent-agent-v1"}
    instance.set_meta({"operator": "张三", "task_name": "桌面整理", "runtime_context": runtime_context})

    assert instance.meta["state_space"] == "joint"  # /v1/capture/status 的 meta 同源
    assert instance.meta["runtime_context"] == runtime_context
    episode = tmp_path / "abc.mcap"
    episode.write_bytes(b"not-a-real-mcap")  # 只验证元信息，不真写 mcap
    meta = json.loads(instance._write_meta_json(episode).read_text(encoding="utf-8"))

    assert meta["robot_name"] == "dual_piper_001"
    assert meta["state_space"] == "joint"
    assert meta["state_dims"] == layout["state_dims"]
    assert meta["action_space"] == "joint" and meta["action_dims"] == layout["action_dims"]
    assert meta["operator"] == "张三" and meta["task_name"] == "桌面整理"
    assert meta["runtime_context"] == runtime_context


def test_collector_layout_defaults_to_empty(collector, tmp_path):
    """未注入布局（如旧机器人 / 测试替身）→ 键在位但为空，不编造维度。"""
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})

    assert instance.meta["state_space"] is None
    assert instance.meta["state_dims"] == []
    assert instance.meta["action_dims"] == []


# ---- 控制模式（采集 JSON）-------------------------------------------------------


def _control_arm(piper_controller, *, ctrl_mode="mit", role="follower", gravity=None):
    """假 PiperController：只填控制模式判定所需的三个属性（不连 SDK、不碰运动学）。"""
    ctrl = piper_controller.PiperController.__new__(piper_controller.PiperController)
    ctrl.role = role
    ctrl.ctrl_mode = ctrl_mode
    ctrl.gravity = gravity
    return ctrl


@pytest.fixture(scope="module")
def piper_controller():
    """导入 robot-pipeline 的 PiperController（占位硬件 SDK，不 connect）。"""
    return _load("robot.controller.piper_controller")


@pytest.fixture(scope="module")
def gravity():
    """导入重力动力学模型与前馈（无硬件依赖）。"""
    return _load("robot.dynamics.gravity")


def test_gravity_runtime_and_calibration_modules_are_separated(gravity):
    """Controller 依赖的运行期模块不再携带采样与拟合流程。"""
    calibration = _load("robot.calibration.gravity")

    assert hasattr(gravity, "GravityCompensator") and not hasattr(gravity, "fit_gravity")
    assert hasattr(calibration, "fit_gravity") and calibration.GravityModel is gravity.GravityModel


def _model(gravity, value=0.1):
    """非占位重力模型（全 0 = 占位 → 不产生前馈）。"""
    return gravity.GravityModel(np.full((6, gravity.PI_PER_LINK), value))


def test_control_mode_prefers_channel_over_feedforward(piper_controller, gravity):
    """``joint`` 通路不下发 t_ff → 即使装了模型也只报 ``joint``（不能只看有没有 gravity）。"""
    model = _model(gravity)
    ff = gravity.GravityCompensator(model, alpha=1.0)

    assert _control_arm(piper_controller, gravity=ff).control_mode == "mit+gravity"
    assert _control_arm(piper_controller, ctrl_mode="joint", gravity=ff).control_mode == "joint"
    assert _control_arm(piper_controller).control_mode == "mit"
    assert _control_arm(piper_controller, role="leader").control_mode == "read_only"
    # 占位（全 0）/ α=0 不产生前馈 → 与纯位置环等价
    assert (
        _control_arm(piper_controller, gravity=gravity.GravityCompensator(_model(gravity, 0.0))).control_mode == "mit"
    )
    assert _control_arm(piper_controller, gravity=gravity.GravityCompensator(model, alpha=0.0)).control_mode == "mit"


def test_control_detail_carries_config_facts_only(piper_controller, gravity):
    """明细报**配置事实**（模式 / 通路 / 角色 / α / 限幅 / 参数来源），不带运行时计数。"""
    detail = _control_arm(piper_controller, gravity=gravity.GravityCompensator(_model(gravity))).control_detail

    assert detail["mode"] == "mit+gravity" and detail["ctrl_mode"] == "mit" and detail["role"] == "follower"
    assert detail["gravity"]["active"] is True and detail["gravity"]["alpha"] == 1.0
    assert detail["gravity"]["limit_nm"] == gravity.T_FF_LIMIT_NM
    assert detail["gravity"]["placeholder"] is False
    assert "degrades" not in detail["gravity"] and "clips" not in detail["gravity"]
    assert _control_arm(piper_controller).control_detail["gravity"] is None


def test_control_layout_records_execution_controllers_only(dual_piper, piper_controller):
    """只记录执行控制器（follower）：主手（leader）与无控制通路的控制器一律不入表。"""
    robot = _robot(dual_piper)
    robot.controllers = {
        "left_arm": _control_arm(piper_controller),
        "right_arm": _control_arm(piper_controller, ctrl_mode="joint"),
        "left_master": _control_arm(piper_controller, role="leader"),
        "right_master": _FakeArm(None),  # 无 control_detail 的控制器
    }
    layout = robot.control_layout()

    assert set(layout["arms"]) == {"left_arm", "right_arm"}
    assert layout["arms"]["right_arm"]["mode"] == "joint" and layout["arms"]["right_arm"]["ctrl_mode"] == "joint"
    assert layout["control_mode"] == "mixed"  # 双臂模式不同


def test_control_layout_aggregate_and_empty(dual_piper, piper_controller):
    """执行臂唯一 → 聚合值就是它；没有执行控制器（如 test_robot）→ None + 空表。"""
    robot = _robot(dual_piper)
    robot.controllers = {
        "left_arm": _control_arm(piper_controller),
        "left_master": _control_arm(piper_controller, role="leader"),
    }
    assert robot.control_layout()["control_mode"] == "mit"

    robot.controllers = {"left_master": _control_arm(piper_controller, role="leader")}
    assert robot.control_layout() == {"control_mode": None, "arms": {}}


def test_collector_writes_control_into_episode_json(collector, tmp_path):
    """采集 JSON 元信息带上控制模式（聚合值 + 逐控制器明细）。"""
    control = {
        "control_mode": "mixed",
        "arms": {
            "right_arm": {
                "mode": "mit+gravity",
                "ctrl_mode": "mit",
                "role": "follower",
                "gravity": {
                    "active": True,
                    "alpha": 1.0,
                    "limit_nm": 16.0,
                    "params": "gravity/piper_6dof.json",
                    "placeholder": False,
                },
            }
        },
    }
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})
    instance.set_control_layout(control)

    assert instance.meta["control_mode"] == "mixed"  # /v1/capture/status 的 meta 同源
    episode = tmp_path / "abc.mcap"
    episode.write_bytes(b"not-a-real-mcap")  # 只验证元信息，不真写 mcap
    meta = json.loads(instance._write_meta_json(episode).read_text(encoding="utf-8"))

    assert meta["control_mode"] == "mixed"
    assert meta["control"] == control["arms"]


def test_collector_control_defaults_to_empty(collector, tmp_path):
    """未注入（如旧机器人）→ 键在位但为空，不编造模式。"""
    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})

    assert instance.meta["control_mode"] is None and instance.meta["control"] == {}


def test_collector_cancel_removes_mcap_and_json(collector, tmp_path):
    """取消录制关闭 writer 后删除 MCAP，并防御性删除同名 JSON。"""

    class _Writer:
        def __init__(self):
            self.finished = False

        def finish(self):
            self.finished = True

    instance = collector.ActMcapCollector({"save_dir": str(tmp_path)})
    path = tmp_path / "cancelled.mcap"
    json_path = path.with_suffix(".json")
    path.write_bytes(b"partial")
    json_path.write_text("{}", encoding="utf-8")
    writer = _Writer()
    instance._path = path
    instance._writer = writer
    instance._fp = path.open("ab")

    assert instance.cancel() == path
    assert writer.finished is True
    assert not path.exists()
    assert not json_path.exists()
    assert instance.cancel() is None


def test_env_control_layout_follows_connect_time_gravity(
    dual_piper, piper_controller, collector, gravity, tmp_path, monkeypatch
):
    """重力前馈在 ``robot.connect()`` 里才装载 → env 连上后**再注入一次**，否则落盘漏报。"""
    env_module = _load("env.base_env")
    # ``env.base_env`` 可能是被别的模块测试（帧头跳过）打桩 collector 包时导入的 → 它的
    # ``get_collector`` 绑在桩上（模块级 ``from collector import get_collector``），这里换成真 collector。
    monkeypatch.setattr(env_module, "get_collector", lambda cfg: collector.ActMcapCollector(cfg))
    robot = _robot(dual_piper)
    robot.controllers = {
        "left_arm": _control_arm(piper_controller),
        "left_master": _control_arm(piper_controller, role="leader"),
    }
    env = env_module.BaseEnv(robot, capture_config={"type": "act_mcap", "save_dir": str(tmp_path)})

    # 构造期（还没 connect）：纯位置环
    assert env._collector.meta["control_mode"] == "mit"

    # connect() 装载重力前馈（现场由 PiperController.connect(gravity=...) 做）→ 再注入
    robot.controllers["left_arm"].gravity = gravity.GravityCompensator(_model(gravity))
    env._inject_control_layout()

    assert env._collector.meta["control_mode"] == "mit+gravity"
    assert env._collector.meta["control"]["left_arm"]["gravity"]["active"] is True


def test_dual_hardware_connection_and_gripper_deadzone(dual_piper):
    from unittest.mock import Mock

    robot = _robot(dual_piper)
    robot.controllers = {name: Mock() for name in robot.controllers}
    robot.sensors = {name: Mock() for name in robot.sensors}
    robot.connect()
    for arm in robot.ARM_NAMES:
        name = robot.MASTER_CONTROLLERS[arm]
        kwargs = {"port": robot.ports[name]}
        if robot.PORT_KINDS[name] == "can":
            kwargs["role"] = "leader"
        robot.controllers[name].connect.assert_called_once_with(**kwargs)
        robot._controller_for_arm(arm).connect.assert_called_once_with(port=robot.ports[arm], gravity=None)
    for name, sensor in robot.sensors.items():
        kwargs = {"device": robot.camera_devices[name], "pixel_format": "jpg"}
        if robot.CAMERA_KINDS[name] == "realsense":
            kwargs["enable_depth"] = name in robot.depth_cameras
        sensor.connect.assert_called_once_with(**kwargs)
    robot._apply_commands(np.zeros(12), np.array([0.1, 0.7]))
    robot.controllers["left_arm"].set_gripper.assert_called_once_with(0.0)
    robot.controllers["right_arm"].set_gripper.assert_called_once_with(0.7)
    robot.disconnect()
    assert robot.ready is False


def test_dual_pose_roundtrip_with_tcp(dual_piper):
    robot = _robot(dual_piper)
    robot.set_tcp(True, [0, 0, 0.12])
    joints = np.array([0, 0.1, 0, 0, 0, 0] * 2, dtype=float)
    pose = robot.get_observation_pose(joints)
    assert pose.shape == (12,) and np.isfinite(pose).all()
    solved = robot._solve_pose(pose, joints)
    np.testing.assert_allclose(robot.get_observation_pose(solved), pose, atol=1e-4)
