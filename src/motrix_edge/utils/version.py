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

"""包版本号（单一来源：pyproject.toml [project].version）。

CLI（--version / version）与 HTTP（GET /v1/health）共用此实现，避免两处重复。
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def get_package_version() -> str:
    """返回已安装包版本号；未安装（如源码直接运行）返回 "unknown"。"""
    try:
        return _pkg_version("motrix-edge")
    except PackageNotFoundError:
        return "unknown"


__all__ = ["get_package_version"]
