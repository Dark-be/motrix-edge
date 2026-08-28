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

"""uvicorn 日志配置 —— HTTP access 写文件、启动 / 错误日志终端 + 文件。

与 ``data_handler.debug_print`` 的 ``logs/log_*.txt`` 分开（纯文本无 ANSI 颜色）。
"""

import copy

from uvicorn.config import LOGGING_CONFIG


def uvicorn_log_config(log_file: str) -> dict:
    """构建 uvicorn 日志配置（经 ``uvicorn.Config(log_config=...)`` 生效）。

    - HTTP access（每请求一行，如 OPTIONS/POST）→ **只写文件**，不再刷终端；
    - uvicorn / uvicorn.error（启动 / 关闭 / 错误）→ 终端 + 文件；
    - 文件为 ``logs/uvicorn.log``（RotatingFileHandler，10MB × 5），与
      ``debug_print`` 的 ``logs/log_*.txt`` 分开，纯文本无 ANSI 颜色。

    注意：不能手动 ``logger.addHandler`` —— uvicorn 启动 ``configure_logging()``
    会 ``dictConfig`` 覆盖已有 handler；必须经 ``log_config`` 传入。
    """
    cfg = copy.deepcopy(LOGGING_CONFIG)
    # 纯文本文件 formatter（默认 formatter 带 ANSI 颜色，不适合文件）
    cfg["formatters"]["file"] = {
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "datefmt": "%Y-%m-%d %H:%M:%S",
    }
    cfg["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": log_file,
        "maxBytes": 10 * 1024 * 1024,  # 10MB
        "backupCount": 5,
        "encoding": "utf-8",
        "formatter": "file",
    }
    # access 日志（每请求一行）只写文件（默认 propagate=False，不会漏到终端）
    cfg["loggers"]["uvicorn.access"]["handlers"] = ["file"]
    # error / 启动日志：终端 + 文件（uvicorn.error 无 handlers，经 propagate 到 uvicorn）
    handlers = cfg["loggers"]["uvicorn"]["handlers"]
    if "file" not in handlers:
        handlers.append("file")
    return cfg


__all__ = ["uvicorn_log_config"]
