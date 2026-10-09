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

"""config 包测试：一个根目录管 config + logs（``MOTRIX_EDGE_DIR`` / ``<cwd>`` 兜底）+ 示例播种。"""

import yaml

from motrix_edge.config import (
    DEFAULT_CONFIG_FILES,
    config_path,
    get_config_dir,
    get_log_dir,
    get_root_dir,
    load_config,
    packaged_config_text,
    seed_config,
    writable_config_path,
)


def test_packaged_defaults_are_listed():
    assert "edge.yml" in DEFAULT_CONFIG_FILES


def test_root_prefers_env_var(monkeypatch, tmp_path):
    """``MOTRIX_EDGE_DIR`` 指定根：配置在 ``<根>/config``、日志在 ``<根>/logs``。"""
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    assert get_root_dir() == tmp_path
    assert get_config_dir() == tmp_path / "config"
    assert get_log_dir() == tmp_path / "logs"
    assert config_path("edge.yml") == tmp_path / "config" / "edge.yml"
    assert writable_config_path("capture.yml") == tmp_path / "config" / "capture.yml"


def test_root_falls_back_under_cwd(monkeypatch, tmp_path):
    """未设环境变量：根 = ``<cwd>/motrix-edge``，配置 / 日志都在它下面。"""
    monkeypatch.delenv("MOTRIX_EDGE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert get_root_dir() == tmp_path / "motrix-edge"
    assert get_config_dir() == tmp_path / "motrix-edge" / "config"
    assert get_log_dir() == tmp_path / "motrix-edge" / "logs"


def test_load_config_seeds_packaged_example(monkeypatch, tmp_path):
    """首次读配置：把包内示例**播种**到 ``<根>/config``，内容与示例一致。"""
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    cfg = load_config("edge.yml")
    seeded = tmp_path / "config" / "edge.yml"

    assert seeded.exists()
    assert seeded.read_text(encoding="utf-8") == packaged_config_text("edge.yml")
    assert cfg["INFO_LEVEL"] == "INFO"
    assert cfg["policy"]["type"] == "lerobot-act"


def test_load_config_reads_local_copy_after_edit(monkeypatch, tmp_path):
    """播种是一次性：改本地副本后以副本为准（包内示例的后续更新不再自动生效）。"""
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    local = config_path("edge.yml")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("discover:\n  host: local-host\n", encoding="utf-8")

    assert load_config("edge.yml")["discover"]["host"] == "local-host"


def test_seed_config_is_idempotent(monkeypatch, tmp_path):
    """已存在的本地副本不会被播种覆盖（现场改过的配置不会被包内示例冲掉）。"""
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    path = config_path("capture.yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("meta:\n  operator: [me]\n", encoding="utf-8")

    assert seed_config("capture.yml") == path
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["meta"]["operator"] == ["me"]


def test_load_config_unknown_name_returns_empty(monkeypatch, tmp_path):
    """包内没有的配置名 → 空字典（按缺省处理），不报错、也不播种。"""
    monkeypatch.setenv("MOTRIX_EDGE_DIR", str(tmp_path))
    assert load_config("no_such.yml") == {}
    assert not config_path("no_such.yml").exists()
