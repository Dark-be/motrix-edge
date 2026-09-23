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

"""server/status —— 只读状态快照（``/v1/captures`` · ``/v1/infers`` · ``/v1/captures/precheck``）。

全部只读 **node / session 内存状态**（节点主循环已周期 discover / 心跳并缓存），不触发任何
对机器人进程的实时请求；``leases`` 只用于回显当前租约 id 与可租状态。

这些快照不是命令（不产生副作用），故不经 CommandBus：写走 ``CommandService``、读走本模块
与 ``state.py``（原子访问器）。
"""

import shutil

from motrix_edge.command import policy_config_status
from motrix_edge.node import NodeState
from motrix_edge.server.state import (
    adapter_state,
    capture_raw,
)
from motrix_edge.server.state import (
    capture_status as capture_agent_status,
)
from motrix_edge.session.base import SessionState


def _session(node):
    """当前会话（可能为 None）。"""
    return getattr(node, "session", None) if node is not None else None


def _session_state(session) -> SessionState:
    return getattr(session, "state", SessionState.INIT) if session is not None else SessionState.INIT


def disk_info(data_dir) -> dict:
    """磁盘占用（``data_dir`` 缺省用 ``/``）；不可用 → ``{"error": ...}``。"""
    try:
        usage = shutil.disk_usage(data_dir if data_dir is not None else "/")
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError as exc:
        return {"error": str(exc) or "unavailable"}


def capture_snapshot(node, leases) -> dict:
    """``GET /v1/captures`` 快照。

    ``capture_status`` = adapter 上报的采集状态缓存（**运行位 + 元信息全集 + 数据目录**，
    见 ``node.capture_status``）；未绑定 / 未缓存 → None。运行位与数据目录**只在这里出现
    一次**（不另设 ``capture_running`` / 顶层 ``data_dir`` 同义字段）。
    """
    session = _session(node)
    raw = capture_raw(node)
    data_dir = getattr(raw, "data_dir", None) if raw is not None else None
    return {
        "node_state": getattr(node, "state", None) if node is not None else None,
        "session_type": getattr(node, "session_type", None) if node is not None else None,
        "state": _session_state(session),
        "adapter": adapter_state(node),  # 当前节点 active adapter 状态（含遥操作位）
        "capture_status": (
            {
                **(capture_agent_status(node) or {}),
                "data_dir": str(data_dir or "") or None,
            }
            if raw is not None
            else None
        ),
        "disk": disk_info(data_dir),
        "lease_id": leases.status()["lease_id"],  # 当前活跃租约（独立于任务，见 /v1/leases/*）
    }


def capture_precheck(node, leases) -> dict:
    """``GET /v1/captures/precheck`` 预检：节点运行中 + 采集会话 + 机器人就绪 + 磁盘 + 租约。"""
    errors: list[str] = []
    if node is None:
        errors.append("node not running")

    node_state = getattr(node, "state", None) if node is not None else None
    session = _session(node)
    session_state = _session_state(session)
    if session is None:
        errors.append("collect session not active")

    robot_ready = False
    adapter = getattr(node, "adapter", None) if node is not None else None
    if adapter is not None:
        try:
            robot_ready = bool(adapter.ready)
        except Exception as exc:  # noqa: BLE001 adapter 探活异常不致命，记进 errors
            errors.append(f"robot not ready: {exc}")

    raw = capture_raw(node)
    data_dir = getattr(raw, "data_dir", None) if raw is not None else None
    disk: dict = {}
    if data_dir is not None:
        try:
            usage = shutil.disk_usage(data_dir)
            disk = {"total": usage.total, "used": usage.used, "free": usage.free}
        except OSError as exc:
            errors.append(f"disk unavailable: {exc}")

    lease_info = leases.status()
    healthy = (
        not errors and robot_ready and node_state not in (None, NodeState.ERROR) and session_state != SessionState.ERROR
    )
    return {
        "ok": healthy,
        "node_state": node_state,
        "state": session_state,
        "robot_ready": robot_ready,
        "disk": disk,
        "errors": errors,
        "lease_id": lease_info["lease_id"],  # 当前活跃租约（无租约为 None）
        "leasable": lease_info["leasable"] and node_state != NodeState.ERROR,  # 可激活租约
    }


def infer_snapshot(node, leases) -> dict:
    """``GET /v1/infers`` 快照（会话 / 策略 / 预热 / 录制 / RTC / 策略配置项 + 租约）。

    - ``prompt`` = 当前推理文本指令（**仅需要 prompt 的策略**：推理 / 录制前必须非空）；
    - ``recording`` = 会话当前是否开启 rollout 录制；``continuous`` = 持续推理是否在跑；
    - ``capture_status`` = 机器人进程实际采集状态缓存（与 ``/v1/captures`` 同构）；
    - ``policy_config`` = 策略配置项 schema + 当前值 + 缺失必填项（前端据此渲染表单并门控按钮）。
    """
    session = _session(node)
    connected = bool(getattr(session, "connected", False)) if session is not None else False
    return {
        "node_state": getattr(node, "state", None) if node is not None else None,
        "session_type": getattr(node, "session_type", None) if node is not None else None,
        "state": _session_state(session),
        "adapter": adapter_state(node),
        "policy": _policy_ref(session),
        # 策略服务器连接状态；metadata 仅在已连接时暴露（连接成功后才有服务端元信息）
        "connected": connected,
        "metadata": (
            dict(getattr(getattr(session, "policy", None), "server_metadata", None) or {}) if connected else None
        ),
        "prompt": getattr(session, "prompt", None) if session is not None else None,
        "prompt_required": bool(getattr(session, "prompt_required", False)) if session is not None else False,
        # 预热（infer connect：连接 + prepare + 取一块丢弃，**不下发动作**）：warmup_required
        # 为 true（缺省）时它是 infer rollout 的硬前置
        "warmed_up": bool(getattr(session, "warmed_up", False)) if session is not None else False,
        "warming": bool(getattr(session, "warming", False)) if session is not None else False,
        "warmup_error": getattr(session, "warmup_error", None) if session is not None else None,
        # 因回执超时被丢弃的动作数（未下发到真机；诊断「调用方超时」时看）
        "dropped_actions": int(getattr(session, "dropped_actions", 0)) if session is not None else 0,
        # 推理会话当前是否开启 rollout 录制（capture episode start 后为 True）
        "recording": bool(getattr(session, "recording", False)) if session is not None else False,
        # 持续推理是否正在运行（infer rollout continuous ↔ infer rollout stop）
        "continuous": bool(getattr(session, "continuous", False)) if session is not None else False,
        "capture_status": capture_agent_status(node),
        "rtc": _rtc_status(session),
        "policy_config": _policy_config_status(node, session),
        "lease_id": leases.status()["lease_id"],
    }


def _policy_ref(session):
    """当前推理会话的策略客户端标识（无会话 → None）。"""
    if session is None:
        return None
    return getattr(getattr(session, "policy", None), "name", None)


def _rtc_status(session) -> dict | None:
    """RTC 运行状态（读会话的 RTCManager；无会话 → None）。"""
    rtc_status = getattr(session, "rtc_status", None)
    return rtc_status() if callable(rtc_status) else None


def _policy_config_status(node, session, policy_type: str | None = None) -> dict:
    """策略配置项状态（schema + 当前值 + 缺失必填项）。

    会话内优先取会话快照（含**已应用**的运行值）；无会话（进入会话前）则由
    ``base_cfg["policy"]`` 直接计算——前端据此在选择策略后即渲染配置表单。
    """
    if policy_type is None and session is not None:
        session_status = getattr(session, "policy_config_status", None)
        if callable(session_status):
            return session_status()
    base_cfg = getattr(node, "base_cfg", None) if node is not None else None
    if base_cfg is None:
        return {}
    try:
        return policy_config_status(base_cfg, policy_type=policy_type)
    except ValueError:  # 未知策略类型（配置异常）：不阻断状态查询
        return {}


__all__ = ["capture_precheck", "capture_snapshot", "disk_info", "infer_snapshot"]
