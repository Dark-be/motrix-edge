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

"""CaptureService —— /v1/captures/* 控制器：桥接 HTTP 请求到「正在运行的 EdgeNode」。

server 层不持有 / 不创建 / 不运行 EdgeNode：node 由 node 程序主线程持续运行
（web 只是 node 进程内的一条独立线程）。CaptureService 绑定「正在运行的 node 实例
+ 共享 CommandBus」：
  - HTTP 动作经 CommandBus.submit 提交命令并同步等待回执（session run capture /
    session quit），由 EdgeNode 主循环（含其采集会话）消费；无需轮询节点状态伪造同步；
  - 状态 / 预检一律读 node（node.state / node.session.*），不另起会话 run。

采集会话为**观测会话**（无回合流程控制）：enter 后以 30 Hz 读共享内存观测缓存
（供 preview / WebRTC），session quit 退出。语义唯一实现在 CaptureSession，此处
不复制。
"""

import json
import shutil

import numpy as np

from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_QPOS
from motrix_edge.lease import LeaseError, LeaseManager
from motrix_edge.node import NodeState
from motrix_edge.session.base import SessionState
from motrix_edge.utils.capture_meta import CaptureMetaStore
from motrix_edge.utils.commands import (
    CMD_CAPTURE_SYNC,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    Command,
    CommandBus,
    CommandResult,
)


class CaptureError(Exception):
    """captures 操作被拒绝（非法状态转移 / 会话未运行）。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class CaptureService:
    """HTTP captures 请求 → 命令总线 + 节点状态的桥接控制器（单活跃采集会话）。

    **不持有 / 不运行 EdgeNode**：节点由 node 程序主线程持续运行，此处只绑定
    「正在运行的 node 实例 + 共享 CommandBus」。HTTP 动作经 submit 提交命令并同步
    等待回执（无需轮询节点状态）；由 EdgeNode 主循环（含其采集会话 CaptureSession）
    消费；状态 / 预检一律读 node。

    租约（独立于任务，见 /v1/leases/*）：受控操作（enter / exit）须持有 Edge 级
    活跃租约（``X-Lease-Id``，经 ``LeaseManager`` 校验）；缺失 / 不匹配 / 过期被拒绝。

    采集会话生命周期（会话 = 采集会话；enter 进入，exit 退出；无回合流程控制）：
      enter   → session run capture：
                READY → ACTIVE（选择并启动采集会话一步完成，持续观测 session READY）
      exit    → session quit：ACTIVE → READY（退出会话，adapter 保留）

    受控动作须携带 ``X-Lease-Id``（与 Console 签发的租约镜像一致，见
    /v1/leases）；**异租约 / 缺失租约一律拒绝**（403），无活跃租约就控制 → 409。
    """

    def __init__(self, node, bus: CommandBus, leases: LeaseManager | None = None, capture_meta_store=None):
        self._node = node  # 正在运行的 EdgeNode（由 node 程序主线程持有）
        self._bus = bus  # 共享命令总线：web / CLI 线程 push，EdgeNode 主循环 poll
        self._leases = leases or LeaseManager()  # Edge 级租约（独立于任务，受控操作校验用）
        # 采集元信息选项存储（config/capture.yml）：前端选择列表 / capture meta 查看；
        # 缺省用默认路径，测试可注入临时 store。
        self._meta_store = capture_meta_store if capture_meta_store is not None else CaptureMetaStore()

    # -- 动作翻译（HTTP → 信号）---------------------------------------------
    def precheck(self) -> dict:
        """预检（只读）：节点运行中 + 采集会话 + 机器人就绪 + 磁盘。"""
        errors: list[str] = []
        if self._node is None:
            errors.append("node not running")

        node = self._node
        node_state = getattr(node, "state", None) if node is not None else None
        session = self._session()
        session_state = getattr(session, "state", SessionState.INIT) if session is not None else SessionState.INIT
        if session is None:
            errors.append("collect session not active")

        robot_ready = False
        adapter = self._adapter()
        if adapter is not None:
            try:
                robot_ready = bool(adapter.ready)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"robot not ready: {exc}")

        save_dir = self._save_dir()
        disk = {}
        if save_dir is not None:
            try:
                usage = shutil.disk_usage(save_dir)
                disk = {"total": usage.total, "used": usage.used, "free": usage.free}
            except OSError as exc:
                errors.append(f"disk unavailable: {exc}")

        lease_info = self._leases.status()
        return {
            "ok": (
                not errors
                and robot_ready
                and node_state not in (None, NodeState.ERROR)
                and session_state != SessionState.ERROR
            ),
            "node_state": node_state,
            "state": session_state,
            "robot_ready": robot_ready,
            "disk": disk,
            "errors": errors,
            "lease_id": lease_info["lease_id"],  # 当前活跃租约（无租约为 None）
            "leasable": lease_info["leasable"] and node_state != NodeState.ERROR,  # 可激活租约（无活跃且节点正常）
        }

    def enter(self, lease_id: str | None = None) -> dict:
        """进入采集会话（READY → ACTIVE）：session run capture 一步完成选择 + 启动。

        单 adapter 包：无 adapter 选择，采集 / 推理都基于节点 discover 绑定的唯一
        adapter。须已持有 Edge 级活跃租约（``X-Lease-Id``，见 /v1/leases）；
        节点未就绪 / 已在会话中 / 节点 ERROR → 409。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.state == NodeState.ERROR:
            raise CaptureError("node in error state")
        if node.state != NodeState.READY or node.session is not None:
            raise CaptureError("node not ready (adapter not bound) or already in a task session")
        # 命令化：session run capture（选择 + 启动一步完成，submit 等「会话已启动」回执）
        result = self._submit(Command(CMD_SESSION_RUN, params={"session": "capture"}, meta={"lease_id": lease_id}))
        self._raise_on_rejected(result)
        return {
            "status": "accepted",
            "state": self._session_state(),
            "lease_id": self._leases.status()["lease_id"],  # 当前租约（回显）
            "adapter": self._adapter_ref(),  # 当前节点 active adapter 身份
        }

    def _adapter_ref(self) -> dict:
        """当前节点 active adapter 身份（name / type）。"""
        node = self._node
        adapter = getattr(node, "adapter", None)
        return {
            "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
            "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
        }

    def _adapter_state(self) -> dict:
        """当前节点 active adapter 状态（身份 + 心跳缓存）。"""
        node = self._node
        adapter = getattr(node, "adapter", None)
        return {
            "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
            "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
            "running": getattr(adapter, "running", None) if adapter is not None else None,
        }

    def exit(self, lease_id: str | None = None) -> dict:
        """退出采集会话（ACTIVE → READY）：session quit 结束任务循环，节点回 READY（adapter 保留）。

        租约不随退出释放（独立于任务，由 /v1/leases/* 管理）。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        node = self._node
        if node.session is None or node.state != NodeState.ACTIVE:
            raise CaptureError("not in a task session")
        # session quit 退出任务：节点在任务结束后补发「node ready」回执（submit 同步等到）
        result = self._submit(Command(CMD_SESSION_QUIT, meta={"lease_id": lease_id}), timeout=10.0)
        self._raise_on_rejected(result)
        return {"status": "accepted"}

    def sync(self, meta: dict, lease_id: str | None = None) -> dict:
        """同步采集元信息（采集员 / 任务名等）到机器人进程：submit ``capture sync``。

        采集会话内消费：解析 meta JSON → ``adapter.sync_capture_meta``，进程保存一轮
        数据时附加。受控操作：须持有有效租约。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)
        result = self._submit(
            Command(CMD_CAPTURE_SYNC, params={"meta": json.dumps(meta or {})}, meta={"lease_id": lease_id})
        )
        self._raise_on_rejected(result)
        return {"status": "accepted", "meta": result.data.get("meta")}

    def meta(self) -> dict:
        """采集元信息选项（config/capture.yml 的 ``meta`` 段；前端选择列表用，只读）。"""
        return {"meta": self._meta_store.list_meta()}

    def status(self) -> dict:
        """状态快照（只读）：node_state / 当前会话类型 / session state / adapter / 采集数据 / capture_status。"""
        node = self._node
        session = self._session()
        session_state = getattr(session, "state", SessionState.INIT) if session is not None else SessionState.INIT
        data_status = self._data_status()
        save_dir = getattr(data_status, "save_dir", None) if data_status is not None else None
        capture_status = getattr(node, "capture_status", None) if node is not None else None
        lease_id = self._leases.status()["lease_id"]
        return {
            "node_state": getattr(node, "state", None) if node is not None else None,
            "session_type": getattr(node, "session_type", None) if node is not None else None,
            "state": session_state,
            "adapter": self._adapter_state(),  # 当前节点 active adapter 状态
            "save_dir": str(save_dir) if save_dir is not None else None,
            "data_files": list(getattr(data_status, "data_files", []) or []) if data_status is not None else [],
            # 采集状态缓存（adapter.capture_status()：采集员 / 任务名等元信息 + 运行位，node 周期刷新）
            "capture_status": (
                {
                    "running": bool(getattr(capture_status, "running", False)),
                    "operator": getattr(capture_status, "operator", None),
                    "task_name": getattr(capture_status, "task_name", None),
                }
                if capture_status is not None
                else None
            ),
            "disk": self._disk_info(save_dir),
            "lease_id": lease_id,  # 当前活跃租约（独立于任务，见 /v1/leases/*）
        }

    def preview(self, lease_id: str | None = None) -> dict:
        """最新观测预览：session state / adapter 身份 / observation（qpos / action + 摄像头名列表）。

        受控操作：须持有有效租约（``X-Lease-Id``）；无会话 → 409。
        观测来自 ``node.frame_manager``（observe 循环每帧写入的缓存）；**图像不内联**
        ——HTTP JSON 不承载二进制，图像（jpeg / raw）由 WebRTC（/v1/webrtc/offer）
        推流到前端，这里只返回摄像头名列表。
        """
        self._ensure_node()
        self._ensure_lease(lease_id)  # 预览属受控操作：须持有有效租约
        node = self._node
        frame_manager = getattr(node, "frame_manager", None)
        if frame_manager is None:
            raise CaptureError("frame manager not available", status_code=501)
        session = self._session()
        if session is None:
            raise CaptureError("no active session (start a session to preview)", status_code=409)
        latest = frame_manager.latest() or {}
        state = getattr(session, "state", SessionState.INIT)
        return {
            "state": state,
            "adapter": self._adapter_ref(),  # 当前节点 active adapter 身份
            "observation": {
                "qpos": self._to_float_list(latest.get(KEY_QPOS)),
                "action": self._to_float_list(latest.get(KEY_ACTION)),
                "images": self._image_names(latest),  # 摄像头名列表（图像由 WebRTC 推流）
            },
        }

    # -- 预览序列化（观测 → JSON 可表达；图像不内联，走 WebRTC）----------------
    @staticmethod
    def _to_float_list(value) -> list | None:
        """ndarray / list → float list（预览用）。"""
        if value is None:
            return None
        return [float(v) for v in np.asarray(value).reshape(-1)]

    @staticmethod
    def _image_names(obs: dict) -> list[str]:
        """观测中的摄像头名列表（``observations/images/<name>``；图像内容走 WebRTC）。"""
        return [k[len(CAMERA_PREFIX) :] for k in obs if k.startswith(CAMERA_PREFIX)]

    # -- 内部 ---------------------------------------------------------------
    def _session(self):
        """当前会话（可能为 None）。"""
        return getattr(self._node, "session", None) if self._node is not None else None

    def _adapter(self):
        """当前节点 active adapter（节点是唯一持有者，session 只引用）。"""
        node = self._node
        return getattr(node, "adapter", None) if node is not None else None

    def _session_state(self):
        """当前会话状态（无会话返回 None）。"""
        session = self._session()
        return getattr(session, "state", None) if session is not None else None

    def _ensure_lease(self, lease_id: str | None):
        """校验控制动作的租约（经 LeaseManager）：缺失 409 / 不匹配 403 / 过期 410。"""
        try:
            self._leases.require(lease_id)
        except LeaseError as exc:
            raise CaptureError(str(exc), status_code=exc.status_code) from exc

    def _ensure_node(self):
        if self._node is None:
            raise CaptureError("node not initialized")

    def _submit(self, cmd: Command, timeout: float = 5.0) -> CommandResult:
        """提交命令并同步等待回执（HTTP 动作 → 命令 → 回执，无需轮询节点状态）。"""
        return self._bus.submit(cmd, timeout=timeout)

    def _raise_on_rejected(self, result: CommandResult) -> None:
        """命令被拒绝 / 失败 → 转 HTTP 错误（默认 409）。"""
        if result.status != "ok":
            code = result.status_code or 409
            raise CaptureError(result.error or "command rejected", status_code=code)

    def _data_status(self):
        """node 缓存的采集数据状态（``node.data_status``）；无会话 / 未缓存 → None。

        数据状态由 **EdgeNode 主循环在采集会话期间自行周期查询并缓存**，此处只读缓存
        ——前端轮询 /v1/captures **不会**实时请求 SDK 进程（edge 运行不依赖前端）。
        """
        node = self._node
        if node is None:
            return None
        return getattr(node, "data_status", None)

    def _save_dir(self):
        data_status = self._data_status()
        if data_status is None:
            return None
        return getattr(data_status, "save_dir", None)

    @staticmethod
    def _disk_info(save_dir) -> dict:
        try:
            usage = shutil.disk_usage(save_dir if save_dir is not None else "/")
            return {"total": usage.total, "used": usage.used, "free": usage.free}
        except OSError:
            return {"error": "unavailable"}
