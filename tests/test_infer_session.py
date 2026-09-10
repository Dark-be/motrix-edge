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

"""InferSession 推理循环测试 —— fake adapter + fake policy + fake signal source。

覆盖：单步 / 持续推理（经 RTCManager 步进）、prompt 门控（为空不能推理/录制）、推理时
rollout 录制（capture episode start/end + capture sync）、多步 & drain 已取消、急停安全停止、
ready 前退出，无网络无硬件可跑。
"""

import shlex
from types import SimpleNamespace

import numpy as np

from motrix_edge.session import infer_session
from motrix_edge.session.base import RunResult
from motrix_edge.utils.commands import build_command_registry

_REGISTRY = build_command_registry()


def make_signals(*seq):
    """命令词（空格分隔，不用点）→ Command（经注册表解析，与 CLI 一致），耗尽后返回 None。"""
    it = iter(seq)

    def source():
        cmd = next(it, None)
        if cmd is None:
            return None
        return _REGISTRY.parse_argv(shlex.split(cmd)) if isinstance(cmd, str) else cmd

    return source


class _FakePolicy:
    def __init__(self):
        self.infer_calls = 0
        self.reset_calls = 0
        self.disconnect_calls = 0
        self.prepare_calls = 0
        self.connect_calls = 0
        self.prompt = None  # 统一文本指令（会话内 infer prompt 预置；推理/录制前必须非空）
        self.bind_calls = 0  # bind_adapter（adapter 布局传入）
        self.bound_cameras = None
        self.bound_action_dim = None
        self.action = np.arange(14, dtype=float)
        self.connected = False
        self.server_metadata = {}

    def bind_adapter(self, action_dim=None, camera_names=None):
        self.bind_calls += 1
        self.bound_action_dim = action_dim
        self.bound_cameras = camera_names

    def connect(self):
        self.connect_calls += 1
        self.connected = True

    def ensure_connected(self):
        if not self.connected:
            self.connect()

    def prepare(self, obs=None):
        self.prepare_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    def reset(self):
        self.reset_calls += 1

    def infer_chunk(self, observation, index=None):
        """策略只负责取推理结果：返回**原始动作块**（RTCManager 负责缓存 / 切分 / 平滑）。

        给足 50 步，避免 RTC 在短块上频繁预取（与真实策略块长一致）。
        """
        self.infer_calls += 1
        return np.tile(np.asarray(self.action, dtype=float), (50, 1))


class _FakeAdapter:
    def __init__(self, ready=True, observations=None, images=None, action_dim=None):
        self.ready = ready
        self.safe_stop_calls = 0
        self.executed = []
        self.reset_calls = 0
        self.teleop_values: list[bool] = []
        self.images = list(images) if images is not None else None  # 启用相机（adapter config 决定）
        self.action_dim = action_dim  # 启用臂 qpos 维数
        self.capabilities = SimpleNamespace(supports=lambda cap: True)  # EXECUTE 能力校验通过
        self._observations = iter(observations) if observations is not None else None
        # 录制（capture episode start/end）+ 采集元信息同步记录
        self.start_capture_calls = 0
        self.end_capture_calls = 0
        self.synced_meta: list[dict] = []

    def release(self):
        pass

    def health(self):
        return SimpleNamespace(ok=self.ready)

    def reset(self):
        self.reset_calls += 1

    def observe(self):
        if self._observations is not None:
            return next(self._observations, None)
        return {"observations/qpos": np.zeros(14, dtype=np.float32)}

    def execute(self, action):
        self.executed.append(action)

    def set_teleop(self, enabled):
        self.teleop_values.append(bool(enabled))

    def rollout(self, action):
        self.executed.append(action)

    def start_capture(self):
        self.start_capture_calls += 1

    def end_capture(self):
        self.end_capture_calls += 1

    def sync_capture_meta(self, meta):
        self.synced_meta.append(meta)

    def safe_stop(self):
        self.safe_stop_calls += 1


def _build_session(adapter, policy, signals):
    # infer_freq 1000 → 主循环几乎不 sleep，测试快；adapter 由节点注入（会话只引用）
    return infer_session.InferSession(
        {"policy": {"infer_freq": 1000}}, command_source=make_signals(*signals), adapter=adapter
    )


def _patch(monkeypatch, policy):
    """注入 fake policy；adapter 由 _build_session 直接注入（会话不再自建）。"""
    monkeypatch.setattr(infer_session, "get_policy", lambda cfg, **kw: policy)


def test_infer_loop_runs_observation_to_action(monkeypatch):
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    # infer prompt 预置 → 显式 infer connect → 单步推理 → 退出
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", "infer connect", "infer rollout", "session quit")
    )
    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 1  # 一次 infer rollout → obs → infer → action
    assert len(adapter.executed) == 1
    assert adapter.safe_stop_calls == 0


def test_infer_rollout_single_step_replies_action(monkeypatch):
    """infer rollout（单步）：一次 观测 → 推理 → 动作下发，回执 count=1/action/actions。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 1
    assert len(adapter.executed) == 1
    assert replies[0].status == "ok"
    assert replies[0].data["count"] == 1
    assert replies[0].data["action"] == list(np.arange(14, dtype=float))
    assert len(replies[0].data["actions"]) == 1


def test_infer_rollout_requires_prompt(monkeypatch):
    """prompt 为空不能开始推理：infer rollout（未设 prompt）→ rejected（不推理）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, (rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "prompt required" in replies[0].error


def test_infer_rollout_continuous_requires_prompt(monkeypatch):
    """prompt 为空不能开始推理：infer rollout continuous（未设 prompt）→ rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    cont = _REGISTRY.parse_argv(["infer", "rollout", "continuous"])
    cont.reply_to = replies.append
    session = _build_session(adapter, policy, (cont, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400


def test_infer_rollout_rejects_multi_step(monkeypatch):
    """多步推理已取消：infer rollout 3 → rejected（提示用单步/持续+录制）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout", "3"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "multi-step rollout removed" in replies[0].error


def test_infer_rollout_rejects_drain(monkeypatch):
    """缓存推理（drain）已取消：infer rollout drain → rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    drain = _REGISTRY.parse_argv(["infer", "rollout", "drain"])
    drain.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", drain, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "drain mode removed" in replies[0].error


def test_infer_rollout_rejects_invalid_mode(monkeypatch):
    """非法 rollout 参数（0 / 非 continuous 文本）→ rejected（不崩溃）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    bad = _REGISTRY.parse_argv(["infer", "rollout", "0"])
    bad.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", bad, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400


def test_infer_prompt_command_sets_policy_prompt(monkeypatch):
    """会话内 ``infer prompt <text>``：预置推理文本指令（推理/录制前必须非空）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    prompt_cmd = _REGISTRY.parse_argv(["infer", "prompt", "把零件放好"])
    prompt_cmd.reply_to = replies.append
    session = _build_session(adapter, policy, (prompt_cmd, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.prompt == "把零件放好"
    assert session.prompt == "把零件放好"  # 会话 status 上报当前 prompt
    assert replies[0].status == "ok"
    assert replies[0].data["prompt"] == "把零件放好"


def test_infer_prompt_requires_text(monkeypatch):
    """``infer prompt`` 缺文本 → rejected（不崩溃，不误改 prompt）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    bad = _REGISTRY.parse_argv(["infer", "prompt"])
    bad.reply_to = replies.append
    session = _build_session(adapter, policy, (bad, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.prompt is None
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400


def test_infer_capture_episode_recording_toggles(monkeypatch):
    """推理时 rollout 录制：capture episode start → adapter.start_capture + recording；
    episode end → adapter.end_capture，recording 复位。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    start = _REGISTRY.parse_argv(["capture", "episode", "start"])
    end = _REGISTRY.parse_argv(["capture", "episode", "end"])
    start.reply_to = replies.append
    end.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", start, end, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.start_capture_calls == 1
    assert adapter.end_capture_calls == 1
    assert session.recording is False  # episode end 后复位
    assert replies[0].status == "ok"
    assert replies[0].data["episode"] == "start"
    assert replies[0].data["recording"] is True
    assert replies[1].data["episode"] == "end"
    assert replies[1].data["recording"] is False


def test_infer_capture_episode_start_requires_prompt(monkeypatch):
    """录制 rollout 需要 task_name=prompt：prompt 为空时 capture episode start → rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    start = _REGISTRY.parse_argv(["capture", "episode", "start"])
    start.reply_to = replies.append
    session = _build_session(adapter, policy, (start, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.start_capture_calls == 0
    assert session.recording is False
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "prompt required" in replies[0].error


def test_infer_capture_sync_syncs_meta(monkeypatch):
    """推理录制同步采集元信息：capture sync --meta <json> → adapter.sync_capture_meta。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    sync = _REGISTRY.parse_argv(["capture", "sync", '{"operator": "policy", "task_name": "把零件放好"}'])
    sync.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", sync, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.synced_meta == [{"operator": "policy", "task_name": "把零件放好"}]
    assert replies[0].status == "ok"
    assert replies[0].data["meta"] == {"operator": "policy", "task_name": "把零件放好"}


def test_infer_capture_sync_requires_meta(monkeypatch):
    """capture sync 缺 meta → rejected（不崩溃）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    bad = _REGISTRY.parse_argv(["capture", "sync"])
    bad.reply_to = replies.append
    session = _build_session(adapter, policy, (bad, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.synced_meta == []
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400


def test_infer_endpoint_set_locked_inside_session(monkeypatch):
    """推理会话内端点锁定：infer ip set / port set 被拒绝（进入会话前经 enter 设置）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    ip_set = _REGISTRY.parse_argv(["infer", "ip", "set", "10.0.0.5"])
    ip_set.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer connect", ip_set, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 409


def test_infer_session_binds_adapter_layout_to_policy(monkeypatch):
    """进入推理会话把 adapter 启用的布局（qpos 维数 + 相机名）传给策略（bind_adapter）。

    策略侧无需另读 edge.yml 相机名：相机名单一事实来源 = adapter 运行时配置。
    """
    adapter = _FakeAdapter(ready=True, images=["cam_head", "cam_right_wrist"], action_dim=7)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ("infer connect", "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.bind_calls == 1  # InferSession 构造即 bind
    assert policy.bound_action_dim == 7  # 单臂 qpos 维数
    assert policy.bound_cameras == ["cam_head", "cam_right_wrist"]  # adapter 启用相机


def test_infer_robot_reset_replies_ok(monkeypatch):
    """会话内 robot reset：调用 adapter.reset 并回执 ok（不阻塞 submit）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    reset = _REGISTRY.parse_argv(["robot", "reset"])
    reset.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer connect", reset, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.reset_calls >= 2  # run() 开头复位 + 命令复位
    assert replies[0].status == "ok"


def test_infer_wait_ready_robot_reset_replies(monkeypatch):
    """等待就绪阶段 robot reset：调用 adapter.reset 并回执 ok（不阻塞 submit）。"""
    adapter = _FakeAdapter(ready=False)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    reset = _REGISTRY.parse_argv(["robot", "reset"])
    reset.reply_to = replies.append
    session = _build_session(adapter, policy, (reset, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.reset_calls >= 2  # run() 开头复位 + 等待就绪阶段命令复位
    assert replies[0].status == "ok"


def test_infer_rollout_continuous_replies_started_and_stops(monkeypatch):
    """infer rollout continuous：启动即回执 started，持续推理直到 session quit 停止。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    cont = _REGISTRY.parse_argv(["infer", "rollout", "continuous"])
    cont.reply_to = replies.append
    # None = 无命令空档：让持续推理推一步后再 session quit
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", cont, None, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["state"] == "continuous"
    assert replies[0].data["started"] is True
    assert policy.infer_calls >= 1  # 持续推理至少推了一步
    assert adapter.executed  # 有动作下发


def test_infer_continuous_records_episode(monkeypatch):
    """持续推理期间可录制 rollout：capture episode start/end 在持续循环内被消费。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    start = _REGISTRY.parse_argv(["capture", "episode", "start"])
    end = _REGISTRY.parse_argv(["capture", "episode", "end"])
    session = _build_session(
        adapter,
        policy,
        ("infer prompt 把零件放好", "infer rollout continuous", start, end, "session quit"),
    )

    assert session.run() == RunResult.FINISHED
    assert adapter.start_capture_calls == 1
    assert adapter.end_capture_calls == 1
    assert session.recording is False


def test_infer_rollout_without_frame_is_rejected_not_error(monkeypatch):
    """共享内存尚无首帧时，本次 rollout 返回 503，但会话继续等待后续命令。"""
    adapter = _FakeAdapter(ready=True, observations=(None,))
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert adapter.executed == []
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 503
    assert replies[0].error == "observation not ready"


def test_estop_during_infer_safe_stops(monkeypatch):
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ("robot estop",))
    assert session.run() == RunResult.ERROR
    assert adapter.safe_stop_calls >= 1


def test_quit_before_ready(monkeypatch):
    adapter = _FakeAdapter(ready=False)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ("session quit",))
    assert session.run() == RunResult.FINISHED


def test_robot_execute_in_infer_loop(monkeypatch):
    """推理循环中 robot execute：qpos 直接作为参数 → adapter.execute。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ("robot execute 0,0,0,0,0,0,0", "session quit"))
    assert session.run() == RunResult.FINISHED
    assert adapter.executed == [[0.0] * 7]  # qpos 直接作为参数传给 adapter.execute


def test_infer_rollout_auto_connects(monkeypatch):
    """未显式 infer connect：rollout 前惰性自动连接（policy.ensure_connected）并推理。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 自动连接一次，无需先 infer connect
    assert policy.infer_calls == 1
    assert len(adapter.executed) == 1
    assert replies[0].status == "ok"


def test_infer_rollout_auto_connect_failure_replies_error(monkeypatch):
    """rollout 自动连接失败 → 回执 error（502），不执行推理，可重试。"""

    class _FailConnectPolicy(_FakePolicy):
        def connect(self):
            self.connect_calls += 1
            raise OSError("inference server not reachable")

    adapter = _FakeAdapter(ready=True)
    policy = _FailConnectPolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 单次尝试，不无限重试
    assert policy.infer_calls == 0
    assert replies[0].status == "error"
    assert replies[0].status_code == 502


def test_infer_connect_success_replies_metadata(monkeypatch):
    """infer connect（可选）：预连成功 → 回执 metadata，并用当前观测预热 prepare(obs)。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    policy.server_metadata = {"action_horizon": 16}
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    session = _build_session(adapter, policy, (connect, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert session.connected is True
    assert policy.connect_calls == 1
    assert policy.prepare_calls == 1  # 预热：adapter 有帧 → policy.prepare(obs)
    assert replies[0].status == "ok"
    assert replies[0].data["connected"] is True
    assert replies[0].data["metadata"] == {"action_horizon": 16}


def test_infer_connect_failure_replies_error(monkeypatch):
    """infer connect：单次尝试失败 → 回执 error（502），连接状态保持未连接，可重试。"""

    class _ConnectingPolicy(_FakePolicy):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0

        def connect(self):
            self.connect_calls += 1
            raise OSError("inference server not reachable")

    adapter = _FakeAdapter(ready=True)
    policy = _ConnectingPolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    session = _build_session(adapter, policy, (connect, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 单次尝试，不无限重试
    assert session.connected is False
    assert replies[0].status == "error"
    assert replies[0].status_code == 502
    assert "infer connect failed" in replies[0].error


def test_infer_stop_returns_error(monkeypatch):
    """外部请求停止（stop）：主循环立即返回 ERROR（node 失联 ERROR 时终止任务线程）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ())
    session.session_start()
    session.stop()
    assert session.run() == RunResult.ERROR
    assert session.state == infer_session.SessionState.ERROR


def test_robot_teleop_in_infer_loop(monkeypatch):
    """推理循环中 robot teleop：true/false 直接作为参数 → adapter.set_teleop。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    session = _build_session(adapter, policy, ("robot teleop true", "session quit"))
    assert session.run() == RunResult.FINISHED
    assert adapter.teleop_values == [True]
