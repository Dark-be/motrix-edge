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

import datetime
import os

from motrix_edge.config import LOG_PATH

# 环境变量名（单点定义：与 robot-pipeline 侧同名，读取方 / 写入方共用，避免字面量散落
# 漂移）。产品变量统一 ``MOTRIX_EDGE_`` 前缀，隔离同机其他 Motrix 产品。
ENV_LOG_FILE = "MOTRIX_EDGE_LOG_FILE"
ENV_LOG_LEVEL = "MOTRIX_EDGE_LOG_LEVEL"

# 日志级别（与 robot-pipeline 侧同一套语义）：``_LOG_LEVEL`` 由 ``set_log_level`` 在启动时
# 解析一次，``debug_print`` 只读它——**不借 ``os.environ`` 传递配置**（否则会继承给子进程、
# 也可能被第三方库读到）。
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_LOG_LEVEL: str = DEFAULT_LOG_LEVEL

# 进程内缓存日志文件路径：首次 debug_print 时确定（含时间戳），之后固定复用——
# 避免每次写日志都重新 makedirs + 生成新文件名（旧实现跨秒产生海量日志文件）。
_LOG_FILE: str | None = None


def file_log_enabled() -> bool:
    """文件日志开关（环境变量 ``MOTRIX_EDGE_LOG_FILE``，**缺省关闭**，防长期运行塞满磁盘）。

    单点定义：``debug_print``（``logs/log_*.txt``）与 uvicorn 日志
    （``utils/logging.uvicorn_log_config``，``logs/uvicorn.log``）共用本函数，
    避免同一个开关在两处各判一次（口径漂移 / 求值时机不同）。终端打印不受影响。
    """
    return os.getenv(ENV_LOG_FILE, "0").strip().lower() not in ("0", "false", "no")


def set_log_level(configured: str | None = None) -> str:
    """解析并设置进程内日志级别，返回生效值（启动时调用一次）。

    优先级：环境变量 ``MOTRIX_EDGE_LOG_LEVEL``（临时调试用）> ``configured``（``edge.yml``
    的 ``INFO_LEVEL``）> ``"INFO"``。**只写模块级 ``_LOG_LEVEL``，不写 ``os.environ``**：
    配置不该借进程环境传递（会继承给子进程、也可能被其他库读到）。
    """
    global _LOG_LEVEL
    level = (os.getenv(ENV_LOG_LEVEL) or configured or DEFAULT_LOG_LEVEL).upper()
    _LOG_LEVEL = level if level in LOG_LEVELS else DEFAULT_LOG_LEVEL
    return _LOG_LEVEL


def _get_log_file() -> str:
    global _LOG_FILE
    if _LOG_FILE is None:
        os.makedirs(LOG_PATH, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        _LOG_FILE = os.path.join(LOG_PATH, f"log_{timestamp}.txt")
    return _LOG_FILE


def debug_print(name, info, level="INFO", end="\n", flush=True):
    if level.upper() not in LOG_LEVELS:
        debug_print("DEBUG_PRINT", f"level setting error : {level}", "ERROR")
        return
    if LOG_LEVELS[level.upper()] < LOG_LEVELS.get(_LOG_LEVEL, 20):
        return

    colors = {
        "DEBUG": "\033[94m",  # blue
        "INFO": "\033[92m",  # green
        "WARNING": "\033[93m",  # yellow
        "ERROR": "\033[91m",  # red
        "ENDC": "\033[0m",
    }
    color = colors.get(level.upper(), "")
    endc = colors["ENDC"]
    msg = f"[{level}][{name}] {info}"
    print(f"{color}{msg}{endc}", end=end, flush=flush)

    # 写入日志文件（INFO 及以上；MOTRIX_EDGE_LOG_FILE=0 关闭文件写入）
    if LOG_LEVELS[level.upper()] >= 20 and file_log_enabled():
        log_file_path = _get_log_file()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}]{msg}\n")
        except Exception as e:
            print(f"\033[91m[ERROR][DEBUG_PRINT] Failed to write log to file: {e}\033[0m")
