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

"""Shape validation for shared/per-arm teleop position scales (no robot state)."""

import math
from numbers import Real


def parse_position_scale(raw, *, arms=None):
    """Normalize finite xyz triples; missing arm entries retain unit scale."""
    if raw is None:
        return None

    def triple(values):
        if not isinstance(values, (list, tuple)) or len(values) != 3:
            raise ValueError("pose_position_scale must contain exactly three finite numbers")
        if any(isinstance(x, bool) or not isinstance(x, Real) for x in values):
            raise ValueError("pose_position_scale must contain exactly three finite numbers")
        try:
            normalized = [float(x) for x in values]
        except (ValueError, OverflowError) as exc:
            raise ValueError("pose_position_scale must contain exactly three finite numbers") from exc
        if not all(math.isfinite(x) for x in normalized):
            raise ValueError("pose_position_scale must contain exactly three finite numbers")
        return normalized

    if isinstance(raw, dict):
        if any(not isinstance(arm, str) or (arms is not None and arm not in arms) for arm in raw):
            raise ValueError(f"pose_position_scale contains unknown arm (expected {arms})")
        return {arm: triple(values) for arm, values in raw.items()}
    return triple(raw)
