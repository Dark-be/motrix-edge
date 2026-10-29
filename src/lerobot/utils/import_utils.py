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

"""Optional-dependency probes for the vendored lerobot remote-inference subset.

Same purpose as ``lerobot.utils.import_utils`` upstream, reduced to the two optional packages the
remote-inference wire needs and pointed at the edge extra that installs them.
"""

import importlib.util
from functools import lru_cache

#: Install target advertised when a remote-inference dependency is missing.
_EXTRA = "lerobot"


@lru_cache(maxsize=None)
def is_package_available(pkg_name: str, import_name: str | None = None) -> bool:
    """Return whether ``pkg_name`` can be imported without importing it."""
    return importlib.util.find_spec(import_name or pkg_name) is not None


def require_package(pkg_name: str, extra: str = _EXTRA, import_name: str | None = None) -> None:
    """Raise an informative ImportError when a remote-inference dependency is missing."""
    if not is_package_available(pkg_name, import_name):
        raise ImportError(
            f"'{pkg_name}' is required but not installed. Install it with: pip install 'motrix-edge[{extra}]'"
        )


_zenoh_available = is_package_available("eclipse-zenoh", import_name="zenoh")
_msgpack_available = is_package_available("msgpack")

__all__ = [
    "_msgpack_available",
    "_zenoh_available",
    "is_package_available",
    "require_package",
]
