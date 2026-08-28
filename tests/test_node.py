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

"""EdgeNode / NodeLifecycle 单元测试 —— 状态机转移、急停安全停止、结果处理，无硬件可跑。"""

import threading

from motrix_edge.adapter import DEFAULT_DISCOVER_HOST, DEFAULT_DISCOVER_PORT
from motrix_edge.node import EdgeNode, NodeLifecycle, NodeState
from motrix_edge.session.base import RunResult
from motrix_edge.utils.commands import (
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    Command,
    build_command_registry,
)


class _FakeSession:
    def __init__(self):
        self.safe_stop_calls = 0
        self.session_finish_calls = 0

    def safe_stop(self):
        self.safe_stop_calls += 1

    def session_finish(self):
        self.session_finish_calls += 1


def test_lifecycle_valid_transitions():
    lc = NodeLifecycle(NodeState.IDLE)
    assert lc.transition(NodeState.READY) == NodeState.READY  # 探测到 adapter
    assert lc.transition(NodeState.ACTIVE) == NodeState.ACTIVE  # 选择会话
    assert lc.transition(NodeState.READY) == NodeState.READY  # 任务结束回 READY
    assert lc.transition(NodeState.ERROR) == NodeState.ERROR
    assert lc.transition(NodeState.IDLE) == NodeState.IDLE  # 恢复


def test_lifecycle_illegal_transition_idle_to_active_ignored():
    lc = NodeLifecycle(NodeState.IDLE)
    # IDLE（无 adapter）→ ACTIVE 非法：必须先探测到 adapter（READY）
    assert lc.transition(NodeState.ACTIVE) == NodeState.IDLE


def test_lifecycle_ignores_illegal_transition():
    lc = NodeLifecycle(NodeState.IDLE)
    assert lc.transition(NodeState.ERROR) == NodeState.ERROR
    # ERROR → ACTIVE 非法，被忽略并保持原状态
    assert lc.transition(NodeState.ACTIVE) == NodeState.ERROR


def test_lifecycle_init_is_initial_state():
    """INIT 是默认初始状态（NodeLifecycle 缺省 / EdgeNode 构造后）。"""
    lc = NodeLifecycle()
    assert lc.state == NodeState.INIT
    node = EdgeNode({}, command_source=lambda: None)
    assert node.state == NodeState.INIT


def test_lifecycle_init_to_idle_is_one_way():
    """INIT → IDLE 合法（初始化完成）；进入 IDLE 后不再回 INIT（ERROR 恢复也回 IDLE）。"""
    lc = NodeLifecycle(NodeState.INIT)
    assert lc.transition(NodeState.IDLE) == NodeState.IDLE
    assert lc.transition(NodeState.INIT) == NodeState.IDLE  # 已离开 INIT，不可回
    assert lc.transition(NodeState.READY) == NodeState.READY
    assert lc.transition(NodeState.ERROR) == NodeState.ERROR
    assert lc.transition(NodeState.IDLE) == NodeState.IDLE  # 恢复回 IDLE，而非 INIT


def test_lifecycle_init_only_allows_idle():
    """INIT 下除 IDLE 外的转移（READY / ACTIVE / ERROR）非法，被忽略。"""
    lc = NodeLifecycle(NodeState.INIT)
    assert lc.transition(NodeState.READY) == NodeState.INIT
    assert lc.transition(NodeState.ACTIVE) == NodeState.INIT
    assert lc.transition(NodeState.ERROR) == NodeState.INIT


def test_initialize_enters_idle_and_is_idempotent():
    """initialize()：INIT → IDLE；重复调用（已离开 INIT）为 no-op。"""
    node = EdgeNode({}, command_source=lambda: None)
    assert node.state == NodeState.INIT
    assert node.initialize() == NodeState.IDLE
    assert node.state == NodeState.IDLE
    assert node.initialize() == NodeState.IDLE  # 幂等：不再回 INIT


def test_dispatch_replies_rejected_for_not_applicable_command():
    """READY 下不适用命令（如 node reset）→ 兜底回执 rejected，避免 submit 挂起。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    node.lifecycle.transition(NodeState.READY)
    replies = []
    node._dispatch(Command(CMD_NODE_RESET, reply_to=replies.append))
    assert len(replies) == 1
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 409
    assert "not applicable" in replies[0].error
    assert node.state == NodeState.READY  # 状态不受影响


def test_estop_calls_safe_stop_and_enters_error():
    """急停：safe_stop 在进入 ERROR 之前被调用（状态标签不能替代停机动作）。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE（急停在 IDLE 下生效）
    stop_states = []

    class _Rec(_FakeSession):
        def safe_stop(self):
            stop_states.append(node.state)  # 记录调用时的节点状态
            super().safe_stop()

    node.session = _Rec()
    node._dispatch(Command(CMD_ROBOT_ESTOP))
    assert node.session.safe_stop_calls == 1
    assert stop_states == [NodeState.IDLE]  # safe_stop 先于状态切换
    assert node.state == NodeState.ERROR


def test_handle_result_error_calls_safe_stop_before_state_switch():
    """任务返回 ERROR：safe_stop 在切 ERROR 前调用（状态标签不能替代停机动作）。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    stop_states = []

    class _Rec(_FakeSession):
        def safe_stop(self):
            stop_states.append(node.state)
            super().safe_stop()

    node.session = _Rec()
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    node._handle_result(RunResult.ERROR)
    assert node.session.safe_stop_calls == 1
    assert stop_states == [NodeState.ACTIVE]  # 调用时仍 ACTIVE（未切 ERROR）
    assert node.state == NodeState.ERROR


def test_enter_error_stops_task_thread_and_node_reset_recovers():
    """失联进入 ERROR：终止任务线程（会话 stop + 清线程标记），node reset 可达 _on_error 恢复。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    node.adapter = _FakeExecAdapter()
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)

    stopped = []

    class _StopSession(_FakeSession):
        def stop(self):
            stopped.append(1)

    node.session = _StopSession()
    # 模拟任务线程（会话循环）仍在运行：失联 ERROR 由 node 主循环触发时会话线程还活着
    node._task_thread = threading.Thread(target=lambda: None, daemon=True)
    node._task_thread.start()
    node._task_thread.join()  # 线程已结束（避免 join 阻塞）；标记仍非 None

    node._enter_error("robot process unreachable")
    assert stopped == [1]  # 会话被请求停止
    assert node._task_thread is None  # 线程标记已清 → node 主循环恢复 poll 命令
    assert node.state == NodeState.ERROR

    # node reset 现在可达 _on_error：恢复 → IDLE
    replies = []
    node._dispatch(Command(CMD_NODE_RESET, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert node.state == NodeState.IDLE


def _start_task_direct(node):
    """直接启动任务线程（模拟 _start_session 启动后的任务执行，供异常路径测试）。"""
    node._task_result = None
    node._task_thread = threading.Thread(target=node._task_entry, name="session-run", daemon=True)
    node._task_thread.start()


def test_run_session_exception_still_safe_stops():
    """会话 run() 抛异常：节点兜底调用 safe_stop（异常路径仍停机）再进入 ERROR。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE

    class _Boom(_FakeSession):
        def run(self):
            raise RuntimeError("boom")

    node.session = _Boom()
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    _start_task_direct(node)
    thread = node._task_thread
    if thread is not None:
        thread.join(timeout=2.0)
    node._finish_task_thread()  # 线程结束 → _handle_result(ERROR) → safe_stop + ERROR
    assert node.session.safe_stop_calls == 1  # 异常路径仍调用 safe_stop
    assert node.state == NodeState.ERROR


def test_handle_result_finished_releases_session_and_goes_ready():
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    session = _FakeSession()
    node.session = session
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    node._handle_result(RunResult.FINISHED)
    assert session.session_finish_calls == 1
    assert node.state == NodeState.READY  # 任务结束回 READY（adapter 保留）


def test_handle_result_interrupted_releases_session_and_goes_ready():
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    session = _FakeSession()
    node.session = session
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    node._handle_result(RunResult.INTERRUPTED)
    assert session.session_finish_calls == 1
    assert node.state == NodeState.READY


def test_enter_error_without_session_no_crash():
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    node._enter_error("boom")  # 无会话 → safe_stop no-op，不抛错
    assert node.state == NodeState.ERROR


# ---------------------------------------------------------------------------
# adapter 选择：adapter_selector（CLI 交互）/ pending_adapter_id（HTTP 预留）
# ---------------------------------------------------------------------------


class _FakeSelectSession:
    name = "FakeSession"

    def __init__(self):
        self._stop = threading.Event()

    def session_start(self):
        pass

    def run(self):
        self._stop.wait(timeout=1.0)
        return RunResult.FINISHED

    def stop(self):
        self._stop.set()


def _patch_get_session(monkeypatch, captured):
    from motrix_edge import node as node_mod

    def fake_get_session(
        base_cfg,
        session_type=None,
        command_source=None,
        frame_manager=None,
        adapter=None,
        policy_type=None,
    ):
        captured["session_type"] = session_type
        captured["adapter"] = adapter
        return _FakeSelectSession()

    monkeypatch.setattr(node_mod, "get_session", fake_get_session)


def test_start_session_reuses_node_adapter(monkeypatch):
    """READY 下 session run <type>：复用节点 active adapter（注入 get_session）+ 启动任务一步完成。"""
    captured = {}
    _patch_get_session(monkeypatch, captured)
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    node.adapter = _FakeAdapter(available=True)  # 模拟 READY 已绑定
    node.lifecycle.transition(NodeState.READY)
    result = node._start_session("capture")
    assert result.status == "ok"
    assert captured["session_type"] == "capture"
    assert captured["adapter"] is node.adapter  # 复用节点 adapter
    assert node.state == NodeState.ACTIVE
    assert node._task_thread is not None and node._task_thread.is_alive()  # 任务已启动
    node.session.stop()
    node._join_task_thread()


# ---------------------------------------------------------------------------
# adapter 探测绑定（IDLE → READY）与失联（READY/ACTIVE → ERROR）
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """模拟已 discover 的 adapter：health 反映失联。"""

    def __init__(self, available=True, alive=True):
        self._available = available
        self.alive = alive
        self.release_calls = 0
        self.name = "Fake"
        self.id = "fake"
        self.type = "fake"

    def release(self):
        self.release_calls += 1

    def health(self):
        from motrix_edge.adapter import HealthStatus

        return HealthStatus(ok=self.alive)


def _patch_discover_adapter(monkeypatch, adapter):
    """patch discover_adapter：discover + 实例化一步完成，返回 adapter 或 None。"""

    def fake_discover(host=DEFAULT_DISCOVER_HOST, port=DEFAULT_DISCOVER_PORT, required_capability=None):
        if not adapter._available:
            return None
        return adapter

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)


def test_probe_binds_adapter_and_enters_ready(monkeypatch):
    """IDLE 下探测到可用 adapter → 实例化并连接（discover）→ READY。"""
    adapter = _FakeAdapter(available=True)
    _patch_discover_adapter(monkeypatch, adapter)
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0)
    node.initialize()  # INIT → IDLE
    node._tick()  # IDLE 下探测
    assert node.adapter is adapter
    assert node.state == NodeState.READY


def test_probe_failure_retries_without_error(monkeypatch):
    """探测失败（机器人进程未上线）→ 持续重试等待，不进 ERROR。"""
    adapter = _FakeAdapter(available=False)
    _patch_discover_adapter(monkeypatch, adapter)
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0)
    node.initialize()  # INIT → IDLE
    node._tick()
    assert node.state == NodeState.IDLE
    assert node.adapter is None


def test_check_adapter_alive_detects_unreachable(monkeypatch):
    """READY 下 adapter 心跳失联 → ERROR。"""
    adapter = _FakeAdapter(available=True)
    _patch_discover_adapter(monkeypatch, adapter)
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0, alive_check_interval=0.0)
    node.initialize()  # INIT → IDLE
    node._tick()  # 绑定 → READY
    assert node.state == NodeState.READY
    adapter.alive = False
    node._tick()  # 失联检查 → ERROR
    assert node.state == NodeState.ERROR


# ---------------------------------------------------------------------------
# robot execute（直接下发 raw 动作，qpos 直接作为参数）
# ---------------------------------------------------------------------------


class _FakeExecAdapter:
    """记录 execute / teleop 的 adapter 桩（robot execute / robot teleop 命令处理器用）。"""

    def __init__(self):
        self.executed = []
        self.reset_calls = 0
        self.teleop_values: list[bool] = []

    def reset(self):
        self.reset_calls += 1

    def execute(self, action):
        self.executed.append(action)

    def set_teleop(self, enabled: bool):
        self.teleop_values.append(bool(enabled))


def _ready_node_with_exec_adapter():
    """构造 READY 且已绑定 adapter 的节点。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    adapter = _FakeExecAdapter()
    node.adapter = adapter
    node.lifecycle.transition(NodeState.READY)
    return node, adapter


def test_robot_execute_in_ready_calls_adapter():
    """READY 下 robot execute：解析 qpos 参数（逗号分隔）→ adapter.execute，回执 ok。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "0,0,0,0,0,0,0"}, reply_to=replies.append))
    assert adapter.executed == [[0.0] * 7]  # qpos 直接作为参数传给 adapter.execute
    assert len(replies) == 1
    assert replies[0].status == "ok"
    assert replies[0].data["action"] == [0.0] * 7  # 回显解析后的 qpos
    assert node.state == NodeState.READY


def test_robot_execute_supports_brackets_and_spaces():
    """robot execute：兼容方括号 / 空白分隔的 qpos 参数。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "[1, 2, 3]"}, reply_to=replies.append))
    assert adapter.executed == [[1.0, 2.0, 3.0]]
    assert replies[0].status == "ok"


def test_robot_execute_supports_fullwidth_commas():
    """robot execute：兼容全角逗号 / 分号分隔的 qpos（中文输入法粘贴）。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "1，1，1，1，1，1，1"}, reply_to=replies.append))
    assert adapter.executed == [[1.0] * 7]
    assert replies[0].status == "ok"

    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "1;2;3"}, reply_to=replies.append))
    assert adapter.executed[-1] == [1.0, 2.0, 3.0]
    assert replies[0].status == "ok"


def test_robot_execute_rejects_invalid_qpos():
    """READY 下 robot execute：qpos 缺失 / 非法 → rejected（不崩溃，不调用 adapter）。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={}, reply_to=replies.append))
    assert len(replies) == 1
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert adapter.executed == []

    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "a,b,c"}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert adapter.executed == []


def test_robot_execute_rejected_when_adapter_dim_mismatch():
    """robot execute：adapter.execute 维度校验失败 → 回执 rejected（异常不外泄）。"""

    class _DimAdapter(_FakeExecAdapter):
        def execute(self, action):
            if len(action) != 14:
                raise ValueError(f"execute action dim {len(action)} != action_dim 14")
            self.executed.append(action)

    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    node.adapter = _DimAdapter()
    node.lifecycle.transition(NodeState.READY)
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "0,0,0,0,0,0,0"}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "execute action dim" in replies[0].error
    assert node.state == NodeState.READY


def test_robot_execute_not_applicable_in_idle():
    """IDLE（无 adapter）下 robot execute → 兜底回执 rejected。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE（未绑定 adapter）
    replies = []
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "0,0,0,0,0,0,0"}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 409


def test_adapter_commands_share_behavior_in_active_state():
    """ACTIVE 与 READY 复用同一 adapter 命令处理器，reset/execute/teleop 行为一致。"""
    node, adapter = _ready_node_with_exec_adapter()
    node.lifecycle.transition(NodeState.ACTIVE)
    replies = []

    node._dispatch(Command(CMD_ROBOT_RESET, reply_to=replies.append))
    node._dispatch(Command(CMD_ROBOT_EXECUTE, params={"qpos": "1,2,3"}, reply_to=replies.append))
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={"enabled": "true"}, reply_to=replies.append))

    assert node.state == NodeState.ACTIVE
    assert adapter.reset_calls == 1
    assert adapter.executed == [[1.0, 2.0, 3.0]]
    assert adapter.teleop_values == [True]
    assert [reply.status for reply in replies] == ["ok", "ok", "ok"]


# ---------------------------------------------------------------------------
# robot teleop（遥操作开关，true / false 直接作为参数）
# ---------------------------------------------------------------------------


def test_robot_teleop_in_ready_calls_adapter():
    """READY 下 robot teleop：解析 true/false 参数 → adapter.set_teleop，回执 ok。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={"enabled": "true"}, reply_to=replies.append))
    assert adapter.teleop_values == [True]
    assert replies[0].status == "ok"
    assert replies[0].data["teleop"] is True
    assert node.state == NodeState.READY

    replies = []
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={"enabled": "false"}, reply_to=replies.append))
    assert adapter.teleop_values == [True, False]
    assert replies[0].status == "ok"
    assert replies[0].data["teleop"] is False


def test_robot_teleop_rejects_invalid_enabled():
    """robot teleop：enabled 缺失 / 非法 → rejected（不调用 adapter）。"""
    node, adapter = _ready_node_with_exec_adapter()
    replies = []
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={"enabled": "maybe"}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert "invalid boolean" in replies[0].error
    assert adapter.teleop_values == []

    replies = []
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert adapter.teleop_values == []


def test_robot_teleop_not_applicable_in_idle():
    """IDLE（无 adapter）下 robot teleop → 兜底回执 rejected。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE（未绑定 adapter）
    replies = []
    node._dispatch(Command(CMD_ROBOT_TELEOP, params={"enabled": "true"}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 409


def test_recover_releases_adapter_and_goes_idle(monkeypatch):
    """ERROR 恢复（rr）→ IDLE：释放 adapter 重新探测。"""
    adapter = _FakeAdapter(available=True)
    _patch_discover_adapter(monkeypatch, adapter)
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0)
    node.initialize()  # INIT → IDLE
    node._tick()
    assert node.state == NodeState.READY
    node._enter_error("boom")
    node._recover()
    assert node.adapter is None
    assert adapter.release_calls == 1
    assert node.state == NodeState.IDLE


def test_init_tick_does_not_probe(monkeypatch):
    """INIT 下 _tick 不探测（初始化未完成前不绑定 adapter）；initialize() 后才探测。"""
    adapter = _FakeAdapter(available=True)
    _patch_discover_adapter(monkeypatch, adapter)
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0)
    node._tick()  # INIT：不探测
    assert node.state == NodeState.INIT
    assert node.adapter is None
    node.initialize()  # → IDLE
    node._tick()  # IDLE：探测绑定 → READY
    assert node.state == NodeState.READY
    assert node.adapter is adapter


def test_tick_refreshes_capture_status_cache():
    """ACTIVE + capture 下 _tick 周期刷新采集状态缓存（采集员 / 任务名等元信息）。"""
    from motrix_edge.adapter.base import CaptureStatus

    class _StatusAdapter(_FakeAdapter):
        def __init__(self):
            super().__init__(available=True)
            self.status = CaptureStatus(running=True, operator="张三", task_name="巡检")

        def capture_status(self):
            return self.status

    adapter = _StatusAdapter()
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0, data_status_interval=0.0)
    node.initialize()  # INIT → IDLE
    node.adapter = adapter
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    node.session_type = "capture"
    node._tick()  # ACTIVE + capture：刷新采集状态缓存
    assert node.capture_status is adapter.status


def test_tick_observes_into_frame_manager_when_ready():
    """READY（无会话）下 _tick 持续观测：adapter.observe() → frame_manager（观测无需进入会话）。"""

    class _ObsAdapter(_FakeAdapter):
        def observe(self):
            return {"observations/qpos": [1.0, 2.0, 3.0]}

    adapter = _ObsAdapter()
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0, observe_interval=0.0)
    node.initialize()  # INIT → IDLE
    node.adapter = adapter
    node.lifecycle.transition(NodeState.READY)
    node._tick()  # READY：持续观测写入 frame_manager
    latest = node.frame_manager.latest() or {}
    assert list(latest.get("observations/qpos", [])) == [1.0, 2.0, 3.0]


def test_tick_observes_during_capture_session():
    """采集会话（ACTIVE + capture）内也由节点持续观测（会话不再写 frame_manager）。"""

    class _ObsAdapter(_FakeAdapter):
        def observe(self):
            return {"observations/qpos": [9.0]}

    adapter = _ObsAdapter()
    node = EdgeNode({}, command_source=lambda: None, probe_interval=0.0, observe_interval=0.0)
    node.initialize()  # INIT → IDLE
    node.adapter = adapter
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    node.session_type = "capture"
    node._tick()  # ACTIVE + capture：节点持续观测（显示观测统一归节点）
    latest = node.frame_manager.latest() or {}
    assert list(latest.get("observations/qpos", [])) == [9.0]


# ---------------------------------------------------------------------------
# infer ip / infer port（推理端点配置命令：配置级，任何状态可用，写内存态 policy 段）
# ---------------------------------------------------------------------------


def _endpoint_node():
    """构造带 policy 段的节点（IDLE，无需 adapter）。"""
    node = EdgeNode({"policy": {"host": "0.0.0.0", "port": 8765}}, command_source=lambda: None)
    node.initialize()  # INIT → IDLE
    return node


def test_infer_endpoint_getters_return_config():
    """infer ip / infer port：查询当前配置端点（回执 ok，含 host / port）。"""
    node = _endpoint_node()
    replies = []
    node._dispatch(Command(CMD_INFER_IP, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "0.0.0.0", "port": 8765}

    replies = []
    node._dispatch(Command(CMD_INFER_PORT, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "0.0.0.0", "port": 8765}


def test_infer_endpoint_getter_without_policy_returns_none():
    """未配置 policy 段时 getter 返回 host/port None（不崩）。"""
    node = EdgeNode({}, command_source=lambda: None)
    node.initialize()
    replies = []
    node._dispatch(Command(CMD_INFER_IP, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": None, "port": None}


def test_infer_ip_set_updates_config():
    """infer ip set <ip>：写内存态 policy.host，回执带回更新后端点。"""
    node = _endpoint_node()
    replies = []
    node._dispatch(Command(CMD_INFER_IP_SET, params={"ip": "192.168.1.10"}, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "192.168.1.10", "port": 8765}
    assert node.base_cfg["policy"]["host"] == "192.168.1.10"
    assert node.base_cfg["policy"]["port"] == 8765  # 未改端口


def test_infer_port_set_updates_config():
    """infer port set <port>：写内存态 policy.port，回执带回更新后端点。"""
    node = _endpoint_node()
    replies = []
    node._dispatch(Command(CMD_INFER_PORT_SET, params={"port": "9000"}, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "0.0.0.0", "port": 9000}
    assert node.base_cfg["policy"]["port"] == 9000
    assert node.base_cfg["policy"]["host"] == "0.0.0.0"  # 未改 ip


def test_infer_ip_set_rejects_empty():
    """infer ip set：ip 缺失 / 空 → rejected（400），配置不变。"""
    node = _endpoint_node()
    replies = []
    node._dispatch(Command(CMD_INFER_IP_SET, params={}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400
    assert node.base_cfg["policy"]["host"] == "0.0.0.0"

    replies = []
    node._dispatch(Command(CMD_INFER_IP_SET, params={"ip": "  "}, reply_to=replies.append))
    assert replies[0].status == "rejected"
    assert replies[0].status_code == 400


def test_infer_port_set_rejects_invalid():
    """infer port set：端口缺失 / 非数字 / 越界 → rejected（400），配置不变。"""
    node = _endpoint_node()
    for bad in ("", "abc", "0", "65536", "-1", "1.5"):
        replies = []
        node._dispatch(Command(CMD_INFER_PORT_SET, params={"port": bad}, reply_to=replies.append))
        assert replies[0].status == "rejected", f"port {bad!r} should be rejected"
        assert replies[0].status_code == 400
        assert node.base_cfg["policy"]["port"] == 8765  # 配置未被污染


def test_infer_endpoint_commands_work_in_error_state():
    """infer ip/port（配置级）在 ERROR 状态也可用（与状态机解耦）。"""
    node = _endpoint_node()
    node._enter_error("boom")
    assert node.state == NodeState.ERROR
    replies = []
    node._dispatch(Command(CMD_INFER_PORT_SET, params={"port": "9000"}, reply_to=replies.append))
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "0.0.0.0", "port": 9000}


def test_infer_endpoint_commands_work_in_active_state():
    """ACTIVE 下配置命令由 node 全局处理，不会落入会话循环拒绝。"""
    node = _endpoint_node()
    node.lifecycle.transition(NodeState.READY)
    node.lifecycle.transition(NodeState.ACTIVE)
    replies = []

    node._dispatch(Command(CMD_INFER_IP_SET, params={"ip": "10.0.0.8"}, reply_to=replies.append))

    assert node.state == NodeState.ACTIVE
    assert replies[0].status == "ok"
    assert replies[0].data == {"host": "10.0.0.8", "port": 8765}


def test_infer_endpoint_command_parsing():
    """CLI 解析：infer ip / infer ip set <ip> / infer port / infer port set <port>。"""
    import shlex

    registry = build_command_registry()
    cmd = registry.parse_argv(shlex.split("infer ip"))
    assert cmd.name == CMD_INFER_IP
    assert cmd.params == {}

    cmd = registry.parse_argv(shlex.split("infer ip set 192.168.1.10"))
    assert cmd.name == CMD_INFER_IP_SET
    assert cmd.params == {"ip": "192.168.1.10"}  # 位置参数绑定

    cmd = registry.parse_argv(shlex.split("infer port"))
    assert cmd.name == CMD_INFER_PORT

    cmd = registry.parse_argv(shlex.split("infer port set 8765"))
    assert cmd.name == CMD_INFER_PORT_SET
    assert cmd.params == {"port": "8765"}
