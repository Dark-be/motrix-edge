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

# 进程内缓存日志文件路径：首次 debug_print 时确定（含时间戳），之后固定复用——
# 避免每次写日志都重新 makedirs + 生成新文件名（旧实现跨秒产生海量日志文件）。
_LOG_FILE: str | None = None


def file_log_enabled() -> bool:
    """文件日志开关（环境变量 ``MOTRIX_LOG_FILE``，**缺省关闭**，防长期运行塞满磁盘）。

    单点定义：``debug_print``（``logs/log_*.txt``）与 uvicorn 日志
    （``utils/logging.uvicorn_log_config``，``logs/uvicorn.log``）共用本函数，
    避免同一个开关在两处各判一次（口径漂移 / 求值时机不同）。终端打印不受影响。
    """
    return os.getenv("MOTRIX_LOG_FILE", "0").strip().lower() not in ("0", "false", "no")


def _get_log_file() -> str:
    global _LOG_FILE
    if _LOG_FILE is None:
        os.makedirs(LOG_PATH, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        _LOG_FILE = os.path.join(LOG_PATH, f"log_{timestamp}.txt")
    return _LOG_FILE


def debug_print(name, info, level="INFO", end="\n", flush=True):
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
    if level not in levels.keys():
        debug_print("DEBUG_PRINT", f"level setting error : {level}", "ERROR")
        return
    env_level = os.getenv("INFO_LEVEL", "INFO").upper()
    env_level_value = levels.get(env_level, 20)

    msg_level_value = levels.get(level.upper(), 20)

    if msg_level_value < env_level_value:
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

    # 写入日志文件（INFO 及以上；MOTRIX_LOG_FILE=0 关闭文件写入）
    if msg_level_value >= 20 and file_log_enabled():
        log_file_path = _get_log_file()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        try:
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}]{msg}\n")
        except Exception as e:
            print(f"\033[91m[ERROR][DEBUG_PRINT] Failed to write log to file: {e}\033[0m")
