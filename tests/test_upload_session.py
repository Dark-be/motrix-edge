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
