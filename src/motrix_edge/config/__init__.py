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

"""config 包 —— 配置 / 日志路径解析 + 选择性加载外界配置（包内默认兜底）。

配置来源分层（**环境变量优先，包内默认兜底**）：
  1. 外界配置目录：环境变量 ``MOTRIX_CONFIG_DIR``（可写；同名 yml 覆盖包内默认）；
  2. 包内默认：``src/motrix_edge/config/*.yml``（package data，只读兜底）。

**Edge 不负责数据目录**：采集数据落盘由 adapter / SDK 进程自维护（``data_status`` 上报）；
Edge 只维护**采集配置**（如 capture.yml 的操作员 / 任务元信息），经 ``capture sync`` 传给
adapter。日志 / 可写配置状态遵循 XDG（``XDG_STATE_HOME``），缺省回退 CWD。
本模块在 import 时计算模块级 ``CONFIG_DIR`` / ``LOG_PATH``（环境变量须在进程启动前设置）。
"""

import os
from importlib import resources
from pathlib import Path

import yaml

# 包内默认配置文件（作为 package data 打包；外界配置目录存在同名文件时优先）。
# 通过 ``importlib.resources`` 只读访问，不可写。
DEFAULT_CONFIG_FILES = ("edge.yml", "capture.yml")


def get_config_dir() -> Path | None:
    """外部配置目录（``MOTRIX_CONFIG_DIR``）；未设置 → None（使用包内默认，只读）。"""
    env = os.getenv("MOTRIX_CONFIG_DIR")
    return Path(env).expanduser() if env else None


def get_log_dir() -> Path:
    """日志目录：``XDG_STATE_HOME``/motrix，缺省 ``CWD/logs``。"""
    xdg = os.getenv("XDG_STATE_HOME")
    return Path(xdg).expanduser() / "motrix" if xdg else Path.cwd() / "logs"


def get_state_dir() -> Path:
    """可写配置状态目录（如 capture.yml 元信息）：``XDG_STATE_HOME``/motrix，缺省 ``CWD``。

    Edge 不负责数据目录，只维护采集配置（元信息）；无可写外界配置目录时，配置状态落到这里。
    """
    xdg = os.getenv("XDG_STATE_HOME")
    return Path(xdg).expanduser() / "motrix" if xdg else Path.cwd()


def config_path(name: str) -> Path | None:
    """配置文件的真实路径：外部配置目录 → ``{config_dir}/{name}``；否则 None（读包内默认）。"""
    ext = get_config_dir()
    return ext / name if ext is not None else None


def writable_config_path(name: str) -> Path:
    """可写配置路径（写操作用）：外部配置目录优先，否则落到状态目录（包内默认只读）。"""
    ext = get_config_dir()
    return ext / name if ext is not None else get_state_dir() / name


def load_config(name: str) -> dict:
    """加载配置：外部配置文件优先，否则读包内默认 yml（只读兜底）；缺失 → {}。"""
    path = config_path(name)
    if path is not None and path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    if name in DEFAULT_CONFIG_FILES:
        text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
        return yaml.safe_load(text) or {}
    return {}


# 对外暴露（模块级：环境 / XDG 已定的路径；无外界配置时 CONFIG_DIR 为 None = 用包内默认）
CONFIG_DIR = get_config_dir()
LOG_PATH = get_log_dir()

__all__ = [
    "DEFAULT_CONFIG_FILES",
    "get_config_dir",
    "get_log_dir",
    "get_state_dir",
    "config_path",
    "writable_config_path",
    "load_config",
    "CONFIG_DIR",
    "LOG_PATH",
]
