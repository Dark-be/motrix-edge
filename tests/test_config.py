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

"""config 包测试：运行根目录契约与包内默认回退。"""

from motrix_edge.config import (
    DEFAULT_CONFIG_FILES,
    config_path,
    get_config_dir,
    get_log_dir,
    get_root_dir,
    load_config,
    writable_config_path,
)


def test_packaged_defaults_are_listed():
    assert "edge.yml" in DEFAULT_CONFIG_FILES


def test_root_dir_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    assert get_root_dir() == tmp_path
    assert get_config_dir() == tmp_path / "config"
    assert get_log_dir() == tmp_path / "logs"
    assert config_path("edge.yml") == tmp_path / "config" / "edge.yml"


def test_root_dir_defaults_under_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MOTRIX_EDGE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert get_root_dir() == tmp_path / "motrix-edge"
    assert get_config_dir() == tmp_path / "motrix-edge" / "config"
    assert get_log_dir() == tmp_path / "motrix-edge" / "logs"


def test_load_config_falls_back_without_writing(monkeypatch, tmp_path):
    """现场配置缺失时读取包内默认，普通读取不创建运行目录。"""
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(runtime_dir))
    cfg = load_config("edge.yml")
    assert cfg["INFO_LEVEL"] == "INFO"
    assert cfg["adapter"]["host"] == "127.0.0.1"
    assert cfg["policy"]["type"] == "lerobot-act"
    assert not runtime_dir.exists()


def test_load_config_unknown_name_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    assert load_config("no_such.yml") == {}


def test_load_config_prefers_runtime_config(monkeypatch, tmp_path):
    """``<root>/config`` 中的同名文件覆盖包内默认。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "edge.yml").write_text("discover:\n  host: local-host\n", encoding="utf-8")
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    cfg = load_config("edge.yml")
    assert cfg["discover"]["host"] == "local-host"


def test_writable_config_path_uses_runtime_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    assert writable_config_path("capture.yml") == tmp_path / "config" / "capture.yml"
