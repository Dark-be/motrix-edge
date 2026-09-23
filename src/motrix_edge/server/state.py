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

"""server 层的节点状态读取（captures / infers 两个服务**共用**的单点实现）。

只读 ``EdgeNode`` 的**缓存**字段（adapter 身份 / 心跳缓存 / 采集状态），**不**触发对 SDK
进程的实时请求——前端轮询 ``/v1/captures`` / ``/v1/infers`` 只读缓存（缓存由节点主循环周期
刷新，见 wiki/design/motrix_edge_server.md）。
"""


def adapter_ref(node) -> dict:
    """当前节点绑定 adapter 的身份（``name`` / ``type``）。

    委托 ``node.adapter_ref``（节点是身份的单一来源）；无该属性的测试替身回退
    按 ``adapter_name`` / ``adapter`` 读，保持向后兼容。
    """
    if node is None:
        return {"name": None, "type": None}
    ref = getattr(node, "adapter_ref", None)
    if ref is not None:
        return ref
    adapter = getattr(node, "adapter", None)
    return {
        "name": getattr(node, "adapter_name", None) or getattr(adapter, "name", None),
        "type": getattr(node, "adapter_type", None) or getattr(adapter, "type", None),
    }


def adapter_state(node) -> dict:
    """adapter 状态：身份 + 心跳缓存（``running`` / ``control_hz`` / ``measured_hz``）。

    心跳字段来自 ``node.adapter_health``（节点周期查询并缓存的 ``HealthStatus``）；未探测到
    （尚未缓存 / 适配器未就绪）时为 ``None``。``control_hz`` / ``measured_hz`` 为**该机器人
    进程的实际控制频率**（名义值 / 实测值），供前端展示与排查抖动。
    """
    adapter = getattr(node, "adapter", None) if node is not None else None
    health = getattr(node, "adapter_health", None) if node is not None else None
    return {
        **adapter_ref(node),
        "running": getattr(adapter, "running", None) if adapter is not None else None,
        "control_hz": getattr(health, "control_hz", None) if health is not None else None,
        "measured_hz": getattr(health, "measured_hz", None) if health is not None else None,
    }


def capture_raw(node):
    """node 缓存的采集状态对象（``CaptureStatus``；读取 ``data_dir`` 等字段用）；未缓存 → None。"""
    return getattr(node, "capture_status", None) if node is not None else None


def capture_status(node) -> dict | None:
    """机器人进程采集状态（序列化版：``running`` + ``meta`` 元信息全集）；未缓存 → None。

    ``meta`` 是 ``capture sync`` 同步到进程的**元信息全集**（采集员 / 任务名等是其中的键，
    分类可拓展）——与 ``/v1/captures`` 的 ``capture_status`` 同构，不另设同义顶层字段。
    """
    raw = capture_raw(node)
    if raw is None:
        return None
    return {
        "running": bool(getattr(raw, "running", False)),
        "meta": dict(getattr(raw, "meta", {}) or {}),
    }
