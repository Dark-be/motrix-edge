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

"""InferService —— /v1/infers/* 控制器：桥接 HTTP 请求到「正在运行的 EdgeNode」。

推理会话（InferSession）**无「多步推理」模式**：单步 / 持续推理由 ``infer rollout`` 驱动，
推理时 **rollout 录制** = 经 ``capture episode start/end`` 控制一轮 episode（robot 不关心
推理/采集）。与采集（CaptureService）共用同一命令总线与租约语义：
  - ``enter`` → submit ``session run infer``（选择 + 启动推理会话一步完成）；
  - ``exit``  → submit ``session quit``（结束推理，节点回 READY）；
  - ``rollout`` → submit ``infer rollout``（单步 / continuous 持续）；
  - ``episode_start`` / ``episode_end`` → submit ``capture episode start/end``（rollout 录制）；
  - ``sync`` → submit ``capture sync``（录制时同步采集元信息 operator/task_name）；
  - ``configure_rtc`` → submit ``infer rtc set``（运行期改 RTC 参数，见 wiki/design/motrix_edge_rtc.md）；
  - ``status`` 只读 node（node_state / adapter / policy / prompt / recording / rtc），不另起会话 run。

受控操作（enter / exit / rollout / episode / sync / rtc）须持有 Edge 级活跃租约（``X-Lease-Id``，
经 ``LeaseManager`` 校验）。**prompt 仅对需要它的策略（语言条件，如 openpi）必需**：该类策略
推理 / 录制开始前必须已 ``infer prompt`` 预置非空文本（prompt_required=True）；lerobot-act 不需要 prompt。
录制 rollout 的默认 task_name = prompt（由调用方组装后经 ``/v1/infers/sync`` 同步）。
"""

import json

from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.node import NodeState
from motrix_edge.server.state import adapter_ref, adapter_state, capture_status
from motrix_edge.session.base import SessionState
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_CONNECT,
    CMD_INFER_PROMPT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_ROLLOUT_STOP,
    CMD_INFER_RTC_SET,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    ROLLOUT_MODE_CONTINUOUS,
    ROLLOUT_MODE_SINGLE,
    Command,
    CommandBus,
    CommandResult,
    policy_config_status,
    set_policy_config,
)


class InferError(Exception):
    """infer 操作被拒绝（非法状态转移 / 会话未运行）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class InferService:
    """HTTP 推理请求 → 命令总线 + 节点状态的桥接控制器（单活跃推理会话）。"""

    def __init__(self, node, bus: CommandBus, leases: LeaseManager | None = None):
        self._node = node  # 正在运行的 EdgeNode（由 node 程序主线程持有）
        self._bus = bus  # 共享命令总线：web / CLI 线程 push，EdgeNode 主循环 poll
        self._leases = leases or LeaseManager()  # Edge 级租约（受控操作校验用）

    def status(self) -> dict:
        """状态快照（只读）：node_state / 会话类型 / session state / adapter / policy /
        connected / metadata / 端点 / prompt / 录制 / 采集状态 / 租约。

        ``prompt`` = 当前推理文本指令（**仅需要 prompt 的策略**：推理 / 录制前必须非空，
        ``prompt_required=True`` 由策略声明，如 openpi；lerobot-act 不需要 prompt）；``recording`` = 推理
        会话当前是否开启 rollout 录制；``continuous`` = 持续推理是否正在运行（据此门控
        「持续推理 / 停止推理」）；``capture_status`` = 机器人进程实际采集状态缓存（running +
        ``meta`` 元信息全集，分类可拓展，与 ``/v1/captures`` 同构）。
        """
        node = self._node
        session = self._session()
        connected = bool(getattr(session, "connected", False)) if session is not None else False
        recording = bool(getattr(session, "recording", False)) if session is not None else False
        prompt = getattr(session, "prompt", None) if session is not None else None
        return {
            "node_state": getattr(node, "state", None) if node is not None else None,
            "session_type": getattr(node, "session_type", None) if node is not None else None,
            "state": getattr(session, "state", SessionState.INIT) if session is not None else SessionState.INIT,
            "adapter": adapter_state(self._node),
            "policy": self._policy_ref(),
            # 策略服务器连接状态；metadata 仅在已连接时暴露（连接成功后才有服务端元信息）
            "connected": connected,
            "metadata": (
                dict(getattr(getattr(session, "policy", None), "server_metadata", None) or {}) if connected else None
            ),
            # 推理文本指令（需要 prompt 的策略：openpi；lerobot-act 不需要 → None）
            "prompt": prompt,
            # 该策略是否需要 prompt（openpi=True 门控；lerobot-act=False 不参与门控）
            "prompt_required": bool(getattr(session, "prompt_required", False)) if session is not None else False,
            # 是否已预热（infer connect：连接 + prepare + 取一块丢弃，**不下发动作**）：
            # warmup_required=true（缺省）时它是 infer rollout 的硬前置
            "warmed_up": bool(getattr(session, "warmed_up", False)) if session is not None else False,
            # 预热工作线程是否在进行中（几分钟级：加载 checkpoint）；未完成原因见 warmup_error
            "warming": bool(getattr(session, "warming", False)) if session is not None else False,
            "warmup_error": getattr(session, "warmup_error", None) if session is not None else None,
            # 因回执超时被丢弃的动作数（未下发到真机；诊断「调用方超时」时看）
            "dropped_actions": int(getattr(session, "dropped_actions", 0)) if session is not None else 0,
            # 推理会话当前是否开启 rollout 录制（capture episode start 后为 True）
            "recording": recording,
            # 持续推理是否正在运行（infer rollout continuous ↔ infer rollout stop）
            "continuous": bool(getattr(session, "continuous", False)) if session is not None else False,
            # 机器人进程实际采集状态缓存（node 周期刷新；录制时 running=True + 已同步元信息）
            "capture_status": capture_status(self._node),
            # RTC（实时动作块）运行状态：enabled / params / index / remaining / inflight /
            # fetches / stale_chunks / failed_chunks / last_chunk / last_delay / last_error
            "rtc": self._rtc_status(),
            # 策略配置项（**每个策略有自己的独立配置项**：prompt / 模型路径 / 设备 / 块长…）：
            # items = schema 项 + 当前值，missing = 缺失必填项（前端据此动态渲染表单并门控按钮）
            "policy_config": self._policy_config_status(),
            "lease_id": self._leases.status()["lease_id"],
        }

    def enter(
        self,
        lease_id: str | None = None,
        policy_type: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """进入推理会话（READY → ACTIVE）：session run infer 一步完成选择 + 启动。

        policy_type：可选策略类型（缺省用配置 policy.type）；随命令下发。
        config：可选**策略配置项**（每个策略独立，见 ``policy.POLICY_CONFIG_ITEMS``）：
        公共项 ``host`` / ``port``（推理节点端点）+ 策略自身项（openpi → prompt；lerobot-act →
        pretrained_name_or_path / device / actions_per_chunk…）。在**进入会话前**写入
        ``base_cfg["policy"]``（内存态），创建会话 / 策略客户端时读取生效；非法键、类型不符、
        越界或必填项为空 → 400（校验按所选策略的 schema）。

        端点与其它配置项**同一语义**（``runtime=False``）：进入会话时固化，会话内改只写内存态
        配置、下次会话生效（见 ``wiki/design/motrix_edge_policy.md``）。
        命令化：submit session run infer（选择 + 启动一步完成，等「任务已启动」回执），
        无需轮询节点状态。须持有 Edge 级活跃租约；节点未就绪 / 已在会话中 / 节点
        ERROR → 409。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.state == NodeState.ERROR:
            raise InferError("node in error state")
        if node.state != NodeState.READY or node.session is not None:
            raise InferError("node not ready (adapter not bound) or already in a task session")
        # 进入会话前应用策略配置项（会话创建时读取，如 lerobot-act 的模型路径 / openpi 的 prompt）
        if config:
            target = policy_type or getattr(node, "policy_type", None) or node.base_cfg.get("policy", {}).get("type")
            try:
                set_policy_config(node.base_cfg, target, dict(config))
            except ValueError as exc:
                raise InferError(str(exc), status_code=400) from exc
        params: dict = {"session": "infer"}
        if policy_type:
            params["policy_type"] = policy_type
        result = self._submit(Command(CMD_SESSION_RUN, params=params, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "lease_id": self._leases.status()["lease_id"],  # 当前租约（回显）
            "adapter": adapter_ref(self._node),  # 当前节点 active adapter 身份
            "policy": policy_type,  # 回显本次选用的策略类型（None = 配置默认）
            "policy_config": self._policy_config_status(policy_type),  # 生效后的配置项状态
        }

    def exit(self, lease_id: str | None = None) -> dict:
        """退出推理会话（ACTIVE → READY）：submit session quit，等节点补发「node ready」回执。"""
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(Command(CMD_SESSION_QUIT, meta={"lease_id": lease_id}), timeout=10.0)
        self._raise_on_rejected(result)
        return {"status": "accepted"}

    def connect(self, lease_id: str | None = None) -> dict:
        """启动 / 查询**异步预热**（``infer connect``：连接 + prepare + 取一块丢弃，不下发动作）。

        **立即回执**，不等预热跑完：加载 checkpoint / 首帧推理可能持续几分钟，阻塞调用方还
        不如让它轮询 `GET /v1/infers` 的 `warming` / `warmed_up` / `warmup_error`。重复调用
        **幂等**（回执即当前状态，`started` 区分本次是否新启动了预热）。

        预热期间：`POST /v1/commands` 的急停（命令旁路，任何状态即时生效）与 `DELETE /v1/infers`
        （会话内 `session quit`）都能立即中断它（取消 + 关传输打断在飞调用）。
        ``warmed_up=true`` 后 ``infer rollout`` 才短超时安全（见配置项 ``warmup_required``）。

        回执里的 ``metadata`` 来自已建立的连接：本次才启动预热时通常还是 None（连上后从 status 看）。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(Command(CMD_INFER_CONNECT, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "state": result.data.get("state"),
            "started": bool(result.data.get("started", False)),
            "connected": bool(result.data.get("connected", False)),
            "warming": bool(result.data.get("warming", False)),
            "warmed_up": bool(result.data.get("warmed_up", False)),
            "warmup_error": result.data.get("warmup_error"),
            "chunk_len": result.data.get("chunk_len"),
            "metadata": result.data.get("metadata"),
        }

    def rollout(
        self,
        lease_id: str | None = None,
        mode: str | None = None,
    ) -> dict:
        """推理闭环（infer rollout）：单步（缺省）或 continuous 持续推理。

        - mode 缺省 / "single"：单步推理（一次 观测 → 推理 → 动作下发），回执 count=1 /
          action / actions；
        - mode="continuous"：持续推理（启动即回执 started，直到 ``infer rollout stop`` /
          session quit / estop；期间可 ``capture episode start/end`` 录制 rollout）。

        **多步与 drain（缓存推理）模式已取消**（改用单步 / 持续 + 录制）；prompt 不随
        rollout 传：由会话内 ``infer prompt`` 预置，会话侧门控（为空不能开始推理）。
        须已在推理会话（ACTIVE）且持有活跃租约；未在会话 → 409。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        mode = mode or ROLLOUT_MODE_SINGLE
        if mode not in (ROLLOUT_MODE_SINGLE, ROLLOUT_MODE_CONTINUOUS):
            raise InferError(f"unsupported rollout mode: {mode} (single / continuous)", status_code=400)
        params: dict = {} if mode == ROLLOUT_MODE_SINGLE else {"mode": mode}
        result = self._submit(Command(CMD_INFER_ROLLOUT, params=params, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "state": result.data.get("state"),
            "count": result.data.get("count"),
            "action": result.data.get("action"),
            "actions": result.data.get("actions"),
        }

    def rollout_stop(self, lease_id: str | None = None) -> dict:
        """停止持续推理（``infer rollout stop``）：回到会话 READY，会话与策略连接保持。

        与 ``session quit`` 的区别：**不退出会话**（不释放策略连接 / 不复位机器人），只是
        结束持续推理循环，之后仍可再次 ``infer rollout`` / 改配置。须已在推理会话
        （ACTIVE）且持有活跃租约；未在持续推理中 → 409。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(Command(CMD_INFER_ROLLOUT_STOP, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {"status": "accepted", "state": result.data.get("state"), "continuous": False}

    def episode_start(self, lease_id: str | None = None) -> dict:
        """开始一轮推理 rollout 录制（capture episode start）：robot 开始录 mcap（含 action）。

        录制 rollout 需要 task_name=prompt → 会话侧门控（prompt 为空 → rejected 400，
        前端应在开始录制前 ``infer prompt`` 预置）。受控操作：须持有活跃租约。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(Command(CMD_CAPTURE_EPISODE_START, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "episode": result.data.get("episode"),
            "recording": bool(result.data.get("recording", False)),
        }

    def episode_end(self, lease_id: str | None = None) -> dict:
        """结束一轮推理 rollout 录制（capture episode end）：robot 保存该 episode。"""
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(Command(CMD_CAPTURE_EPISODE_END, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "episode": result.data.get("episode"),
            "recording": bool(result.data.get("recording", False)),
        }

    def sync(self, meta: dict, lease_id: str | None = None) -> dict:
        """同步采集元信息（capture sync）：推理录制 rollout 时把 operator/task_name 同步到进程。

        录制 rollout 的默认元信息 = ``{operator: "policy", task_name: <prompt>}``，由调用方
        自行组装并显式提交本端点（Edge **不自动 sync**、也不在状态里代报默认值）。"""
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(
            Command(CMD_CAPTURE_SYNC, params={"meta": json.dumps(meta or {})}, meta={"lease_id": lease_id})
        )
        self._raise_on_rejected(result)
        return {"status": "accepted", "meta": result.data.get("meta")}

    def configure_rtc(self, params: dict, lease_id: str | None = None) -> dict:
        """运行期设置 RTC 参数（``infer rtc set``）：写内存态 + 应用到正在运行的 RTCManager。

        body 为 RTC 参数对象（可部分：enabled / action_horizon / execution_horizon /
        suffix_len / prefix_len / aggregate_fn，查看 ``rtc.TRANSITION_FUNCTIONS`` 取全部过渡策略名）；
        非法参数或交叉约束不满足（P+E+S<=H、E>P）→ 400。须已在推理会话
        （ACTIVE）且持有活跃租约（与其它受控操作一致）。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(
            Command(CMD_INFER_RTC_SET, params={"json": json.dumps(params or {})}, meta={"lease_id": lease_id})
        )
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "rtc": result.data.get("rtc"),
        }

    def configure_policy_config(self, params: dict, lease_id: str | None = None) -> dict:
        """运行期设置**策略配置项**（``infer config set``）：写内存态 + 应用到运行中的策略客户端。

        body 为策略配置项对象（按当前策略 schema 白名单校验，可部分：openpi → prompt；
        lerobot-act → pretrained_name_or_path / device / actions_per_chunk）；未知键 / 类型不符 /
        必填为空 → 400。须已在推理会话（ACTIVE）且持有活跃租约（与 RTC 等受控操作一致）。

        **只有 ``runtime: True`` 的键下一推理请求生效**（openpi 的 prompt / image_size）；
        ``runtime: False`` 的键（host / port、模型路径 / device / 块长）是会话级配置，会话内改
        只写内存态、**退出会话重进才生效**——未即时生效的键由回执 ``deferred`` 列出，前端据此
        提示（不报错、也不回 409）。缺失必填项（prompt / 模型路径）时前端应门控推理按钮。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        result = self._submit(
            Command(CMD_INFER_CONFIG_SET, params={"json": json.dumps(params or {})}, meta={"lease_id": lease_id})
        )
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "policy_config": result.data.get("policy_config") or self._policy_config_status(),
            "deferred": list(result.data.get("deferred") or []),  # 未即时生效（退出会话重进才生效）的键
        }

    def set_prompt(self, lease_id: str | None = None, prompt: str | None = None) -> dict:
        """运行时更新推理文本指令（openpi 动态 prompt；会话内生效，下个推理请求携带）。

        须已在推理会话（ACTIVE）且持有活跃租约；未在会话 → 409；缺 prompt → 400。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        if prompt is None or not str(prompt).strip():
            raise InferError("prompt required", status_code=400)
        prompt = str(prompt)
        result = self._submit(Command(CMD_INFER_PROMPT, params={"prompt": prompt}, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {"status": "accepted", "prompt": prompt}

    # -- 内部 ---------------------------------------------------------------

    def _policy_config_status(self, policy_type: str | None = None) -> dict:
        """策略配置项状态（schema + 当前值 + 缺失必填项）。

        会话内优先取会话快照（含**已应用**的运行值）；无会话（进入会话前）则由
        ``base_cfg["policy"]`` 直接计算——前端据此在选择策略后即渲染配置表单。
        """
        if policy_type is None:
            session = self._session()
            session_status = getattr(session, "policy_config_status", None)
            if callable(session_status):
                return session_status()
        base_cfg = getattr(self._node, "base_cfg", None) if self._node is not None else None
        if base_cfg is None:
            return {}
        try:
            return policy_config_status(base_cfg, policy_type=policy_type)
        except ValueError:  # 未知策略类型（配置异常）：不阻断状态查询
            return {}

    def _rtc_status(self) -> dict | None:
        """RTC（实时动作块）运行状态（读会话的 RTCManager；无会话 → None）。

        含 enabled / params / index / remaining / inflight / fetches / stale_chunks /
        failed_chunks / last_chunk（最近一块的切分步数）/ last_delay / last_error，供前端与运维
        展示（见 wiki/design/motrix_edge_rtc.md）。
        """
        session = self._session()
        rtc_status = getattr(session, "rtc_status", None)
        if not callable(rtc_status):
            return None
        return rtc_status()

    def _policy_ref(self):
        """当前推理会话的策略客户端标识（无会话为 None）。"""
        session = self._session()
        if session is None:
            return None
        return getattr(getattr(session, "policy", None), "name", None)

    def _session(self):
        """当前会话（可能为 None）。"""
        return getattr(self._node, "session", None) if self._node is not None else None

    def _submit(self, cmd: Command, timeout: float = 5.0) -> CommandResult:
        """提交命令并同步等待回执（HTTP 动作 → 命令 → 回执，无需轮询节点状态）。"""
        return self._bus.submit(cmd, timeout=timeout)

    def _raise_on_rejected(self, result: CommandResult) -> None:
        """命令被拒绝 / 失败 → 转 HTTP 错误（默认 409）。"""
        if result.status != "ok":
            code = result.status_code or 409
            raise InferError(result.error or "command rejected", status_code=code)

    def _ensure_node(self):
        if self._node is None:
            raise InferError("node not initialized")

    def _ensure_lease(self, lease_id: str | None):
        """校验控制动作的租约（经 LeaseManager）：缺失 409 / 不匹配 403 / 过期 410。"""
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise InferError(str(exc), status_code=exc.status_code) from exc
