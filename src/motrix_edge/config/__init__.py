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

"""config 包 —— 路径解析（**一个根目录管 config + logs**）+ 配置加载（包内 yml 是**示例**）。

根目录由环境变量 ``MOTRIX_EDGE_DIR`` 指定；**未设置时回落到 ``<cwd>/motrix-edge/``**
（robot-pipeline 同构：变量 ``MOTRIX_ROBOT_PIPELINE_DIR``，兜底 ``<cwd>/motrix-robot-pipeline/``）：:

    <根>/
    ├── config/   实际配置（edge.yml / capture.yml）：包内示例**首次访问播种**到此处，之后以它为准
    └── logs/     log_<时间戳>.txt、uvicorn.log（写入开关 MOTRIX_EDGE_LOG_FILE，缺省关闭）

⚠️ **包内 ``src/motrix_edge/config/*.yml`` 只是示例**（随包分发、只读）：播种是**一次性**的，
包内示例以后更新不会自动覆盖现场副本（想回到示例：删掉 ``<根>/config/<name>`` 再跑一次）。

- 读配置：:func:`load_config` = 先播种 → 读 ``<根>/config/<name>``；包内没有该示例 → ``{}``；
- 写配置：:func:`writable_config_path` = ``<根>/config/<name>``（capture.yml 的可写副本就是它）；
- **``<cwd>`` 兜底的含义**：从不同目录启动 → 读不同配置 / 写不同日志。现场与容器请显式设
  ``MOTRIX_EDGE_DIR``（容器内必须是容器可见路径，否则落容器可写层、重启即丢）。

本模块在 import 时计算模块级 ``CONFIG_DIR`` / ``LOG_PATH``（环境变量须在进程启动前设置）。
"""

import os
from importlib import resources
from pathlib import Path

import yaml

# 环境变量名（单点定义：本模块与 utils / __main__ 共用，避免字面量散落漂移）。
ENV_ROOT_DIR = "MOTRIX_EDGE_DIR"
# 根下的两个子目录（配置与日志同一个根）
CONFIG_DIR_NAME = "config"
LOG_DIR_NAME = "logs"
# 未设环境变量时的根目录名：<cwd>/motrix-edge（robot-pipeline 用 motrix-robot-pipeline）
ROOT_DIR_NAME = "motrix-edge"

# 包内示例配置（package data；实际配置由 :func:`seed_config` 播种到 <根>/config，见模块 docstring）。
DEFAULT_CONFIG_FILES = ("edge.yml", "capture.yml")


def get_root_dir() -> Path:
    """根目录：``$MOTRIX_EDGE_DIR``；未设置 → ``<cwd>/motrix-edge``（``~`` 展开）。"""
    env = os.getenv(ENV_ROOT_DIR)
    return Path(env).expanduser() if env else Path.cwd() / ROOT_DIR_NAME


def get_config_dir() -> Path:
    """实际配置目录：``<根>/config``（唯一的配置位置，总是可写目标）。"""
    return get_root_dir() / CONFIG_DIR_NAME


def get_log_dir() -> Path:
    """日志目录：``<根>/logs``。"""
    return get_root_dir() / LOG_DIR_NAME


def config_path(name: str) -> Path:
    """配置文件的**实际路径**：``<根>/config/<name>``（是否存在见 :func:`seed_config`）。"""
    return get_config_dir() / name


def writable_config_path(name: str) -> Path:
    """可写配置路径：与 :func:`config_path` 同（配置目录本身就是可写位置）。"""
    return config_path(name)


def packaged_config_text(name: str) -> str | None:
    """包内示例 yml 的文本；包内没有该文件 → ``None``。"""
    if name not in DEFAULT_CONFIG_FILES:
        return None
    try:
        return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):  # 打包缺失：按「没有示例」处理
        return None


def seed_config(name: str) -> Path:
    """把包内示例 yml **播种**到 ``<根>/config/<name>``（幂等：目标已存在则不动）；返回目标路径。

    包内没有该示例（如运行期产物）→ 只返回路径、不创建文件。写失败（只读挂载 / 无权限）抛
    ``OSError``——由调用方决定是降级（:func:`load_config` 会吞掉并回落示例文本）还是报错。
    """
    target = config_path(name)
    if target.exists():
        return target
    text = packaged_config_text(name)
    if text is None:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    # 固定 0644：配置文件是给人看 / 给人改的，不该受 umask（可能 0664）或 mkstemp（0600）影响
    target.chmod(0o644)
    return target


def load_config(name: str) -> dict:
    """加载实际配置：先播种，再读 ``<根>/config/<name>``；包内也没有该示例 → ``{}``。

    只读挂载导致播种失败时不阻断：能读就读本地副本，读不到才回落包内示例文本。
    """
    try:
        path = seed_config(name)
    except OSError:
        path = config_path(name)
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    text = packaged_config_text(name)
    return (yaml.safe_load(text) or {}) if text else {}


# 对外暴露（模块级：环境已定的路径）
CONFIG_DIR = get_config_dir()
LOG_PATH = get_log_dir()

__all__ = [
    "ENV_ROOT_DIR",
    "ROOT_DIR_NAME",
    "CONFIG_DIR_NAME",
    "LOG_DIR_NAME",
    "DEFAULT_CONFIG_FILES",
    "get_root_dir",
    "get_config_dir",
    "get_log_dir",
    "config_path",
    "writable_config_path",
    "packaged_config_text",
    "seed_config",
    "load_config",
    "CONFIG_DIR",
    "LOG_PATH",
]
