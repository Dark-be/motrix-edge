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

"""TestRobotAdapter —— 测试 / 无硬件联调用适配器（HTTP + 共享内存**薄客户端**）。

机器人硬件初始化和连接由 SDK 进程（``scripts/test_robot_sdk.py``）自行维护；本适配器
只是 Edge 侧薄客户端，**不实现任何硬件 / 连接逻辑**：

- **指令走 HTTP**：``execute`` / ``rollout`` / ``safe_stop`` / ``reset`` 等经 HTTP POST
  转发给 SDK 进程；SDK 执行硬件动作，并把观测填充到共享内存。
- **observe() 走共享内存**：SDK 进程按 ``run_hz`` 持续把观测（raw RGB + 关节）写入
  共享内存（``ObsShmWriter``），本适配器经 ``ObsShmReader`` 读取，并把图像编码为
  JPEG（Edge 观测契约）返回。
- **health() 实时查询**：直接 ``GET /v1/health`` 反映进程状态（节点经 alive-check 驱动）。

实现继承 ``HttpShmAdapter`` 共享基类（HTTP 指令下行 + 共享内存观测上行 + 状态查询），
本类只声明类常量：身份（discover 解析传入，缺省回退类常量）、能力（动作维度 / 相机
布局 / 支持的 ``AdapterCapability``）、连接参数（SDK URL / 共享内存名 / 超时）。能力与
连接参数**全部由类级常量定义**（自包含，不随 discover 传输、不接收 Edge 配置）。
"""

from motrix_edge.adapter.base import AdapterCapability
from motrix_edge.adapter.http_shm_adapter import HttpShmAdapter


class TestRobotAdapter(HttpShmAdapter):
    # ---- 身份（discover 解析传入；缺省回退类常量）----
    NAME = "test_robot"  # 实例名（debug_print 前缀）
    ADAPTER_TYPE = "test_robot"  # 本 adapter 的 entry point 类型

    # ---- 能力 / 连接参数（类级常量，自包含，不随 discover 传输）----
    ROBOT_MODEL_ID = "test-robot"
    ROBOT_MODEL_VERSION = "0.0.0"
    ACTION_DIM = 14  # 动作维度
    # 相机布局：{相机名: 分辨率 (width, height)}（SDK 产出 raw RGB；observe 编码 JPEG 原图）
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }

    # 中间件连接参数（SDK 自维护硬件与连接）
    SDK_URL = "http://127.0.0.1:8090"  # SDK HTTP 服务地址（指令下行）
    SHM_NAME = "test_robot_obs"  # 共享内存名（观测上行）
    HTTP_TIMEOUT = 3.0  # HTTP 指令超时（秒）

    # 能力声明：采集 + 执行 + 视频流（模拟相机）
    CAPABILITIES: dict[AdapterCapability, bool] = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }
