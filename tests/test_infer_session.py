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
import threading
import time
from types import SimpleNamespace

import numpy as np

from motrix_edge.command import META_REPLY_DEADLINE, build_command_registry
from motrix_edge.errors import ErrorCode
from motrix_edge.session import infer_session
from motrix_edge.session.base import RunResult

_REGISTRY = build_command_registry()


def _as_command(item):
    """命令序列项 → Command：字符串按 CLI 解析；可调用项先调用（注入副作用 / 采样状态）。"""
    if callable(item):
        item = item()
    return _REGISTRY.parse_argv(shlex.split(item)) if isinstance(item, str) else item


def make_signals(*seq):
    """命令词（空格分隔，不用点）→ Command（经注册表解析，与 CLI 一致），耗尽后返回 None。

    序列里的**可调用项**会被调用（用于注入副作用，如模拟连接丢失），其返回值才是要发的命令。
    """
    it = iter(seq)

    def source():
        cmd = next(it, None)
        return None if cmd is None else _as_command(cmd)

    return source


class _FakePolicy:
    def __init__(self, requires_prompt=False):
        self.requires_prompt = requires_prompt  # 是否语言条件策略（openpi=True 门控；lerobot-act=False 不门控）
        self.infer_calls = 0
        self.reset_calls = 0
        self.disconnect_calls = 0
        self.prepare_calls = 0
        self.connect_calls = 0
        self.prompt = None  # 语言条件策略的配置项（会话内 infer prompt 预置；推理/录制前必须非空）
        self.bind_calls = 0  # bind_adapter（adapter 布局传入）
        self.bound_cameras = None
        self.bound_action_dim = None
        self.action = np.arange(14, dtype=float)
        self.connected = False
        self.server_metadata = {}
        # 本连接是否已真正取到过一块（策略基类同名字段）：预热据此决定要不要补一次 infer_chunk
        self.chunk_seen = False
        self.observed_chunk_len = None

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
        chunk = np.tile(np.asarray(self.action, dtype=float), (50, 1))
        self.chunk_seen = True  # 拿到真实块（与策略基类 _note_chunk_len 同效）
        self.observed_chunk_len = chunk.shape[0]
        return chunk


class _FakeAdapter:
    def __init__(self, ready=True, observations=None, images=None, action_dim=None):
        self.ready = ready
        self.safe_stop_calls = 0
        self.executed = []
        self.reset_calls = 0
        self.teleop_values: list[bool] = []
        self.teleop_calls: list[tuple[bool, str | None]] = []
        self.rollout_spaces: list[str | None] = []
        self.teleop_refused = False  # True = 模拟 SDK 409（遥操作中）：rollout 本拍被拒
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

    def set_teleop(self, enabled, mode=None):
        self.teleop_values.append(bool(enabled))
        self.teleop_calls.append((bool(enabled), mode))

    def rollout(self, action, action_space=None) -> bool:
        if self.teleop_refused:  # 模拟 SDK 409（遥操作 / 人工接管中）：本拍不下发
            return False
        self.executed.append(action)
        self.rollout_spaces.append(None if action_space is None else str(action_space))
        return True

    def start_capture(self):
        self.start_capture_calls += 1

    def end_capture(self):
        self.end_capture_calls += 1

    def sync_capture_meta(self, meta):
        self.synced_meta.append(meta)

    def safe_stop(self):
        self.safe_stop_calls += 1


def _build_session(adapter, policy, signals, warmup_required=True):
    """构造会话（**默认与生产一致**：``warmup_required=true``，即未预热不能 rollout）。

    与预热门正交的用例（推理循环 / 录制 / RTC / prompt 等机制）显式传 ``warmup_required=False``，
    这样「忘了显式声明」时会踩到生产缺省而不是悄悄绕过预热门。
    """
    # infer_freq 1000 → 主循环几乎不 sleep，测试快；adapter 由节点注入（会话只引用）
    cfg = {"policy": {"infer_freq": 1000, "warmup_required": warmup_required}}
    return infer_session.InferSession(cfg, command_source=make_signals(*signals), adapter=adapter)


# 时序前置条件的有界等待：测试用的 fake policy 是即时返回的，5s 已是数量级余量
_SETTLE_TIMEOUT = 5.0


def _warmup_settled(session) -> bool:
    """缺省时序前置条件：预热线程已收尾（``warming=False``）。"""
    return not session.warming


def _wait_for(check, expect, command):
    """标记一步：**先等 ``check(session)`` 成立**，成立后才下发 ``command``。

    用于「上一步启动的异步处理会改变某个状态值，改变后才能进行下一步」的严格时序校验——例：
    ``infer connect`` 启动异步预热后，``warmed_up`` 要从 False 跃迁到 True、``warmup_error``
    要由 ``None`` 变成失败原因，紧随其后的命令才该下发（否则读到的是中间态）。

    ``expect`` = 该前置状态的人类可读描述（超时失败信息里会带上）。
    """
    return (command, check, expect)


def _warmup_gated_source(holder, *seq):
    """命令源：**每一步都在时序前置条件成立后**才下发（对异步命令的严格时序校验）。

    预热是异步的（``infer connect`` 立即回执 + 工作线程），紧随其后的命令若不等它收尾就会读到
    中间态。历史事故：``test_infer_connect_rewarms_after_connection_loss`` 的探针与第一轮预热
    赛跑，``warmed_up`` 因「预热未结束」而为 False（**恰好**满足断言，理由却是错的），紧接着
    ``"connection lost" in None`` 抛 TypeError（复现率约 90%）。

    故每一步都先等前置条件成立：

    - 缺省 = 预热静默（``_warmup_settled``，覆盖「被门控的命令又启动了新预热」的情形）；
    - 断言依赖更强的状态跃迁时，把该步包成 ``_wait_for(check, expect, command)``（如 ``warmed_up``）。

    等待超过 ``_SETTLE_TIMEOUT`` 仍不成立 → ``AssertionError``（把「时序不成立」变成可读失败，
    而不是忙等到底、或读到中间态）；未成立时返回 ``None``——会话循环空转（0.02s/轮），与真实
    无命令时一致。

    要验证**预热进行中**的行为（409 / 取消）**不要**用本源——用 ``make_signals`` / ``_build_session``
    的即时命令源，并靠 fake policy 阻塞 ``connect`` 制造稳定的「预热中」窗口。

    序列里的**可调用项**会被调用（用于注入副作用，如模拟连接丢失），其返回值才是要发的命令。
    """
    cmds = list(seq)
    state: dict = {"i": 0}

    def source():
        if state["i"] >= len(cmds):
            return None
        session = holder.get("session")
        if session is None:  # 会话尚未构造（本源只在 run() 内被调用，正常不会走到）
            return None
        item = cmds[state["i"]]
        command, check, expect = (
            item if isinstance(item, tuple) else (item, _warmup_settled, "预热静默（warming=False）")
        )
        if not check(session):
            if "deadline" not in state:
                state["deadline"] = time.monotonic() + _SETTLE_TIMEOUT
            elif time.monotonic() > state["deadline"]:
                raise AssertionError(
                    f"时序不成立：第 {state['i'] + 1}/{len(cmds)} 条命令的前置条件「{expect}」在 "
                    f"{_SETTLE_TIMEOUT:g}s 内未成立（warming={session.warming}, "
                    f"warmed_up={session.warmed_up}, warmup_error={session.warmup_error!r}）"
                )
            return None  # 条件未成立 → 再等一轮（预热跑在别的线程里）
        state.pop("deadline", None)
        state["i"] += 1
        return _as_command(command)

    return source


def _build_gated_session(adapter, policy, *seq, warmup_required=True):
    """按**严格预热门控时序**构造会话（见 ``_warmup_gated_source``）。"""
    holder: dict = {}
    cfg = {"policy": {"infer_freq": 1000, "warmup_required": warmup_required}}
    session = infer_session.InferSession(cfg, command_source=_warmup_gated_source(holder, *seq), adapter=adapter)
    holder["session"] = session
    return session


def _patch(monkeypatch, policy):
    """注入 fake policy；adapter 由 _build_session 直接注入（会话不再自建）。"""
    monkeypatch.setattr(infer_session, "get_policy", lambda cfg, **kw: policy)


def test_infer_loop_runs_observation_to_action(monkeypatch):
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    # 缺省 warmup_required=true：infer prompt → **infer connect 预热** → （预热完成后）单步推理 → 退出
    session = _build_gated_session(
        adapter,
        policy,
        "infer prompt 把零件放好",
        "infer connect",
        # rollout 受预热门控：等预热状态跃迁（warmed_up=True）完成后再下发
        _wait_for(lambda s: s.warmed_up, "预热收尾（warmed_up=True）", "infer rollout"),
        "session quit",
    )
    assert session.run() == RunResult.FINISHED
    assert session.warmed_up is True
    assert policy.infer_calls == 2  # 预热取一块（丢弃）+ rollout 一块
    assert len(adapter.executed) == 1  # 只有 rollout 下发动作：**预热不动机器人**
    assert adapter.safe_stop_calls == 0


def test_infer_rollout_single_step_replies_action(monkeypatch):
    """infer rollout（单步）：一次 观测 → 推理 → 动作下发，回执 count=1/action/actions。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 1
    assert len(adapter.executed) == 1
    assert replies[0].status == "ok"
    assert replies[0].data["count"] == 1
    assert replies[0].data["action"] == list(np.arange(14, dtype=float))
    assert len(replies[0].data["actions"]) == 1


def test_infer_rollout_requires_prompt(monkeypatch):
    """prompt 为空不能开始推理：语言条件策略（openpi）infer rollout（未设 prompt）→ rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(adapter, policy, (rollout, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT
    assert "prompt required" in replies[0].error


def test_infer_rollout_continuous_requires_prompt(monkeypatch):
    """prompt 为空不能开始推理：语言条件策略 infer rollout continuous（未设 prompt）→ rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    _patch(monkeypatch, policy)
    replies = []
    cont = _REGISTRY.parse_argv(["infer", "rollout", "continuous"])
    cont.reply_to = replies.append
    session = _build_session(adapter, policy, (cont, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT


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
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT
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
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT
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
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT


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
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT


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
    """录制 rollout 需要 task_name=prompt：语言条件策略 prompt 为空时 capture episode start → rejected。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    _patch(monkeypatch, policy)
    replies = []
    start = _REGISTRY.parse_argv(["capture", "episode", "start"])
    start.reply_to = replies.append
    session = _build_session(adapter, policy, (start, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert adapter.start_capture_calls == 0
    assert session.recording is False
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT
    assert "prompt required" in replies[0].error


def test_infer_non_language_policy_needs_no_prompt(monkeypatch):
    """非语言条件策略（lerobot-act：requires_prompt=False）**不需要 prompt**：不门控推理 / 录制。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()  # requires_prompt=False（lerobot-act 语义）
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    start = _REGISTRY.parse_argv(["capture", "episode", "start"])
    rollout.reply_to = replies.append
    start.reply_to = replies.append
    session = _build_session(adapter, policy, (start, rollout, "session quit"), warmup_required=False)

    assert session.run() == RunResult.FINISHED
    assert session.prompt_required is False
    assert adapter.start_capture_calls == 1  # 无 prompt 也可开录制
    assert policy.infer_calls == 1  # 无 prompt 也可推理
    assert replies[0].status == "ok"


def test_infer_config_command_queries_policy_items(monkeypatch):
    """``infer config``：返回当前策略的配置项（schema + 当前值 + 缺失必填项）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    _patch(monkeypatch, policy)
    replies = []
    config = _REGISTRY.parse_argv(["infer", "config"])
    config.reply_to = replies.append
    session = _build_session(adapter, policy, (config, "session quit"), warmup_required=True)

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    snapshot = replies[0].data["policy_config"]
    assert snapshot["policy_type"] == "openpi"  # 未显式选策略 → 配置缺省类型
    assert snapshot["requires_prompt"] is True
    assert snapshot["missing"] == ["prompt"]  # 未预置 → 缺失必填项
    # 公共项（推理端点 host / port + 预热门控 warmup_required）= 与策略项同一张表单（无「连接后锁定」轴）
    assert [item["key"] for item in snapshot["items"]] == ["host", "port", "warmup_required", "prompt", "image_size"]
    assert [item.get("group") for item in snapshot["items"]] == ["endpoint", "endpoint", None, None, None]
    assert [item["runtime"] for item in snapshot["items"]] == [False, False, False, True, True]
    assert snapshot["values"]["warmup_required"] is True  # 缺省要求先预热
    # 会话内即时生效的键 = runtime=True（端点 / 预热门是会话级配置：进入会话时固化）
    assert snapshot["runtime_keys"] == ["image_size", "prompt"]


def test_infer_config_set_endpoint_is_session_level(monkeypatch):
    """会话内改推理端点（``runtime=False``）：写内存态 + 回执 ``deferred``，**不**即时生效。

    端点与 prompt / image_size 同一通道、同一校验，区别只在 ``runtime``：进入会话时固化的
    配置改了要退出会话重进（不再有「连接后锁定」这条额外的 409 轴）。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    base_cfg = {"policy": {"infer_freq": 1000, "type": "openpi", "host": "127.0.0.1", "port": 8000}}
    _patch(monkeypatch, policy)
    replies = []
    set_cmd = _REGISTRY.parse_argv(["infer", "config", "set", '{"host": "10.0.0.9", "port": 9000}'])
    set_cmd.reply_to = replies.append
    session = infer_session.InferSession(
        base_cfg, command_source=make_signals(set_cmd, "session quit"), adapter=adapter
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["written"] == {"host": "10.0.0.9", "port": 9000}
    assert replies[0].data["deferred"] == ["host", "port"]  # 未即时生效（下个会话生效）
    assert base_cfg["policy"]["host"] == "10.0.0.9"  # 写内存态
    assert base_cfg["policy"]["port"] == 9000


def test_infer_config_set_mixed_runtime_applies_only_runtime_keys(monkeypatch):
    """同一批设置里 ``runtime`` 各管各的：端点延后（deferred），prompt 立刻生效。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    policy.connected = True  # 已连接也**不再**整批拒绝
    base_cfg = {"policy": {"infer_freq": 1000, "type": "openpi", "host": "127.0.0.1", "port": 8000}}
    _patch(monkeypatch, policy)
    replies = []
    mixed = _REGISTRY.parse_argv(["infer", "config", "set", '{"host": "10.0.0.9", "prompt": "x"}'])
    mixed.reply_to = replies.append
    session = infer_session.InferSession(base_cfg, command_source=make_signals(mixed, "session quit"), adapter=adapter)

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["deferred"] == ["host"]
    assert base_cfg["policy"]["host"] == "10.0.0.9"  # 写内存态（下次会话生效）
    assert policy.prompt == "x"  # runtime=True：立刻应用到运行中的客户端


def test_infer_config_set_applies_to_running_policy(monkeypatch):
    """``infer config set <json>``：写内存态 + 应用到运行中的策略客户端（下一请求生效）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    config = {"policy": {"infer_freq": 1000, "type": "openpi"}}
    _patch(monkeypatch, policy)
    replies = []
    set_cmd = _REGISTRY.parse_argv(["infer", "config", "set", '{"prompt": "把零件放好"}'])
    set_cmd.reply_to = replies.append
    session = infer_session.InferSession(config, command_source=make_signals(set_cmd, "session quit"), adapter=adapter)

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["written"] == {"prompt": "把零件放好"}
    assert policy.prompt == "把零件放好"  # 立刻应用到策略客户端
    assert config["policy"]["prompt"] == "把零件放好"  # 同时写入内存态（下次会话生效）
    assert session.policy_config_status()["missing"] == []


def test_infer_config_set_rejects_unknown_key(monkeypatch):
    """``infer config set`` 非本策略配置项 / 类型不符 → rejected（400，不崩溃）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy(requires_prompt=True)
    _patch(monkeypatch, policy)
    replies = []
    bad_key = _REGISTRY.parse_argv(["infer", "config", "set", '{"pretrained_name_or_path": "/tmp/x"}'])
    bad_type = _REGISTRY.parse_argv(["infer", "config", "set", '{"prompt": ""}'])
    bad_key.reply_to = replies.append
    bad_type.reply_to = replies.append
    session = _build_session(adapter, policy, (bad_key, bad_type, "session quit"))

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT
    assert "unknown openpi config key" in replies[0].error
    assert replies[1].status == "rejected"
    assert policy.prompt is None  # 空文本不生效


def test_infer_model_set_applies_to_policy_config(monkeypatch):
    """``infer model set <path>``：lerobot 类策略（lerobot-act）的模型路径运行时给定。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    config = {"policy": {"infer_freq": 1000, "type": "lerobot-act", "device": "cuda"}}
    _patch(monkeypatch, policy)
    replies = []
    set_model = _REGISTRY.parse_argv(["infer", "model", "set", "/tmp/pretrained_model"])
    set_model.reply_to = replies.append
    session = infer_session.InferSession(
        config, command_source=make_signals(set_model, "session quit"), adapter=adapter
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["written"] == {"pretrained_name_or_path": "/tmp/pretrained_model"}
    assert config["policy"]["pretrained_name_or_path"] == "/tmp/pretrained_model"
    assert session.policy_config_status()["requires_model_path"] is True


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
    assert replies[0].code == ErrorCode.INVALID_ARGUMENT


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
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", cont, None, "session quit"), warmup_required=False
    )

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
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 0
    assert adapter.executed == []
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.UNAVAILABLE
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
    """``warmup_required=false`` 时保留旧行为：rollout 前惰性自动连接并推理（无预热门）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 自动连接一次，无需先 infer connect
    assert policy.infer_calls == 1
    assert len(adapter.executed) == 1
    assert replies[0].status == "ok"


def test_infer_connect_warms_up_without_motion(monkeypatch):
    """``infer connect`` = **异步**启动预热（连接 + prepare + 取一块丢弃），**不下发任何动作**。

    回执**立即**返回（``started=True`` / ``warming=True``），不等预热跑完（加载模型可能几分钟）；
    真机动作只由 ``infer rollout`` 经 ``adapter.rollout`` 下发。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    session = _build_gated_session(
        adapter,
        policy,
        connect,
        # 等预热跃迁完成后才退出：提前 quit 会把在飞预热取消（用例断言 warmed_up 为真）
        _wait_for(lambda s: s.warmed_up, "预热收尾（warmed_up=True）", "session quit"),
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"
    assert replies[0].data["started"] is True  # 本次新启动了预热线程
    assert replies[0].data["warming"] is True  # 立即回执：不等预热跑完
    assert replies[0].data["warmed_up"] is False
    assert session.warmed_up is True  # 预热收尾后为真
    assert session.warmup_error is None
    assert policy.prepare_calls == 1
    assert policy.infer_calls == 1  # 预热自己取了一块（丢弃）
    assert adapter.executed == []  # **预热不动机器人**


def test_infer_connect_is_idempotent_and_reports_state(monkeypatch):
    """重复 ``infer connect`` 幂等：回执即当前预热状态（无需另加状态命令）。

    ``started=False`` = 本次没新起线程（已在预热中或已预热完）。已预热时回执给出
    ``warmed_up`` / ``connected`` / ``metadata`` / ``chunk_len``。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    policy.server_metadata = {"action_horizon": 16}
    _patch(monkeypatch, policy)
    replies = []
    first = _REGISTRY.parse_argv(["infer", "connect"])
    second = _REGISTRY.parse_argv(["infer", "connect"])
    second.reply_to = replies.append
    session = _build_gated_session(
        adapter,
        policy,
        first,
        # 第二次 connect 的幂等回执要读 warmed_up：等跃迁完成后再下发
        _wait_for(lambda s: s.warmed_up, "预热收尾（warmed_up=True）", second),
        "session quit",
    )

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 第二次没有重新连接（已预热）
    assert replies[0].data["started"] is False
    assert replies[0].data["warming"] is False
    assert replies[0].data["warmed_up"] is True
    assert replies[0].data["connected"] is True
    assert replies[0].data["metadata"] == {"action_horizon": 16}
    assert replies[0].data["chunk_len"] == 50


def test_infer_connect_failure_lands_in_warmup_error(monkeypatch):
    """预热失败：命令仍立即回执（异步），失败原因记在 ``warmup_error``；rollout 仍被拦下。"""

    class _FailConnectPolicy(_FakePolicy):
        def connect(self):
            self.connect_calls += 1
            raise OSError("inference server not reachable")

    adapter = _FakeAdapter(ready=True)
    policy = _FailConnectPolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_gated_session(
        adapter,
        policy,
        connect,
        # rollout 要在**预热失败收尾之后**下发：warmup_error 由 None 变为失败原因
        _wait_for(lambda s: s.warmup_error is not None, "预热失败收尾（warmup_error 已写入）", rollout),
        "session quit",
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "ok"  # 异步：命令本身受理成功
    assert session.warmed_up is False
    assert session.warmup_error is not None, "预热失败应写入 warmup_error，而不是留 None"
    assert "not reachable" in session.warmup_error
    assert session.connected is False
    assert replies[1].status == "rejected"  # rollout 仍被预热门拦下
    assert replies[1].code == ErrorCode.CONFLICT
    assert "not warmed up" in replies[1].error
    assert policy.infer_calls == 0
    assert adapter.executed == []


def test_infer_rollout_during_warmup_is_rejected(monkeypatch):
    """预热进行中：``infer rollout`` → 409（文案指向「等预热完成」），且不发起推理。

    同时验证核心收益：预热跑在工作线程里，**会话循环照旧响应命令**（不会把急停 / 状态查询一起挡住）。
    """
    release = threading.Event()

    class _SlowConnectPolicy(_FakePolicy):
        def connect(self):
            self.connect_calls += 1
            release.wait(timeout=5)  # 模拟加载 checkpoint（分钟级）
            self.connected = True

        def disconnect(self):
            self.disconnect_calls += 1
            release.set()  # 关传输 → 打断在飞调用
            self.connected = False

    adapter = _FakeAdapter(ready=True)
    policy = _SlowConnectPolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    # 不等预热跑完：紧跟 rollout（此时 warming=True）→ 409，再 quit 取消（release 解开阻塞）
    session = _build_session(adapter, policy, (connect, rollout, "session quit"), warmup_required=True)

    assert session.run() == RunResult.FINISHED
    assert replies[0].data["warming"] is True
    assert replies[1].status == "rejected"
    assert replies[1].code == ErrorCode.CONFLICT
    assert "warmup in progress" in replies[1].error
    assert adapter.executed == []  # 既没 rollout 也没预热下发动作
    assert session.warmed_up is False  # quit 已取消预热


def test_infer_connect_cancelled_by_estop(monkeypatch):
    """预热可中断：预热中 ``robot estop`` → 立即取消（关传输打断在飞调用）+ 安全停止 + 进 ERROR。

    这是「预热不能占着会话循环」的关键：否则急停会被一起挡在总线队列里（预热可能几分钟）。
    """
    release = threading.Event()

    class _SlowConnectPolicy(_FakePolicy):
        def connect(self):
            self.connect_calls += 1
            release.wait(timeout=5)
            self.connected = True

        def disconnect(self):
            self.disconnect_calls += 1
            release.set()
            self.connected = False

    adapter = _FakeAdapter(ready=True)
    policy = _SlowConnectPolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    connect.reply_to = replies.append
    estop = _REGISTRY.parse_argv(["robot", "estop"])
    estop.reply_to = replies.append
    session = _build_session(adapter, policy, (connect, estop), warmup_required=True)

    assert session.run() == RunResult.ERROR  # 急停 → ERROR
    assert replies[0].data["warming"] is True
    assert replies[1].status == "ok"  # 与 node 同形：命令已执行（停机），节点转 ERROR
    assert replies[1].data["node_state"] == "error"
    assert adapter.safe_stop_calls == 1  # 安全停止已执行
    assert policy.disconnect_calls == 1  # 关传输 → 打断在飞的 connect
    session.session_finish()  # 节点侧收尾：取消 + 回收预热线程（真实流程同此）
    assert session.warmed_up is False
    assert session.warmup_error == "cancelled"


def test_infer_connect_rewarms_after_connection_loss(monkeypatch):
    """连接丢失（服务端重启 / 链路断开）→ 预热闩锁**自动失效**，且可重新预热。

    ``warmed_up`` 是「本连接」的闩锁：连接断了服务端的模型也不在（lerobot-act 重连还会重发
    策略指令重新加载 checkpoint）。不复位会有三个连带后果：status 同时显示「已预热 + 未连接」；
    ``infer connect`` 被幂等短路、会话内无任何途径重新预热；``infer rollout`` 反而被预热门放行 →
    惰性重连 + 首块内联等模型加载（分钟级）。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    first = _REGISTRY.parse_argv(["infer", "connect"])
    second = _REGISTRY.parse_argv(["infer", "connect"])
    second.reply_to = replies.append
    holder: dict = {}
    observed = {}

    def drop_connection_and_probe():
        session = holder["session"]
        deadline = time.monotonic() + 2.0
        while session.warming and time.monotonic() < deadline:
            time.sleep(0.005)  # 预热是异步的：先等首次预热收尾，再模拟断连
        policy.connected = False  # 模拟服务端重启 / 链路断开
        observed["warmed_up"] = session.warmed_up  # 读 `warmed_up` 即与连接状态对账
        observed["warmup_error"] = session.warmup_error
        return second

    session = infer_session.InferSession(
        {"policy": {"infer_freq": 1000, "warmup_required": True}},
        command_source=_warmup_gated_source(
            holder,
            first,
            # 探针的前提是「第一轮预热已成功」（闩锁 warmed_up=True）：否则读到的 warmed_up=False
            # 是「预热还没结束」而不是「连接丢失使闩锁失效」，后半段断言全部失去意义
            _wait_for(lambda s: s.warmed_up, "第一轮预热收尾（warmed_up=True）", drop_connection_and_probe),
            # 重新预热的状态跃迁完成后再退出（提前 quit 会取消在飞的重新预热）
            _wait_for(lambda s: s.warmed_up, "重新预热收尾（warmed_up=True）", "session quit"),
        ),
        adapter=adapter,
    )
    holder["session"] = session
    assert session.run() == RunResult.FINISHED
    # 连接丢失 → 闩锁自动失效（而不是留着 stale True）
    assert observed["warmed_up"] is False
    assert observed["warmup_error"] is not None, "连接丢失应写入 warmup_error，而不是留 None"
    assert "connection lost" in observed["warmup_error"]
    # 重新 infer connect：**不再**被幂等短路（started=True），且重新预热成功
    assert replies[0].data["started"] is True
    assert policy.connect_calls == 2  # 首次 connect + 重新预热
    assert session.warmed_up is True
    assert session.warmup_error is None


def test_infer_rollout_blocked_again_after_connection_loss(monkeypatch):
    """连接丢失后 ``infer rollout`` 重新被预热门拦下（409），不走「惰性重连 + 首块内联」危险路径。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    infer_calls_after_warmup = {}

    def drop_connection():
        infer_calls_after_warmup["n"] = policy.infer_calls
        policy.connected = False  # 预热完成后服务端重启
        return rollout

    session = _build_gated_session(
        adapter,
        policy,
        connect,
        # 探针的前提是「预热已成功」（注释即为此意）：等跃迁完成后再模拟服务端重启
        _wait_for(lambda s: s.warmed_up, "预热收尾（warmed_up=True）", drop_connection),
        "session quit",
    )
    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.CONFLICT
    assert "not warmed up" in replies[0].error
    assert policy.infer_calls == infer_calls_after_warmup["n"]  # 没发起新的推理
    assert policy.connect_calls == 1  # 也没做惰性重连
    assert adapter.executed == []


def test_infer_rollout_blocked_while_warming_even_if_warmup_not_required(monkeypatch):
    """``warmup_required=false`` 也不能在**预热进行中**放行 rollout（只表示「允许不预热直接 rollout」）。

    策略客户端契约：可跨线程调用，但**同一时刻至多一个请求**（`rtc/manager.py`）——预热线程的
    ``prepare`` / ``infer_chunk`` 与 rollout 的 inline 取块并发，会在同一条 ws 上并发 send/recv
    （响应错位）或抢走 lerobot-act 的单飞块，也会并发 ``adapter.observe()``。
    """
    release = threading.Event()

    class _SlowConnectPolicy(_FakePolicy):
        def connect(self):
            self.connect_calls += 1
            release.wait(timeout=5)
            self.connected = True

        def disconnect(self):
            self.disconnect_calls += 1
            release.set()
            self.connected = False

    adapter = _FakeAdapter(ready=True)
    policy = _SlowConnectPolicy()
    _patch(monkeypatch, policy)
    replies = []
    connect = _REGISTRY.parse_argv(["infer", "connect"])
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    observed = {}

    def probe_before_rollout():
        # 发 rollout 的那一刻采样：此时不应有与预热并发的推理在飞
        observed["infer_calls"] = policy.infer_calls
        return rollout

    session = _build_session(adapter, policy, (connect, probe_before_rollout, "session quit"), warmup_required=False)

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.CONFLICT
    assert "warmup in progress" in replies[0].error
    assert observed["infer_calls"] == 0  # 没有与预热并发发起推理
    assert policy.connect_calls == 1  # 也没走惰性自连
    assert adapter.executed == []


def test_infer_rollout_requires_warmup(monkeypatch):
    """缺省 ``warmup_required=true``：未预热就 ``infer rollout`` → rejected 409，且不发起推理。

    理由：连接 + 加载模型可能几百秒，落在一臂之力的 rollout 上会变成「调用方超时判失败、
    动作却已下发到真机」。故预热（``infer connect``）是 rollout 的硬前置，错误文案直接告知怎么修。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=True
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.CONFLICT
    assert "not warmed up" in replies[0].error
    assert policy.infer_calls == 0  # 未预热：连推理都不发起
    assert policy.connect_calls == 0  # 不做惰性自连（预热是显式一步）
    assert adapter.executed == []


def test_infer_rollout_drops_action_when_reply_deadline_passed(monkeypatch):
    """回执已过期（调用方 submit 超时放弃）→ **动作丢弃、真机不动**，回执 504。

    命令超时 ≠ 取消执行：若不自查就会「调用方看到失败、机器人却动了」。丢弃的步数记入
    ``dropped_actions`` 供诊断。
    """
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    rollout.meta[META_REPLY_DEADLINE] = time.monotonic() - 1.0  # 模拟 submit 早已超时
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 1  # 推理照常跑（块会进 RTC 队列）
    assert adapter.executed == []  # 但动作不下发
    assert session.dropped_actions == 1
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.TIMEOUT
    assert "deadline exceeded" in replies[0].error


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
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 单次尝试，不无限重试
    assert policy.infer_calls == 0
    assert replies[0].status == "error"
    assert replies[0].code == ErrorCode.UPSTREAM_ERROR


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


# ---- 以下为 dev 侧独有（本次 merge 保留） ----
def test_infer_rollout_action_repr_rounds_to_display_digits(monkeypatch):
    """日志 / 回执 / 网页的动作数值统一保留 3 位小数（内部链路仍用全精度）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    policy.action = np.array([-0.06196591258049011, 0.4469754695892334, -0.0004])
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert replies[0].data["action"] == [-0.062, 0.447, 0.0]  # 3 位小数（-0.0 → 0.0）


def test_infer_rollout_stop_returns_to_session_loop(monkeypatch):
    """infer rollout stop：停止持续推理并**回到会话主循环**（会话不退出，仍可单步推理）。"""
    adapter = _FakeAdapter(ready=True)
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    cont = _REGISTRY.parse_argv(["infer", "rollout", "continuous"])
    cont.reply_to = replies.append
    stop = _REGISTRY.parse_argv(["infer", "rollout", "stop"])
    stop.reply_to = replies.append
    single = _REGISTRY.parse_argv(["infer", "rollout"])
    single.reply_to = replies.append
    # None = 无命令空档：让持续推理推一步后再下发 stop
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", cont, None, stop, single, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED  # 直到 session quit 才退出会话
    assert [r.status for r in replies] == ["ok", "ok", "ok"]
    assert replies[0].data["state"] == "continuous"
    assert replies[1].data["continuous"] is False  # 停止回执
    assert session.continuous is False  # 运行位已清
    assert replies[2].data["count"] == 1  # 停止后单步推理仍可用（会话未退出）


def test_infer_connect_failure_records_warmup_error(monkeypatch):
    """infer connect：连接失败 → 回执**仍是 ok**（异步预热立即回执，`started=True`），
    失败原因记在 `warmup_error`，连接保持未连接、可重试。"""

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
    # 预热在工作线程里跑：等它收尾（失败原因写入 warmup_error）再退出会话
    session = _build_gated_session(
        adapter,
        policy,
        connect,
        _wait_for(lambda s: s.warmup_error is not None, "预热失败收尾（warmup_error 已写入）", "session quit"),
    )

    assert session.run() == RunResult.FINISHED
    assert policy.connect_calls == 1  # 单次尝试，不无限重试
    assert session.connected is False
    assert replies[0].status == "ok"  # 同步回执只表示「已启动预热」
    assert replies[0].data["started"] is True
    assert session.warmed_up is False
    assert "inference server not reachable" in (session.warmup_error or "")


def test_infer_rollout_refused_during_teleop(monkeypatch):
    """遥操作（人工接管）中单步 infer rollout：SDK 拒绝（409）→ 回执 rejected，不下发动作。"""
    adapter = _FakeAdapter(ready=True)
    adapter.teleop_refused = True  # 模拟 SDK 409：遥操作中推理让位
    policy = _FakePolicy()
    _patch(monkeypatch, policy)
    replies = []
    rollout = _REGISTRY.parse_argv(["infer", "rollout"])
    rollout.reply_to = replies.append
    session = _build_session(
        adapter, policy, ("infer prompt 把零件放好", rollout, "session quit"), warmup_required=False
    )

    assert session.run() == RunResult.FINISHED
    assert policy.infer_calls == 1  # 推理照常跑（只是不下发）
    assert adapter.executed == []  # 本拍动作未下发
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.CONFLICT
    assert "teleop" in replies[0].error
