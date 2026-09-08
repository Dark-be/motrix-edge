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

推理会话（InferSession）**无回合概念**：enter 进入后持续推理，直到 exit 退出。
与采集（CaptureService）共用同一命令总线与租约语义：
  - ``enter`` → submit ``session run infer``（选择 + 启动推理会话一步完成）；
  - ``exit``  → submit ``session quit``（结束推理，节点回 READY）；
  - ``rollout`` → submit ``infer rollout``（单步闭环）；
  - ``status`` 只读 node（node_state / adapter / policy），不另起会话 run。

受控操作（enter / exit）须持有 Edge 级活跃租约（``X-Lease-Id``，经 ``LeaseManager`` 校验）。
"""

from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.node import NodeState
from motrix_edge.session.base import SessionState
from motrix_edge.utils.commands import (
    CMD_INFER_CONNECT,
    CMD_INFER_ROLLOUT,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    ROLLOUT_MODE_CONTINUOUS,
    ROLLOUT_MODE_DRAIN,
    Command,
    CommandBus,
    CommandResult,
    get_policy_endpoint,
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
        connected / metadata / 端点 / 租约。"""
        node = self._node
        session = self._session()
        connected = bool(getattr(session, "connected", False)) if session is not None else False
        return {
            "node_state": getattr(node, "state", None) if node is not None else None,
            "session_type": getattr(node, "session_type", None) if node is not None else None,
            "state": getattr(session, "state", SessionState.INIT) if session is not None else SessionState.INIT,
            "adapter": self._adapter_state(),
            "policy": self._policy_ref(),
            # 策略服务器连接状态；metadata 仅在已连接时暴露（连接成功后才有服务端元信息）
            "connected": connected,
            "metadata": (
                dict(getattr(getattr(session, "policy", None), "server_metadata", None) or {}) if connected else None
            ),
            "endpoint": self._policy_endpoint(),  # 当前配置的推理节点 host / port（前端推理卡片设置）
            "lease_id": self._leases.status()["lease_id"],
        }

    def enter(self, lease_id: str | None = None, policy_type: str | None = None) -> dict:
        """进入推理会话（READY → ACTIVE）：session run infer 一步完成选择 + 启动。

        policy_type：可选策略类型（缺省用配置 policy.type）；随命令下发。
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
        params: dict = {"session": "infer"}
        if policy_type:
            params["policy_type"] = policy_type
        result = self._submit(Command(CMD_SESSION_RUN, params=params, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "lease_id": self._leases.status()["lease_id"],  # 当前租约（回显）
            "adapter": self._adapter_ref(),  # 当前节点 active adapter 身份
            "policy": policy_type,  # 回显本次选用的策略类型（None = 配置默认）
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
        """单次尝试连接推理节点（infer connect）：提交命令，回执含服务端 metadata。

        须已在推理会话（ACTIVE）且持有活跃租约；连接失败 → 回执 error（502）透传，
        连接状态保持未连接（前端可再次触发重连）。
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
            "connected": bool(result.data.get("connected", False)),
            "metadata": result.data.get("metadata"),
        }

    def rollout(
        self,
        lease_id: str | None = None,
        mode: str | None = None,
        count: int | None = None,
    ) -> dict:
        """推理闭环（infer rollout [count] / continuous / drain），返回最后 action 与 actions 列表。

        - mode 缺省 / "count"：连续推理 count 次（缺省 1，1–100），回执 count / action / actions；
        - mode="continuous"：持续推理（启动即回执 started，直到 session quit / estop）；
        - mode="drain"：只消耗当前缓存动作块（不发新推理请求），回执消耗步数。

        须已在推理会话（ACTIVE）且持有活跃租约；未在会话 → 409；未连接推理节点 → 503。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise InferError("not in a task session")
        params: dict = {}
        if mode in (ROLLOUT_MODE_CONTINUOUS, ROLLOUT_MODE_DRAIN):
            params["count"] = mode  # 命令层 parse_rollout_mode 按字符串识别 continuous / drain
        elif count is not None:
            params["count"] = count
        result = self._submit(Command(CMD_INFER_ROLLOUT, params=params, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "state": result.data.get("state"),
            "count": result.data.get("count"),
            "action": result.data.get("action"),
            "actions": result.data.get("actions"),
        }

    # -- 内部 ---------------------------------------------------------------
    def _adapter_state(self) -> dict:
        """当前节点 active adapter 状态（身份 + 心跳缓存）。"""
        node = self._node
        adapter = getattr(node, "adapter", None)
        return {
            "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
            "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
            "running": getattr(adapter, "running", None) if adapter is not None else None,
        }

    def _adapter_ref(self) -> dict:
        """当前节点 active adapter 身份（name / type）。"""
        node = self._node
        adapter = getattr(node, "adapter", None)
        return {
            "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
            "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
        }

    def _policy_ref(self):
        """当前推理会话的策略客户端标识（无会话为 None）。"""
        session = self._session()
        if session is None:
            return None
        return getattr(getattr(session, "policy", None), "name", None)

    def _policy_endpoint(self) -> dict:
        """当前配置的推理节点端点（``base_cfg["policy"]`` 的默认 host / port）。"""
        if self._node is None:
            return {"host": None, "port": None}
        return get_policy_endpoint(self._node.base_cfg)

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
