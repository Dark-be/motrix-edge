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

"""日志开关测试：``MOTRIX_EDGE_LOG_FILE`` 解析 + uvicorn log_config 的 handler 裁剪。"""

from uvicorn.config import LOGGING_CONFIG

from motrix_edge.utils.data_handler import file_log_enabled, set_log_level
from motrix_edge.utils.logging import uvicorn_log_config


def test_file_log_enabled_reads_env(monkeypatch):
    monkeypatch.delenv("MOTRIX_EDGE_LOG_FILE", raising=False)
    assert file_log_enabled() is False  # 缺省关闭
    monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", "1")
    assert file_log_enabled() is True
    monkeypatch.setenv("MOTRIX_EDGE_LOG_FILE", "false")
    assert file_log_enabled() is False


def test_set_log_level_priority(monkeypatch):
    """级别解析优先级：环境变量 > 传入值（yml 的 ``INFO_LEVEL``）> INFO；非法值 / 空串回退。"""
    monkeypatch.delenv("MOTRIX_EDGE_LOG_LEVEL", raising=False)
    assert set_log_level("DEBUG") == "DEBUG"  # 传入值生效
    assert set_log_level("nope") == "INFO"  # 非法值 → 缺省
    assert set_log_level() == "INFO"  # 未传 → 缺省
    monkeypatch.setenv("MOTRIX_EDGE_LOG_LEVEL", "error")
    assert set_log_level("DEBUG") == "ERROR"  # 环境变量优先（并归一大小写）
    monkeypatch.setenv("MOTRIX_EDGE_LOG_LEVEL", "")
    assert set_log_level("DEBUG") == "DEBUG"  # 空串视为未设
    set_log_level("INFO")  # 复原模块级状态，避免影响其他用例


def test_uvicorn_log_config_default_keeps_error_output():
    """缺省：只静默 HTTP access；uvicorn 启动 / 错误保留默认终端 handler（排障可见）。"""
    cfg = uvicorn_log_config("/tmp/uvicorn.log", file_enabled=False)
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["null"]
    assert cfg["loggers"]["uvicorn"]["handlers"] == ["default"]  # 未被 null 覆盖
    assert "file" not in cfg["handlers"]  # 不写文件
    # deepcopy 生效：不得原地改 uvicorn 默认配置
    assert LOGGING_CONFIG["loggers"]["uvicorn.access"]["handlers"] == ["access"]


def test_uvicorn_log_config_file_enabled():
    """开启：access 只写文件；uvicorn（启动 / 错误）终端 + 文件。"""
    cfg = uvicorn_log_config("/tmp/uvicorn.log", file_enabled=True)
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["file"]
    assert cfg["loggers"]["uvicorn"]["handlers"] == ["default", "file"]
    assert cfg["handlers"]["file"]["filename"] == "/tmp/uvicorn.log"
    assert cfg["handlers"]["file"]["class"] == "logging.handlers.RotatingFileHandler"
