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

"""calibration/store —— 标定产物（``<根>/config/calibration/frames.json``）的读取与写出。

-   **不播种 / 不占位**：本地没有副本时**不写盘**（不把产物偷偷拷到 ``<根>/config``）；
    但会**只读回落**随仓库分发的**实测**产物（``src/config/calibration/frames.json``，即
    现场量出来的那一份，见下），并打一条带 ``tool`` / ``calibrated_at`` 的 WARNING——
    「当前用的不是本机产物」要看得见。包内**从不**放占位/零值外参（一份假的外参比没有外参
    危险得多）；换台位 / 动过相机后必须重标，本地产物永远优先。
-   **不抛**：机器人进程不该因为标定产物写坏了起不来。非法产物 → **整份忽略** + 一条 WARNING，
    坐标功能视为不可用，其余链路（观测 / 采集 / 推理）照常；
-   **缓存**：外参是静态元数据（``GET /v1/cameras`` 每次请求都会读），进程内缓存一份；
    ``reset_cache()`` 供测试与「标定后热载」使用。
"""

from __future__ import annotations

from pathlib import Path

from config import config_path, resolve_config_file
from utils.data_handler import debug_print

from motrix_edge.geometry import FrameSet

#: 产物相对**配置目录**的路径（``<根>/config/<它>``）。
FRAMES_RELATIVE = "calibration/frames.json"

#: 告警来源名（机器人进程日志里的前缀）。
_LOG_NAME = "calibration"

_cache: FrameSet | None = None
_loaded = False
_last_notice: list[str | None] = [None]  # 同一条告知 / 告警只打一次（进程内）


def frames_path() -> Path:
    """本地产物路径（``<根>/config/calibration/frames.json``；写产物写这里）。"""
    return config_path(FRAMES_RELATIVE)


def effective_path() -> Path:
    """**实际生效**的产物路径：本地产物优先，缺失 → 包内随仓库分发的实测产物
    （不写盘）。

        与 ``config.resolve_config_file`` 同一口径（gravity 产物同款），供启动清单 /
        排障时二者一比就可看出当前用的是哪一份。
    """
    return resolve_config_file(FRAMES_RELATIVE)


def _load(local_first: bool = True) -> tuple[FrameSet | None, str | None]:
    """读产物 → ``(frames, 提示)``；提示非空时调用方往外打一条（去重后）。"""
    path = frames_path()
    if path.exists():
        try:
            return FrameSet.load(path), None
        except Exception as exc:  # noqa: BLE001 产物坏了不该拖垮机器人进程
            return None, f"标定产物不可用（{path}）：{exc}——坐标功能关闭，其余链路不受影响"
    packaged = effective_path()
    try:
        frames = FrameSet.load(packaged)
    except Exception as exc:  # noqa: BLE001 包内那份也没了 / 坏了：等同未标定
        return None, f"无本地产物，包内回落也不可用（{packaged}）：{exc}——坐标功能关闭"
    return frames, (
        f"本地无标定产物（{path}），暂用随仓库分发的台位产物（{packaged}）："
        f"tool={frames.tool} · calibrated_at={frames.calibrated_at}——"
        f"换台位 / 动过相机后必须重标（或用 --install 写入本机产物）"
    )


def load_frames(*, use_cache: bool = True) -> FrameSet | None:
    """读标定产物：本地产物优先 → 包内实测产物 → 都没有 / 非法 → ``None``。

    两种情况各打一条（进程内去重）WARNING：产物非法（整份忽略）与「当前用的是包内
    那份台位产物」（不是本机的标定结果）。
    """
    global _cache, _loaded
    if use_cache and _loaded:
        return _cache
    frames, notice = _load()
    if notice is not None and notice != _last_notice[0]:
        _last_notice[0] = notice
        debug_print(_LOG_NAME, notice, "WARNING")
    _cache = frames
    _loaded = True
    return frames


def save_frames(frames: FrameSet) -> Path:
    """写出产物（``--install`` 用）并刷新缓存。"""
    global _cache, _loaded
    path = frames.save(frames_path())
    _cache = frames
    _loaded = True
    return path


def reset_cache() -> None:
    """清缓存（测试 / 现场重新标定后不想重启进程时用）。"""
    global _cache, _loaded
    _cache = None
    _loaded = False
    _last_notice[0] = None


def camera_extrinsics() -> dict[str, dict]:
    """各相机的外参块（``GET /v1/cameras`` 的附加段）；未标定 → 空 dict。

    形状与契约键名无关（键名单点在 ``server/contract_server._camera_payload``）：这里只给
    ``{"camename": {"mount": ..., "arm": ..., "transform": 4×4}}``。
    """
    frames = load_frames()
    if frames is None:
        return {}
    return {
        name: {"mount": item.mount, "arm": item.arm, "transform": item.transform, "rms_m": item.rms_m}
        for name, item in frames.cameras.items()
    }


__all__ = [
    "FRAMES_RELATIVE",
    "camera_extrinsics",
    "effective_path",
    "frames_path",
    "load_frames",
    "reset_cache",
    "save_frames",
]
