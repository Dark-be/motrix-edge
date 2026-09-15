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


def uvicorn_log_config(log_file: str, file_enabled: bool = True) -> dict:
    """构建 uvicorn 日志配置（经 ``uvicorn.Config(log_config=...)`` 生效）。

    - ``file_enabled=True``：HTTP access → **只写文件**（``logs/uvicorn.log``，RotatingFileHandler
      10MB × 5），uvicorn 启动 / 错误 → 终端 + 文件；不刷终端；
    - ``file_enabled=False``（**默认**，``MOTRIX_LOG_FILE`` 未设 / =0）：uvicorn 日志**全部丢弃**
      （NullHandler）——不写文件、不占终端（防长期运行塞满磁盘 / 刷屏）。

    注意：不能手动 ``logger.addHandler`` —— uvicorn 启动 ``configure_logging()``
    会 ``dictConfig`` 覆盖已有 handler；必须经 ``log_config`` 传入。
    """
    cfg = copy.deepcopy(LOGGING_CONFIG)
    if not file_enabled:
        # 默认：uvicorn 日志静默（丢弃，不写文件、不占终端）；MOTRIX_LOG_FILE=1 恢复
        cfg["handlers"]["null"] = {"class": "logging.NullHandler"}
        cfg["loggers"]["uvicorn"]["handlers"] = ["null"]
        cfg["loggers"]["uvicorn.access"]["handlers"] = ["null"]
        return cfg
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
