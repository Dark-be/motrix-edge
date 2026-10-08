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

"""机器档案探测：把「这台机器实际插了什么」枚举出来，供 ``scripts/setup_robot.sh`` 填档。

稳定性判据与 ``scripts/can_muti_activate.sh`` 一致——**认物理口 / 稳定软链，不认设备号**：

===============  ============================================================================
kind             枚举内容（写进档案的值）
===============  ============================================================================
``realsense``    RealSense 序列号（缺 ``pyrealsense2`` → :class:`ProbeUnavailable`）
``v4l2``         ``/dev/v4l/by-id/*``（稳定软链；无 → 回退 ``/dev/video*``）
``serial``       ``/dev/serial/by-id/*``（USB 串口，如 Alicia 示教臂）
``can``          ``ip -br link show type can`` + ``ethtool -i`` 的 ``bus-info``（USB 物理口）
``virtual``      测试替身（无需探测，恒空）
===============  ============================================================================

本模块只做**枚举与差分**（纯逻辑 + 只读子进程），不做交互、不写档案：交互在
``scripts/setup_robot.sh``，落盘用 :func:`config.save_machine_profile`。

识别单个角色的做法：该角色插拔前后各取一次 :func:`probe_map`，差集（:func:`added`）就是「刚插上的
设备」——现场因此不需要事先知道序列号 / bus-info。
"""

from __future__ import annotations

import glob
import subprocess
from pathlib import Path
from typing import Any

#: 设备类型（与机器人类的 ``PORT_KINDS`` / ``CAMERA_KINDS`` 取值一一对应）。
KIND_REALSENSE = "realsense"
KIND_V4L2 = "v4l2"
KIND_SERIAL = "serial"
KIND_CAN = "can"
KIND_VIRTUAL = "virtual"
#: 全部合法 kind（``setup_robot.sh`` 用它校验机器人类里的声明）。
KINDS = (KIND_REALSENSE, KIND_V4L2, KIND_SERIAL, KIND_CAN, KIND_VIRTUAL)

#: CAN 缺省波特率（Piper 臂 1 Mbps，与 ``can_muti_activate.sh`` 同一缺省）。
CAN_BITRATE_DEFAULT = 1000000
#: 稳定软链目录（换 USB 口不变）——V4L2 相机 / USB 串口。
V4L2_BY_ID = Path("/dev/v4l/by-id")
SERIAL_BY_ID = Path("/dev/serial/by-id")


class ProbeUnavailable(RuntimeError):
    """该 kind 的枚举依赖不可用（如缺 ``pyrealsense2``）：按提示装可选面后重试。"""


def _run(*args: str) -> str:
    """跑一条**只读**命令 → stdout（命令不存在 / 失败 → 空串，调用方按「没找到」处理）。"""
    try:
        done = subprocess.run(args, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def _ethtool(iface: str, field: str) -> str:
    """``ethtool -i <iface>`` 的某个字段值（取不到 → 空串）。"""
    for line in _run("ethtool", "-i", iface).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == field:
            return value.strip()
    return ""


def _bitrate(iface: str) -> str:
    """当前 CAN 波特率（未设置 → 空串）。"""
    for line in _run("ip", "-details", "link", "show", iface).splitlines():
        if "bitrate" in line:
            parts = line.split("bitrate", 1)[1].split()
            if parts:
                return parts[0]
    return ""


def _rs_info(device, key) -> str:
    """读一条 RealSense 设备信息；型号 / 固件不支持该字段时返回空串。"""
    try:
        return str(device.get_info(key))
    except Exception:  # noqa: BLE001 老固件没有 physical_port 之类的字段
        return ""


def list_realsense() -> list[dict]:
    """枚举 RealSense 相机：``[{"serial", "name", "physical_port"}]``（按序列号排序）。

    Raises:
        ProbeUnavailable: 没装 ``pyrealsense2``（``uv sync --extra realsense`` / ``--extra camera``）。
    """
    try:
        import pyrealsense2 as rs
    except ImportError as exc:
        raise ProbeUnavailable(
            "缺少 pyrealsense2：在 robot-pipeline 下执行 uv sync --extra realsense（或 --extra camera）后重试"
        ) from exc
    try:
        devices = [
            {
                "serial": _rs_info(device, rs.camera_info.serial_number),
                "name": _rs_info(device, rs.camera_info.name),
                "physical_port": _rs_info(device, rs.camera_info.physical_port),
            }
            for device in rs.context().query_devices()
        ]
    except Exception as exc:  # noqa: BLE001 设备被占用 / 权限不足 / 后端异常：按「该类不可用」处理，不炸整个 --list
        raise ProbeUnavailable(f"RealSense 枚举失败：{exc}") from exc
    return sorted(devices, key=lambda item: item["serial"])


def list_v4l2(root: Path = V4L2_BY_ID) -> list[str]:
    """V4L2 相机设备节点：优先 ``/dev/v4l/by-id/*``（稳定软链），无 → ``/dev/video*``。

    一个 UVC 相机会暴露多条 by-id（``-video-index0/1/2``，不同 stream）→ 只取主采集节点
    ``-video-index0``（无该后缀时取全部）。
    """
    if root.is_dir():
        entries = sorted(str(path) for path in root.iterdir())
        if entries:
            primary = [entry for entry in entries if entry.endswith("video-index0")]
            return primary or entries
    return sorted(glob.glob("/dev/video*"))


def list_serial(root: Path = SERIAL_BY_ID) -> list[str]:
    """USB 串口设备：``/dev/serial/by-id/*``（稳定软链，换 USB 口不变）。"""
    return sorted(str(path) for path in root.iterdir()) if root.is_dir() else []


def list_can() -> list[dict]:
    """枚举 CAN 接口：``[{"iface", "bus_info", "bitrate", "up"}]``（按 ``bus_info`` 排序）。

    ``bus_info`` = USB 物理口（``ethtool -i``）：与设备无关，故档案以它为键绑定目标名
    （与 ``can_muti_activate.sh`` 同一判据）。缺 ``ip`` / ``ethtool`` → 空列表。
    """
    ifaces = [line.split()[0] for line in _run("ip", "-br", "link", "show", "type", "can").splitlines() if line.split()]
    entries = [
        {
            "iface": iface,
            "bus_info": _ethtool(iface, "bus-info"),
            "bitrate": _bitrate(iface),
            "up": "UP" in _run("ip", "-br", "link", "show", iface),
        }
        for iface in ifaces
    ]
    return sorted(entries, key=lambda item: (item["bus_info"] or "", item["iface"]))


def probe(kind: str) -> list:
    """按 ``kind`` 枚举设备（``virtual`` → 空列表：测试替身无需探测）。

    Raises:
        ValueError: 未知 ``kind``（不在 :data:`KINDS` 中）。
        ProbeUnavailable: 该 kind 的依赖不可用（如缺 ``pyrealsense2``）。
    """
    if kind == KIND_REALSENSE:
        return list_realsense()
    if kind == KIND_V4L2:
        return list_v4l2()
    if kind == KIND_SERIAL:
        return list_serial()
    if kind == KIND_CAN:
        return list_can()
    if kind == KIND_VIRTUAL:
        return []
    raise ValueError(f"unknown device kind {kind!r} (expected one of {KINDS})")


def identity(kind: str, item: Any) -> str:
    """设备的**稳定标识**：RealSense → 序列号；CAN → ``bus-info``；V4L2 / 串口 → 软链路径。"""
    if kind == KIND_REALSENSE:
        return str(item.get("serial") or "")
    if kind == KIND_CAN:
        return str(item.get("bus_info") or "")
    return str(item)


def probe_map(kind: str) -> dict[str, Any]:
    """``{稳定标识: 设备条目}``（标识为空 / 重复的条目丢弃）——插拔前后对比用这个。"""
    result: dict[str, Any] = {}
    for item in probe(kind):
        key = identity(kind, item)
        if key:
            result.setdefault(key, item)
    return result


def added(before, after) -> list[str]:
    """``after`` 相对 ``before`` **新增**的稳定标识（排序）——「刚才插上的设备」。"""
    return sorted(set(after) - set(before))
