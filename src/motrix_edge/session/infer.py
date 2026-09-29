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

import threading
import time

from motrix_edge.adapter import AdapterCapability
from motrix_edge.command import (
    CMD_INFER_CONNECT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_ROLLOUT_STOP,
    CMD_INFER_RTC,
    CMD_INFER_RTC_SET,
    ROLLOUT_MODE_CONTINUOUS,
    CommandResult,
    deadline_exceeded,
    handle_infer_rtc,
    ok_result,
    parse_rollout_mode,
)
from motrix_edge.errors import ErrorCode
from motrix_edge.policy import get_policy
from motrix_edge.rtc import build_rtc
from motrix_edge.utils.data_handler import debug_print, round_floats

from .base import STEP_LOOP_COMMANDS, BaseSession, CommonDispatch, RunResult, SessionState, _cmd_name
from .engine import RtcEngine
from .policy_config import PolicyConfigFeature


class InferSession(PolicyConfigFeature, BaseSession):
    """推理会话 —— 组合 RobotAdapter + 推理策略客户端的推理执行器。

    生命周期由 EdgeNode 管理（session_start → run → session_finish）。**无「多步推理」模式**：
    推理由单步 ``infer rollout`` / 持续 ``infer rollout continuous`` 驱动；**动作块切分与
    时序平滑由 RTCManager 负责**（策略只提供原始动作块：``policy.infer_chunk``）。**推理时
    rollout 录制** = 像采集一样经 ``capture episode start/end`` 控制一轮 episode——robot 不关心
    是推理还是采集（capturing 期间按帧录 mcap，含 action）。**prompt 仅语言条件策略需要**：
    需要 prompt 的策略（openpi）在推理/录制开始前必须已预置非空文本（策略配置项 prompt，经
    ``infer config set`` / ``POST /v1/infers/config`` 写入），否则拒绝；非语言条件策略
    （lerobot-act）不需要 prompt，不参与门控。录制时由调用方显式
    ``capture sync`` 同步采集元信息（operator=policy、task_name=prompt 由会话默认上报）。RTC 参数经
    ``infer rtc`` / ``infer rtc set <json>`` 查询与运行期修改（见 wiki/design/motrix_edge_rtc.md）。
    """

    required_capability = AdapterCapability.EXECUTE

    def __init__(
        self, base_cfg, command_source=None, frame_manager=None, adapter=None, policy_type=None, capture_meta_store=None
    ):
        super().__init__(
            base_cfg=base_cfg,
            name="InferSession",
            command_source=command_source,
            frame_manager=frame_manager,
            adapter=adapter,
            capture_meta_store=capture_meta_store,
        )
        self.policy_config = self.base_cfg.get("policy", {})
        # 推理步进频率（Hz）：会话 观测→推理→下发动作 的节奏。``policy.infer_freq`` 可调
        # （默认 10Hz ≈ 0.1s/步；测试传 1000 让主循环几乎不 sleep）。间隔 = 1 / infer_freq。
        infer_freq = float(self.policy_config.get("infer_freq", 10.0))
        self.step_interval = 1.0 / infer_freq if infer_freq > 0 else 0.1
        # 运行时策略选择：session run infer 携带 policy_type（HTTP / 命令）；由节点校验
        self.policy_type = policy_type
        self.policy = get_policy(base_cfg, policy_type=self.policy_type)
        # 会话创建即应用内存态已预置的配置项：如 prompt（进入会话前经策略配置通道 /
        # POST /v1/infers body 的 config 预置）；不声明该项的策略无此入口（no-op）。
        self._apply_prompt(self.policy_config.get("prompt"))
        # 把 adapter 运行时启用的布局（qpos 维数 + 相机名）传给策略客户端（openpi 据此
        # 过滤要下发的相机，不另读 edge.yml 相机名；策略无 bind_adapter 则 no-op）
        self._bind_policy_adapter()
        # 实时动作块管理器（RTC）：策略只提供原始动作块（infer_chunk），块长上限 H / 三元切分
        # （前置段 P 跳过 + 执行段 + 后缀段）/ 时序平滑 / **异步预取** 由 RTCManager 负责
        # （参数 = base_cfg policy.rtc，运行期可改）。control_hz = 控制频率：把实测推理耗时折算成
        # 步数上报（供人工设定前置段 P）。配置非法（P + S >= H / E <= P）不应到此——命令层已拦截。
        try:
            self.rtc = build_rtc(
                self.policy,
                self.policy_config.get("rtc") or {},
                control_hz=(1.0 / self.step_interval) if self.step_interval > 0 else None,
            )
        except ValueError as exc:  # 兜底：内存态配置非法时退回代码缺省，保证会话可进入
            debug_print(self.name, f"RTC config invalid ({exc}); falling back to defaults", "WARNING")
            self.rtc = build_rtc(self.policy, control_hz=(1.0 / self.step_interval) if self.step_interval > 0 else None)
        # 步进引擎：本会话的「本步动作从哪来」= RTC（块缓存 / 切分 / 平滑 / 预取）。manager 仍归
        # 本会话持有（``infer rtc set`` / ``rtc_status`` 直接作用于它），引擎只是一层统一的取步面。
        self.engine = RtcEngine(self.rtc)

        # 录制状态归基座 ``self.recorder``（capture / infer / rl 共用一份）：recording 属性直接
        # 读它；录制本身由机器人进程自维护（capturing=True 按帧录 mcap），会话只上报。
        # 预热状态：``infer connect`` 启动**工作线程**（连接 + prepare + 取一块丢弃，**不下发动作**）；
        # ``warmup_required``（缺省 true）时 ``warmed_up`` 是 ``infer rollout`` 的硬前置。
        # 预热必须异步：它可能持续几分钟（加载 checkpoint），占了会话循环就会把急停 / 退出 / 状态查询
        # 一起挡住（node 主循环在任务运行期间不 poll 普通命令）。
        # 写路径在锁内（工作线程与命令线程并发）；读路径（``warming`` 等 property）不加锁——
        # 这些标志只用于上报与门控，最坏读到一次上一瞬间的值（下一个轮询周期就正）。
        self._warming = False
        self._warmed_up = False
        self._warmup_error: str | None = None
        self._warmup_thread: threading.Thread | None = None
        self._warmup_lock = threading.Lock()
        self._warmup_cancel = threading.Event()
        # 因回执超时被丢弃的动作数（未下发到真机；诊断用，见 ``deadline_exceeded``）
        self._dropped_actions = 0
        # 持续推理运行位（infer rollout continuous ↔ infer rollout stop）：供 server
        # 上报（/v1/infers 的 continuous），前端据此门控「持续推理 / 停止推理」。
        self._continuous = False

        debug_print(
            self.name,
            f"Policy config: {self.policy_config} (type={self.policy_type})",
            "INFO",
        )

    @property
    def connected(self) -> bool:
        """策略是否已连接推理节点（委托 ``policy.connected``）。

        正常情况下由预热（``infer connect``）建立；只有 ``warmup_required=false`` 的会话才会在
        ``infer rollout`` 前惰性自连（见 ``_ensure_connected``）。
        """
        return bool(getattr(self.policy, "connected", False))

    @property
    def warmed_up(self) -> bool:
        """本**连接**是否已预热（``infer connect`` 工作线程跑完：连接 + prepare + 取一块丢弃）。

        读时先与连接状态对账（见 ``_sync_warmup_with_connection``）：连接没了就是没预热。
        """
        self._sync_warmup_with_connection()
        return bool(self._warmed_up)

    @property
    def warming(self) -> bool:
        """预热工作线程是否在进行中（前端据此显示「预热中…」）。"""
        return bool(self._warming)

    @property
    def warmup_error(self) -> str | None:
        """上次预热未完成的原因（``prepare`` / 取块失败 / 被取消 / 连接丢失；成功后清空）。"""
        self._sync_warmup_with_connection()
        return self._warmup_error

    @property
    def warmup_required(self) -> bool:
        """``infer rollout`` 是否要求先预热（公共配置项 ``warmup_required``，缺省 true）。"""
        return bool(self.policy_config.get("warmup_required", True))

    @property
    def dropped_actions(self) -> int:
        """因回执超时被丢弃的动作数（未下发到真机；诊断用，见 ``deadline_exceeded``）。"""
        return int(self._dropped_actions)

    @property
    def continuous(self) -> bool:
        """持续推理是否正在运行（``infer rollout continuous`` 启动 → ``infer rollout stop`` 结束）。"""
        return bool(self._continuous)

    def _sync_warmup_with_connection(self) -> None:
        """连接丢失 → 预热失效（``warmed_up`` 复位），可重新预热。

        ``warmed_up`` 是**本连接**的闩锁：连接一断（推理服务端重启 / 链路断开），服务端的会话状态与
        已加载的模型都不再可信（lerobot-act 重连后 ``_forget_policy`` 会重发策略指令、服务端**重新
        加载 checkpoint**），留着 ``warmed_up=True`` 会连带三个后果：

        1. status / 前端同时显示「已预热」与「未连接」（自相矛盾）；
        2. ``infer connect`` 被幂等短路（``started=false``），会话内没有任何途径重新预热；
        3. ``infer rollout`` 反而被预热门放行 → 惰性重连 + 首块**内联**等模型加载（最长
           ``policy_setup_timeout``）→ 回执 5s 超时丢动作、随后取块异常把整个会话打成 ERROR。

        复位后 ``warmup_error`` 记下原因（status 可解释），用户重新 ``infer connect`` 即恢复。
        读路径（``warmed_up`` / ``warmup_error``）都会先对账，因此不可能有读方看到过期闩锁。
        """
        if not self._warmed_up or self.connected:
            return
        with self._warmup_lock:
            if self._warmed_up and not self.connected:  # 锁内复查（并发下只复位一次）
                self._warmed_up = False
                self._warmup_error = "connection lost: re-run 'infer connect' to warm up again"
                debug_print(self.name, "Connection lost → warmup invalidated (re-warm needed).", "WARNING")

    def session_start(self):
        """进入会话（节点进入 ACTIVE 前调用）：adapter 已由节点 discover 绑定。

        进入会话**不连接**推理节点：连接与预热由 ``infer connect`` 显式驱动（异步、可中断，
        见 ``_connect_policy``）；只有 ``warmup_required=false`` 的会话才保留首个 ``infer rollout``
        前 ``policy.ensure_connected()`` 惰性自连。
        """
        self.state = SessionState.READY

    def session_finish(self):
        """释放资源（节点释放会话时调用）。adapter 由节点持有，不在此释放。

        先取消并回收预热工作线程（它可能正阻塞在加载模型，几分钟级），再停 RTC 预取线程、
        断开策略——否则资源释放会与在飞的网络调用竞争。
        """
        self.cancel_warmup("session finish")
        self._join_warmup()
        self.rtc.close()  # 先停 RTC 预取工作线程（异步预取），再断开策略
        self.policy.disconnect()
        self.state = SessionState.FINISHED

    def _join_warmup(self, timeout: float = 5.0) -> None:
        """等待预热工作线程收尾（已取消 + 已关传输后应当很快返回；daemon 线程不阻塞进程退出）。"""
        with self._warmup_lock:
            thread = self._warmup_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _connect_policy(self, cmd) -> None:
        """``infer connect``：启动**异步预热**（连接 + prepare + 取一块丢弃），立即回执。

        预热 = ``policy.connect()`` → ``policy.prepare(obs)``（lerobot-act 下发策略指令加载
        checkpoint；openpi 发一帧丢弃）→ 若本连接还没真正取到过块，再 ``infer_chunk`` 一次丢弃。
        **全程不调用 ``adapter.rollout``：预热不下发任何动作**，因此它可以安全地异步化。

        为什么异步（而不是原地等完成）：它可能跑几十秒到几分钟，占着会话循环就会把急停 /
        会话退出 / 状态查询一起挡住（node 主循环在任务运行期间不 poll 普通命令）。所以本命令
        **立即回执**（``warming=true``），调用方轮询 status 的 ``warming`` / ``warmed_up`` /
        ``warmup_error``；重复本命令是**幂等**的（回执即当前预热状态），无需另加状态命令。

        预热期间：``robot estop``（走总线旁路，node 侧）与 ``session quit``（会话循环正常消费）
        都能立即生效，并由 ``cancel_warmup`` 打断在飞的阻塞调用；``infer rollout`` 被预热门
        拦下（409）。

        **连接丢失后仍可重新预热**：先与连接状态对账（见 ``_sync_warmup_with_connection``）——
        否则 stale 的 ``warmed_up`` 会把本命令幂等短路掉，会话内就再没有重新预热的途径了。
        """
        self._sync_warmup_with_connection()
        with self._warmup_lock:
            if self._warming or self._warmed_up:  # 幂等：已在进行 / 已预热 → 回执当前状态
                self._reply(cmd, self._warmup_reply(started=False))
                return
            self._warmup_cancel.clear()
            self._warmup_error = None
            self._warming = True
            self._warmup_thread = threading.Thread(target=self._warmup_worker, name="infer-warmup", daemon=True)
            self._warmup_thread.start()
            # 回执快照在**启动瞬间**取（此刻必定 warming=True）：策略端很快时预热可能已经跑完，
            # 但「本次是否新启动」应由命令处理时刻的状态回答（后续状态由调用方轮询 status）。
            reply = self._warmup_reply(started=True)
        debug_print(self.name, "Warmup started (connect + prepare, no motion).", "INFO")
        self._reply(cmd, reply)

    def _warmup_reply(self, *, started: bool):
        """预热命令回执（含当前状态；``started`` = 本次是否新启动了工作线程）。

        本方法在**持锁**上下文里被调用（见 ``_connect_policy``），因此内部读原始字段而不是
        ``warmed_up`` / ``warmup_error`` 属性（那些会在读时对账并取同一把非可重入锁）。
        """
        return ok_result(
            state=getattr(self, "state", None),
            started=started,
            warming=self._warming,
            warmed_up=self._warmed_up,
            warmup_error=self._warmup_error,
            connected=self.connected,
            chunk_len=getattr(getattr(self, "policy", None), "observed_chunk_len", None),
            metadata=dict(getattr(getattr(self, "policy", None), "server_metadata", None) or {}),
        )

    def _warmup_worker(self) -> None:
        """预热工作线程：连接 → prepare → 取一块丢弃（**不下发动作**）。可被取消。

        与 RTC 预取线程同理：把可能分钟级的阻塞移出会话循环。取消（急停 / 退出 / 节点失联）
        时抛出的异常**不算失败**，只记为 ``cancelled``（不进 ``warmup_error`` 的告警路径）。
        """
        warmed, reason = False, None
        try:
            if not self.connected:
                self.policy.connect()
            warmed, reason = self._warmup_policy()
        except Exception as exc:  # noqa: BLE001 连接失败 / 取消时传输被关 → 都到这里
            warmed, reason = False, str(exc)
        cancelled = self._warmup_cancel.is_set()
        with self._warmup_lock:
            self._warming = False
            self._warmup_thread = None
            self._warmed_up = warmed and not cancelled
            self._warmup_error = (
                None if self._warmed_up else ("cancelled" if cancelled else (reason or "warmup failed"))
            )
        if self._warmed_up:
            debug_print(self.name, "Warmup done (model ready, chunk measured, no motion).", "INFO")
        elif cancelled:
            debug_print(self.name, "Warmup cancelled (estop / session stop).", "WARNING")
        else:
            debug_print(self.name, f"Warmup failed ({self._warmup_error}); retry 'infer connect'.", "WARNING")

    def cancel_warmup(self, reason: str) -> None:
        """取消进行中的预热：置取消标志 + **关传输**（打断在飞的阻塞调用）。立即返回，不等线程。

        调用时机：``robot estop`` / ``session quit`` / 节点失联进 ERROR（``stop()``）/ 会话释放
        （``session_finish``）。关闭传输是跨线程打断阻塞调用的手段（gRPC channel / ws 关闭后
        在飞的调用会以异常结束）；取消后 ``warmed_up`` 保持 False，可重新 ``infer connect``。

        本方法只负责「发取消信号」，线程回收在 ``_join_warmup``（``session_finish`` 调用）——
        故已关传输也不响应的工作线程最多让会话释放多等 ``_join_warmup`` 的超时（daemon 线程不阻
        塞进程退出）。
        """
        with self._warmup_lock:
            if not self._warming:
                return
            self._warmup_cancel.set()
        debug_print(self.name, f"Warmup cancelled ({reason}).", "WARNING")
        try:
            self.policy.disconnect()  # 关传输 → 打断 connect / SendPolicyInstructions / infer 的阻塞
        except Exception as exc:  # noqa: BLE001 取消失败不致命（工作线程仍会被超时兜住）
            debug_print(self.name, f"disconnect while cancelling warmup failed: {exc}", "WARNING")

    def stop(self) -> None:
        """请求会话停止（节点失联 / ERROR 时）：先取消在飞预热（可能阻塞几分钟），再置停止标志。"""
        self.cancel_warmup("session stop")
        super().stop()

    def _warmup_policy(self) -> tuple[bool, str | None]:
        """预热策略端（**不下发动作**）：``prepare(obs)`` + 必要时取一块丢弃。

        返回 ``(warmed, reason)``。无观测（机器人未就绪）→ 未热，可稍后重试。取到的那一块**丢弃**：
        它只用于让服务端把模型跑起来 / 测出真实块长（``observed_chunk_len`` → RTC 校准），
        真机的动作只在 ``infer rollout`` 里经 ``adapter.rollout`` 下发。
        """
        obs = self.adapter.observe()
        if obs is None:
            return False, "observation not ready"
        try:
            self.policy.prepare(obs)  # lerobot-act：SendPolicyInstructions（加载 checkpoint）；openpi：发一帧丢弃
        except Exception as exc:  # noqa: BLE001 预热失败不致命：可重试本命令 / 首个 rollout 再报
            return False, f"prepare failed: {exc}"
        if not getattr(self.policy, "chunk_seen", False):
            try:  # 只 prepare 不取块的策略（lerobot-act）：补一块，否则首个 rollout 仍要等取块
                self.policy.infer_chunk(obs)
            except Exception as exc:  # noqa: BLE001
                return False, f"warmup infer failed: {exc}"
        self.rtc.calibrate(getattr(self.policy, "observed_chunk_len", None))
        return True, None

    def _require_warmup(self, cmd) -> bool:
        """``infer rollout`` 门控：**预热进行中**、或未预热（``warmup_required=true`` 时）→ 409。

        理由（真机安全）：预热是唯一允许长阻塞的路径（连接 + 加载模型）且不下发动作；把这段耗时
        留在 rollout 上会变成「调用方超时判失败、动作却已下发到真机」。故缺省把「先 ``infer
        connect`` 预热」做成硬前置。

        **预热进行中一律拦下（与 ``warmup_required`` 取值无关）**：策略客户端的契约是「可跨线程
        调用，但同一时刻至多一个请求」（见 ``rtc/manager.py``）——预热线程与 rollout 并发会在同一条
        ws 上并发 send/recv（响应错位 / 连接被弄断）或抢走 lerobot-act 的单飞块，也会并发
        ``adapter.observe()``。``warmup_required=false`` 只表示「允许**不预热**直接 rollout（惰性
        自连）」，不表示「允许与预热并发」。
        """
        if self.warming:  # 预热中：等它跑完（或 estop / 退出取消）——与 warmup_required 无关
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="warmup in progress: wait for warmed_up (status) before rollout",
                    code=ErrorCode.CONFLICT,
                ),
            )
            return False
        if self.warmed_up or not self.warmup_required:
            return True
        self._reply(
            cmd,
            CommandResult(
                status="rejected",
                error="not warmed up: run 'infer connect' first (connect + prepare, no motion)",
                code=ErrorCode.CONFLICT,
            ),
        )
        return False

    def _ensure_connected(self, cmd) -> bool:
        """rollout 前惰性连接：未连接则 ``policy.ensure_connected()``（单次限时）。

        已预热（``warmed_up``）时这是 no-op；只有 ``warmup_required=false`` 的会话才会真的走到
        「未预热即 rollout」这条路上——此时行为与旧版一致（单次连接尝试，失败回执 error 可重试）。

        成功 → True；失败 → 回执 error 并返回 False（不执行推理，可重试）。
        """
        try:
            self.policy.ensure_connected()
            return True
        except Exception as exc:  # noqa: BLE001
            debug_print(self.name, f"policy connect failed: {exc}", "WARNING")
            self._reply(
                cmd, CommandResult(status="error", error=f"policy connect failed: {exc}", code=ErrorCode.UPSTREAM_ERROR)
            )
            return False

    def run(self):
        """阻塞式推理主循环：等待就绪 → 显式 infer connect → 等待 infer rollout 步进闭环。"""
        # 复位（reset() 非阻塞设 home 目标；RTC 块队列 / 步号归零）
        self.adapter.reset()
        self.policy.reset()
        self.rtc.reset()
        # 等待机器人就绪（期间可 session quit 退出 / robot estop 急停 / robot reset 复位）
        result = self._wait_ready()
        if result is not None:
            self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
            return result
        self.state = SessionState.READY

        debug_print(
            self.name,
            "Robot READY. infer connect to link, infer rollout to step, session quit to exit.",
            "INFO",
        )
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                self.state = SessionState.ERROR
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if self._dispatch_session_cmd(name, cmd):  # 会话特有：策略配置 / RTC
                continue
            outcome = self.dispatch_common(cmd)  # 公共命令：退出 / 急停 / 复位 / 录制 / 同步 / 元信息…
            if outcome == CommonDispatch.HANDLED:
                continue
            if outcome == CommonDispatch.QUIT:
                self.state = SessionState.FINISHED
                debug_print(self.name, "Inference finished, robot reset to home.", "INFO")
                return RunResult.FINISHED
            if outcome == CommonDispatch.ESTOP:
                self.state = SessionState.ERROR
                return RunResult.ERROR
            if name == CMD_INFER_CONNECT:  # 显式连接推理节点（单次尝试，可反复触发重连）
                self._connect_policy(cmd)
            elif name == CMD_INFER_ROLLOUT:  # 推理闭环：单步 / continuous 持续（多步 & drain 已取消）
                try:
                    mode = parse_rollout_mode(cmd.params.get("mode"))
                except ValueError as exc:
                    self._reply(cmd, CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT))
                    continue
                if not self._require_prompt(cmd):  # 需要 prompt 的策略：为空不能开始推理
                    continue
                if not self._require_warmup(cmd):  # 未预热（且 warmup_required）：先 infer connect 预热
                    continue
                if not self._ensure_connected(cmd):  # 已预热时为 no-op；warmup_required=false 才是惰性自连
                    continue
                if mode == ROLLOUT_MODE_CONTINUOUS:  # 持续推理：启动即回执，直到 stop / session quit / estop
                    self._reply(cmd, ok_result(state="continuous", started=True))
                    self._continuous = True
                    result = self._run_continuous()
                    self._continuous = False
                    if result is None:  # infer rollout stop：回到本循环（会话保持 ACTIVE / READY）
                        self.state = SessionState.READY
                        continue
                    self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
                    return result
                self._run_single(cmd)  # 单步推理（缺省）
            else:  # 未识别命令（当前任务不适用）统一回执，避免 submit 挂起
                self.reject_not_applicable(cmd)
                time.sleep(0.02)  # 无命令时轻量轮询（避免忙等）

    def _set_teleop(self, cmd) -> None:
        """遥操作 / 人工接管开关：**接管开始即丢弃未执行的动作块**（RTC 只存在于本会话）。

        接管意味着「接下来由人决定 target」，接管前预取 / 缓存的动作（含在途请求）都不应在交回
        后被执行——否则交回瞬间机械臂会朝接管**前**的轨迹走。``rtc.reset()`` 同时按世代作废
        在途结果，交回后的第一块因此用**当时**观测现算（模型动作与人工位姿的差仍由 ``step_rad``
        限速拉回）。

        判据用**命令语义**（``enabled``）而非适配器状态：后者在“不支持遥操作的适配器”上不会变，
        而这类适配器本来也没有可丢弃的接管前后语义（实时上也无副作用：队列本来就是空的）。
        """
        enabled, _mode = super()._set_teleop(cmd)
        if enabled:
            self.rtc.reset()

    def _dispatch_session_cmd(self, name, cmd) -> bool:
        """**本会话特有**命令：策略配置（``infer config(set)`` / ``infer model(set)``）与 RTC 参数。

        公共命令（退出 / 急停 / 复位 / 直发动作 / 遥操作 / 录制边界 / 同步 / 元信息）已由基座
        :meth:`BaseSession.dispatch_common` 统一处理；返回 True = 已回执。

        - ``infer config(set)`` / ``infer model(set)``：策略配置项（**功能模块**
          :class:`~motrix_edge.session.policy_config.PolicyConfigFeature`，与 RL 会话共用），
          会话内可改（下个请求生效）；
        - ``infer rtc(set)``：RTC 参数查询 / 设置（应用到运行中的 manager，下一块起生效）。

        ``runtime=False`` 的配置项（host / port 等）在**进入会话时**固化：会话内设置只写内存态
        配置（回执 ``deferred``），下次会话生效——不额外拒绝，也不需要单独一条命令。
        """
        if self.dispatch_policy_config(name, cmd):  # 策略配置项（功能模块）
            return True
        if name in (CMD_INFER_RTC, CMD_INFER_RTC_SET):  # RTC 参数（操作运行中的 manager）
            self._reply(cmd, self._on_infer_rtc(cmd))
            return True
        return False

    def _run_single(self, cmd) -> None:
        """infer rollout：单步推理闭环（一次 观测 → 推理（RTC 取块）→ 动作下发），回执动作。

        **下发前自查回执是否已过期**（``deadline_exceeded``）：调用方（HTTP / CLI 的 submit）
        超时放弃后，本步动作**必须丢弃**——否则就是「调用方看到失败、机器人却动了」。
        丢弃的那一步已在 RTC 里推进过步号（不自作回退，只记 ``dropped_actions`` 供诊断）。

        另外两类不下发：动作块为空（``action is None``，只回执）；遥操作（人工接管）中
        ``adapter.rollout`` 返回 False（SDK 409）→ 推理让位，回执说明原因（不回退步号）。
        """
        obs = self.adapter.observe()  # 推理输入（显示观测由节点级写入 frame_manager）
        if obs is None:
            self._reply(
                cmd, CommandResult(status="rejected", error="observation not ready", code=ErrorCode.UNAVAILABLE)
            )
            return
        action = self.engine.step(obs)  # 引擎（RtcEngine）：必要时登记预取（后台线程）→ 取本步动作
        if action is not None and deadline_exceeded(cmd):  # 调用方已放弃等回执 → 不下发动作
            self._dropped_actions += 1
            debug_print(
                self.name,
                "Rollout action dropped: reply deadline exceeded (robot not moved).",
                "WARNING",
            )
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="reply deadline exceeded: action dropped (robot not moved)",
                    code=ErrorCode.TIMEOUT,
                ),
            )
            return
        if action is not None and self.adapter.rollout(action) is False:
            # 遥操作（人工接管）中：推理让位（SDK 409）——本步不下发，回执说明原因
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="teleop (human takeover) active: rollout refused",
                    code=ErrorCode.CONFLICT,
                ),
            )
            return
        repr_action = self._action_repr(action)
        debug_print(self.name, f"Rollout step executed (action={repr_action}).", "INFO")
        self._reply(
            cmd,
            ok_result(state="ready", count=1, action=repr_action, actions=[repr_action]),
        )

    # ---- 基座钩子（会话特有差异；公共命令的骨架在 BaseSession.dispatch_common）----
    def _on_common_quit(self, cmd) -> None:
        """退出会话：预热在跑也要能退出（取消 + 打断在飞调用）→ 回 home（回执由基座做）。"""
        self.cancel_warmup("session quit")
        self.adapter.reset()

    def _on_common_estop(self, cmd) -> None:
        """急停：预热在跑也立即取消（不等模型加载完）；``safe_stop`` 由基座调用。"""
        self.cancel_warmup("estop")

    def _on_episode_start(self, cmd) -> bool:
        """``capture episode start``：开始一轮 rollout 录制（录制 ``task_name`` = prompt → 同一门控）。

        通知机器人进程开启录制（``adapter.start_capture``）；录制期间 robot 按帧录 mcap
        （含 action）。录制元信息（operator=policy、task_name=prompt）由调用方**显式**
        ``capture sync`` 同步（见 server /v1/infers status 的 ``capture_meta``）。
        """
        if not self._require_prompt(cmd):  # 需要 prompt 的策略：录制 task_name 为空无意义
            return False
        self.recorder.start()  # 开录 + 生成 episode_id（基座共用一份）
        debug_print(self.name, "Rollout recording started (capture episode start).", "INFO")
        self._reply(cmd, ok_result(state="recording", episode="start", recording=True))
        return True

    def _on_episode_end(self, cmd) -> None:
        """``capture episode end``：结束一轮 rollout 录制（机器人进程保存 episode）。"""
        self.recorder.end()
        debug_print(self.name, "Rollout recording ended (capture episode end).", "INFO")
        self._reply(cmd, ok_result(state="ready", episode="end", recording=False))

    def _on_infer_rtc(self, cmd):
        """``infer rtc`` / ``infer rtc set <json>``：查询 / 设置 RTC 参数（应用到运行中 manager）。

        先经 ``handle_infer_rtc`` 校验并写入内存态 ``base_cfg["policy"]["rtc"]``（下次会话
        生效）；设置成功时额外 ``self.rtc.configure`` 应用到当前会话的 manager（下一块起生效）。
        参数非法 → rejected（400，不崩溃）。
        """
        result = handle_infer_rtc(self.base_cfg, cmd)
        if result.status != "ok":
            return result
        if cmd.name == CMD_INFER_RTC_SET:
            try:
                self.rtc.configure(**result.data["rtc"])
            except ValueError as exc:  # 理论上 handle_infer_rtc 已校验，双保险
                return CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT)
        return ok_result(state=getattr(self, "state", "ready"), rtc=self.rtc.status())

    def rtc_status(self) -> dict | None:
        """RTC 运行状态（server ``/v1/infers`` 的 ``rtc`` 字段）；无 manager → None。"""
        rtc = getattr(self, "rtc", None)
        return rtc.status() if rtc is not None else None

    def _run_continuous(self) -> RunResult | None:
        """infer rollout continuous：持续推理，每步轮询命令响应停止 / 退出 / 复位 / 急停 / 录制。

        启动命令已回执 started（prompt 已在启动前校验非空）；持续直到 ``infer rollout
        stop``（**回到会话主循环，会话保持 ACTIVE / READY，策略连接不断**）/ session quit
        （FINISHED）/ robot estop（ERROR）/ node 失联（ERROR）。持续期间接受
        ``capture episode start/end``（录制 rollout）与 ``capture sync``（同步元信息）——
        录制与持续推理正交（robot 不关心模式）。返回 ``None`` = 回到会话主循环。
        """
        debug_print(self.name, "Continuous rollout started (infer rollout stop to stop).", "INFO")
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == CMD_INFER_ROLLOUT_STOP:  # 停止持续推理：留在会话（策略连接 / 机器人状态不变）
                # 暂停即丢弃未执行的动作块：恢复时块可能已经过时（尤其中间插了人工接管——
                # 机械臂已被带离原轨迹，旧块会把动作往回拉）；下一块用**当时**观测现算。
                self.rtc.reset()
                self._reply(cmd, ok_result(state="ready", continuous=False))
                debug_print(self.name, "Continuous rollout stopped by request (session kept).", "INFO")
                return None
            if name == CMD_INFER_ROLLOUT:  # 持续中重复 rollout：拒绝
                self._reply(
                    cmd,
                    CommandResult(
                        status="rejected", error="continuous rollout already running", code=ErrorCode.CONFLICT
                    ),
                )
                continue
            if self._dispatch_session_cmd(name, cmd):  # 会话特有：策略配置 / RTC
                continue
            # 步进循环内只允许「安全 + 本轮边界」类公共命令（见 STEP_LOOP_COMMANDS）：
            # 退出 / 急停 / 复位 / 录制边界 / 同步走基座钩子，其余（如 robot execute）一律拒绝
            outcome = self.dispatch_common(cmd, reset_state="continuous", only=STEP_LOOP_COMMANDS)
            if outcome == CommonDispatch.HANDLED:
                continue
            if outcome == CommonDispatch.QUIT:
                debug_print(self.name, "Continuous rollout stopped, inference finished.", "INFO")
                return RunResult.FINISHED
            if outcome == CommonDispatch.ESTOP:
                return RunResult.ERROR
            self.reject_not_applicable(cmd, "during continuous rollout")
            obs = self.adapter.observe()
            if obs is None:  # 观测未就绪：按步进间隔轮询
                time.sleep(self.step_interval)
                continue
            action = self.engine.step(obs)  # 引擎（RtcEngine）：必要时登记预取（后台线程）→ 取本步动作
            if action is not None:
                # 遥操作（人工接管）中 SDK 拒绝本拍（409，adapter 已限流日志）：继续下一拍，
                # 遥操作关闭（robot teleop false）后自动恢复下发。
                self.adapter.rollout(action)
            time.sleep(self.step_interval)  # 按 infer_freq 控制步进节奏

    @staticmethod
    def _action_repr(action):
        """动作 → JSON 可表达（list，**保留 3 位小数**）；None 保持 None。

        该值同时用于日志与 HTTP 回执 → 日志与网页展示口径一致（内部链路仍用全精度）。
        """
        if action is None:
            return None
        return round_floats(action)
