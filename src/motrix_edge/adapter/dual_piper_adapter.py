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

"""DualPiperAdapter —— 双臂 Piper 机器人适配器（HTTP + 共享内存薄客户端）。

继承 ``HttpShmAdapter`` 的共享实现（HTTP 指令下行 + 共享内存观测上行 + 状态查询），
本类只声明类常量：身份（discover 解析传入，缺省回退类常量）、能力（动作维度 / 相机
布局 / 支持的 ``AdapterCapability``）、连接参数（SDK URL / 共享内存名 / 超时）。

动作维度为 14（左右臂各 6 关节 + 1 夹爪），相机布局为 ``cam_head`` /
``cam_left_wrist`` / ``cam_right_wrist``（640×480）。
"""

from motrix_edge.adapter.base import AdapterCapability
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter


class DualPiperAdapter(HttpShmAdapter):
    # ---- 身份（discover 解析传入；缺省回退类常量）----
    NAME = "dual_piper"  # 实例名（debug_print 前缀）
    ADAPTER_TYPE = "dual_piper"  # 本 adapter 的 entry point 类型

    # ---- 能力 / 连接参数（类级常量，自包含，不随 discover 传输）----
    ROBOT_MODEL_ID = "dual-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    # 双臂 Piper：左 + 右臂，各 6 关节 + 1 夹爪 = 7，共 14
    ACTION_DIM = 14
    # 相机布局：{相机名: 分辨率 (width, height)}（SDK 产出 raw RGB；observe 编码 JPEG 原图）
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }

    CAPABILITIES: dict[AdapterCapability, bool] = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }

    # 中间件连接参数（与双臂 Piper SDK 进程约定）
    SDK_URL = "http://127.0.0.1:8090"
    SHM_NAME = "dual_piper_obs"
    HTTP_TIMEOUT = 3.0
