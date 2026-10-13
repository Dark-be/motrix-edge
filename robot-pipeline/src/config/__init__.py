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

"""config 包 —— robot server 运行目录解析与配置加载。

``MOTRIX_ROBOT_PIPELINE_DIR`` 指定运行根目录，未设置时使用
``<cwd>/motrix-robot-pipeline``。配置固定在 ``<root>/config``，日志固定在 ``<root>/logs``。
现场配置不存在时读取包内只读默认值；普通读取不创建目录或文件。

路径在每次调用时解析，不保留 import 时快照。
"""

import os
from importlib import resources
from pathlib import Path

import yaml

ROOT_DIR_ENV = "MOTRIX_ROBOT_PIPELINE_DIR"
DEFAULT_ROOT_DIR_NAME = "motrix-robot-pipeline"
CONFIG_DIR_NAME = "config"
LOG_DIR_NAME = "logs"

# robot server 默认配置（package data；现场目录存在同名文件时优先）
DEFAULT_CONFIG_FILES = (
    "test_robot.yml",
    "dual_piper.yml",
    "dual_alicia_piper.yml",
    "single_piper.yml",
)


def get_root_dir() -> Path:
    """返回运行根目录：``$MOTRIX_ROBOT_PIPELINE_DIR`` 或 ``<cwd>/motrix-robot-pipeline``。"""
    value = os.getenv(ROOT_DIR_ENV)
    return Path(value).expanduser() if value else Path.cwd() / DEFAULT_ROOT_DIR_NAME


def get_config_dir() -> Path:
    """返回现场配置目录 ``<root>/config``。"""
    return get_root_dir() / CONFIG_DIR_NAME


def get_log_dir() -> Path:
    """返回日志目录 ``<root>/logs``。"""
    return get_root_dir() / LOG_DIR_NAME


def config_path(name: str) -> Path:
    """返回现场配置路径 ``<root>/config/<name>``，不创建文件。"""
    return get_config_dir() / name


def writable_config_path(name: str) -> Path:
    """返回明确写操作使用的配置路径；路径规则与 :func:`config_path` 相同。"""
    return config_path(name)


def load_config(name: str) -> dict:
    """加载现场配置；不存在时读取包内默认值，均不存在则返回空字典。"""
    path = config_path(name)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    if name in DEFAULT_CONFIG_FILES:
        text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
        return yaml.safe_load(text) or {}
    return {}


__all__ = [
    "ROOT_DIR_ENV",
    "DEFAULT_ROOT_DIR_NAME",
    "CONFIG_DIR_NAME",
    "LOG_DIR_NAME",
    "DEFAULT_CONFIG_FILES",
    "get_root_dir",
    "get_config_dir",
    "get_log_dir",
    "config_path",
    "writable_config_path",
    "load_config",
]
