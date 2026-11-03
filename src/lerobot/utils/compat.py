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

"""Python 3.10 compatibility shims for the vendored lerobot subset.

``enum.StrEnum`` only exists from Python 3.11. Edge runs on 3.10, so the vendored modules import
``StrEnum`` from here instead of the standard library.
"""

import sys
from enum import Enum

if sys.version_info >= (3, 11):  # pragma: no cover - exercised only on newer runtimes
    from enum import StrEnum

    __all__ = ["StrEnum"]
else:

    class StrEnum(str, Enum):
        """Backport of ``enum.StrEnum``: a str-valued enum whose ``str()`` is its value."""

        def __str__(self) -> str:
            return str(self.value)

        @staticmethod
        def _generate_next_value_(name, start, count, last_values):  # noqa: N805
            return name.lower()

    __all__ = ["StrEnum"]
