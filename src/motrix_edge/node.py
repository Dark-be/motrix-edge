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

"""EdgeNode —— 节点生命周期控制器（本项目的唯一节点入口/启动对象）。

生命周期状态机（5 状态，转移合法性由 NodeLifecycle 校验）：
  INIT    初始化中（构造后默认状态）：initialize()/run() 完成初始化后进入 IDLE，不再回 INIT
  IDLE    无 adapter（探测中）：周期探测配置适配器，等待机器人进程上线
  READY   有 adapter 就绪：session run <type> 一步启动会话 → ACTIVE
  ACTIVE  任务执行中（session run 已启动；session quit 退出回 READY）
  ERROR   硬件故障 / 通信异常 / 机器人进程失联：node reset 恢复回 IDLE

命令驱动（Command / CommandBus，见 utils/commands.py）：命令词**空格分隔不用点**。
本地命令（CLI / 脚本）无需租约、不走 HTTP；HTTP 经 submit 同步等回执。**进入会话与
启动会话合并为一个流程**：``session run <type>``（type = capture / infer）选择并启动
会话一步完成（原 capture.start/infer.start + session.run 两个流程合并）。adapter 生命
周期归节点（READY 绑定、任务间保持、ERROR 恢复释放），会话复用节点 adapter（只引用
不持有）。run() 持续监听命令源并驱动 _tick 周期任务（IDLE 探测 / READY·ACTIVE 失联
检查），直到 Ctrl-C。
"""

import threading
import time

from motrix_edge.frame import FrameManager
from motrix_edge.policy import validate_policy_type
from motrix_edge.session import get_session
from motrix_edge.session.base import RunResult
from motrix_edge.utils.capture_meta import CaptureMetaStore
from motrix_edge.utils.commands import (
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    CommandResult,
    handle_capture_meta,
    handle_infer_endpoint,
    ok_result,
    parse_bool,
    parse_qpos,
)
from motrix_edge.utils.data_handler import debug_print


def _noop_command_source():
    """默认命令源：无输入（返回 None）。实际 CLI / HTTP 经 CommandBus 注入。"""
    return None


class NodeState:
    """EdgeNode 节点生命周期状态（5 状态）。"""

    INIT = "init"  # 初始化中（构造后默认状态；进入 IDLE 后不再回）
    IDLE = "idle"  # 无 adapter（探测中）
    READY = "ready"  # 有 adapter 就绪
    ACTIVE = "active"  # 任务执行中
    ERROR = "error"


# 合法状态转移表（其余转移被忽略并告警）
NODE_TRANSITIONS = {
    NodeState.INIT: {NodeState.IDLE},  # 初始化完成 → IDLE（单向：进入 IDLE 后不再回 INIT）
    NodeState.IDLE: {NodeState.READY, NodeState.ERROR},
    NodeState.READY: {NodeState.ACTIVE, NodeState.ERROR},
    NodeState.ACTIVE: {NodeState.READY, NodeState.ERROR},
    NodeState.ERROR: {NodeState.IDLE},
}

# 状态 → 命令处理器方法名（处理器返回是否已回执；未处理由 _dispatch 兜底回执）
_STATE_HANDLERS = {
    NodeState.INIT: "_on_idle",  # 初始化中：任务命令被拒（与 IDLE 一致）
    NodeState.IDLE: "_on_idle",
    NodeState.READY: "_on_ready",
    NodeState.ACTIVE: "_on_active",
    NodeState.ERROR: "_on_error",
}


class NodeLifecycle:
    """EdgeNode 生命周期状态机：校验转移合法性并回调状态钩子。"""

    def __init__(self, initial=NodeState.INIT):
        self._state = initial
        self._on_enter = {}
        self._on_exit = {}

    @property
    def state(self):
        return self._state

    def can_transition(self, target):
        return target in NODE_TRANSITIONS[self._state]

    def transition(self, target, reason=""):
        if target == self._state:
            return self._state
        if not self.can_transition(target):
            debug_print(
                "NodeLifecycle",
                f"Illegal transition ignored: {self._state} → {target} ({reason})",
                "ERROR",
            )
            return self._state
        if self._state in self._on_exit:
            self._on_exit[self._state](self._state)
        self._state = target
        if self._state in self._on_enter:
            self._on_enter[self._state](self._state)
        return self._state

    def on_enter(self, state, cb):
        self._on_enter[state] = cb

    def on_exit(self, state, cb):
        self._on_exit[state] = cb


class EdgeNode:
    """EdgeNode 节点控制器 —— 拥有完整持续的控制流程。"""

    def __init__(
        self,
        base_cfg,
        command_source=None,
        poll_interval=0.02,
        frame_manager=None,
        probe_interval=2.0,
        alive_check_interval=2.0,
        data_status_interval=2.0,
        observe_interval=0.05,
        capture_meta_store=None,
    ):
        self.base_cfg = base_cfg
        self.command_source = command_source if command_source is not None else _noop_command_source
        self.poll_interval = poll_interval
        # Edge 级观测帧缓存（FrameManager）：session 写入、preview / WebRTC 读取
        self.frame_manager = frame_manager or FrameManager()
        self.lifecycle = NodeLifecycle(NodeState.INIT)  # 构造后默认 INIT；initialize()/run() 后进入 IDLE
        self.session = None
        self.session_type = None
        # 采集元信息选项存储（config/capture.yml）：capture meta 配置级命令读写；测试可注入临时 store
        self.capture_meta_store = capture_meta_store if capture_meta_store is not None else CaptureMetaStore()
        # 任务线程（session run 启动后台线程）：启动后置非 None，任务期间主循环不 poll
        # 命令（会话命令由任务线程内的会话循环消费）；_tick 检测线程结束收尾。
        self._task_thread = None
        self._task_result = None

        # 节点级 active adapter（生命周期归节点）：IDLE 探测到可用后绑定，任务间保持，
        # ERROR 恢复时释放；会话复用（注入），不各自实例化。
        self.adapter = None
        self.adapter_name = None  # 绑定 adapter 的展示名称（discover 赋予 name）
        self.adapter_type = None  # 绑定 adapter 的类型（entry point 名，用于实例化）
        # 周期任务参数（_tick）：探测间隔 / 失联检查间隔 / 采集数据状态刷新间隔 / 观测间隔（秒）
        self.probe_interval = probe_interval
        self.alive_check_interval = alive_check_interval
        self.data_status_interval = data_status_interval
        self.observe_interval = observe_interval
        self._last_probe = 0.0
        self._last_alive_check = 0.0
        self._last_data_status = 0.0
        self._last_capture_status = 0.0
        self._last_observe = 0.0
        # 采集数据状态缓存（adapter.data_status()）：由主循环在采集会话期间周期刷新，
        # server /v1/captures 只读缓存，**不因前端轮询而实时请求 SDK**。
        self._data_status = None
        # 采集状态缓存（adapter.capture_status()）：采集员 / 任务名等元信息 + 运行位；
        # 主循环在采集会话期间周期刷新，server /v1/captures 只读缓存。
        self._capture_status = None

        # 状态进入日志钩子（INIT/IDLE/READY/ACTIVE/ERROR 统一注册到 _log_state）
        for state in (NodeState.INIT, NodeState.IDLE, NodeState.READY, NodeState.ACTIVE, NodeState.ERROR):
            self.lifecycle.on_enter(state, self._log_state)

    @property
    def state(self):
        return self.lifecycle.state

    @property
    def data_status(self):
        """采集数据状态缓存（adapter.data_status()；主循环周期刷新，server 只读）。

        Edge 主循环在采集会话（ACTIVE + capture）期间自行周期查询并缓存，前端轮询
        /v1/captures 只读本缓存——**edge 运行不依赖前端，前端只是命令下发 / 状态
        显示的辅助页面**。
        """
        return self._data_status

    @property
    def capture_status(self):
        """采集状态缓存（adapter.capture_status()；主循环周期刷新，server 只读）。

        采集员 / 任务名等元信息 + 运行位；查询 / 缓存语义同 ``data_status``。
        """
        return self._capture_status

    def _log_state(self, state):
        """状态进入日志（NodeLifecycle.on_enter 钩子，消息在进入时求值）。"""
        if state == NodeState.INIT:
            debug_print("EdgeNode", "INIT: 节点初始化中（构造后默认状态）。", "INFO")
        elif state == NodeState.IDLE:
            debug_print("EdgeNode", "IDLE: 无 adapter，正在探测机器人进程...", "INFO")
        elif state == NodeState.READY:
            debug_print("EdgeNode", f"READY: adapter 就绪（{self.adapter_type or self.adapter_name}）。", "INFO")
        elif state == NodeState.ACTIVE:
            debug_print("EdgeNode", f"ACTIVE: 任务执行中（{self.session_type}）。", "INFO")
        else:
            debug_print("EdgeNode", "ERROR: 硬件故障或通信异常，等待恢复(rr)。", "ERROR")

    # ------------------------------------------------------------------
    # 主控制循环（完整持续的控制流程，直到 Ctrl-C）
    # ------------------------------------------------------------------
    def initialize(self):
        """执行节点初始化（INIT → IDLE）：初始化完成后进入 IDLE 探测机器人进程。

        INIT 仅存在于初始化阶段（构造后默认状态）；进入 IDLE 后不再回 INIT（状态机
        单向转移）。重复调用（已离开 INIT）为 no-op。
        """
        debug_print("EdgeNode", "初始化完成，进入 IDLE 开始探测机器人进程...", "INFO")
        self.lifecycle.transition(NodeState.IDLE)
        return self.state

    def run(self):
        """持续监听命令并驱动生命周期。返回最终状态。"""
        self.initialize()  # 初始化（INIT → IDLE）；INIT 仅存在于初始化阶段
        debug_print("EdgeNode", "EdgeNode 主循环启动，持续监听命令...", "INFO")
        try:
            while True:
                self._tick()  # 周期任务：探测 / 失联检查 / 任务线程收尾
                if self._task_thread is None:
                    cmd = self.command_source()
                    if cmd is not None:
                        self._dispatch(cmd)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            debug_print("EdgeNode", "用户中断，退出。", "WARNING")
        finally:
            self._join_task_thread()
            self._shutdown_session()
            self._release_adapter()
        return self.lifecycle.state

    # ------------------------------------------------------------------
    # 命令分发（依据当前生命周期状态；处理后统一回执）
    # ------------------------------------------------------------------
    def _dispatch(self, cmd):
        """按当前状态分发命令到处理器；未处理的命令统一回执「not applicable」。"""
        if cmd is None:
            return
        state = self.lifecycle.state

        # 急停：全局安全命令，任何非 ERROR 状态都先转 ERROR 再回执
        if cmd.name == CMD_ROBOT_ESTOP:
            if state != NodeState.ERROR:
                self._enter_error("estop")
            self._reply(cmd, ok_result(node_state=self.state))
            return

        # 推理端点配置命令（infer ip / infer ip set / infer port / infer port set）：配置级
        # 命令与节点状态机解耦，任何状态（IDLE / READY / ACTIVE / ERROR）均可用；写入内存态
        # base_cfg["policy"]，下次 session run infer 生效（进行中会话不受影响）。
        if cmd.name in (CMD_INFER_IP, CMD_INFER_IP_SET, CMD_INFER_PORT, CMD_INFER_PORT_SET):
            self._reply(cmd, self._on_infer_endpoint(cmd))
            return

        # 采集元信息选项（capture meta list/add/edit/delete/delete-key）：配置级命令，任何状态
        # 均可用（读写 config/capture.yml 的 meta 段；与 infer ip 同一语义，与会话状态机解耦）。
        if cmd.name in (
            CMD_CAPTURE_META_LIST,
            CMD_CAPTURE_META_ADD,
            CMD_CAPTURE_META_EDIT,
            CMD_CAPTURE_META_DELETE,
            CMD_CAPTURE_META_DELETE_KEY,
        ):
            self._reply(cmd, handle_capture_meta(cmd, self.capture_meta_store))
            return

        # 状态处理器返回是否已回执；未回执（当前状态不适用）→ 兜底回执，避免 submit 挂起
        if not getattr(self, _STATE_HANDLERS[state])(cmd):
            self._reply(
                cmd,
                CommandResult(status="rejected", error=f"{cmd.name} not applicable in {state}", status_code=409),
            )

    def _reply(self, cmd, result):
        """统一回执：命令携带 reply_to（submit 通道）则回调结果。"""
        if cmd is not None and cmd.reply_to is not None:
            cmd.reply_to(result)
        return result

    def _on_infer_endpoint(self, cmd):
        """infer ip / infer ip set / infer port / infer port set：读写推理节点端点配置。

        委托给 ``utils.commands.handle_infer_endpoint``（写内存态 ``base_cfg["policy"]``）；
        配置命令由节点主循环（非任务态）与会话循环（任务态）共用，保证「任何状态可用」。
        """
        return handle_infer_endpoint(self.base_cfg, cmd)

    def _on_idle(self, cmd):
        """IDLE：无 adapter，拒绝启动会话命令（探测由 _tick 驱动；探测到可用 adapter 才进 READY）。

        返回是否已回执（False 由 _dispatch 兜底回执「not applicable」）。
        """
        if cmd.name in (CMD_SESSION_RUN, CMD_ROBOT_RESET):
            debug_print("EdgeNode", "IDLE: 机器人进程未就绪，等待探测到 adapter 后再启动会话。", "WARNING")
            self._reply(
                cmd,
                CommandResult(status="rejected", error="robot process not ready", status_code=409),
            )
            return True
        return False

    def _on_ready(self, cmd):
        """READY：session run <type> 一步完成「选择会话 + 启动任务」→ ACTIVE。

        返回是否已回执（False 由 _dispatch 兜底回执「not applicable」）。
        """
        if cmd.name == CMD_SESSION_RUN:
            session_type = cmd.params.get("session")
            if session_type not in ("capture", "infer"):
                self._reply(
                    cmd,
                    CommandResult(status="rejected", error=f"unknown session type: {session_type}", status_code=400),
                )
                return True
            policy_type = cmd.params.get("policy_type")
            if session_type == "infer":
                # policy_type 可选：缺省用配置 policy.type（与 InferService.enter / get_policy
                # 的契约一致，而非强制调用方显式提供 —— 修复此前真实节点对缺省 400 的分歧）。
                if not policy_type:
                    policy_type = self.base_cfg.get("policy", {}).get("type", "openpi")
                try:
                    policy_type = validate_policy_type(policy_type)
                except ValueError as exc:
                    self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                    return True
            # session run <type>：选择 + 启动一步完成（原 capture.start/infer.start + session.run 合并）
            self._reply(cmd, self._start_session(session_type, policy_type=policy_type))
            return True
        return self._handle_adapter_command(cmd)

    def _on_active(self, cmd):
        """ACTIVE：session quit 退出当前会话 → READY；robot reset 复位。其余命令不适用。

        （任务运行期间主循环不 poll，会话内命令由任务线程内的会话循环消费。）
        返回是否已回执（False 由 _dispatch 兜底回执「not applicable」）。
        """
        if cmd.name == CMD_SESSION_QUIT:  # 退出当前会话 → READY
            self._shutdown_session()
            self.lifecycle.transition(NodeState.READY)
            self._reply(cmd, ok_result(node_state=self.state))
            return True
        return self._handle_adapter_command(cmd)

    def _handle_adapter_command(self, cmd) -> bool:
        """处理 READY / ACTIVE 共用的 adapter 控制命令并统一回执。"""
        if cmd.name == CMD_ROBOT_RESET:
            self.adapter.reset()
            self._reply(cmd, ok_result(node_state=self.state))
            return True
        if cmd.name == CMD_ROBOT_EXECUTE:
            try:
                qpos = parse_qpos(cmd.params.get("qpos"))
                self.adapter.execute(qpos)  # 维度校验在 adapter.execute
            except ValueError as exc:
                self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                return True
            self._reply(cmd, ok_result(node_state=self.state, action=qpos))
            return True
        if cmd.name == CMD_ROBOT_TELEOP:
            try:
                enabled = parse_bool(cmd.params.get("enabled"))
                self.adapter.set_teleop(enabled)
            except ValueError as exc:
                self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                return True
            self._reply(cmd, ok_result(node_state=self.state, teleop=enabled))
            return True
        return False

    def _on_error(self, cmd):
        """ERROR：node reset 恢复 → IDLE；其余命令不适用（adapter 已释放）。返回是否已回执。"""
        if cmd.name == CMD_NODE_RESET:  # node reset：故障恢复 → IDLE
            self._recover()
            self._reply(cmd, ok_result(node_state=self.state))
            return True
        return False

    # ------------------------------------------------------------------
    # 核心动作
    # ------------------------------------------------------------------
    def _start_session(self, session_type, policy_type=None):
        """session run <type>：实例化并连接会话（复用节点 active adapter）→ ACTIVE → 启动任务线程。

        选择会话与启动任务**合并为一个流程**（原 capture.start/infer.start + session.run
        两步合并）：实例化 + session_start → 进入 ACTIVE → 后台线程跑会话 run()。
        单 adapter 包：采集 / 推理都基于节点 discover 绑定的唯一 adapter，无选择环节。
        policy_type：推理策略类型（仅 infer；命令携带，缺省用配置）。
        返回 CommandResult（成功带 session state / adapter 身份；失败 rejected）。
        """
        if self._task_thread is not None and self._task_thread.is_alive():
            return CommandResult(status="rejected", error="task already running", status_code=409)
        try:
            self._shutdown_session()
            self.session = get_session(
                self.base_cfg,
                session_type=session_type,
                command_source=self.command_source,
                frame_manager=self.frame_manager,
                adapter=self.adapter,  # 复用节点 active adapter（生命周期归节点）
                policy_type=policy_type,
            )
            self.session_type = session_type
            self.session.session_start()  # 连接硬件/初始化会话（adapter 已就绪则跳过 discover）
            debug_print(
                "EdgeNode",
                f"已实例化并连接会话: {self.session.name} "
                f"(adapter={self.adapter_name or self.adapter_type or 'bound'})",
                "INFO",
            )
            self.lifecycle.transition(NodeState.ACTIVE)  # 进入 ACTIVE
            # 启动任务：后台线程跑会话 run()（任务运行期间主循环不再 poll 命令）
            self._task_result = None
            self._task_thread = threading.Thread(target=self._task_entry, name="session-run", daemon=True)
            self._task_thread.start()
            return ok_result(
                node_state=self.state,
                session=self.session_type,
                state=getattr(self.session, "state", None),
                adapter=self._adapter_ref(),
                policy=getattr(self.session, "policy_type", None),
            )
        except Exception as exc:
            self._shutdown_session()
            debug_print("EdgeNode", f"会话 {session_type} 启动失败: {exc}", "ERROR")
            self._enter_error(str(exc))
            return CommandResult(status="rejected", error=str(exc), status_code=409, data={"node_state": self.state})

    def _task_entry(self):
        """任务线程入口：阻塞跑会话 run()，结果存 _task_result（由 _tick 收尾消费）。"""
        try:
            result = self.session.run()
        except Exception as exc:
            debug_print("EdgeNode", f"会话运行异常: {exc}", "ERROR")
            result = RunResult.ERROR
        self._task_result = result

    def _handle_result(self, result):
        """根据会话 run() 的返回结果推进节点状态（4 状态），并补发退出命令回执。

        退出命令（session quit）由会话记录于 ``session.exit_command``，处理时不立即
        回执，由这里在节点状态落定后补发（HTTP exit 同步等到 node READY）。
        """
        exit_cmd = self._session_exit_command()
        if result == RunResult.ERROR:
            # _enter_error 内部先 safe_stop（急停/异常路径立即停机）再切 ERROR
            self._enter_error("任务执行中发生错误")
            if exit_cmd is not None:
                self._reply(
                    exit_cmd,
                    CommandResult(status="error", error="task error", status_code=500, data={"node_state": self.state}),
                )
        else:
            # FINISHED / INTERRUPTED：释放会话，回到 READY（adapter 保留，可再选任务）
            self._shutdown_session()
            self.lifecycle.transition(NodeState.READY)
            if exit_cmd is not None:
                self._reply(exit_cmd, ok_result(node_state=self.state))

    def _session_exit_command(self):
        """当前会话记录的退出命令（任务结束时补发回执用）。"""
        session = self.session
        return getattr(session, "exit_command", None) if session is not None else None

    def _join_task_thread(self):
        """退出前等待任务线程结束（避免后台线程与资源释放竞争）。"""
        thread = self._task_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _safe_stop(self):
        """安全停止（幂等）：让当前会话立即停止机器人运动；无会话时 no-op。"""
        if self.session is not None:
            try:
                self.session.safe_stop()
            except Exception as exc:
                debug_print("EdgeNode", f"safe_stop failed: {exc}", "ERROR")

    def _enter_error(self, reason):
        # 先安全停止（急停/任意任务异常路径立即停机），再进入 ERROR：状态标签不能替代现场停止动作
        self._safe_stop()
        # 失联/健康失败等由 node 主循环触发的 ERROR 时，任务线程（会话循环）仍在运行，
        # 会继续消费总线命令并把 node reset 等恢复命令拦截为「not applicable」。
        # 终止任务线程，让 node 主循环恢复 poll 命令（ERROR 恢复命令可达 _on_error）。
        self._stop_task_thread()
        self.lifecycle.transition(NodeState.ERROR, reason=reason)

    def _stop_task_thread(self):
        """结束任务线程：请求会话停止并等待线程退出；失败仅清标记（线程 daemon，随进程退出）。"""
        session = self.session
        if session is not None:
            stop = getattr(session, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:  # noqa: BLE001
                    debug_print("EdgeNode", f"stop session failed: {exc}", "ERROR")
        thread = self._task_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)  # 等会话循环检查 stop 后退出（每轮极快）
        self._task_thread = None
        self._task_result = None

    def _recover(self):
        """ERROR → IDLE：释放会话与 adapter，重新探测机器人进程。"""
        debug_print("EdgeNode", "尝试恢复：释放会话与 adapter 回到 IDLE...", "INFO")
        self._shutdown_session()
        self._release_adapter()
        self.lifecycle.transition(NodeState.IDLE)

    # ------------------------------------------------------------------
    # 周期任务（_tick）：adapter 探测 / 失联检查
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        """主循环周期任务：任务线程收尾 + IDLE 探测 + 失联检查 + 持续观测 + 采集数据状态刷新。"""
        self._finish_task_thread()
        state = self.lifecycle.state
        if state == NodeState.IDLE:
            self._probe_adapter()
        elif state in (NodeState.READY, NodeState.ACTIVE):
            self._check_adapter_alive()
            self._observe()  # 持续观测（无需进入会话；显示观测统一归节点，会话不再写）
        if state == NodeState.ACTIVE and self.session_type == "capture":
            self._refresh_data_status()  # 采集会话期间 edge 自行周期查询数据状态并缓存
            self._refresh_capture_status()  # 采集状态（采集员 / 任务名等元信息）同样周期刷新缓存

    def _observe(self) -> None:
        """节点级持续观测：把最新观测写入 FrameManager（观测不依赖「进入会话」）。

        有绑定 adapter（READY / ACTIVE，含采集会话）时周期 ``adapter.observe()`` →
        ``frame_manager``，供 preview / WebRTC 读取——**显示观测统一由节点负责**，会话
        不再写 frame_manager（采集会话不再 observe；推理会话 observe 仅作推理输入）。
        """
        if self.adapter is None:
            return
        now = time.monotonic()
        if now - self._last_observe < self.observe_interval:
            return
        self._last_observe = now
        try:
            obs = self.adapter.observe()
        except Exception as exc:  # noqa: BLE001 观测失败不中断（瞬态无帧 / 进程忙）
            debug_print("EdgeNode", f"observe failed: {exc}", "WARNING")
            return
        if obs is not None:
            self.frame_manager.update(obs)

    def _finish_task_thread(self) -> None:
        """任务线程结束收尾：清线程标记并按结果推进节点状态。"""
        thread = self._task_thread
        if thread is None:
            return
        if thread.is_alive():
            return
        self._task_thread = None
        result = self._task_result if self._task_result is not None else RunResult.FINISHED
        self._handle_result(result)

    def _probe_adapter(self) -> None:
        """IDLE 下周期 discover 机器人进程：找到 → 实例化并绑定 → READY。

        探测失败（机器人进程未上线 / 网络错误 / 实例化失败）**持续重试等待上线**，
        不报错；急停 / 明确异常仍走 ERROR。
        """
        now = time.monotonic()
        if now - self._last_probe < self.probe_interval:
            return
        self._last_probe = now
        from motrix_edge.adapter import DEFAULT_DISCOVER_HOST, DEFAULT_DISCOVER_PORT, discover_adapter

        # 解析 adapter 段（host/port）→ discover_adapter 一步完成「发现 + 实例化」
        adapter_cfg = self.base_cfg.get("adapter") or {}
        host = adapter_cfg.get("host", DEFAULT_DISCOVER_HOST)
        port = adapter_cfg.get("port", DEFAULT_DISCOVER_PORT)
        adapter = discover_adapter(host=host, port=port)  # 返回 None 或实例化后的 adapter
        if adapter is None:
            return
        self.adapter = adapter
        self.adapter_name = getattr(adapter, "name", None) or getattr(adapter, "type", None)
        self.adapter_type = getattr(adapter, "type", None) or getattr(adapter, "name", None)
        self.lifecycle.transition(NodeState.READY)

    def _check_adapter_alive(self) -> None:
        """READY / ACTIVE 下周期检查 adapter 心跳：进程失联 → ERROR。"""
        if self.adapter is None:
            return
        now = time.monotonic()
        if now - self._last_alive_check < self.alive_check_interval:
            return
        self._last_alive_check = now
        try:
            if not self.adapter.health().ok:
                self._enter_error("robot process unreachable")
        except Exception as exc:  # noqa: BLE001
            self._enter_error(f"adapter health check failed: {exc}")

    def _refresh_data_status(self) -> None:
        """采集会话（ACTIVE + capture）期间周期查询采集数据状态并缓存。

        数据状态由 **edge 自行**向 adapter / 机器人进程查询（`data_status()`）并缓存，
        server 的 /v1/captures 只读本缓存——前端轮询状态**不会**触发对 SDK 进程的
        实时请求（edge 运行不依赖前端）。
        """
        if self.adapter is None:
            self._data_status = None
            return
        now = time.monotonic()
        if now - self._last_data_status < self.data_status_interval:
            return
        self._last_data_status = now
        try:
            self._data_status = self.adapter.data_status()
        except Exception as exc:  # noqa: BLE001 查询失败 → 缓存置空，不中断
            debug_print("EdgeNode", f"data_status refresh failed: {exc}", "WARNING")
            self._data_status = None

    def _refresh_capture_status(self) -> None:
        """采集会话（ACTIVE + capture）期间周期查询采集状态并缓存。

        ``capture_status()`` 返回机器人进程当前采集元信息（采集员 / 任务名等）+ 运行位；
        查询 / 缓存语义与 ``data_status`` 一致（edge 自行查询，不依赖前端轮询）。
        """
        if self.adapter is None:
            self._capture_status = None
            return
        now = time.monotonic()
        if now - self._last_capture_status < self.data_status_interval:
            return
        self._last_capture_status = now
        try:
            self._capture_status = self.adapter.capture_status()
        except Exception as exc:  # noqa: BLE001 查询失败 → 缓存置空，不中断
            debug_print("EdgeNode", f"capture_status refresh failed: {exc}", "WARNING")
            self._capture_status = None

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------
    def _adapter_ref(self) -> dict:
        """当前节点绑定的唯一 adapter 身份（单 adapter 包，回执 / 状态用）。"""
        return {
            "name": self.adapter_name,
            "type": self.adapter_type,
        }

    def _shutdown_session(self):
        """释放当前会话资源（注入 adapter 由节点持有，不释放），不改变节点状态。"""
        if self.session is not None:
            try:
                self.session.session_finish()
            except Exception as exc:
                debug_print("EdgeNode", f"释放会话资源失败: {exc}", "ERROR")
            self.session = None
            self.session_type = None
        self._data_status = None  # 清空采集数据状态缓存（会话结束后无意义）
        self._capture_status = None  # 清空采集状态缓存（会话结束后无意义）

    def _release_adapter(self) -> None:
        """释放节点级 active adapter（ERROR 恢复 / 退出时；回到无 adapter）。"""
        if self.adapter is not None:
            try:
                self.adapter.release()
            except Exception as exc:
                debug_print("EdgeNode", f"释放 adapter 失败: {exc}", "ERROR")
            self.adapter = None
            self.adapter_name = None
            self.adapter_type = None
