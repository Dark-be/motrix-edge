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

"""session 基类 —— 会话（任务执行器）的最小接口，不包含节点生命周期。

节点生命周期（IDLE / ACTIVE / ERROR）由 EdgeNode（见 node.py）统一管理。
会话仅作为被节点选择性实例化、启停的任务执行器：
  session_start()  节点进入 ACTIVE 前调用：连接硬件、初始化会话
  run()            节点进入 ACTIVE 时调用：阻塞式会话执行，返回 RunResult 告知结束原因
  session_finish() 节点释放会话资源时调用：断开硬件
  safe_stop()      安全停止（幂等、失败安全）：急停/异常时立即停止机器人运动
"""

import time

from motrix_edge.adapter import AdapterCapability
from motrix_edge.command import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_SYNC,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_SESSION_QUIT,
    TELEOP_COMMANDS,
    CommandResult,
    apply_teleop,
    deadline_exceeded,
    handle_capture_meta,
    ok_result,
    parse_layout,
    parse_meta,
    parse_qpos,
    reject_legacy_action_space,
)
from motrix_edge.errors import ErrorCode
from motrix_edge.frame import FrameManager
from motrix_edge.utils.capture_meta import CaptureMetaStore
from motrix_edge.utils.data_handler import debug_print, round_floats

from .recording import EpisodeRecorder


def _noop_command_source():
    """默认命令源：无输入（返回 None）。实际 CLI / HTTP 经 CommandBus 注入。"""
    return None


def _cmd_name(src) -> str | None:
    """命令源返回值 → 命令名（兼容 Command / 命令名字符串 / None）。"""
    if src is None:
        return None
    return getattr(src, "name", None) or (src if isinstance(src, str) else None)


class CommonDispatch:
    """``BaseSession.dispatch_common`` 的返回口径（会话循环据此继续 / 结束）。"""

    UNHANDLED = "unhandled"  # 不是（本次允许的）公共命令 → 调用方走自己的分支
    HANDLED = "handled"  # 已回执 → 继续循环
    QUIT = "quit"  # 已回执 + 记录退出 → 会话应结束（FINISHED）
    ESTOP = "estop"  # 已回执（node_state=error）→ 会话应结束（ERROR）


# 采集元信息选项命令（配置级：任务态同样可用，读写 capture.yml）
_CAPTURE_META_COMMANDS = (
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
)

# **步进循环**（RL 闭环 / 持续推理）内允许的公共命令：只留安全与“本轮边界”类。
# 其余（`robot execute` / 遥操作 / 元信息选项）在步进中途一律按「本轮不适用」拒绘：
# 中途插入外来动作会破坏「下一步观测由本步动作产生」的数据链，而元信息选项 在数采会话里改即可。
STEP_LOOP_COMMANDS = (
    CMD_SESSION_QUIT,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_RESET,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_SYNC,
)


class RunResult:
    """会话 run() 的返回结果 —— session → node 的任务结束契约。

    OK          任务执行成功（会话继续运行）
    FINISHED    任务正常结束（节点释放会话并回到 IDLE，等待下一次任务选择）
    ERROR       硬件/通信异常（已安全停止；节点转 ERROR）
    INTERRUPTED 当前回合被打断（已丢弃未提交数据）、会话仍可继续（会话回 READY）
    """

    OK = "ok"
    FINISHED = "finished"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class SessionState:
    """会话实时状态（供外部随时查询当前任务流程所处阶段）。"""

    INIT = "init"  # 已创建，未连接
    READY = "ready"  # 运行中（持续观测 / 持续推理）
    ERROR = "error"  # 硬件/通信异常
    FINISHED = "finished"  # 会话结束


class BaseSession:
    """所有会话的基类（任务执行器接口 + **无引擎的命令循环**）。

    ``capture`` 就是基座的直接装配（见 :class:`CaptureSession`）；有步进引擎的会话
    （``infer`` / ``rl``）覆写 :meth:`run`。
    """

    #: 本会话要求的 adapter 能力（``AdapterCapability``；``None`` = 不要求注入 adapter）。
    #: 构造时由基座统一校验（缺 adapter / 能力不符 → ValueError），会话不再各写一遍。
    required_capability = None

    def __init__(
        self,
        base_cfg,
        name="BaseSession",
        command_source=None,
        frame_manager=None,
        adapter=None,
        capture_meta_store=None,
    ):
        self.name = name
        self.base_cfg = base_cfg
        self.command_source = command_source if command_source is not None else _noop_command_source
        self.frame_manager = frame_manager or FrameManager()  # 观测帧缓存（preview / WebRTC 消费）
        # 节点级 active adapter（注入，生命周期归节点）：会话只引用，不持有 / 不释放
        self.adapter = adapter
        # 回合录制（开轮 / 关轮 + ``episode_id``）：capture / infer / rl 共用一份，见 recording.py
        self.recorder = EpisodeRecorder(adapter)
        # 采集元信息选项存储（capture.yml）：capture meta 配置命令任务态读写；
        # 进程内单实例——节点注入同一份（与 /v1/captures/meta 共用同一把锁）；
        # 缺省 None 表示会话自行按需创建，测试可注入临时 store。
        self.capture_meta_store = capture_meta_store or CaptureMetaStore()
        # 退出命令（session quit）：submit 通道命令由节点任务结束后补发回执
        self.exit_command = None
        # 外部请求停止标志（node 失联 ERROR 时终止仍在运行的任务线程用）
        self._stop_requested = False
        self.state = SessionState.INIT  # 实时状态（供外部查询）
        self._check_adapter_capability()

    def _check_adapter_capability(self) -> None:
        """校验注入的 adapter 满足 :attr:`required_capability`（不符 → ``ValueError``）。

        文案里的会话短名取自 ``name``（``"CaptureSession"`` → ``capture``），与各会话此前
        各自手写的报错逐字一致；``required_capability=None``（如 ``BaseSession`` 自身）不校验。
        """
        if self.required_capability is None:
            return
        scope = self.name.removesuffix("Session").lower()
        if self.adapter is None:
            raise ValueError(f"{scope} session requires an injected adapter (owned by node)")
        if not self.adapter.capabilities.supports(self.required_capability):
            raise ValueError(f"injected adapter does not support {self.required_capability.name} capability")

    def stop(self):
        """请求会话停止：设置标志，会话主循环检查后尽快返回（RunResult.ERROR）。

        供 node 失联 / 健康失败进入 ERROR 时终止仍在运行的任务线程——否则会话循环
        会与 node 主循环竞争消费总线命令，把 node reset 等恢复命令拦截为 rejected。
        """
        self._stop_requested = True

    @property
    def recording(self) -> bool:
        """当前是否开启了一轮数据回合（``capture episode start`` 之后为 True）。"""
        return self.recorder.recording

    @property
    def episode_id(self) -> str | None:
        """当前 / 最近一轮数据回合的标识（``{毫秒}-{序号}``；关轮后保留）。"""
        return self.recorder.episode_id

    def session_start(self):
        """连接硬件、初始化会话（节点进入 ACTIVE 前调用）。"""
        pass

    def session_finish(self):
        """释放资源（节点释放会话时调用）。"""
        pass

    def safe_stop(self):
        """安全停止（幂等、失败安全）：委托给机器人适配器的 safe_stop；无适配器时 no-op。"""
        adapter = getattr(self, "adapter", None)
        if adapter is not None:
            try:
                adapter.safe_stop()
            except Exception as exc:
                debug_print(self.name, f"safe_stop failed: {exc}", "ERROR")

    def _wait_ready(self) -> RunResult | None:
        """阻塞等待机器人就绪（``adapter.ready``）；期间响应命令：

        - ``session quit`` → 记录退出命令并返回 ``FINISHED``；
        - 急停 → 安全停止并返回 ``ERROR``；
        - 复位（robot reset）→ 重新 ``adapter.reset()``。

        就绪后返回 ``None``，调用方继续任务流程。命令处理复用 :meth:`dispatch_common`
        （与主循环同一份语义）：等待就绪期间也能正确响应复位 / 直发动作 / 遥操作 / 元信息，
        且不适用命令同样回执（不让 submit 通道白等）。
        """
        debug_print(self.name, "Waiting for robot ready...", "INFO")
        while not self.adapter.ready:
            if self._stop_requested:
                return RunResult.ERROR
            debug_print(self.name, "Robot not started yet, verify hardware.", "WARNING")
            cmd = self.command_source()
            outcome = self.dispatch_common(cmd)
            if outcome == CommonDispatch.QUIT:
                return RunResult.FINISHED
            if outcome == CommonDispatch.ESTOP:
                return RunResult.ERROR
            if outcome == CommonDispatch.UNHANDLED:
                self.reject_not_applicable(cmd)
            time.sleep(1)
        return None

    def _on_capture_meta(self, cmd):
        """处理采集元信息选项命令（capture meta list/add/edit/delete/delete-key）。

        会话运行期间（ACTIVE）命令由会话循环 poll，本方法让配置命令在任务态也可用——
        委托 ``command.config_commands.handle_capture_meta``（读写 capture.yml），与节点
        主循环（非任务态）共用同一逻辑，保证「任何状态可用」。
        """
        return handle_capture_meta(cmd, self.capture_meta_store)

    # ---- 公共命令分发（各会话共用一份；会话差异只在钩子里）-------------------
    def dispatch_common(self, cmd, *, reset_state: str = "ready", only=None) -> str:
        """公共命令分发：退出 / 急停 / 复位 / 直发动作 / 遥操作 / 录制边界 / 同步 / 元信息。

        返回 :class:`CommonDispatch` 的口径值；``UNHANDLED`` 表示「不是（本次允许的）公共命令」，
        由调用方走自己的分支（会话特有命令 + 未识别兜底）。

        Args：
            reset_state: ``robot reset`` 回执里的 ``state`` 字段（缺省 ``ready``；RL 闭环内为 ``rl``）。
            only: 允许处理的命令白名单（``None`` = 全部）；**步进循环**传 :data:`STEP_LOOP_COMMANDS`，
                避免中途插入会破坏数据链的命令（如 ``robot execute``）。

        会话差异全部走**钩子**（``_on_common_estop`` / ``_on_common_quit`` / ``_on_episode_start`` …）：
        基座只负责「取命令 → 分发 → 回执」这条骨架，避免每个会话循环各写一遍而漂移。
        """
        if cmd is None:
            return CommonDispatch.UNHANDLED
        name = _cmd_name(cmd)
        if only is not None and name not in only:
            return CommonDispatch.UNHANDLED
        if name == CMD_SESSION_QUIT:
            self._on_common_quit(cmd)
            self._record_exit(cmd)
            return CommonDispatch.QUIT
        if name == CMD_ROBOT_ESTOP:
            self._on_common_estop(cmd)
            self.safe_stop()
            self._reply(cmd, ok_result(node_state="error"))
            return CommonDispatch.ESTOP
        if name == CMD_ROBOT_RESET:
            self._on_common_reset(cmd)
            self.adapter.reset()
            self._reply(cmd, ok_result(state=reset_state))
            return CommonDispatch.HANDLED
        if name == CMD_ROBOT_EXECUTE:
            self._execute_action(cmd)
            return CommonDispatch.HANDLED
        if name in TELEOP_COMMANDS:
            self._set_teleop(cmd)
            return CommonDispatch.HANDLED
        if name == CMD_CAPTURE_EPISODE_START:
            self._on_episode_start(cmd)
            return CommonDispatch.HANDLED
        if name == CMD_CAPTURE_EPISODE_END:
            self._on_episode_end(cmd)
            return CommonDispatch.HANDLED
        if name == CMD_CAPTURE_SYNC:
            self._on_capture_sync(cmd)
            return CommonDispatch.HANDLED
        if name in _CAPTURE_META_COMMANDS:
            self._reply(cmd, self._on_capture_meta(cmd))
            return CommonDispatch.HANDLED
        return CommonDispatch.UNHANDLED

    def reject_not_applicable(self, cmd, scope: str | None = None) -> None:
        """未识别 / 当前循环不适用命令的统一回执（``<cmd> not applicable``）。

        ``scope`` 追加循环限定语（如 ``during rl rollout`` / ``during continuous rollout``），
        与各循环既有文案逐字一致；``cmd`` 为 ``None``（本轮无命令）时 no-op。
        """
        if cmd is None:
            return
        suffix = f" {scope}" if scope else ""
        self._reply(
            cmd,
            CommandResult(status="rejected", error=f"{_cmd_name(cmd)} not applicable{suffix}", code=ErrorCode.CONFLICT),
        )

    # ---- 会话差异钩子（基类为无副作用默认实现，子类按需覆盖）-------------------
    def _on_common_quit(self, cmd) -> None:
        """钩子：``session quit`` 的会话特有清理（默认 no-op；``_record_exit`` 由基座做）。"""

    def _on_common_estop(self, cmd) -> None:
        """钩子：急停的会话特有清理（默认 no-op；``safe_stop`` 与回执由基座做）。"""

    def _on_common_reset(self, cmd) -> None:
        """钩子：``robot reset`` 的会话特有清理（默认 no-op；``adapter.reset`` 与回执由基座做）。"""

    def _on_episode_start(self, cmd) -> bool:
        """``capture episode start``：默认实现 = **采集会话**（通知机器人开录 + 回执），返回是否已开轮。

        推理会话在此加 prompt 门控（录制 ``task_name`` = prompt）；RL 会话在此清空过渡缓冲并记回合号。
        """
        self.recorder.start()
        self._reply(cmd, ok_result(state="ready", episode="start"))
        return True

    def _on_episode_end(self, cmd) -> None:
        """``capture episode end``：默认实现 = **采集会话**（通知机器人保存 episode + 回执）。"""
        self.recorder.end()
        self._reply(cmd, ok_result(state="ready", episode="end"))

    def _on_capture_sync(self, cmd) -> None:
        """``capture sync --meta <json>``：同步采集元信息到机器人进程（三个会话同款）。"""
        try:
            meta = parse_meta(cmd.params.get("meta"))
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT))
            return
        self.adapter.sync_capture_meta(meta)
        self._reply(cmd, ok_result(state=getattr(self, "state", "ready"), meta=meta))

    def _record_exit(self, cmd) -> None:
        """记录退出命令（仅 submit 通道命令需节点补发回执），由节点任务结束后补发。"""
        if cmd is not None and getattr(cmd, "reply_to", None) is not None:
            self.exit_command = cmd

    def _reply(self, cmd, result) -> None:
        """统一回执：命令携带 reply_to（submit 通道）则回调结果。"""
        if cmd is not None and getattr(cmd, "reply_to", None) is not None:
            cmd.reply_to(result)

    def _execute_action(self, cmd) -> None:
        """robot execute：解析 qpos + 可选动作空间 → ``adapter.execute``（维度 / 空间校验在 adapter）。

        ``layout``（``joint`` 缺省 / ``pose``）声明 ``qpos`` 位置参数的语义：``pose`` 时
        机器人进程按位姿解算成关节目标再运行（见 ``/v1/execute`` 契约）。
        参数缺失 / 非法 / 维度不符 / 布局不支持 → 回执 rejected（不崩溃）；成功 → 回执 ok
        （回显 action 与布局，数值 3 位小数）。**下发前自查回执是否已过期**
        （``deadline_exceeded``）：调用方超时放弃后不再动真机。
        """
        if deadline_exceeded(cmd):  # 调用方已放弃等回执 → 不下发动作
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="reply deadline exceeded: action dropped (robot not executed)",
                    code=ErrorCode.TIMEOUT,
                ),
            )
            return
        try:
            qpos = parse_qpos(cmd.params.get("qpos"))
            reject_legacy_action_space(cmd.params)
            layout = parse_layout(cmd.params.get("layout"))
            self.adapter.execute(qpos, layout=layout)
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT))
            return
        self._reply(cmd, ok_result(state="ready", action=round_floats(qpos), layout=layout))

    def _set_teleop(self, cmd) -> tuple[bool, str | None]:
        """遥操作 / 人工接管的统一入口（``robot teleop`` | ``robot teach`` | ``robot takeover``）。

        模式：``robot teach`` → ``absolute``（示教）、``robot takeover`` → ``delta``（人工接管，
        接管瞬间主 / 从位姿为锚点、只叠加主臂增量）；``robot teleop`` 用可选 ``mode``（缺省
        ``absolute``）。遥操作开启期间机器人侧会拒绝 ``rollout``（推理让位，见
        `wiki/design/robot_pipeline_teleop.md`）。
        参数缺失 / 非法 → 回执 rejected（不崩溃）；成功 → 回执 ok（回显 teleop 与模式）。

        返回 ``(enabled, mode)`` 供子类复用（如推理会话据此丢弃未执行的动作块）；参数非法时
        回执 rejected 并返回 ``(False, None)``。
        """
        try:
            enabled, mode = apply_teleop(self.adapter, cmd)
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT))
            return False, None
        self._reply(cmd, ok_result(state="ready", teleop=enabled, mode=mode))
        return enabled, mode

    def _bind_policy_adapter(self):
        """把 adapter 运行时启用的布局（qpos 维数 + 相机名）传给策略客户端。

        布局单一事实来源 = adapter 配置（``adapter config set`` 的 enabled_arms /
        enabled_cameras）：openpi 客户端据此过滤要下发的相机（**不另读 edge.yml 的相机名**）；
        策略无 ``bind_adapter``（如 lerobot-act / 测试替身）→ no-op。adapter 未提供相机名 /
        绑定失败不致命（observe 本就只含启用相机，仍按观测透传）。
        """
        bind = getattr(getattr(self, "policy", None), "bind_adapter", None)
        adapter = getattr(self, "adapter", None)
        if bind is None or adapter is None or not callable(bind):
            return
        camera_names = list(getattr(adapter, "images", None) or [])
        if not camera_names:
            caps = getattr(adapter, "capabilities", None)
            camera_names = list(getattr(caps, "image_names", None) or [])
        action_dim = getattr(adapter, "action_dim", None)
        try:
            bind(action_dim=action_dim, camera_names=camera_names or None)
            debug_print(
                self.name,
                f"Policy bound to adapter layout: action_dim={action_dim}, cameras={camera_names}",
                "INFO",
            )
        except Exception as exc:  # noqa: BLE001 布局绑定失败不致命
            debug_print(self.name, f"policy bind_adapter failed: {exc}", "WARNING")

    def run(self):
        """基座默认循环（**无引擎装配**）：复位 → 等待就绪 → 持续消费公共命令。

        命令面（退出 / 急停 / 复位 / 直发动作 / 遥操作 / 录制边界 / 同步 / 元信息）全部来自
        :meth:`dispatch_common` 及其默认钩子——本类没有自己的命令分支。「每步做什么」归引擎，
        故基座不 ``observe``（显示观测由节点级持续写入 ``frame_manager``）。``capture`` 直接
        使用本实现；``infer`` / ``rl`` 覆写。
        """
        # 复位 + 等待就绪（期间可 session quit 退出 / robot estop 急停 / robot reset 复位）
        self.adapter.reset()
        result = self._wait_ready()
        if result is not None:
            self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
            return result
        self.state = SessionState.READY

        debug_print(self.name, "Robot READY. session commands, session quit to exit.", "INFO")
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                self.state = SessionState.ERROR
                return RunResult.ERROR
            cmd = self.command_source()
            outcome = self.dispatch_common(cmd)
            if outcome == CommonDispatch.QUIT:
                self.state = SessionState.FINISHED
                debug_print(self.name, f"{self.name} finished.", "INFO")
                return RunResult.FINISHED
            if outcome == CommonDispatch.ESTOP:
                self.state = SessionState.ERROR
                return RunResult.ERROR
            if outcome == CommonDispatch.UNHANDLED:  # 未识别命令：统一回执，避免 submit 挂起
                self.reject_not_applicable(cmd)
                time.sleep(0.02)  # 无命令时轻量轮询（避免忙等）


class CaptureSession(BaseSession):
    """数据采集会话 —— **基座装配**（无步进引擎）。

    生命周期由 EdgeNode 管理（session_start → run → session_finish）：进入会话后持续消费公共
    命令（session quit 退出、robot estop 急停、robot execute / teleop、capture episode / sync）。
    **显示观测由节点级持续写入 ``frame_manager``**，本会话不 ``observe`` / 不写 ``frame_manager``
    ——采集只驱动机器人进程采集（一轮起止、元信息同步）。要求 adapter 具备 ``CAPTURE`` 能力。
    """

    required_capability = AdapterCapability.CAPTURE

    def session_start(self):
        """进入会话（节点进入 ACTIVE 前调用）：adapter 已由节点 discover 绑定，直接就绪。"""
        self.state = SessionState.READY

    def session_finish(self):
        """释放资源（节点释放会话时调用）。adapter 由节点持有，不在此释放。"""
        self.state = SessionState.FINISHED
