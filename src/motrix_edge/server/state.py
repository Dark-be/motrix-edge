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

"""server 状态助手 —— 状态快照的公共只读片段。

``/v1/captures`` 与 ``/v1/infers`` 的状态快照（``server/status.py``）都含「当前节点绑定的
adapter」这一段（身份 + 心跳缓存 + 控制频率 + 遥操作位）与「机器人进程采集状态」一段；
单点实现在此，避免两处各写一份而漂移（历史上有过两份逐字段重复的实现）。

全部只读 **node 内存状态**（节点主循环已周期 discover / 心跳并缓存），不触发任何对机器人
进程的实时请求（前端轮询不穿透到 SDK 进程）。
"""

from __future__ import annotations


def adapter_ref(node) -> dict:
    """当前节点 active adapter 身份（``name`` / ``type``）；未绑定 → 空值。

    ``name`` / ``type`` 优先取节点绑定时的记录（discover 赋予的名称 + entry point 类型），
    回退 adapter 实例自身字段。实现委托 ``node.adapter_ref``（节点是身份的单一来源），
    无该属性的测试替身走下面的回退读取。
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
    """当前节点 active adapter 状态：身份 + 心跳缓存（running / 控制频率）+ 遥操作位。

    - ``running``：进程运行位；``control_hz`` / ``measured_hz``：名义 / 实测控制频率
      （来自节点缓存的心跳 ``adapter_health``，未缓存 → None）；
    - ``teleop`` / ``teleop_mode``：遥操作（人工接管）当前是否开启与映射模式
      （``absolute`` 示教 / ``delta`` 增量接管）——**仅支持遥操作的 adapter 会上报**
      （``set_teleop`` 记录；不支持者为 False / None），供前端显示「当前：程控 / 遥操作 /
      人工接管」并据此决定是否允许推理。
    """
    adapter = getattr(node, "adapter", None)
    health = getattr(node, "adapter_health", None)
    teleop = bool(getattr(adapter, "teleop_enabled", False))
    return {
        **adapter_ref(node),
        "running": getattr(adapter, "running", None) if adapter is not None else None,
        "control_hz": getattr(health, "control_hz", None) if health is not None else None,
        "measured_hz": getattr(health, "measured_hz", None) if health is not None else None,
        "teleop": teleop,
        "teleop_mode": getattr(adapter, "teleop_mode", None) if teleop else None,
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
