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

"""UploadSession 单元测试：目录扫描、episode 配对、选择、边界与打包。"""

import json
import threading
from pathlib import Path

import pytest

from motrix_edge.errors import ErrorCode
from motrix_edge.session import UploadError, UploadSession


def _session(tmp_path, **upload) -> UploadSession:
    """构造带白名单的会话：允许扫描的根 = tmp_path（等价于 upload.data_dir 指向它）。"""
    return UploadSession({"upload": {"data_dir": str(tmp_path), **upload}})


def test_scan_pairs_episode_files_and_reads_metadata(tmp_path):
    (tmp_path / "episode_10.mcap").write_bytes(b"mcap-10")
    (tmp_path / "episode_10.json").write_text(
        json.dumps({"collector": "operator-1", "task_name": "pick"}), encoding="utf-8"
    )
    (tmp_path / "episode_2.mcap").write_bytes(b"mcap-2")
    (tmp_path / "episode_2.json").write_text(json.dumps({"duration_s": 12.5}), encoding="utf-8")

    status = _session(tmp_path).scan(str(tmp_path))

    assert status["folder_path"] == str(tmp_path.resolve())
    assert status["episode_count"] == 2
    assert [episode["episode_id"] for episode in status["episodes"]] == ["episode_2", "episode_10"]
    episode = status["episodes"][0]
    assert episode["status"] == "ready"
    assert episode["metadata_content"] == {"duration_s": 12.5}
    assert episode["mcap"]["size"] == len(b"mcap-2")
    assert len(episode["mcap"]["sha256"]) == 64


def test_scan_marks_missing_or_invalid_metadata(tmp_path):
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_1.json").write_text("{invalid", encoding="utf-8")
    (tmp_path / "episode_2.json").write_text(json.dumps(["not", "object"]), encoding="utf-8")

    status = _session(tmp_path).scan(str(tmp_path))

    assert status["invalid_count"] == 3
    by_id = {episode["episode_id"]: episode for episode in status["episodes"]}
    assert "missing .json metadata file" in by_id["episode_0"]["errors"]
    assert by_id["episode_1"]["metadata_content"] is None
    assert "missing .mcap file" in by_id["episode_1"]["errors"]
    assert "metadata JSON root must be an object" in by_id["episode_2"]["errors"]


def test_scan_survives_file_vanishing_mid_scan(tmp_path, monkeypatch):
    """列目录后、算 SHA-256 前文件被删（手工清理 / 收拾 pack 残留）：

    只把该 episode 降级为 invalid（errors 注明 vanished），整次扫描不失败——否则
    ``FileNotFoundError`` 会一路冒到 HTTP 层变成裸 500，已有扫描结果全丢。
    """
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_0.json").write_text("{}", encoding="utf-8")
    original_open = Path.open

    def _vanished(self, *args, **kwargs):
        if self.suffix == ".mcap":  # 模拟文件在扫描途中消失
            raise FileNotFoundError(str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _vanished)

    status = _session(tmp_path).scan(str(tmp_path))

    assert status["episode_count"] == 1  # 扫描整体仍然成功
    episode = status["episodes"][0]
    assert episode["status"] == "invalid"
    assert episode["mcap"] is None
    assert any("vanished during scan" in error for error in episode["errors"])
    # 未受影响的文件照常解析（只有失联文件的那一项降级）
    assert episode["metadata"]["size"] == len(b"{}")
    assert episode["meta"]["task_name"] is None


def test_select_replaces_episode_selection_and_enqueue_requires_endpoint(tmp_path):
    for episode_id in ("episode_0", "episode_1"):
        (tmp_path / f"{episode_id}.mcap").write_bytes(b"mcap")
        (tmp_path / f"{episode_id}.json").write_text("{}", encoding="utf-8")

    session = _session(tmp_path)
    session.scan(str(tmp_path))
    uploader = _session(tmp_path, endpoint="https://upload.example.test")
    uploader.scan(str(tmp_path))
    selected = uploader.select(["episode_1"])
    assert selected["selected_episode_ids"] == ["episode_1"]

    queued = uploader.enqueue()
    assert queued["episodes"][1]["status"] == "pending"

    # 未配置 endpoint：**选择本身可用**，enqueue 才拒绝（501）
    assert session.select(["episode_0"])["selected_episode_ids"] == ["episode_0"]
    with pytest.raises(UploadError, match="endpoint is not configured") as excinfo:
        session.enqueue()
    assert excinfo.value.code == ErrorCode.NOT_IMPLEMENTED


def test_select_rejects_unknown_or_invalid_episode(tmp_path):
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    session = _session(tmp_path)
    session.scan(str(tmp_path))

    with pytest.raises(UploadError, match="unknown episode_ids"):
        session.select(["episode_9"])

    with pytest.raises(UploadError, match="episodes are not selectable"):
        session.select(["episode_0"])


def test_scan_extracts_structured_metadata_fields(tmp_path):
    """schema 驱动的结构化元信息解析：已知字段归一化类型（int / float / str）。"""
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_0.json").write_text(
        json.dumps(
            {
                "relative_path": "3ded50a2b2f74ed097c42687a38c73a5.mcap",
                "robot_name": "test_robot_my_pc",
                "robot_type": "test_robot",
                "operator": "李四",
                "task_name": "桌面前移",
                "frames": 110,
                "size_bytes": 1056419,
                "duration": 3.6409189701080322,
                "sha256": "d721f62bf386028a6d09e41dfb128d6ef5597b5772a21493e42aa96d7c74dfe8",
                "created_at": "2026-09-01T16:01:08",
            }
        ),
        encoding="utf-8",
    )

    episode = _session(tmp_path).scan(str(tmp_path))["episodes"][0]

    assert episode["status"] == "ready"
    meta = episode["meta"]
    assert meta["relative_path"] == "3ded50a2b2f74ed097c42687a38c73a5.mcap"
    assert meta["robot_name"] == "test_robot_my_pc"
    assert meta["robot_type"] == "test_robot"
    assert meta["operator"] == "李四"
    assert meta["task_name"] == "桌面前移"
    assert meta["frames"] == 110
    assert isinstance(meta["frames"], int)
    assert meta["size_bytes"] == 1056419
    assert isinstance(meta["size_bytes"], int)
    assert meta["duration"] == pytest.approx(3.6409189701080322)
    assert isinstance(meta["duration"], float)
    assert len(meta["sha256"]) == 64
    assert meta["created_at"] == "2026-09-01T16:01:08"
    assert episode["metadata_unknown"] == {}
    assert episode["metadata_content"]["operator"] == "李四"  # 原始 JSON 保留


def test_scan_metadata_unknown_and_type_mismatch_handled(tmp_path):
    """未知字段保留在 metadata_unknown；类型不符 / 缺失字段 → meta 中为 None（不判 invalid）。"""
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_0.json").write_text(
        json.dumps(
            {
                "frames": "not-an-int",  # 类型不符 → None
                "future_field": {"nested": 1},  # schema 未识别 → metadata_unknown
            }
        ),
        encoding="utf-8",
    )

    episode = _session(tmp_path).scan(str(tmp_path))["episodes"][0]

    assert episode["status"] == "ready"  # 类型不符不判 invalid
    assert episode["meta"]["frames"] is None
    assert episode["meta"]["duration"] is None  # 缺失字段补 None
    assert episode["metadata_unknown"] == {"future_field": {"nested": 1}}


# ---- 打包（pack）-------------------------------------------------------------


def _make_episodes(folder, count):
    for index in range(count):
        (folder / f"episode_{index}.mcap").write_bytes(b"mcap")
        (folder / f"episode_{index}.json").write_text("{}", encoding="utf-8")


def _packed_session(tmp_path, count=2):
    _make_episodes(tmp_path, count)
    session = _session(tmp_path)
    session.scan(str(tmp_path))
    session.select([f"episode_{index}" for index in range(count)])
    return session


def test_pack_moves_selected_episodes_to_default_folder(tmp_path):
    """默认包名 pack<选中数量>：选中 episode 的 .mcap + .json **移动**进包目录（源文件不再保留）。"""
    session = _packed_session(tmp_path, 2)
    assert session.status()["suggested_pack_name"] == "pack2"

    result = session.pack()

    assert result["name"] == "pack2"
    assert result["path"] == str(tmp_path / "pack2")
    assert result["episode_count"] == 2
    assert result["episode_ids"] == ["episode_0", "episode_1"]
    assert result["file_count"] == 4
    assert sorted(path.name for path in (tmp_path / "pack2").iterdir()) == [
        "episode_0.json",
        "episode_0.mcap",
        "episode_1.json",
        "episode_1.mcap",
    ]
    # **移动**语义：源目录不再保留（打包是本地整理），重扫看不到已打包的 episode，选择集清空
    for episode_id in ("episode_0", "episode_1"):
        assert not (tmp_path / f"{episode_id}.mcap").exists()
        assert not (tmp_path / f"{episode_id}.json").exists()
    assert result["scan"]["episode_count"] == 0
    assert result["scan"]["selected_episode_ids"] == []
    assert session.status()["suggested_pack_name"] == ""


def test_pack_survives_failed_post_pack_rescan(tmp_path, monkeypatch):
    """pack 已成功（文件已移动、选择集已清空）后重扫失败：降级为 warnings，不把成功报成失败。

    重扫可能失败：扫描目录中途被删（404）、adapter 数据目录掉线使白名单变化（409）。
    此时文件已经不在原位，若回执按失败处理，前端会以为要重试 / 数据丢了。
    """
    session = _packed_session(tmp_path, 2)

    def _boom(folder_path=None):
        raise UploadError("upload folder not found", code=ErrorCode.NOT_FOUND)

    monkeypatch.setattr(session, "_scan", _boom)

    result = session.pack()

    assert result["name"] == "pack2"  # 打包本身仍报成功
    assert result["file_count"] == 4
    assert result["scan"] is None  # 重扫降级：scan 为空
    assert result["warnings"] and "post-pack rescan failed" in result["warnings"][0]
    # 文件确实已移动、选择集已清空（与成功路径一致，只是没有重扫结果）
    assert sorted(path.name for path in (tmp_path / "pack2").iterdir()) == [
        "episode_0.json",
        "episode_0.mcap",
        "episode_1.json",
        "episode_1.mcap",
    ]
    assert session.status()["selected_episode_ids"] == []


def test_pack_only_includes_selected_episodes(tmp_path):
    """只打包选中的：包目录里只有选中的文件，其余 episode 仍在扫描目录里。"""
    _make_episodes(tmp_path, 3)
    session = _session(tmp_path)
    session.scan(str(tmp_path))
    session.select(["episode_1"])

    result = session.pack("my_pack")

    assert result["name"] == "my_pack"
    assert result["episode_ids"] == ["episode_1"]
    assert sorted(path.name for path in (tmp_path / "my_pack").iterdir()) == ["episode_1.json", "episode_1.mcap"]
    # 未选中的仍在扫描目录里（只有选中的被移走）
    assert sorted(episode["episode_id"] for episode in result["scan"]["episodes"]) == ["episode_0", "episode_2"]


def test_pack_rejects_existing_folder_name(tmp_path):
    """重名无法打包：目录已存在 → 409（不覆盖、不合并），改名后成功。"""
    session = _packed_session(tmp_path, 2)
    (tmp_path / "pack2").mkdir()

    with pytest.raises(UploadError, match="already exists") as excinfo:
        session.pack()
    assert excinfo.value.code == ErrorCode.CONFLICT
    assert list((tmp_path / "pack2").iterdir()) == []  # 未写入任何文件

    ok = session.pack("pack2_new")
    assert ok["name"] == "pack2_new"
    assert ok["file_count"] == 4
    assert not (tmp_path / "episode_0.mcap").exists()  # 已移入新包目录


@pytest.mark.parametrize("bad_name", ["", "   ", "a/b", "..", ".hidden", "a\\b", "a:b"])
def test_pack_rejects_unsafe_name(tmp_path, bad_name):
    """非法包名（非单个安全目录名）→ 400，且不建目录、不动源文件。"""
    session = _packed_session(tmp_path, 1)
    with pytest.raises(UploadError) as excinfo:
        session.pack(bad_name)
    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT
    assert (tmp_path / "episode_0.mcap").exists()


def test_pack_requires_scan_and_selection(tmp_path):
    """未扫描 → 409；已扫描但未选择 → 409。"""
    with pytest.raises(UploadError, match="scan a folder") as excinfo:
        _session(tmp_path).pack()
    assert excinfo.value.code == ErrorCode.CONFLICT

    _make_episodes(tmp_path, 1)
    session = _session(tmp_path)
    session.scan(str(tmp_path))
    with pytest.raises(UploadError, match="no episodes selected"):
        session.pack()


def test_pack_rolls_back_when_move_fails(tmp_path, monkeypatch):
    """移动失败 → 回滚（已移动的移回原处 + 删掉空包目录）→ 500；源数据不丢。"""
    session = _packed_session(tmp_path, 2)
    import motrix_edge.session.upload_session as module

    real_move = module.shutil.move
    calls = {"count": 0}

    def flaky_move(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:  # 第二个文件移动失败
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(module.shutil, "move", flaky_move)
    with pytest.raises(UploadError, match="pack failed") as excinfo:
        session.pack()

    assert excinfo.value.code == ErrorCode.INTERNAL
    assert not (tmp_path / "pack2").exists()  # 空包目录已清理
    for episode_id in ("episode_0", "episode_1"):  # 源文件完好（已移动的那个已回滚）
        assert (tmp_path / f"{episode_id}.mcap").exists()
        assert (tmp_path / f"{episode_id}.json").exists()
    assert session.status()["selected_episode_ids"] == ["episode_0", "episode_1"]  # 失败不清选择集


def test_pack_keeps_leftovers_when_rollback_also_fails(tmp_path, monkeypatch):
    """回滚也失败 → **不删数据**：残留文件留在包目录，并在错误里给出路径（500）。

    文件是从原位置**移动**过来的（源位置已不存在），此时删包目录 = 永久丢数据；
    因此宁可留下残留让人来收拾，也不静默删除。
    """
    session = _packed_session(tmp_path, 1)  # 选中 episode_0（.mcap + .json）
    import motrix_edge.session.upload_session as module

    real_move = module.shutil.move
    calls = {"count": 0}

    def failing_move(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:  # 第 1 个文件搬进包目录：成功
            return real_move(src, dst)
        raise OSError("permission denied")  # 第 2 个搬入 + 随后的回滚移回：都失败

    monkeypatch.setattr(module.shutil, "move", failing_move)
    with pytest.raises(UploadError, match="已保留在") as excinfo:
        session.pack()

    assert excinfo.value.code == ErrorCode.INTERNAL
    kept = tmp_path / "pack1" / "episode_0.mcap"
    assert kept.exists()  # 残留保留在包目录里（不删数据）
    assert str(kept) in str(excinfo.value)  # 错误里给出路径（人工可收拾）
    assert not (tmp_path / "episode_0.mcap").exists()  # 源位置已不存在，所以更不能删
    assert (tmp_path / "episode_0.json").exists()  # 未搬成功的那个仍在原处
    assert session.status()["selected_episode_ids"] == ["episode_0"]  # 失败不清选择集


def test_pack_missing_source_file(tmp_path):
    """源文件在打包前被删（选择集仍指向它）→ 404，不建目录。"""
    session = _packed_session(tmp_path, 1)
    (tmp_path / "episode_0.mcap").unlink()

    with pytest.raises(UploadError, match="source files missing") as excinfo:
        session.pack()
    assert excinfo.value.code == ErrorCode.NOT_FOUND
    assert not (tmp_path / "pack1").exists()


def test_scan_rejects_folder_outside_allowed_roots(tmp_path):
    """扫描越界目录（白名单之外）→ 400：不允许扫任意路径。"""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    session = UploadSession({"upload": {"data_dir": str(allowed)}})

    with pytest.raises(UploadError, match="outside the allowed upload roots") as excinfo:
        session.scan(str(outside))
    assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT

    nested = allowed / "sub"  # 子目录在允许范围内（根自身与子目录均可）
    nested.mkdir()
    assert session.scan(str(nested))["folder_path"] == str(nested.resolve())


def test_scan_rejects_when_no_allowed_root(tmp_path):
    """未配置任何允许根（无 upload.data_dir 且无 adapter 数据目录）→ 409，不扫任意路径。"""
    _make_episodes(tmp_path, 1)
    with pytest.raises(UploadError, match="no allowed upload root") as excinfo:
        UploadSession({"upload": {}}).scan(str(tmp_path))  # 既无 upload.data_dir 也无 adapter 目录
    assert excinfo.value.code == ErrorCode.CONFLICT


def test_allowed_roots_list_is_not_mutated(tmp_path):
    """白名单以**共享 list** 传入时，_roots() 不会往调用方的 list 里追加（拷贝后再用）。

    否则每次解析都会在别人的 list 里多插一份 upload.data_dir（无界增长）。
    """
    shared = [str(tmp_path / "allowed")]
    session = UploadSession({"upload": {"data_dir": str(tmp_path / "cfg")}}, allowed_roots=shared)

    for _ in range(2):
        session._roots()

    assert shared == [str(tmp_path / "allowed")]


def test_scan_is_mutually_exclusive(tmp_path):
    """重操作互斥：已有 scan / pack 在跑时再触发 → 409（不重复对整个目录算哈希）。"""
    _make_episodes(tmp_path, 1)
    session = _session(tmp_path)
    released = threading.Event()
    holding = threading.Event()

    def _hold():  # 另一个线程持住重操作锁（模拟并发的 scan / pack）
        session._heavy.acquire()
        holding.set()
        released.wait(timeout=5)
        session._end_heavy()

    worker = threading.Thread(target=_hold)
    worker.start()
    assert holding.wait(timeout=5)
    try:
        with pytest.raises(UploadError, match="already in progress") as excinfo:
            session.scan(str(tmp_path))
        assert excinfo.value.code == ErrorCode.CONFLICT
        with pytest.raises(UploadError, match="already in progress"):
            session.pack()
    finally:
        released.set()
        worker.join(timeout=5)
