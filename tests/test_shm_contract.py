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

"""共享内存观测契约测试（真实 /dev/shm）—— 陈旧检测与写者重启后重新 attach。

覆盖：正常读帧、写者停更时**不返回冻结帧**、旧段 unlink 后同名重建时 reader 自动重新 attach。
"""

import time
import uuid

import numpy as np

from motrix_edge.adapter.shm_contract import ObsShmReader, ObsShmWriter

_IMAGE_SIZE = (4, 3)  # (width, height)
_IMAGE_COUNT = 1
_QPOS_DIM = 2
_ACTION_DIM = 2
_STALE = 0.05  # 测试用陈旧阈值（秒）


def _new_name() -> str:
    return f"mtox_{uuid.uuid4().hex[:8]}"


def _write(writer: ObsShmWriter, value: int, action: int | None = None) -> None:
    """写一帧：qpos / action / 图像都可辨识（action 缺省 = qpos）。"""
    writer.write(
        np.full(_QPOS_DIM, value, dtype="<f8"),
        np.full(_ACTION_DIM, value if action is None else action, dtype="<f8"),
        [np.full((_IMAGE_SIZE[1], _IMAGE_SIZE[0], 3), value, dtype="<u1")],
    )


def _make_writer(name: str) -> ObsShmWriter:
    return ObsShmWriter(
        name=name,
        image_count=_IMAGE_COUNT,
        image_size=_IMAGE_SIZE,
        qpos_dim=_QPOS_DIM,
        action_dim=_ACTION_DIM,
    )


def test_read_returns_latest_frame():
    """正常路径：未产出首帧 → None；产出后读到该帧（qpos + 图像）。"""
    name = _new_name()
    writer = _make_writer(name)
    reader = ObsShmReader(name)
    try:
        assert reader.read() is None  # 写者尚未产出第一帧
        _write(writer, 7)
        frame = reader.read()
        assert frame is not None
        assert frame["qpos"].tolist() == [7, 7]
        assert frame["action"].tolist() == [7, 7]
        assert int(frame["images"][0][0, 0, 0]) == 7
    finally:
        reader.close()
        writer.close()
        writer.unlink()


def test_read_returns_target_action_not_qpos():
    """action 区独立传输：读到的是进程侧**目标动作**，而不是 qpos 的副本。"""
    name = _new_name()
    writer = _make_writer(name)
    reader = ObsShmReader(name)
    try:
        _write(writer, 1, action=9)
        frame = reader.read()
        assert frame is not None
        assert frame["qpos"].tolist() == [1, 1]
        assert frame["action"].tolist() == [9, 9]  # 关键：不是 [1, 1]
    finally:
        reader.close()
        writer.close()
        writer.unlink()


def test_stale_frame_is_not_returned(monkeypatch):
    """写者停更超过阈值：冻结帧不再当实时帧返回（返回 None）。"""
    monkeypatch.setattr(ObsShmReader, "STALE_AFTER", _STALE)
    name = _new_name()
    writer = _make_writer(name)
    reader = ObsShmReader(name)
    try:
        _write(writer, 3)
        assert reader.read() is not None
        time.sleep(_STALE * 2)  # 写者停更（如主循环卡住 / 进程已退出）
        assert reader.read() is None
    finally:
        reader.close()
        writer.close()
        writer.unlink()


def test_reattaches_after_writer_restart(monkeypatch):
    """写者快速重启（旧段 unlink 后同名重建）：reader 检出陈旧并接到新段，观测不永久冻结。"""
    monkeypatch.setattr(ObsShmReader, "STALE_AFTER", _STALE)
    name = _new_name()
    old = _make_writer(name)
    reader = ObsShmReader(name)
    try:
        _write(old, 1)
        assert reader.read()["qpos"].tolist() == [1, 1]

        # 模拟服务端重启：旧段 close + unlink，再同名重建（adapter 仍映射旧段）
        old.close()
        old.unlink()
        new = _make_writer(name)

        time.sleep(_STALE * 2)
        assert reader.read() is None  # 检出陈旧 → 重新 attach 新段（本帧无观测）
        _write(new, 9)
        frame = reader.read()
        assert frame is not None
        assert frame["qpos"].tolist() == [9, 9]  # 已跟上新段，而不是旧段的冻结值
    finally:
        reader.close()
        new.close()
        new.unlink()
