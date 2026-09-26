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

"""config 包 —— robot server 配置加载（包内默认兜底，外部目录优先）。

配置来源分层（环境变量优先，包内默认兜底，与 motrix_edge config 同机制）：
  1. 外界配置目录：环境变量 ``MOTRIX_EDGE_CONFIG_DIR``（可写；同名 yml 覆盖包内默认）；
  2. 包内默认：``src/config/*.yml``（package data，只读兜底，按 robot.type 选择）。

状态 / 日志目录遵循 XDG（``XDG_STATE_HOME``）：本项目产物收在
``$XDG_STATE_HOME/motrix-robot-pipeline/``（日志在 ``logs/`` 子目录）；未设 XDG 时收在
``<cwd>/motrix-robot-pipeline/``——与 motrix_edge 的 ``motrix-edge/`` 并列且互不干扰。
本模块在 import 时计算模块级 ``CONFIG_DIR`` / ``LOG_PATH``。
"""

import os
from importlib import resources
from pathlib import Path

import yaml

# 环境变量名（单点定义：与 motrix_edge 侧同名，保证同一台机器 / 同一容器内两个子项目
# 认同一套变量）。产品变量统一 ``MOTRIX_EDGE_`` 前缀；``XDG_STATE_HOME`` 属 XDG 规范不加前缀。
ENV_CONFIG_DIR = "MOTRIX_EDGE_CONFIG_DIR"
ENV_STATE_HOME = "XDG_STATE_HOME"

# robot server 配置文件（package data；外界配置目录存在同名文件时优先）
DEFAULT_CONFIG_FILES = (
    "test_robot.yml",
    "dual_piper.yml",
    "dual_alicia_piper.yml",
    "single_piper.yml",
)


def get_config_dir() -> Path | None:
    """外部配置目录（``MOTRIX_EDGE_CONFIG_DIR``）；未设置 → None（使用包内默认，只读）。"""
    env = os.getenv(ENV_CONFIG_DIR)
    return Path(env).expanduser() if env else None


# 无 XDG 时的状态目录名（``<cwd>/<STATE_DIR_NAME>``）。加 ``motrix-`` 前缀而不是直接叫
# ``robot-pipeline``：避免与仓库里的 ``robot-pipeline/`` 源码目录同名——从仓库根启动时
# 日志会被写进源码目录。与 motrix_edge 的 ``motrix-edge/`` 并列且互不干扰。
STATE_DIR_NAME = "motrix-robot-pipeline"


def _state_root() -> Path:
    """状态根：``$XDG_STATE_HOME``；未设 → ``<cwd>``。"""
    xdg = os.getenv(ENV_STATE_HOME)
    return Path(xdg).expanduser() if xdg else Path.cwd()


def get_state_dir() -> Path:
    """可写状态目录：``$XDG_STATE_HOME/motrix-robot-pipeline``；未设 → ``<cwd>/motrix-robot-pipeline``。"""
    return _state_root() / STATE_DIR_NAME


def get_log_dir() -> Path:
    """日志目录：状态目录下的 ``logs/``（设 / 未设 XDG 两个分支结构一致）。"""
    return get_state_dir() / "logs"


def config_path(name: str) -> Path | None:
    """配置文件的真实路径：外部配置目录 → ``{config_dir}/{name}``；否则 None（读包内默认）。"""
    ext = get_config_dir()
    return ext / name if ext is not None else None


def load_config(name: str) -> dict:
    """加载 robot server 配置：外部配置目录优先，否则读包内默认 yml（只读兜底）；缺失 → {}。"""
    path = config_path(name)
    if path is not None and path.exists():
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    if name in DEFAULT_CONFIG_FILES:
        text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
        return yaml.safe_load(text) or {}
    return {}


# 对外暴露（模块级：环境已定的路径；无外界配置时 CONFIG_DIR 为 None = 用包内默认）
CONFIG_DIR = get_config_dir()
LOG_PATH = get_log_dir()

__all__ = [
    "ENV_CONFIG_DIR",
    "ENV_STATE_HOME",
    "STATE_DIR_NAME",
    "DEFAULT_CONFIG_FILES",
    "get_config_dir",
    "get_state_dir",
    "get_log_dir",
    "config_path",
    "load_config",
    "CONFIG_DIR",
    "LOG_PATH",
]
