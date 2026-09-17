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

"""LLM 轨迹策略测试 —— 轨迹解析 / 校验 / 重采样 / 客户端失败策略（无网络无硬件）。

覆盖：模型输出（含代码块包裹 / 缺字段 / 非法臂名 / 时间非单调）的解析与拒绝、
重采样（线性插值 / 夹爪分段常数 / 未提及臂保持 / 单步位移限幅）、
客户端的 prompt 门控、失败即不下发、历史裁剪与图像编码。
"""

import json

import cv2
import httpx
import numpy as np
import pytest

from motrix_edge.adapter.base import ActionSpace
from motrix_edge.policy import POLICY_REGISTRY, get_policy, policy_features
from motrix_edge.policy.llm.client import LLMPolicyClient
from motrix_edge.policy.llm.trajectory import (
    TrajectoryError,
    extract_json_object,
    hold_from_observation,
    parse_trajectory,
    resample_trajectory,
    trajectory_block,
)

_ARMS = ["left", "right"]


def _point(t=0.0, pos=(0.0, 0.0, 0.0), rot=(0.0, 0.0, 0.0), gripper=0.0, arm="left"):
    return {"t": t, "arm": arm, "pos": list(pos), "rot": list(rot), "gripper": gripper}


def _payload(*points):
    return {"trajectory": list(points)}


# ---- 解析 / 校验 ---------------------------------------------------------------


def test_single_arm_layout_allows_missing_arm_field():
    """单臂布局：点可省略 arm（默认该臂）。"""
    points = parse_trajectory(_payload({"t": 0, "pos": [0, 0, 0]}), arms=["left"])
    assert len(points) == 1 and points[0].arm == "left"


def test_multi_arm_layout_requires_arm_field():
    """多臂布局：点缺 arm → 非法（避免「未标臂」被误解为两臂同时动）。"""
    with pytest.raises(TrajectoryError, match="must declare 'arm'"):
        parse_trajectory(_payload({"t": 0, "pos": [0, 0, 0]}), arms=_ARMS)


def test_unknown_arm_is_rejected():
    with pytest.raises(TrajectoryError, match="unknown arm"):
        parse_trajectory(_payload(_point(arm="middle")), arms=_ARMS)


def test_non_monotonic_time_is_rejected():
    with pytest.raises(TrajectoryError, match="non-decreasing"):
        parse_trajectory(_payload(_point(t=1.0), _point(t=0.5)), arms=_ARMS)


def test_bad_pos_is_rejected():
    with pytest.raises(TrajectoryError, match="pos"):
        parse_trajectory(_payload({"t": 0, "arm": "left", "pos": [0, 0]}), arms=_ARMS)


def test_missing_rot_and_gripper_inherit_previous_point():
    """rot / gripper 缺省 → 继承上一点（模型可只给变化量）。"""
    points = parse_trajectory(
        _payload(_point(gripper=0.7, rot=(1.0, 0.0, 0.0)), {"t": 1.0, "arm": "left", "pos": [0.1, 0, 0]}),
        arms=_ARMS,
    )
    assert points[1].rot.tolist() == [1.0, 0.0, 0.0]
    assert points[1].gripper == 0.7


def test_gripper_out_of_range_is_clipped():
    points = parse_trajectory(_payload(_point(gripper=3.5)), arms=_ARMS)
    assert points[0].gripper == 1.0


def test_points_truncated_to_max_points():
    payload = _payload(*[_point(t=float(i), pos=[i * 0.01, 0, 0]) for i in range(10)])
    points = parse_trajectory(payload, arms=_ARMS, max_points=4)
    assert len(points) == 4 and points[-1].pos[0] == pytest.approx(0.03)


def test_extract_json_object_tolerates_fences_and_prose():
    """模型输出常带 ```json 包裹与解说文字：取首个 JSON 对象。"""
    text = 'Here is the plan:\n```json\n{"trajectory": [{"t": 0, "arm": "left", "pos": [0, 0, 0]}]}\n```'
    body = extract_json_object(text)
    assert len(body["trajectory"]) == 1


def test_extract_json_object_rejects_plain_text():
    with pytest.raises(TrajectoryError):
        extract_json_object("no json here")


# ---- 重采样 -------------------------------------------------------------------


def test_resample_linear_interpolation_and_gripper_step():
    """位置线性插值；夹爪分段常数（开关型执行器不插值）。"""
    points = parse_trajectory(
        _payload(
            {**_point(t=0.0, pos=(0.0, 0.0, 0.0), gripper=0.0), "arm": "left"},
            {**_point(t=1.0, pos=(1.0, 0.0, 0.0), gripper=1.0), "arm": "left"},
        ),
        arms=["left"],
    )
    block = resample_trajectory(points, horizon=5, arms=["left"])
    assert block.shape == (5, 7)
    assert [round(float(v), 3) for v in block[:, 0]] == [0.0, 0.25, 0.5, 0.75, 1.0]
    # 夹爪只在该点时间被采样到时才切换（t=1.0 的最后一个采样点）→ 前 4 步保持 0.0
    assert block[:4, 6].tolist() == [0.0, 0.0, 0.0, 0.0]
    assert block[4, 6] == 1.0


def test_resample_holds_arms_absent_from_trajectory():
    """轨迹只提到右臂：左臂用 hold（当前位姿 + 夹爪）保持，不被拉回零位。"""
    points = parse_trajectory(_payload(_point(arm="right", pos=(0.2, 0, 0))), arms=_ARMS)
    hold = {"left": np.array([0.5, 0.4, 0.3, 0.0, 0.0, 0.0, 0.9])}
    block = resample_trajectory(points, horizon=3, arms=_ARMS, hold=hold)
    assert block[:, 0].tolist() == [0.5, 0.5, 0.5]
    assert block[:, 6].tolist() == [0.9, 0.9, 0.9]
    assert block[:, 7].tolist() == [0.2, 0.2, 0.2]


def test_resample_scales_block_when_step_too_large():
    """单步位移超限 → 整块按比例缩放（以块首点为锚，不改变形状方向）。"""
    points = parse_trajectory(
        _payload(_point(t=0.0, pos=(0, 0, 0), arm="left"), _point(t=1.0, pos=(1.0, 0, 0), arm="left")),
        arms=["left"],
    )
    block = resample_trajectory(points, horizon=5, arms=["left"], max_pose_step=0.05)
    steps = np.diff(block[:, 0])
    assert float(np.max(steps)) <= 0.05 + 1e-9
    assert block[0, 0] == 0.0  # 首点不动


def test_hold_from_observation_uses_pose_and_gripper():
    pose = np.arange(12, dtype=float)  # 两臂 × 6 维
    qpos = np.array([0, 0, 0, 0, 0, 0, 0.3, 1, 1, 1, 1, 1, 1, 0.8], dtype=float)
    hold = hold_from_observation(pose, qpos, ["left", "right"])
    assert hold["left"].tolist() == [0, 1, 2, 3, 4, 5, 0.3]
    assert hold["right"].tolist() == [6, 7, 8, 9, 10, 11, pytest.approx(0.8)]


def test_trajectory_block_composes_parse_and_resample():
    payload = _payload(_point(arm="left", pos=(0, 0, 0)), _point(t=0.5, arm="left", pos=(0.1, 0, 0)))
    block = trajectory_block(payload, arms=["left"], horizon=4)
    assert block.shape == (4, 7)


# ---- 客户端（fake HTTP，无网络）------------------------------------------------


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class _FakeHTTP:
    """记录请求体并返回固定内容的假 HTTP 客户端。"""

    def __init__(self, content="", status_code=200, finish_reason=None):
        self.content = content
        self.status_code = status_code
        self.finish_reason = finish_reason
        self.payloads: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.payloads.append({"url": url, "json": json, "headers": headers})
        if self.status_code >= 400:
            return _FakeResponse({}, self.status_code)
        choice = {"message": {"content": self.content}}
        if self.finish_reason is not None:
            choice["finish_reason"] = self.finish_reason
        return _FakeResponse({"choices": [choice]})

    def get(self, url, headers=None):
        return _FakeResponse({"data": []})

    def close(self):
        pass


def _jpeg():
    ok, buf = cv2.imencode(".jpg", np.full((8, 8, 3), 120, dtype=np.uint8))
    assert ok
    return buf.tobytes()


def _observation(pose=None):
    return {
        "observations/qpos": np.zeros(14, dtype=np.float32),
        "observations/pose": np.asarray(np.zeros(12) if pose is None else pose, dtype=np.float32),
        "observations/images/cam_head": _jpeg(),
        "observations/images/cam_left_wrist": _jpeg(),
    }


def _client(monkeypatch, content="", status_code=200, finish_reason=None, **cfg):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    client = LLMPolicyClient({"model": "test-model", "api_key_env": "TEST_LLM_KEY", "horizon": 5, **cfg})
    client.connect()
    client._http = _FakeHTTP(content=content, status_code=status_code, finish_reason=finish_reason)
    client.bind_adapter(action_dim=14, camera_names=["cam_head"], arms=_ARMS)
    return client


def _trajectory_content(arms=_ARMS, points=6):
    return json.dumps(
        {
            "reasoning": "move",
            "trajectory": [
                {"t": i * 0.2, "arm": arms[i % len(arms)], "pos": [0.1 * i, 0.0, 0.0], "rot": [0, 0, 0], "gripper": 0.0}
                for i in range(points)
            ],
        }
    )


def test_llm_policy_declares_prompt_gate_and_cartesian_space():
    assert LLMPolicyClient.requires_prompt is True
    assert LLMPolicyClient.action_space is ActionSpace.CARTESIAN_POSE
    assert POLICY_REGISTRY["llm"][1] == "LLMPolicyClient"
    assert policy_features("llm")["requires_prompt"] is True


def test_get_policy_builds_llm_client():
    policy = get_policy({"policy": {"type": "llm", "model": "m"}}, policy_type="llm")
    assert isinstance(policy, LLMPolicyClient)


def test_infer_chunk_returns_cartesian_block_and_records_history(monkeypatch):
    client = _client(monkeypatch, content="```json\n" + _trajectory_content() + "\n```")
    client.prompt = "stack the blocks"
    obs = _observation(pose=[0.1 * i for i in range(12)])
    chunk = client.infer_chunk(obs, index=7)
    assert chunk is not None
    assert chunk.actions.shape == (5, 14)  # horizon × (7 × 臂数)
    assert chunk.start_index == 7
    assert len(client._history) == 1
    payload = client._http.payloads[0]
    assert payload["url"].endswith("/chat/completions")
    assert payload["headers"]["Authorization"] == "Bearer secret"
    content = payload["json"]["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in content)  # 图像已内联
    assert sum(1 for part in content if part.get("type") == "image_url") == 1  # 只发启用相机
    assert "stack the blocks" in content[0]["text"]


def test_infer_chunk_returns_none_without_prompt(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content())
    assert client.infer_chunk(_observation()) is None
    assert client._http.payloads == []  # 不下发请求


def test_infer_chunk_returns_none_on_http_error(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content(), status_code=500)
    client.prompt = "task"
    assert client.infer_chunk(_observation()) is None


def test_infer_chunk_returns_none_on_invalid_trajectory(monkeypatch):
    client = _client(monkeypatch, content="the arm should move left")
    client.prompt = "task"
    assert client.infer_chunk(_observation()) is None


def test_history_is_trimmed_to_configured_length(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content(), history_len=2)
    client.prompt = "task"
    for _ in range(4):
        client.infer_chunk(_observation())
    assert len(client._history) == 2


def test_reset_clears_history(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content())
    client.prompt = "task"
    client.infer_chunk(_observation())
    client.reset()
    assert client._history == []


def test_arms_from_adapter_layout_drive_dimension(monkeypatch):
    """只启用右臂：动作块 7 维（臂清单一是 adapter 的 enabled_arms）。"""
    client = _client(monkeypatch, content=_trajectory_content(arms=["right"]))
    client.bind_adapter(action_dim=7, camera_names=["cam_head"], arms=["right"])
    client.prompt = "task"
    chunk = client.infer_chunk(_observation(pose=[0.0] * 6))
    assert chunk is not None and chunk.actions.shape == (5, 7)


def test_state_text_reports_pose_per_arm(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content())
    client.prompt = "task"
    client.infer_chunk(_observation(pose=[0.11, 0.22, 0.33, 0, 0, 0, 0.44, 0.55, 0.66, 0, 0, 0]))
    text = client._http.payloads[0]["json"]["messages"][1]["content"][0]["text"]
    assert "pose_pos=[0.110, 0.220, 0.330]" in text
    assert "pose_pos=[0.440, 0.550, 0.660]" in text


def test_state_text_omits_joint_angles_and_keeps_gripper(monkeypatch):
    """state 只给末端位姿 + 夹爪：关节角不下发（笛卡尔策略用不到，省 token）。"""
    client = _client(monkeypatch, content=_trajectory_content())
    client.prompt = "task"
    obs = _observation(pose=[0.11, 0.22, 0.33, 0, 0, 0, 0.44, 0.55, 0.66, 0, 0, 0])
    obs["observations/qpos"] = np.array([0.1] * 6 + [0.7] + [0.2] * 6 + [0.3], dtype=np.float32)
    client.infer_chunk(obs)
    text = client._http.payloads[0]["json"]["messages"][1]["content"][0]["text"]
    assert "joints=" not in text  # 关节角不下发
    assert "pose_pos=[0.110, 0.220, 0.330] pose_rot=[0.000, 0.000, 0.000] gripper=0.70" in text
    assert "pose_pos=[0.440, 0.550, 0.660] pose_rot=[0.000, 0.000, 0.000] gripper=0.30" in text


def test_state_text_warns_once_without_pose(monkeypatch):
    """机器人不提供位姿观测时：仍发请求（图像在），但只提醒一次。"""
    client = _client(monkeypatch, content=_trajectory_content())
    client.prompt = "task"
    obs = _observation()
    obs.pop("observations/pose")
    client.infer_chunk(obs)
    client.infer_chunk(obs)
    assert client._pose_missing_logged is True


def test_config_snapshot_hides_secret(monkeypatch):
    client = _client(monkeypatch, content=_trajectory_content())
    snapshot = client.config_snapshot()
    assert snapshot["api_key_present"] is True
    assert snapshot["api_key_source"] == "env"  # 本用例只设了环境变量
    assert "secret" not in json.dumps(snapshot)


def test_runtime_api_key_takes_priority_over_env(monkeypatch):
    """密钥来源：运行时值（前端表单 / infer config set）优先，且不回显到状态里。"""
    client = _client(monkeypatch, content=_trajectory_content())
    client.policy_config["api_key"] = "runtime-only-key"
    client.prompt = "task"
    client.infer_chunk(_observation())
    headers = client._http.payloads[0]["headers"]
    assert headers["Authorization"] == "Bearer runtime-only-key"  # 优先于 TEST_LLM_KEY=secret
    snapshot = client.config_snapshot()
    assert snapshot["api_key_source"] == "runtime"
    assert "runtime-only-key" not in json.dumps(snapshot)


def test_runtime_api_key_alone_is_enough(monkeypatch):
    """只填运行时密钥（无环境变量）也能连接。"""
    monkeypatch.delenv("TEST_LLM_KEY", raising=False)
    client = LLMPolicyClient({"model": "test-model", "api_key_env": "TEST_LLM_KEY", "horizon": 5})
    with pytest.raises(ValueError, match="requires API key"):
        client.connect()  # 两边都为空：拒绝连接
    client.policy_config["api_key"] = "from-panel"
    client.connect()
    assert client.connected is True


# ---- 历史摘要（结构化）与截断告警 -----------------------------------------------


def test_history_uses_structured_summary(monkeypatch):
    """历史条目 = 每臂「起点 → 终点 + 夹爪变化」（结构化，不是模型原始输出）。"""
    content = json.dumps(
        {
            "trajectory": [
                {"t": 0.0, "arm": "left", "pos": [0.0, 0.0, 0.0], "rot": [0, 0, 0], "gripper": 0.0},
                {"t": 0.5, "arm": "left", "pos": [0.1, 0.0, 0.0], "rot": [0, 0, 0], "gripper": 1.0},
            ]
        }
    )
    client = _client(monkeypatch, content=content)
    client.prompt = "task"
    client.infer_chunk(_observation())
    assert client._history == [
        "left (0.000, 0.000, 0.000) -> (0.100, 0.000, 0.000), gripper 0.00 -> 1.00; "
        "right held at (0.000, 0.000, 0.000), gripper 0.00"
    ]
    # 第二次请求把摘要回灌给模型（结构化、单行）
    client.infer_chunk(_observation())
    text = client._http.payloads[1]["json"]["messages"][1]["content"][0]["text"]
    assert "Recent chunks (oldest first):" in text
    assert "- left (0.000, 0.000, 0.000) -> (0.100, 0.000, 0.000), gripper 0.00 -> 1.00" in text


def test_history_entry_truncation_warns(monkeypatch, capsys):
    """历史条目超长 → 截断到 HISTORY_ENTRY_MAX 并告警（正常结构化摘要远达不到）。"""
    client = _client(monkeypatch, content=_trajectory_content())
    client._remember("x" * 500)
    assert len(client._history[0]) == LLMPolicyClient.HISTORY_ENTRY_MAX
    assert "history entry truncated" in capsys.readouterr().out


def test_too_many_waypoints_warns(monkeypatch, capsys):
    """模型给的轨迹点数超 max_points → 截断并告警。"""
    content = json.dumps(
        {
            "trajectory": [
                {"t": i * 0.1, "arm": "left", "pos": [0.01 * i, 0, 0], "rot": [0, 0, 0], "gripper": 0.0}
                for i in range(20)
            ]
        }
    )
    client = _client(monkeypatch, content=content, max_points=5)
    client.prompt = "task"
    assert client.infer_chunk(_observation()) is not None
    out = capsys.readouterr().out
    assert "20 waypoints" in out and "truncated to max_points=5" in out


def test_finish_reason_length_warns(monkeypatch, capsys):
    """响应被 max_tokens 截断（finish_reason=length）→ 告警。"""
    client = _client(monkeypatch, content=_trajectory_content(), finish_reason="length")
    client.prompt = "task"
    assert client.infer_chunk(_observation()) is not None  # JSON 完整时仍可下发
    assert "finish_reason=length" in capsys.readouterr().out
