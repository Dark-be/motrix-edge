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

import time

import numpy as np

from motrix_edge.adapter import AdapterCapability
from motrix_edge.policy import (
    get_policy,
    policy_config_connect_locked_keys,
    validate_policy_type,
)
from motrix_edge.rtc import build_rtc
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_CONNECT,
    CMD_INFER_IP,
    CMD_INFER_IP_SET,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
    CMD_INFER_PORT,
    CMD_INFER_PORT_SET,
    CMD_INFER_PROMPT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_RTC,
    CMD_INFER_RTC_SET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    ROLLOUT_MODE_CONTINUOUS,
    CommandResult,
    handle_infer_rtc,
    handle_policy_config,
    ok_result,
    parse_meta,
    parse_rollout_mode,
    policy_config_status,
)
from motrix_edge.utils.data_handler import debug_print

from .base import BaseSession, RunResult, SessionState, _cmd_name


class InferSession(BaseSession):
    """推理会话 —— 组合 RobotAdapter + 推理策略客户端的推理执行器。

    生命周期由 EdgeNode 管理（session_start → run → session_finish）。**无「多步推理」模式**：
    推理由单步 ``infer rollout`` / 持续 ``infer rollout continuous`` 驱动；**动作块切分与
    时序平滑由 RTCManager 负责**（策略只提供原始动作块：``policy.infer_chunk``）。**推理时
    rollout 录制** = 像采集一样经 ``capture episode start/end`` 控制一轮 episode——robot 不关心
    是推理还是采集（capturing 期间按帧录 mcap，含 action）。**prompt 仅语言条件策略需要**：
    需要 prompt 的策略（openpi）在推理/录制开始前必须已 ``infer prompt <text>`` 预置非空文本，
    否则拒绝；非语言条件策略（act）不需要 prompt，不参与门控。录制时由调用方显式
    ``capture sync`` 同步采集元信息（operator=policy、task_name=prompt 由会话默认上报）。RTC 参数经
    ``infer rtc`` / ``infer rtc set <json>`` 查询与运行期修改（见 wiki/design/motrix_edge_rtc.md）。
    """

    def __init__(self, base_cfg, command_source=None, frame_manager=None, adapter=None, policy_type=None):
        super().__init__(
            base_cfg=base_cfg,
            name="InferSession",
            command_source=command_source,
            frame_manager=frame_manager,
            adapter=adapter,
        )
        if self.adapter is None:
            raise ValueError("infer session requires an injected adapter (owned by node)")
        if not self.adapter.capabilities.supports(AdapterCapability.EXECUTE):
            raise ValueError("injected adapter does not support EXECUTE capability")
        self.policy_config = self.base_cfg.get("policy", {})
        # 推理步进频率（Hz）：会话 观测→推理→下发动作 的节奏。``policy.infer_freq`` 可调
        # （默认 10Hz ≈ 0.1s/步；测试传 1000 让主循环几乎不 sleep）。间隔 = 1 / infer_freq。
        infer_freq = float(self.policy_config.get("infer_freq", 10.0))
        self.step_interval = 1.0 / infer_freq if infer_freq > 0 else 0.1
        # 运行时策略选择：session run infer 携带 policy_type（HTTP / 命令）；由节点校验
        self.policy_type = policy_type
        self.policy = get_policy(base_cfg, policy_type=self.policy_type)
        # 会话创建即应用内存态已预置的配置项：如 prompt（进入会话前经 `infer prompt` /
        # POST /v1/infers body 的 config 预置）；不声明该项的策略无此入口（no-op）。
        self._apply_prompt(self.policy_config.get("prompt"))
        # 把 adapter 运行时启用的布局（qpos 维数 + 相机名）传给策略客户端（openpi 据此
        # 过滤要下发的相机，不另读 edge.yml 相机名；策略无 bind_adapter 则 no-op）
        self._bind_policy_adapter()
        # 实时动作块管理器（RTC）：策略只提供原始动作块（infer_chunk），块长上限 H / 三元切分
        # （前置段 P 跳过 + 执行段 + 后缀段）/ 时序平滑 / 预取时机由 RTCManager 负责
        # （参数 = base_cfg policy.rtc，运行期可改）。control_hz = 控制频率：把实测推理耗时折算成
        # 步数上报（供人工设定前置段 P）。配置非法（P+E+S>H / S<=P）不应到此——命令层已拦截。
        try:
            self.rtc = build_rtc(
                self.policy,
                self.policy_config.get("rtc") or {},
                control_hz=(1.0 / self.step_interval) if self.step_interval > 0 else None,
            )
        except ValueError as exc:  # 兜底：内存态配置非法时退回代码缺省，保证会话可进入
            debug_print(self.name, f"RTC config invalid ({exc}); falling back to defaults", "WARNING")
            self.rtc = build_rtc(self.policy, control_hz=(1.0 / self.step_interval) if self.step_interval > 0 else None)

        self.state = SessionState.INIT  # 实时状态（供外部查询）
        # 录制状态：推理会话内是否开启了一轮 rollout 录制（capture episode start/end）。
        # 录制本身由机器人进程自维护（capturing=True 按帧录 mcap）；本标记只作会话侧
        # 上报（server /v1/infers status 的 recording 字段）。
        self._recording = False

        debug_print(self.name, f"Policy config: {self.policy_config} (type={self.policy_type})", "INFO")

    @property
    def connected(self) -> bool:
        """策略是否已连接推理节点（委托 policy.connected；rollout 可惰性自动连接）。"""
        return bool(getattr(self.policy, "connected", False))

    @property
    def recording(self) -> bool:
        """推理会话当前是否开启了一轮 rollout 录制（capture episode start 后为 True）。"""
        return bool(self._recording)

    @property
    def prompt(self) -> str | None:
        """当前推理文本指令（策略客户端 prompt；未设置为 None）。

        仅语言条件策略（``requires_prompt=True``，如 openpi）必需；act 等非语言条件策略
        不需要（保持 None，不参与门控、不下发）。
        """
        return getattr(getattr(self, "policy", None), "prompt", None)

    @property
    def prompt_required(self) -> bool:
        """当前策略是否需要 prompt（委托 ``policy.requires_prompt``）。"""
        return bool(getattr(getattr(self, "policy", None), "requires_prompt", False))

    def _require_prompt(self, cmd) -> bool:
        """推理 / rollout 录制前门控：**仅对需要 prompt 的策略**（``policy.requires_prompt``）。

        语言条件策略（openpi）要求会话内已 ``infer prompt <text>`` 预置非空文本——空 →
        回执 rejected（400）并返回 False（不执行推理 / 不开录制）；prompt 同时作为录制
        episode 的 task_name。非语言条件策略（act：ACT 不接受文本条件）**不需要 prompt**，
        不门控、直接放行。
        """
        if not self.prompt_required:
            return True
        prompt = self.prompt
        if not prompt or not str(prompt).strip():
            self._reply(
                cmd,
                CommandResult(
                    status="rejected",
                    error="prompt required: set via 'infer prompt <text>' before inference / recording",
                    status_code=400,
                ),
            )
            return False
        return True

    def session_start(self):
        """进入会话（节点进入 ACTIVE 前调用）：adapter 已由节点 discover 绑定。

        推理节点**不自动连接**：连接时机内聚到 policy——首个 ``infer rollout`` 前经
        ``policy.ensure_connected()`` 惰性自连（单次限时）；显式 ``infer connect`` 可选
        预连 + 预热（避免同步连接阻塞命令回执 / 无限重试）。
        """
        self.state = SessionState.READY

    def session_finish(self):
        """释放资源（节点释放会话时调用）。adapter 由节点持有，不在此释放。"""
        self.policy.disconnect()
        self.state = SessionState.FINISHED

    def _connect_policy(self, cmd) -> None:
        """infer connect（可选）：显式预连 + 预热。

        连接成功后用当前观测 ``policy.prepare(obs)`` 提前下发策略指令（act：服务端加载
        模型；openpi：no-op）；预热失败不致命（首个 rollout 会自动重试）。成功回执 ok
        （含服务端 metadata）；连接失败回执 error（保持未连接，可重试；rollout 也可惰性
        自动连接）。
        """
        try:
            self.policy.connect()
            metadata = dict(getattr(self.policy, "server_metadata", None) or {})
            self._reply(cmd, ok_result(state="ready", connected=True, metadata=metadata))
            debug_print(self.name, f"Inference server connected: {metadata}", "INFO")
        except Exception as exc:  # noqa: BLE001 单次尝试失败：保持未连接，不无限重试
            debug_print(self.name, f"infer connect failed: {exc}", "WARNING")
            self._reply(cmd, CommandResult(status="error", error=f"infer connect failed: {exc}", status_code=502))
            return
        # 预热：用当前观测下发策略指令（act 提前加载模型；openpi no-op）。
        try:
            obs = self.adapter.observe()
            if obs is not None:
                self.policy.prepare(obs)
                debug_print(self.name, "Policy prepared (model warmed up).", "INFO")
        except Exception as exc:  # noqa: BLE001 预热失败不致命：首个 rollout 会自动重试
            debug_print(self.name, f"policy prepare failed (will retry on rollout): {exc}", "WARNING")

    def _ensure_connected(self, cmd) -> bool:
        """rollout 前惰性连接：未连接则 ``policy.ensure_connected()``（单次限时）。

        成功 → True；失败 → 回执 error 并返回 False（不执行推理，可重试）。
        """
        try:
            self.policy.ensure_connected()
            return True
        except Exception as exc:  # noqa: BLE001
            debug_print(self.name, f"policy connect failed: {exc}", "WARNING")
            self._reply(cmd, CommandResult(status="error", error=f"policy connect failed: {exc}", status_code=502))
            return False

    def _apply_prompt(self, prompt) -> None:
        """应用推理文本指令（语言条件策略的配置项）：写入策略客户端运行时 ``prompt``。

        openpi 每次 infer 请求携带该文本（服务端每帧重新 tokenize，可换）；不声明 prompt
        配置项的策略（如 act）无 ``prompt`` 属性，此处 no-op。prompt 由 ``infer prompt <text>``
        会话内预置（不随 rollout 命令传）；需要 prompt 的策略在推理 / 录制开始前必须非空。
        ``prompt`` 非 None 即设置（空文本已在调用方校验）。
        """
        if prompt is None:
            return
        policy = getattr(self, "policy", None)
        if policy is not None and hasattr(policy, "prompt"):
            policy.prompt = str(prompt)
            debug_print(self.name, f"Policy prompt set: {prompt!r}", "INFO")

    def _on_policy_config(self, cmd):
        """策略配置命令族：``infer config`` / ``infer config set <json>`` / ``infer prompt`` /
        ``infer model(set)``（**公共项 host / port + 每个策略自己的配置项**，见 ``policy.POLICY_CONFIG_ITEMS``）。

        先经 ``handle_policy_config`` 按**当前策略的配置项 schema** 校验并持久化到内存态
        ``base_cfg["policy"]``（下次会话生效）；设置类命令再应用到运行中的策略客户端（下一请求生效）。
        参数缺失 / 非法键 / 类型不符 → rejected（400，不崩溃）。

        **策略已连接后锁定的项**（``locked_when_connected``，如端点 host / port）：连接目标不能热改
        （否则与实际连接不一致）→ rejected（409）。**未连接时与其它配置项同层级**：可改，
        并同步到运行中的策略客户端（下次 ``infer connect`` 用新端点）。
        """
        if cmd.name in (CMD_INFER_CONFIG_SET, CMD_INFER_MODEL_SET):  # 仅设置类命令需门控
            locked = self._connect_locked_config_keys(cmd)
            if locked:
                return CommandResult(
                    status="rejected",
                    error=(
                        f"策略已连接，端点已锁定：{locked}（连接目标不能热改：请先退出推理会话 / 断开连接后再修改）"
                    ),
                    status_code=409,
                )
        result = handle_policy_config(self.base_cfg, cmd, policy_type=self.policy_type)
        if result.status != "ok":
            return result
        written = result.data.get("written") or {}
        if written:
            self._apply_policy_config(written)
        if cmd.name == CMD_INFER_PROMPT:  # 保持既有回执形状（prompt=...）
            return ok_result(state=getattr(self, "state", "ready"), prompt=written.get("prompt"))
        return ok_result(state=getattr(self, "state", "ready"), **result.data)

    def _connect_locked_config_keys(self, cmd) -> list[str]:
        """本次设置里属于「策略已连接后禁改」的配置键（``locked_when_connected``，如端点）。

        未连接 → 返回空列表（与其它配置项一样可改）；只统计**本策略 schema 内**的键，
        不在 schema 内的键不算锁定（留给主处理回执「unknown config key」）。
        """
        if not self.connected:
            return []
        if cmd.name == CMD_INFER_MODEL_SET:
            params = {"pretrained_name_or_path": cmd.params.get("path")}
        else:
            try:
                params = parse_meta(cmd.params.get("json"), what="infer config set")
            except ValueError:
                return []
        policy_type = self._effective_policy_type()
        locked_keys = policy_config_connect_locked_keys(policy_type)
        return sorted(key for key in params if key in locked_keys)

    def _effective_policy_type(self) -> str:
        """本会话实际使用的策略类型（显式选择优先，否则配置 ``policy.type``；非法 → 空串）。"""
        try:
            return validate_policy_type(self.policy_type or self.policy_config.get("type", "openpi"))
        except ValueError:
            return ""

    def _apply_policy_config(self, written: dict) -> None:
        """把设置项应用到运行中的策略客户端（下一请求生效）。

        ``host`` / ``port`` → ``policy.set_endpoint``（重建传输层连接目标，下次 connect 生效）；
        ``prompt`` → 策略 prompt（语言条件策略每次请求携带）；其余键 → ``policy.policy_config``
        （策略自读，如 act 的 ``pretrained_name_or_path`` 在首次下发策略指令时读取）。
        """
        endpoint = {key: written[key] for key in ("host", "port") if key in written}
        if endpoint:
            set_endpoint = getattr(getattr(self, "policy", None), "set_endpoint", None)
            if callable(set_endpoint):
                set_endpoint(host=endpoint.get("host"), port=endpoint.get("port"))
        if written.get("prompt") is not None:
            self._apply_prompt(written["prompt"])
        policy_config = getattr(getattr(self, "policy", None), "policy_config", None)
        if isinstance(policy_config, dict):
            for key, value in written.items():
                if key not in ("prompt", "host", "port"):
                    policy_config[key] = value

    def policy_config_status(self) -> dict:
        """策略配置项状态（schema + 当前值 + 缺失必填项；server ``/v1/infers`` 上报 / 前端表单）。"""
        return policy_config_status(self.base_cfg, policy_type=self.policy_type)

    def _set_prompt_cmd(self, cmd) -> None:
        """``infer prompt <text>``：会话内预置推理文本指令（推理 / 录制前必须非空）。

        主循环（等待命令）与持续推理循环均可处理；缺文本 → rejected（不崩溃）。
        仅对**声明了 prompt 配置项**的策略（语言条件，如 openpi）可用。
        """
        text = cmd.params.get("prompt")
        if text is None or not str(text).strip():
            self._reply(cmd, CommandResult(status="rejected", error="infer prompt requires <text>", status_code=400))
            return
        self._reply(cmd, self._on_policy_config(cmd))

    def _bind_policy_adapter(self):
        """把 adapter 运行时启用的布局（qpos 维数 + 相机名）传给策略客户端。

        布局单一事实来源 = adapter 配置（``adapter config set`` 的 enabled_arms /
        enabled_cameras）：openpi 客户端据此过滤要下发的相机（**不另读 edge.yml 的相机名**）；
        策略无 ``bind_adapter``（如 act / 测试替身）→ no-op。adapter 未提供相机名 /
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
        """阻塞式推理主循环：等待就绪 → 显式 infer connect → 等待 infer rollout 步进闭环。"""
        # 复位（reset() 非阻塞设 home 目标；RTC 块队列 / 步号归零）
        self.adapter.reset()
        self.policy.reset()
        self.rtc.reset()
        # 等待机器人就绪（期间可 session quit 退出 / robot estop 急停 / robot reset 复位）
        result = self._wait_ready(CMD_SESSION_QUIT)
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
            if name == CMD_INFER_CONNECT:  # 显式连接推理节点（单次尝试，可反复触发重连）
                self._connect_policy(cmd)
            elif name == CMD_INFER_ROLLOUT:  # 推理闭环：单步 / continuous 持续（多步 & drain 已取消）
                try:
                    mode = parse_rollout_mode(cmd.params.get("mode"))
                except ValueError as exc:
                    self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
                    continue
                if not self._require_prompt(cmd):  # 需要 prompt 的策略：为空不能开始推理
                    continue
                if not self._ensure_connected(cmd):  # 惰性自连：未连接则自动连接（失败已回执）
                    continue
                if mode == ROLLOUT_MODE_CONTINUOUS:  # 持续推理：启动即回执，直到 session quit / estop
                    self._reply(cmd, ok_result(state="continuous", started=True))
                    result = self._run_continuous()
                    self.state = SessionState.FINISHED if result == RunResult.FINISHED else SessionState.ERROR
                    return result
                self._run_single(cmd)  # 单步推理（缺省）
            elif name == CMD_CAPTURE_EPISODE_START:  # 推理时 rollout 录制开始（robot 不关心模式）
                if not self._require_prompt(cmd):  # 需要 prompt 的策略：录制 task_name=prompt
                    continue
                self._start_recording(cmd)
            elif name == CMD_CAPTURE_EPISODE_END:  # 推理时 rollout 录制结束
                self._end_recording(cmd)
            elif name == CMD_CAPTURE_SYNC:  # 推理录制同步采集元信息（operator/task_name 等）
                self._sync_capture_meta_cmd(cmd)
            elif name == CMD_SESSION_QUIT:  # 退出推理会话
                self.adapter.reset()  # 推理结束回到 home
                self.state = SessionState.FINISHED
                self._record_exit(cmd)
                debug_print(self.name, "Inference finished, robot reset to home.", "INFO")
                return RunResult.FINISHED
            elif name == CMD_ROBOT_ESTOP:  # 急停：立即安全停止再进 ERROR
                self.safe_stop()
                self.state = SessionState.ERROR
                self._reply(cmd, CommandResult(status="error", error="estop", status_code=500))
                return RunResult.ERROR
            elif name == CMD_ROBOT_RESET:  # 复位（会话期间）
                self.adapter.reset()
                self._reply(cmd, ok_result(state="ready"))
            elif name == CMD_ROBOT_EXECUTE:  # 直接下发 raw 动作（qpos 直接作为参数）
                self._execute_action(cmd)
            elif name == CMD_ROBOT_TELEOP:  # 遥操作开关（true/false 直接作为参数）
                self._set_teleop(cmd)
            elif name in (  # 策略配置项：查询 / 设置（每个策略有独立配置项，见 policy.POLICY_CONFIG_ITEMS）
                CMD_INFER_PROMPT,
                CMD_INFER_CONFIG,
                CMD_INFER_CONFIG_SET,
                CMD_INFER_MODEL,
                CMD_INFER_MODEL_SET,
            ):
                if name == CMD_INFER_PROMPT:  # 文本指令：会话内预置（推理/录制前必须非空）
                    self._set_prompt_cmd(cmd)
                else:
                    self._reply(cmd, self._on_policy_config(cmd))
            elif name in (CMD_INFER_RTC, CMD_INFER_RTC_SET):  # RTC 参数：查询 / 设置（应用到运行中 manager）
                self._reply(cmd, self._on_infer_rtc(cmd))
            elif name in (  # 推理端点：查询可用；**设置随会话锁定**（进入会话前经 enter / 前端设置）
                CMD_INFER_IP,
                CMD_INFER_IP_SET,
                CMD_INFER_PORT,
                CMD_INFER_PORT_SET,
            ):
                if name in (CMD_INFER_IP_SET, CMD_INFER_PORT_SET):
                    self._reply(
                        cmd,
                        CommandResult(
                            status="rejected",
                            error="推理端点已随推理会话锁定：请退出会话后再设置",
                            status_code=409,
                        ),
                    )
                else:
                    self._reply(cmd, self._on_infer_endpoint(cmd))
            elif name in (  # 配置级命令：任务态也可用（capture meta list/add/edit/delete/delete-key）
                CMD_CAPTURE_META_LIST,
                CMD_CAPTURE_META_ADD,
                CMD_CAPTURE_META_EDIT,
                CMD_CAPTURE_META_DELETE,
                CMD_CAPTURE_META_DELETE_KEY,
            ):
                self._reply(cmd, self._on_capture_meta(cmd))
            else:  # 未识别命令（当前任务不适用）统一回执，避免 submit 挂起
                if cmd is not None:
                    self._reply(cmd, CommandResult(status="rejected", error=f"{name} not applicable", status_code=409))
                time.sleep(0.02)  # 无命令时轻量轮询（避免忙等）

    def _run_single(self, cmd) -> None:
        """infer rollout：单步推理闭环（一次 观测 → 推理（RTC 取块）→ 动作下发），回执动作。"""
        obs = self.adapter.observe()  # 推理输入（显示观测由节点级写入 frame_manager）
        if obs is None:
            self._reply(cmd, CommandResult(status="rejected", error="observation not ready", status_code=503))
            return
        action = self.rtc.infer(obs)  # RTC：必要时拉新块（三元切分 + 重叠融合）→ 取本步动作
        if action is not None:
            self.adapter.rollout(action)  # 解析模型 action 为限速目标并推进一帧
        repr_action = self._action_repr(action)
        debug_print(self.name, f"Rollout step executed (action={repr_action}).", "INFO")
        self._reply(
            cmd,
            ok_result(state="ready", count=1, action=repr_action, actions=[repr_action]),
        )

    def _start_recording(self, cmd) -> None:
        """capture episode start：开始一轮推理 rollout 录制（robot 不关心推理/采集）。

        通知机器人进程开启录制（``adapter.start_capture``）；录制期间 robot 按帧录 mcap
        （含 action）。录制元信息（operator=policy、task_name=prompt）由调用方**显式**
        ``capture sync`` 同步（本会话只负责默认上报）。
        """
        self.adapter.start_capture()
        self._recording = True
        debug_print(self.name, "Rollout recording started (capture episode start).", "INFO")
        self._reply(cmd, ok_result(state="recording", episode="start", recording=True))

    def _end_recording(self, cmd) -> None:
        """capture episode end：结束一轮推理 rollout 录制（机器人进程保存 episode）。"""
        self.adapter.end_capture()
        self._recording = False
        debug_print(self.name, "Rollout recording ended (capture episode end).", "INFO")
        self._reply(cmd, ok_result(state="ready", episode="end", recording=False))

    def _sync_capture_meta_cmd(self, cmd) -> None:
        """capture sync --meta <json>：同步采集元信息（operator/task_name 等）到机器人进程。

        推理录制时（rollout episode）由调用方显式同步：operator 暂定 "policy"、task_name =
        prompt（会话默认上报，见 server /v1/infers status 的 capture_meta）。
        """
        try:
            meta = parse_meta(cmd.params.get("meta"))
        except ValueError as exc:
            self._reply(cmd, CommandResult(status="rejected", error=str(exc), status_code=400))
            return
        self.adapter.sync_capture_meta(meta)
        self._reply(cmd, ok_result(state=getattr(self, "state", "ready"), meta=meta))

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
                return CommandResult(status="rejected", error=str(exc), status_code=400)
        return ok_result(state=getattr(self, "state", "ready"), rtc=self.rtc.status())

    def rtc_status(self) -> dict | None:
        """RTC 运行状态（server ``/v1/infers`` 的 ``rtc`` 字段）；无 manager → None。"""
        rtc = getattr(self, "rtc", None)
        return rtc.status() if rtc is not None else None

    def _run_continuous(self) -> RunResult:
        """infer rollout continuous：持续推理，每步轮询命令响应退出 / 复位 / 急停 / 录制。

        启动命令已回执 started（prompt 已在启动前校验非空）；持续直到 session quit
        （FINISHED）/ robot estop（ERROR）/ node 失联（ERROR）。持续期间接受
        ``capture episode start/end``（录制 rollout）与 ``capture sync``（同步元信息）——
        录制与持续推理正交（robot 不关心模式）。返回 RunResult（由调用方置会话状态）。
        """
        debug_print(self.name, "Continuous rollout started (session quit to stop).", "INFO")
        while True:
            if self._stop_requested:  # 外部请求停止（node 失联 ERROR）：立即退出
                return RunResult.ERROR
            cmd = self.command_source()
            name = _cmd_name(cmd)
            if name == CMD_SESSION_QUIT:  # 停止持续推理并退出会话
                self.adapter.reset()  # 推理结束回到 home
                self._record_exit(cmd)
                debug_print(self.name, "Continuous rollout stopped, inference finished.", "INFO")
                return RunResult.FINISHED
            if name == CMD_ROBOT_ESTOP:  # 急停
                self.safe_stop()
                self._reply(cmd, CommandResult(status="error", error="estop", status_code=500))
                return RunResult.ERROR
            if name == CMD_ROBOT_RESET:  # 持续中复位（回执 ok，继续推理）
                self.adapter.reset()
                self._reply(cmd, ok_result(state="continuous"))
                continue
            if name == CMD_INFER_ROLLOUT:  # 持续中重复 rollout：拒绝
                self._reply(
                    cmd,
                    CommandResult(status="rejected", error="continuous rollout already running", status_code=409),
                )
                continue
            if name in (CMD_INFER_PROMPT, CMD_INFER_CONFIG, CMD_INFER_CONFIG_SET, CMD_INFER_MODEL, CMD_INFER_MODEL_SET):
                # 持续推理中动态改策略配置项（prompt / model path 等，下个推理请求生效）
                if name == CMD_INFER_PROMPT:
                    self._set_prompt_cmd(cmd)
                else:
                    self._reply(cmd, self._on_policy_config(cmd))
                continue
            if name in (CMD_INFER_RTC, CMD_INFER_RTC_SET):  # 持续中查询 / 设置 RTC 参数
                self._reply(cmd, self._on_infer_rtc(cmd))
                continue
            if name == CMD_CAPTURE_EPISODE_START:  # 持续中开始 rollout 录制（prompt 已非空）
                self._start_recording(cmd)
                continue
            if name == CMD_CAPTURE_EPISODE_END:  # 持续中结束 rollout 录制
                self._end_recording(cmd)
                continue
            if name == CMD_CAPTURE_SYNC:  # 持续中同步采集元信息（录制 rollout 附加）
                self._sync_capture_meta_cmd(cmd)
                continue
            if name in (CMD_INFER_IP_SET, CMD_INFER_PORT_SET):  # 端点已随会话锁定
                self._reply(
                    cmd,
                    CommandResult(status="rejected", error="推理端点已随推理会话锁定", status_code=409),
                )
                continue
            if cmd is not None:  # 持续中其它命令：拒绝（避免 submit 挂起）
                self._reply(
                    cmd,
                    CommandResult(
                        status="rejected",
                        error=f"{name} not applicable during continuous rollout",
                        status_code=409,
                    ),
                )
            obs = self.adapter.observe()
            if obs is None:  # 观测未就绪：按步进间隔轮询
                time.sleep(self.step_interval)
                continue
            action = self.rtc.infer(obs)  # RTC：必要时拉新块（三元切分 + 重叠融合）→ 本步动作
            if action is not None:
                self.adapter.rollout(action)
            time.sleep(self.step_interval)  # 按 infer_freq 控制步进节奏

    @staticmethod
    def _action_repr(action):
        """动作 → JSON 可表达（list）；None 保持 None。"""
        if action is None:
            return None
        return np.asarray(action).reshape(-1).tolist()
