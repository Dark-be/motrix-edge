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

"""geometry/deproject —— 像素 + 深度 → 相机光学系坐标（纯 numpy，单点公式）。

深度图**已对齐到彩色图**（采集侧 ``rs.align(rs.stream.color)``），故只需一套彩色内参：

.. math::

    X_{cam} = \\Big[\\frac{u_{px} - c_x}{f_x}\\,z,\\ \\frac{v_{px} - c_y}{f_y}\\,z,\\ z\\Big],
    \\quad z = \\text{depth_m}

单位：像素用**源分辨率**像素、``z`` 用米；返回值在**相机光学系**（RealSense：``x`` 右 / ``y`` 下 /
``z`` 前）。要换到机械臂轴向的 ``world``，再乘外参（见 :mod:`motrix_edge.geometry.extrinsics`）。

本模块不读观测、不碰硬件：调用方（``server/depth.py``）自己从 ``FrameManager`` 缓存取深度图与内参。
"""

from __future__ import annotations

import numpy as np


def pixel_to_camera(
    u_px: float, v_px: float, depth_m: float, *, fx: float, fy: float, cx: float, cy: float
) -> np.ndarray:
    """像素 + 米深度 → 相机光学系 ``[x, y, z]``（米）。

    ``fx`` / ``fy`` ≤ 0 → ``ValueError``（内参缺失 / 损坏时不要「算出一个数」）。
    """
    if not (fx > 0.0) or not (fy > 0.0):
        raise ValueError(f"invalid focal length (fx={fx}, fy={fy}); camera intrinsics required")
    z = float(depth_m)
    return np.array(
        [(float(u_px) - float(cx)) * z / float(fx), (float(v_px) - float(cy)) * z / float(fy), z],
        dtype=np.float64,
    )


__all__ = ["pixel_to_camera"]
