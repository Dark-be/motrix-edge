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


def test_load_config_falls_back_to_packaged_default(monkeypatch):
    """未设置 MOTRIX_CONFIG_DIR：load_config 读包内默认 yml（只读兜底）。"""
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    cfg = load_config("edge.yml")
    assert cfg["INFO_LEVEL"] == "INFO"
    assert cfg["adapter"]["host"] == "127.0.0.1"
    assert cfg["policy"]["type"] == "act"


def test_load_config_unknown_name_returns_empty(monkeypatch):
    monkeypatch.delenv("MOTRIX_CONFIG_DIR", raising=False)
    assert load_config("no_such.yml") == {}


def test_load_config_prefers_external_dir(monkeypatch, tmp_path):
    """设置 MOTRIX_CONFIG_DIR：同名 yml 优先（覆盖包内默认）；缺失文件回退包内默认。"""
    (tmp_path / "edge.yml").write_text("discover:\n  host: external-host\n", encoding="utf-8")
    monkeypatch.setenv("MOTRIX_CONFIG_DIR", str(tmp_path))
    assert get_config_dir() == tmp_path
    assert config_path("edge.yml") == tmp_path / "edge.yml"
    cfg = load_config("edge.yml")
    assert cfg["discover"]["host"] == "external-host"


def test_writable_config_path_state_dir(monkeypatch, tmp_path):
    """无外界配置目录：writable_config_path 落到状态目录（XDG_STATE_HOME/motrix）。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert get_state_dir() == tmp_path / "motrix"
    assert get_log_dir() == tmp_path / "motrix"
    assert writable_config_path("edge.yml") == tmp_path / "motrix" / "edge.yml"
