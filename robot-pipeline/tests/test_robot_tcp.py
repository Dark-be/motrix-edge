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

"""末端 TCP 帧换算 + ``robot.tcp`` 配置解析（纯 numpy，不需要硬件 SDK）。

钉住四件事：① 关闭（缺省 / ``enabled: false``）时是**恒等**——行为与不开这个开关时逐字节一致；
② 读（法兰 → TCP）与写（TCP → 法兰）互为逆，含带姿态的位姿；③ 逐臂偏移（两臂末端不等长）；
④ 配置写错一律报错（未知键 / 旋转键 / 缺 offset / 非法数值 / 未知臂名），不静默忽略。
设计见 ``wiki/design/robot_pipeline_tcp.md``。
"""

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROBOT_PIPELINE_SRC = _REPO_ROOT / "robot-pipeline" / "src"
_ARMS = ("left", "right")


def _import(name: str):
    """导入 robot-pipeline 的纯 numpy 模块（插入其 ``src`` 到 ``sys.path``）。"""
    if str(_ROBOT_PIPELINE_SRC) not in sys.path:
        sys.path.insert(0, str(_ROBOT_PIPELINE_SRC))
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def tcp():
    return _import("robot.kinematics.tcp")


@pytest.fixture(scope="module")
def base_robot():
    return _import("robot.base_robot")


def _robot(base_robot, section=None):
    """最小可实例化的机器人（只为验证基类的 TCP 钩子；无控制器 / 无硬件）。"""

    class _Stub(base_robot.BaseRobot):
        NAME = "stub"
        ARM_NAMES = _ARMS
        QPOS = 12
        POSE = 12
        POSE_DIM_PER_ARM = 6
        GRIPPER = 2

    config = {"init_joint": [0.0] * 12}
    if section is not None:
        config["tcp"] = section
    return _Stub(config)


# ---- 配置解析 ---------------------------------------------------------------


def test_resolve_tcp_is_identity_when_disabled(tcp):
    """缺段 / ``enabled: false`` → 空 dict（偏移留着也不生效：关闭即与现状一致）。"""
    assert tcp.resolve_tcp(None, _ARMS) == {}
    assert tcp.resolve_tcp({}, _ARMS) == {}
    assert tcp.resolve_tcp({"enabled": False, "offset": [0.0, 0.0, 0.1]}, _ARMS) == {}


def test_resolve_tcp_applies_common_and_per_arm_offset(tcp):
    """公共 ``offset`` + ``arms.<arm>`` 覆盖（两臂末端不等长是常态）。"""
    resolved = tcp.resolve_tcp(
        {"enabled": True, "offset": [0.0, 0.0, 0.1], "arms": {"right": {"offset": [0.0, 0.0, 0.08]}}}, _ARMS
    )
    assert set(resolved) == set(_ARMS)
    assert resolved["left"] == pytest.approx([0.0, 0.0, 0.1])
    assert resolved["right"] == pytest.approx([0.0, 0.0, 0.08])


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"enabled": True, "offset": [0.0, 0.0, 0.0], "foo": 1}, "未知键"),
        ({"enabled": True, "offset": [0.0, 0.0, 0.0], "rpy": [0.0, 0.0, 0.0]}, "只允许纯平移"),
        ({"enabled": True}, "必须给"),
        ({"enabled": True, "offset": [0.0, 0.0]}, "3 个数"),
        ({"enabled": True, "offset": [0.0, 0.0, float("nan")]}, "非有限数"),
        ({"enabled": True, "offset": [0.0, 0.0, 0.0], "arms": {"middle": {"offset": [0.0, 0.0, 0.0]}}}, "未知臂"),
        ({"enabled": True, "offset": [0.0, 0.0, 0.0], "arms": {"left": {"rpy": [0.0, 0.0, 0.0]}}}, "未知键"),
    ],
)
def test_resolve_tcp_rejects_bad_config(tcp, section, expected):
    """配置写错 → 构造期报错（不静默忽略：否则现场只看到「位姿差一个常量」）。"""
    with pytest.raises(ValueError, match=expected):
        tcp.resolve_tcp(section, _ARMS)


# ---- 换算 -------------------------------------------------------------------


def test_to_tcp_shifts_along_flange_z(tcp):
    """0 姿态下 ``[0, 0, L]`` = 沿**法兰 +z**（工具伸出方向）伸出 L 米。"""
    pose = np.array([0.1, 0.2, 0.3, 0.0, 0.0, 0.0])
    assert tcp.to_tcp_block(pose, [0.0, 0.0, 0.1]) == pytest.approx([0.1, 0.2, 0.4, 0.0, 0.0, 0.0])


def test_to_tcp_rotates_the_offset_with_the_pose(tcp):
    """偏移随姿态转：绕 yaw 转 90° 后，法兰 ``+x`` 上的 0.1 m 落在基座 ``+y``。"""
    pose = np.array([0.0, 0.0, 0.0, 0.0, 0.0, np.pi / 2])
    assert tcp.to_tcp_block(pose, [0.1, 0.0, 0.0]) == pytest.approx([0.0, 0.1, 0.0, 0.0, 0.0, np.pi / 2], abs=1e-12)


def test_round_trip_with_rotation(tcp):
    """``from_tcp(to_tcp(pose)) == pose``（纯平移不改变姿态，往返必须精确）。"""
    rng = np.random.default_rng(7)
    for _ in range(20):
        pose = np.concatenate([rng.uniform(-1, 1, 3), rng.uniform(-np.pi, np.pi, 3)])
        offset = rng.uniform(-0.2, 0.2, 3)
        restored = tcp.from_tcp_block(tcp.to_tcp_block(pose, offset), offset)
        assert restored == pytest.approx(pose, abs=1e-12)


def test_flat_helpers_use_per_arm_offsets(tcp):
    """扁平位姿逐臂换算：未给偏移的臂原样返回（``None`` = 恒等）。"""
    flat = np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0])
    shifted = tcp.to_tcp(flat, [np.array([0.0, 0.0, 0.2]), None])
    assert shifted[:3] == pytest.approx([0.1, 0.0, 0.2])
    assert shifted[6:9] == pytest.approx([0.1, 0.0, 0.0])
    # 全 None / 缺省 → 原值（不就地改调用方数组）
    same = tcp.to_tcp(flat, [None, None])
    assert same == pytest.approx(flat) and same is not flat


def test_tcp_target_means_the_tip_moves_not_the_flange(tcp):
    """语义：TCP 目标 = 法兰位姿 + ``[0, 0, 0.1]`` → 解算前的法兰目标 = 原法兰位姿。

    即「沿工具 z 让**尖端**前移 10 cm」；法兰原点原地不动（IK 结果因此与不开 TCP 时不同）。
    """
    flange = np.array([0.3, -0.1, 0.5, 0.0, 0.0, 0.0])
    tcp_target = tcp.to_tcp_block(flange, [0.0, 0.0, 0.1])
    assert tcp.from_tcp_block(tcp_target, [0.0, 0.0, 0.1]) == pytest.approx(flange)


# ---- 基类钩子（读 / 写各自的唯一进出口）---------------------------------------


def test_hooks_are_identity_when_disabled(base_robot):
    """未开 ``robot.tcp`` → 帧名 ``flange``、两个钩子原值返回（零数值扰动）。"""
    robot = _robot(base_robot)
    pose = np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0] * 2)
    assert (robot.pose_frame, robot.tcp_offsets) == ("flange", {})
    assert robot.pose_to_tcp(pose) == pytest.approx(pose)
    assert robot.tcp_to_flange(pose) == pytest.approx(pose)
    assert robot.tcp_offset_of("left") is None


def test_hooks_shift_both_arms_and_report_tcp_frame(base_robot):
    """开启后：帧名 ``tcp``、读向乘偏移、写向还原（两个方向互为逆）。"""
    robot = _robot(
        base_robot,
        {"enabled": True, "offset": [0.0, 0.0, 0.1], "arms": {"right": {"offset": [0.0, 0.0, 0.08]}}},
    )
    assert robot.pose_frame == "tcp"
    assert robot.tcp_offset_of("left") == pytest.approx([0.0, 0.0, 0.1])
    assert robot.tcp_offset_of("right") == pytest.approx([0.0, 0.0, 0.08])
    flange = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    tcp_pose = robot.pose_to_tcp(flange)
    assert tcp_pose[:3] == pytest.approx([0.0, 0.0, 0.1])
    assert tcp_pose[6:9] == pytest.approx([0.0, 0.0, 0.08])
    assert robot.tcp_to_flange(tcp_pose) == pytest.approx(flange)


def test_enabled_but_no_arms_declared_stays_flange(base_robot):
    """未声明 ``ARM_NAMES`` 的机型（无臂概念）→ 配置不生效、帧名仍为 ``flange``。"""

    class _NoArm(base_robot.BaseRobot):
        NAME = "no_arm"
        QPOS = 6
        POSE = 6
        POSE_DIM_PER_ARM = 6

    robot = _NoArm({"init_joint": [0.0] * 6, "tcp": {"enabled": True, "offset": [0.0, 0.0, 0.1]}})
    assert robot.tcp_offsets == {}
    assert robot.pose_frame == "flange"


# ---- 运行期切换（``robot tcp`` → POST /v1/tcp；``robot gravity`` → POST /v1/gravity）----------


def test_set_tcp_rejects_bad_config_without_touching_runtime_state(base_robot):
    """非法配置 → ``ValueError`` 且**运行位不变**（先校验后动状态：失败即零副作用）。

    与 ``set_gravity`` 同一口径：不做单独的预检接口，调用方（robot server）拿到异常即回 422。
    """
    robot = _robot(base_robot)
    with pytest.raises(ValueError, match="3 个数"):
        robot.set_tcp(True, [0.0, 0.1])
    with pytest.raises(ValueError, match="非有限数"):
        robot.set_tcp(True, [0.0, 0.0, float("nan")])
    assert (robot.pose_frame, robot.tcp_offsets) == ("flange", {})  # 未动运行位


def test_set_tcp_switches_frame_and_offset_at_runtime(base_robot):
    """运行期开关：``set_tcp`` 改帧名与偏移（读 / 写钩子跟着变），关掉即回恒等。"""
    robot = _robot(base_robot)
    pose = np.zeros(12)
    robot.set_tcp(True, [0.0, 0.0, 0.1])
    assert robot.pose_frame == "tcp"
    assert robot.tcp_status() == {
        "pose_frame": "tcp",
        "tcp_offset": {"left": [0.0, 0.0, 0.1], "right": [0.0, 0.0, 0.1]},
    }
    assert robot.pose_to_tcp(pose)[:3] == pytest.approx([0.0, 0.0, 0.1])
    robot.set_tcp(False)  # 关闭：偏移不再生效（配置里的 offset 留着也不影响）
    assert robot.pose_frame == "flange"
    assert robot.tcp_status() == {"pose_frame": "flange", "tcp_offset": None}
    assert robot.pose_to_tcp(pose) == pytest.approx(pose)
    assert robot.tcp_to_flange(pose) == pytest.approx(pose)


def test_set_tcp_falls_back_to_configured_offset(base_robot):
    """不带 ``offset`` 的切换沿用配置里的偏移（``offset`` 是**运行期覆盖**，不写回 yml）。"""
    robot = _robot(base_robot, {"enabled": False, "offset": [0.0, 0.0, 0.1]})
    assert robot.pose_frame == "flange"
    robot.set_tcp(True)
    assert robot.pose_frame == "tcp"
    assert robot.tcp_offset_of("left") == pytest.approx([0.0, 0.0, 0.1])


def test_set_gravity_reports_unsupported_robot(base_robot):
    """机型 / 控制器不支持重力前馈 → ``ValueError``（不回 ok，也不静默当作成功）。"""
    robot = _robot(base_robot)
    with pytest.raises(ValueError, match="no controller supports"):
        robot.set_gravity(True, 0.5)
    with pytest.raises(ValueError, match="no controller supports"):
        robot.set_gravity(False)  # 卸载也对：不支持就不该假装成功
    with pytest.raises(ValueError, match="invalid alpha"):
        robot.set_gravity(True, float("nan"))


def test_set_gravity_installs_and_unloads_on_each_executing_arm(base_robot):
    """支持时逐执行臂装载 / 卸载，``alpha`` 运行期覆盖随段下发；返回实际生效的控制器键。"""

    class _Controller:
        def __init__(self):
            self.calls = []

        def enable_gravity_compensation(self, section):
            self.calls.append(dict(section))

    class _Stub(base_robot.BaseRobot):
        NAME = "stub"
        ARM_NAMES = _ARMS
        QPOS = 12
        POSE = 12
        POSE_DIM_PER_ARM = 6
        GRIPPER = 2
        ARM_CONTROLLERS = {"left": "arm_left", "right": "arm_right"}

    robot = _Stub({"init_joint": [0.0] * 12, "gravity": {"enabled": False, "alpha": 0.8}})
    left, right = _Controller(), _Controller()
    robot.controllers = {"arm_left": left, "arm_right": right}
    assert robot.set_gravity(True, 0.5) == ["arm_left", "arm_right"]
    assert left.calls == [{"enabled": True, "alpha": 0.5}]
    robot.set_gravity(False)  # 不带 alpha → 沿用配置里的 0.8
    assert right.calls[-1] == {"enabled": False, "alpha": 0.8}
