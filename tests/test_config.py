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

"""config 包测试：选择性加载外界配置（MOTRIX_CONFIG_DIR / XDG）+ 包内默认兜底。"""

from motrix_edge.config import (
    DEFAULT_CONFIG_FILES,
    config_path,
    get_config_dir,
    get_log_dir,
    get_state_dir,
    load_config,
    writable_config_path,
)


def test_packaged_defaults_are_listed():
    assert "edge.yml" in DEFAULT_CONFIG_FILES
    assert "capture.yml" in DEFAULT_CONFIG_FILES


def test_load_config_falls_back_to_packaged_default(monkeypatch):
    """未设置 MOTRIX_CONFIG_DIR：load_config 读包内默认 yml（只读兜底）。"""
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    cfg = load_config("edge.yml")
    assert cfg["identity"]["edge_id"] == "edge-test-001"
    assert cfg["adapter"]["host"] == "127.0.0.1"
    # 启用臂 / 相机 / home_qpos 为运行时配置（命令 / 前端），不在 edge.yml 静态配置
    assert "enabled_arms" not in cfg["adapter"]
    meta = load_config("capture.yml")
    assert "operator" in meta["meta"]


def test_load_config_unknown_name_returns_empty(monkeypatch):
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    assert load_config("no_such.yml") == {}


def test_load_config_prefers_external_dir(monkeypatch, tmp_path):
    """设置 MOTRIX_CONFIG_DIR：同名 yml 优先（覆盖包内默认）；缺失文件回退包内默认。"""
    (tmp_path / "edge.yml").write_text("identity:\n  edge_id: external-001\n", encoding="utf-8")
    monkeypatch.setenv("MOTRIX_CONFIG_DIR", str(tmp_path))
    assert get_config_dir() == tmp_path
    assert config_path("edge.yml") == tmp_path / "edge.yml"
    cfg = load_config("edge.yml")
    assert cfg["identity"]["edge_id"] == "external-001"
    # 外部目录不存在 capture.yml → 回退包内默认
    assert load_config("capture.yml")["meta"]["operator"]


def test_writable_config_path_external_preferred(monkeypatch, tmp_path):
    """写操作路径：外部配置目录优先。"""
    monkeypatch.setenv("MOTRIX_CONFIG_DIR", str(tmp_path))
    assert writable_config_path("capture.yml") == tmp_path / "capture.yml"


def test_writable_config_path_falls_back_to_state_dir(monkeypatch, tmp_path):
    """写操作路径：无外界配置目录 → 状态目录（包内默认只读；Edge 不负责数据目录）。"""
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert writable_config_path("capture.yml") == tmp_path / "xdg" / "motrix" / "capture.yml"


def test_state_log_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert get_state_dir() == tmp_path / "xdg-state" / "motrix"
    assert get_log_dir() == tmp_path / "xdg-state" / "motrix"


def test_capture_meta_store_seeds_packaged_default(monkeypatch, tmp_path):
    """缺省路径：无外界配置目录时把包内默认播种到状态目录可写位置。"""
    from motrix_edge.utils.capture_meta import CaptureMetaStore

    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    store = CaptureMetaStore()  # 缺省路径
    assert store.path == tmp_path / "xdg" / "motrix" / "capture.yml"
    assert store.list_meta()["operator"]  # 已播种包内默认


def test_capture_meta_store_explicit_path_no_seed(tmp_path):
    """显式路径：不播种（测试注入临时路径）。"""
    from motrix_edge.utils.capture_meta import CaptureMetaStore

    store = CaptureMetaStore(tmp_path / "capture.yml")
    assert store.list_meta() == {}
