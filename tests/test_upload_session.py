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

"""UploadSession 单元测试：目录扫描、episode 配对、选择和队列状态。"""

import json

import pytest

from motrix_edge.session import UploadError, UploadSession


def test_scan_pairs_episode_files_and_reads_metadata(tmp_path):
    (tmp_path / "episode_10.mcap").write_bytes(b"mcap-10")
    (tmp_path / "episode_10.json").write_text(
        json.dumps({"collector": "operator-1", "task_name": "pick"}), encoding="utf-8"
    )
    (tmp_path / "episode_2.mcap").write_bytes(b"mcap-2")
    (tmp_path / "episode_2.json").write_text(json.dumps({"duration_s": 12.5}), encoding="utf-8")

    status = UploadSession().scan(str(tmp_path))

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

    status = UploadSession().scan(str(tmp_path))

    assert status["invalid_count"] == 3
    by_id = {episode["episode_id"]: episode for episode in status["episodes"]}
    assert "missing .json metadata file" in by_id["episode_0"]["errors"]
    assert by_id["episode_1"]["metadata_content"] is None
    assert "missing .mcap file" in by_id["episode_1"]["errors"]
    assert "metadata JSON root must be an object" in by_id["episode_2"]["errors"]


def test_select_replaces_episode_selection_and_enqueue_requires_endpoint(tmp_path):
    for episode_id in ("episode_0", "episode_1"):
        (tmp_path / f"{episode_id}.mcap").write_bytes(b"mcap")
        (tmp_path / f"{episode_id}.json").write_text("{}", encoding="utf-8")

    session = UploadSession()
    session.scan(str(tmp_path))
    uploader = UploadSession({"upload": {"endpoint": "https://upload.example.test"}})
    uploader.scan(str(tmp_path))
    selected = uploader.select(["episode_1"])
    assert selected["selected_episode_ids"] == ["episode_1"]

    queued = uploader.enqueue()
    assert queued["episodes"][1]["status"] == "pending"

    with pytest.raises(UploadError, match="endpoint is not configured"):
        session.select(["episode_0"])
        session.enqueue()


def test_select_rejects_unknown_or_invalid_episode(tmp_path):
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    session = UploadSession()
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

    episode = UploadSession().scan(str(tmp_path))["episodes"][0]

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

    episode = UploadSession().scan(str(tmp_path))["episodes"][0]

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
    session = UploadSession()
    session.scan(str(tmp_path))
    session.select([f"episode_{index}" for index in range(count)])
    return session


def test_pack_moves_selected_episodes_to_default_folder(tmp_path):
    """默认包名 pack<选中数量>：选中 episode 的 .mcap + .json **移动**进包目录，并自动重扫。"""
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
    # 源文件已移走（移动而非复制）→ 重扫后列表为空、选择集清空
    assert not (tmp_path / "episode_0.mcap").exists()
    assert result["scan"]["episode_count"] == 0
    assert result["scan"]["selected_episode_ids"] == []
    assert session.status()["suggested_pack_name"] == ""


def test_pack_keeps_unselected_episodes(tmp_path):
    """只打包选中的：未选中的 episode 留在扫描目录里。"""
    _make_episodes(tmp_path, 3)
    session = UploadSession()
    session.scan(str(tmp_path))
    session.select(["episode_1"])

    result = session.pack("my_pack")

    assert result["name"] == "my_pack"
    assert result["episode_ids"] == ["episode_1"]
    assert result["scan"]["episode_count"] == 2
    assert sorted(episode["episode_id"] for episode in result["scan"]["episodes"]) == ["episode_0", "episode_2"]


def test_pack_rejects_existing_folder_name(tmp_path):
    """重名无法打包：目录已存在 → 409（不覆盖、不合并），改名后成功。"""
    session = _packed_session(tmp_path, 2)
    (tmp_path / "pack2").mkdir()

    with pytest.raises(UploadError, match="already exists") as excinfo:
        session.pack()
    assert excinfo.value.status_code == 409
    assert (tmp_path / "episode_0.mcap").exists()  # 没有动源文件

    ok = session.pack("pack2_new")
    assert ok["name"] == "pack2_new"
    assert ok["scan"]["episode_count"] == 0


@pytest.mark.parametrize("bad_name", ["", "   ", "a/b", "..", ".hidden", "a\\b", "a:b"])
def test_pack_rejects_unsafe_name(tmp_path, bad_name):
    """非法包名（非单个安全目录名）→ 400，且不建目录、不动源文件。"""
    session = _packed_session(tmp_path, 1)
    with pytest.raises(UploadError) as excinfo:
        session.pack(bad_name)
    assert excinfo.value.status_code == 400
    assert (tmp_path / "episode_0.mcap").exists()


def test_pack_requires_scan_and_selection(tmp_path):
    """未扫描 → 409；已扫描但未选择 → 409。"""
    with pytest.raises(UploadError, match="scan a folder") as excinfo:
        UploadSession().pack()
    assert excinfo.value.status_code == 409

    _make_episodes(tmp_path, 1)
    session = UploadSession()
    session.scan(str(tmp_path))
    with pytest.raises(UploadError, match="no episodes selected"):
        session.pack()


def test_pack_rolls_back_when_move_fails(tmp_path, monkeypatch):
    """移动失败 → 回滚（已移动的移回原处、删除空目录）→ 500，不留半成品包。"""
    session = _packed_session(tmp_path, 2)
    import motrix_edge.session.upload_session as module

    real_move = module.shutil.move
    calls = {"count": 0}

    def flaky_move(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:  # 第二个文件移动失败
            raise OSError("disk error")
        return real_move(src, dst)

    monkeypatch.setattr(module.shutil, "move", flaky_move)
    with pytest.raises(UploadError, match="pack failed") as excinfo:
        session.pack()

    assert excinfo.value.status_code == 500
    assert not (tmp_path / "pack2").exists()  # 半成品目录已清理
    assert (tmp_path / "episode_0.mcap").exists()  # 已移动的文件移回原处
    assert (tmp_path / "episode_0.json").exists()
    assert session.status()["selected_episode_ids"] == ["episode_0", "episode_1"]  # 选择集保留（可重试）


def test_pack_missing_source_file(tmp_path):
    """源文件在打包前被删（选择集仍指向它）→ 404，不建目录。"""
    session = _packed_session(tmp_path, 1)
    (tmp_path / "episode_0.mcap").unlink()

    with pytest.raises(UploadError, match="source files missing") as excinfo:
        session.pack()
    assert excinfo.value.status_code == 404
    assert not (tmp_path / "pack1").exists()
