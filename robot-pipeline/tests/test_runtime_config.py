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
