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

"""robot-pipeline 运行根目录与配置回退测试。"""

import pytest
from config import config_path, get_config_dir, get_log_dir, get_root_dir, load_config


def test_runtime_paths_prefer_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))
    assert get_root_dir() == tmp_path
    assert get_config_dir() == tmp_path / "config"
    assert get_log_dir() == tmp_path / "logs"
    assert config_path("test_robot.yml") == tmp_path / "config" / "test_robot.yml"


def test_runtime_paths_default_under_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("MOTRIX_ROBOT_PIPELINE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert get_root_dir() == tmp_path / "motrix-robot-pipeline"


def test_load_config_falls_back_without_writing(monkeypatch, tmp_path):
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(runtime_dir))
    assert load_config("test_robot.yml")["robot"]["type"] == "test_robot"
    assert not runtime_dir.exists()


def test_load_config_prefers_runtime_config(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "test_robot.yml").write_text("robot:\n  type: local_robot\n", encoding="utf-8")
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))
    assert load_config("test_robot.yml")["robot"]["type"] == "local_robot"


def test_load_config_inherits_before_machine_profile(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    profile_dir = config_dir / "robot"
    profile_dir.mkdir(parents=True)
    (config_dir / "parent.yml").write_text(
        "robot:\n  type: parent\n  ports: {left: parent-left, right: parent-right}\nitems: [parent]\n",
        encoding="utf-8",
    )
    (config_dir / "child.yml").write_text(
        "extends: parent.yml\nrobot:\n  type: child\n  ports: {right: child-right}\nitems: [child]\n",
        encoding="utf-8",
    )
    (profile_dir / "machine.yml").write_text(
        "robot:\n  ports: {left: machine-left}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))

    config = load_config("child", machine="machine")

    assert config["robot"] == {
        "type": "child",
        "ports": {"left": "machine-left", "right": "child-right"},
    }
    assert config["items"] == ["child"]


def test_load_config_rejects_missing_invalid_and_cyclic_inheritance(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))

    with pytest.raises(FileNotFoundError, match="找不到配置 missing.yml"):
        load_config("missing")

    (config_dir / "invalid.yml").write_text("extends: ../parent.yml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="extends 必须是配置文件名"):
        load_config("invalid")

    (config_dir / "a.yml").write_text("extends: b.yml\n", encoding="utf-8")
    (config_dir / "b.yml").write_text("extends: a.yml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="a.yml -> b.yml -> a.yml"):
        load_config("a")


def test_load_config_rejects_non_mapping(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "list.yml").write_text("- not\n- a mapping\n", encoding="utf-8")
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="必须是 YAML 映射"):
        load_config("list")
