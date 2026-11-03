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

"""robot-pipeline 日志级别、文件日志开关与 uvicorn 配置测试。"""

from server import contract_server
from utils import data_handler
from utils.logging import uvicorn_log_config
from uvicorn.config import LOGGING_CONFIG


def test_file_log_enabled_reads_shared_env(monkeypatch):
    monkeypatch.delenv("MOTRIX_EDGE_LOG_FILE", raising=False)
    assert data_handler.file_log_enabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", value)
        assert data_handler.file_log_enabled() is True
    for value in ("0", "false", "no", "", "unexpected"):
        monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", value)
        assert data_handler.file_log_enabled() is False


def test_set_log_level_uses_config_only(monkeypatch):
    monkeypatch.setenv("INFO_LEVEL", "ERROR")
    assert data_handler.set_log_level("DEBUG") == "DEBUG"
    assert data_handler.set_log_level("invalid") == "INFO"
    data_handler.set_log_level("INFO")


def test_debug_print_writes_to_robot_runtime_root(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", "1")
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))
    monkeypatch.setattr(data_handler, "_LOG_FILE", None)

    data_handler.debug_print("TEST", "persisted", "INFO")

    log_files = list((tmp_path / "logs").glob("log_*.txt"))
    assert len(log_files) == 1
    assert "[INFO][TEST] persisted" in log_files[0].read_text(encoding="utf-8")


def test_uvicorn_log_config_default_keeps_error_output():
    cfg = uvicorn_log_config("/tmp/uvicorn.log", file_enabled=False)
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["null"]
    assert cfg["loggers"]["uvicorn"]["handlers"] == ["default"]
    assert "file" not in cfg["handlers"]
    assert LOGGING_CONFIG["loggers"]["uvicorn.access"]["handlers"] == ["access"]


def test_uvicorn_log_config_file_enabled():
    cfg = uvicorn_log_config("/tmp/uvicorn.log", file_enabled=True)
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["file"]
    assert cfg["loggers"]["uvicorn"]["handlers"] == ["default", "file"]
    assert cfg["handlers"]["file"]["filename"] == "/tmp/uvicorn.log"
    assert cfg["handlers"]["file"]["class"] == "logging.handlers.RotatingFileHandler"


def test_serve_wires_uvicorn_to_robot_runtime_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", "1")
    monkeypatch.setenv("MOTRIX_ROBOT_PIPELINE_DIR", str(tmp_path))
    monkeypatch.setattr(data_handler, "_LOG_FILE", None)
    called = {}

    def fake_run(app, **kwargs):
        called["app"] = app
        called.update(kwargs)

    monkeypatch.setattr(contract_server.uvicorn, "run", fake_run)
    app = object()
    contract_server.serve(app, host="127.0.0.1", port=8123)

    assert called["app"] is app
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 8123
    assert called["log_config"]["handlers"]["file"]["filename"] == str(tmp_path / "logs" / "uvicorn.log")
