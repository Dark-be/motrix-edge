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

"""uvicorn 日志配置 —— 由 ``MOTRIX_LOG_FILE`` 开关决定是否写文件（缺省关闭）。

开启：HTTP access 写 ``logs/uvicorn.log``、启动 / 错误日志终端 + 文件；关闭：只有 HTTP
access 静默，启动 / 错误日志仍写终端（不写文件）。
与 ``data_handler.debug_print`` 的 ``logs/log_*.txt`` 共用同一开关（纯文本无 ANSI 颜色）。
"""

import copy

from uvicorn.config import LOGGING_CONFIG

from motrix_edge.utils.data_handler import file_log_enabled


def uvicorn_log_config(log_file: str, file_enabled: bool | None = None) -> dict:
    """构建 uvicorn 日志配置（经 ``uvicorn.Config(log_config=...)`` 生效）。

    ``file_enabled`` 缺省读 ``MOTRIX_LOG_FILE``（与 ``debug_print`` 同一开关，**缺省关闭**）：

    - 开启：HTTP access → **只写文件**（``logs/uvicorn.log``，RotatingFileHandler 10MB × 5），
      uvicorn 启动 / 错误 → 终端 + 文件；不刷终端；
    - 关闭（缺省）：HTTP access **丢弃**（NullHandler）——不写文件、不占终端
      （防长期运行刷屏 / 塞满磁盘）；uvicorn 启动 / 错误日志仍走默认终端 handler
      （不写文件）——端口占用 bind 失败、uvicorn 内部异常在终端可见，排障不丢现场。

    注意：不能手动 ``logger.addHandler`` —— uvicorn 启动 ``configure_logging()``
    会 ``dictConfig`` 覆盖已有 handler；必须经 ``log_config`` 传入。
    """
    if file_enabled is None:
        file_enabled = file_log_enabled()
    cfg = copy.deepcopy(LOGGING_CONFIG)
    if not file_enabled:
        # 缺省：只静默 HTTP access（每请求一行，长期运行刷屏 / 塞满磁盘）；
        # uvicorn（启动 / 错误）保留默认终端 handler，不写文件。
        cfg["handlers"]["null"] = {"class": "logging.NullHandler"}
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


__all__ = ["file_log_enabled", "uvicorn_log_config"]
