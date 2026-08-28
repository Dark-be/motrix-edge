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

"""全局路径常量。

统一使用 ``pathlib.Path``，基于本文件在包内的固定位置
（``src/motrix_edge/config/_GLOBAL_CONFIG.py``）推导仓库根目录，不依赖
运行时 CWD。对外暴露的常量见 :data:`__all__`。
"""

from pathlib import Path

# 仓库根目录：本文件向上 4 级（config → motrix_edge → src → 仓库根）。
ROOT_DIR = Path(__file__).resolve().parents[3]

# 配置目录（yaml 配置）。
CONFIG_DIR = ROOT_DIR / "config"

# 数据目录（采集产物）。
DATA_PATH = ROOT_DIR / "data"

# 日志目录。
LOG_PATH = ROOT_DIR / "logs"

__all__ = [
    "ROOT_DIR",
    "CONFIG_DIR",
    "DATA_PATH",
    "LOG_PATH",
]
