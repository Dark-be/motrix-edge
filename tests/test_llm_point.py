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

"""模拟 LLM 端点 + 模拟机器人笛卡尔链路的测试（无网络无硬件）。

覆盖：``SimLLMCore`` 的观测解析与轨迹生成（朝着目标收敛、夹爪在接近目标后闭合）、
``SimRobotCore`` 的简易运动学（FK / IK 互逆）、笛卡尔 rollout 走 IK 分支、位姿观测合成。
"""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

# scripts/ 非安装包：把仓库根加入 sys.path 以便导入虚拟端点模块（与 test_robot_sdk 同源）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motrix_edge.policy.llm.client import LLMPolicyClient  # noqa: E402
from scripts.test_llm_point import SimLLMCore, create_app  # noqa: E402
from scripts.test_robot_sdk import SimRobotCore  # noqa: E402

# ---- 模拟 LLM 端点 -------------------------------------------------------------


def test_parse_poses_reads_per_arm_positions():
    text = (
        "Task: stack\n- left: pose_pos=[0.100, 0.200, 0.300] pose_rot=[0.000, 0.000, 0.000]\n"
        "- right: pose_pos=[-0.100, 0.000, 0.500] pose_rot=[0.000, 0.000, 0.000]"
    )
    poses = SimLLMCore.parse_poses(text)
    assert poses == {"left": [0.1, 0.2, 0.3], "right": [-0.1, 0.0, 0.5]}


def test_trajectory_moves_toward_target_and_closes_gripper():
    core = SimLLMCore(target=(0.30, 0.0, 0.15), arm="left", points=6, step=0.05, close_distance=0.05)
    points = core.trajectory({"left": [0.10, 0.0, 0.15]})
    assert len(points) == 6
    assert all(point["arm"] == "left" for point in points)
    x_first, x_last = points[0]["pos"][0], points[-1]["pos"][0]
    assert x_first > 0.10 and x_last > x_first  # 单调朝目标前进
    assert x_last <= 0.30 + 1e-9
    assert [point["t"] for point in points] == sorted(point["t"] for point in points)
    assert points[0]["gripper"] == 0.0  # 尚未接近目标
    assert any(point["gripper"] == 1.0 for point in points)  # 接近目标后闭合


def test_chat_completions_returns_fenced_json():
    """端点返回 ```json 包裹的内容（同时校验 edge 侧解析健壮性）。"""
    core = SimLLMCore(target=(0.3, 0.0, 0.15), arm="left", points=3)
    app = create_app(core)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/v1/chat/completions")
    body = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "- left: pose_pos=[0.000, 0.000, 0.000]"}]},
        ]
    }
    response = route.endpoint(body)
    assert core.requests == 1
    content = response["choices"][0]["message"]["content"]
    assert content.startswith("```json")
    payload = json.loads(content.strip("`").strip().removeprefix("json"))
    assert len(payload["trajectory"]) == 3


# ---- 模拟机器人：简易运动学与笛卡尔执行 ------------------------------------------


def test_fk_ik_roundtrip():
    core = SimRobotCore()
    qpos = np.arange(core.action_dim, dtype=np.float64) * 0.1
    qpos[6], qpos[13] = 0.3, 0.8  # 夹爪在 [0, 1] 内
    pose = core.fk_pose(qpos)  # [12] = 两臂 × 6
    assert pose.shape == (core.pose_dim,)
    # 每臂动作 = 6 维位姿 + 夹爪（夹爪按 qpos 原值透传）
    pose_action = np.concatenate([pose[:6], [qpos[6]], pose[6:], [qpos[13]]])
    recovered = core._ik_action(pose_action)
    assert recovered[0:6] == pytest.approx(qpos[0:6])
    assert recovered[6] == pytest.approx(qpos[6])
    assert recovered[7:13] == pytest.approx(qpos[7:13])
    assert recovered[13] == pytest.approx(qpos[13])


def test_cartesian_rollout_goes_through_ik():
    core = SimRobotCore()
    pose_action = np.array([0.3, 0.0, 0.15, 0.0, 0.0, 0.0, 1.0, 0.4, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0])
    core.rollout(pose_action, action_space="cartesian_pose")
    assert core.rollout_spaces == ["cartesian_pose"]
    assert core._target is not None
    assert core._target.shape == (core.action_dim,)
    # IK 结果再 FK 应回到指令位姿（线性映射互逆）
    assert core.fk_pose(core._target).tolist() == pytest.approx(
        np.concatenate([pose_action[:6], pose_action[7:13]]).tolist()
    )


def test_joint_rollout_keeps_legacy_default():
    core = SimRobotCore()
    core.rollout(np.zeros(core.action_dim))
    assert core.rollout_spaces == ["joint"]


def test_frame_carries_pose():
    core = SimRobotCore()
    frame = core.frame()
    assert frame["observations/pose"].shape == (12,)
    qpos = frame["observations/qpos"].astype(np.float64)
    assert frame["observations/pose"].tolist() == pytest.approx(core.fk_pose(qpos).tolist(), abs=1e-6)


# ---- 闭环收敛（模拟 LLM + 模拟机器人 + 真实 policy 客户端）----------------------


class _SimResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _SimHTTP:
    """把 ``LLMPolicyClient`` 的 HTTP 调用直接接到 ``SimLLMCore``（进程内闭环，无网络）。"""

    def __init__(self, core):
        self.core = core
        self.requests = 0

    def post(self, url, json=None, headers=None):
        self.requests += 1
        return _SimResponse(self.core.reply(json))

    def get(self, url, headers=None):
        return _SimResponse({"data": []})

    def close(self):
        pass


def _observation_from(core: SimRobotCore) -> dict:
    """模拟机器人观测 → edge 观测契约（qpos + pose；本用例不带图像）。"""
    return {
        "observations/qpos": core._qpos.astype(np.float32),
        "observations/pose": core.fk_pose().astype(np.float32),
    }


def test_closed_loop_converges_to_target(monkeypatch):
    """端到端：LLM 轨迹 → 解析重采样 → 笛卡尔动作 → 模拟 IK → 限速执行 → 观测回灌 → 收敛。

    模拟 LLM 每次回复都朝目标前进一小段（真机上的「闭环重规划」），验证笛卡尔动作空间
    与位姿观测这条新链路可以真正把机械臂收敛到目标位姿。
    """
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    target = (0.30, 0.0, 0.15)
    robot = SimRobotCore(random_walk=False)  # 关闭模拟遥操作随机输入：目标只由指令决定
    model = SimLLMCore(target=target, arm="left", points=6, dt=0.3, step=0.05, close_distance=0.05)
    http = _SimHTTP(model)

    client = LLMPolicyClient(
        {"model": "sim", "api_key_env": "TEST_LLM_KEY", "horizon": 8, "max_pose_step": 0.0, "history_len": 2}
    )
    client.connect()
    client._http = http
    client.bind_adapter(action_dim=14, arms=["left", "right"])
    client.prompt = "stack the blocks"

    for index in range(4):  # 最多 4 轮「观测 → 推理 → 执行」
        chunk = client.infer_chunk(_observation_from(robot), index=index * 8)
        assert chunk is not None and chunk.actions.shape == (8, 14)
        for action in chunk.actions:
            robot.rollout(action, action_space="cartesian_pose")
            for _ in range(8):  # 模拟机器人 30Hz 限速步进（逼近当前指令）
                robot.step()

    for _ in range(60):  # 收尾：继续按控制频率步进，把最后一条指令走完（限速执行）
        robot.step()

    pose = robot.fk_pose()
    assert pose[0:3] == pytest.approx(list(target), abs=0.01)  # 收敛到目标位置
    assert robot.rollout_spaces == ["cartesian_pose"] * 32  # 全程按笛卡尔语义下发
    assert http.requests >= 2  # 至少两轮闭环（不是一次开环执行）
