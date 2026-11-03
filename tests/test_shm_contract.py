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

覆盖：正常读帧、写者停更时**不返回冻结帧**、旧段 unlink 后同名重建时 reader 自动重新 attach、
位姿区（pose_dim > 0 时布局 v3 且读到 ``pose``；= 0 时与 v2 兼容且无 ``pose`` 键）。
"""

import time
import uuid
from multiprocessing import shared_memory

import numpy as np
import pytest

from motrix_edge.adapter.shm_contract import ObsShmReader, ObsShmWriter

_IMAGE_SIZE = (4, 3)  # (width, height)
_IMAGE_COUNT = 1
_QPOS_DIM = 2
_ACTION_DIM = 2
_POSE_DIM = 6  # 位姿区（v3）；0 = 不启用（v2 兼容）
_STALE = 0.05  # 测试用陈旧阈值（秒）

# MR 前的真实 v2 header。新字段只能追加，不能改变这些字段的 offset。
_LEGACY_V2_HEADER = np.dtype(
    [
        ("magic", "<i8"),
        ("version", "<i8"),
        ("frame_seq", "<i8"),
        ("timestamp", "<f8"),
        ("image_count", "<i8"),
        ("image_height", "<i8"),
        ("image_width", "<i8"),
        ("image_channels", "<i8"),
        ("qpos_dim", "<i8"),
        ("qpos_offset", "<i8"),
        ("action_dim", "<i8"),
        ("action_offset", "<i8"),
        ("image_data_offset", "<i8"),
        ("image_data_size", "<i8"),
        ("running", "<i8"),
        ("capturing", "<i8"),
    ]
)


def _new_name() -> str:
    return f"mtox_{uuid.uuid4().hex[:8]}"


def _write(writer: ObsShmWriter, value: int, action: int | None = None) -> None:
    """写一帧：qpos / action / 图像都可辨识（action 缺省 = qpos）。"""
    writer.write(
        np.full(_QPOS_DIM, value, dtype="<f8"),
        np.full(_ACTION_DIM, value if action is None else action, dtype="<f8"),
        [np.full((_IMAGE_SIZE[1], _IMAGE_SIZE[0], 3), value, dtype="<u1")],
    )


def _make_writer(name: str, pose_dim: int = 0) -> ObsShmWriter:
    return ObsShmWriter(
        name=name,
        image_count=_IMAGE_COUNT,
        image_size=_IMAGE_SIZE,
        qpos_dim=_QPOS_DIM,
        action_dim=_ACTION_DIM,
        pose_dim=pose_dim,
    )


def test_pose_region_roundtrip():
    """pose_dim > 0：布局升到 v3，读者拿到位姿（与 qpos / action 同一帧）。"""
    name = _new_name()
    writer = _make_writer(name, pose_dim=_POSE_DIM)
    reader = ObsShmReader(name)
    try:
        assert reader.pose_dim == _POSE_DIM
        writer.write(
            np.full(_QPOS_DIM, 1, dtype="<f8"),
            np.full(_ACTION_DIM, 2, dtype="<f8"),
            [np.zeros((_IMAGE_SIZE[1], _IMAGE_SIZE[0], 3), dtype="<u1")],
            pose=np.arange(_POSE_DIM, dtype="<f8"),
        )
        frame = reader.read()
        assert frame is not None
        assert frame["pose"].tolist() == [0, 1, 2, 3, 4, 5]
    finally:
        reader.close()
        writer.close()
        writer.unlink()


def test_pose_dim_zero_keeps_v2_layout():
    """pose_dim = 0：不占位姿区（版本保持 v2），读数不含 ``pose`` 键（向后兼容）。"""
    name = _new_name()
    writer = _make_writer(name)
    reader = ObsShmReader(name)
    try:
        assert reader.pose_dim == 0
        _write(writer, 5)
        frame = reader.read()
        assert frame is not None and "pose" not in frame
    finally:
        reader.close()
        writer.close()
        writer.unlink()


def test_reader_accepts_legacy_v2_header_without_misreading_offsets():
    """新 reader 可读取 MR 前的 v2 writer；新增 header 字段不能移动旧字段。"""
    name = _new_name()
    qpos_offset = _LEGACY_V2_HEADER.itemsize
    action_offset = qpos_offset + _QPOS_DIM * 8
    image_offset = action_offset + _ACTION_DIM * 8
    image_bytes = _IMAGE_COUNT * _IMAGE_SIZE[1] * _IMAGE_SIZE[0] * 3
    shm = shared_memory.SharedMemory(name=name, create=True, size=image_offset + image_bytes)
    header = np.ndarray((), dtype=_LEGACY_V2_HEADER, buffer=shm.buf)
    header["magic"] = 0x4D4F544F4E474F
    header["version"] = 2
    header["frame_seq"] = 1
    header["timestamp"] = time.time()
    header["image_count"] = _IMAGE_COUNT
    header["image_height"] = _IMAGE_SIZE[1]
    header["image_width"] = _IMAGE_SIZE[0]
    header["image_channels"] = 3
    header["qpos_dim"] = _QPOS_DIM
    header["qpos_offset"] = qpos_offset
    header["action_dim"] = _ACTION_DIM
    header["action_offset"] = action_offset
    header["image_data_offset"] = image_offset
    header["image_data_size"] = image_bytes
    header["running"] = 1
    header["capturing"] = 0
    np.ndarray((_QPOS_DIM,), dtype="<f8", buffer=shm.buf, offset=qpos_offset)[:] = [1, 2]
    np.ndarray((_ACTION_DIM,), dtype="<f8", buffer=shm.buf, offset=action_offset)[:] = [3, 4]
    np.ndarray((_IMAGE_COUNT, _IMAGE_SIZE[1], _IMAGE_SIZE[0], 3), dtype="<u1", buffer=shm.buf, offset=image_offset)[
        :
    ] = 5

    reader = ObsShmReader(name)
    try:
        frame = reader.read()
        assert reader.pose_dim == 0
        assert frame is not None
        assert frame["qpos"].tolist() == [1, 2]
        assert frame["action"].tolist() == [3, 4]
        assert "pose" not in frame
        assert reader.running is True
    finally:
        reader.close()
        shm.close()
        shm.unlink()


def test_pose_writer_requires_pose_every_frame():
    """启用 pose 区后缺 pose 必须拒绝，不能发布混合新 qpos / 旧 pose 的帧。"""
    name = _new_name()
    writer = _make_writer(name, pose_dim=_POSE_DIM)
    try:
        with pytest.raises(ValueError, match="pose is required"):
            _write(writer, 1)
    finally:
        writer.close()
        writer.unlink()


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
