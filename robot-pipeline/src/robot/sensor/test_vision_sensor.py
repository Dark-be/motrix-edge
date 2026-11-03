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

import cv2
import numpy as np
from utils.data_handler import debug_print

from .sensor import Sensor


class TestVisionSensor(Sensor):
    # 合成深度的固定参数（无硬件，取值必须**确定性**——测试要阶断言到具体米数）：
    # ``depth[y, x] = DEPTH_MM_AT_U0 + x``（毫米）→ ``depth_m = (DEPTH_MM_AT_U0 + x) × DEPTH_SCALE``
    DEPTH_SCALE = 0.001  # 深度原始值 → 米
    DEPTH_MM_AT_U0 = 1000  # u = 0 处的深度（毫米）
    FOCAL_PX = 600.0  # 合成内参：fx = fy（像素）

    def __init__(self, name="test_vision_sensor"):
        super().__init__(name)
        self.timestep = 0
        self.width = 640
        self.height = 480
        self.is_jpeg = True
        self.enable_depth = False

    def connect(self, is_jpeg=True, seed=0, enable_depth=False):
        """``enable_depth=True`` 时额外合成深度图（尺寸同彩色，无需硬件）。"""
        self.is_jpeg = is_jpeg
        self.seed = seed
        self.enable_depth = bool(enable_depth)

    def camera_info(self) -> dict:
        """相机静态元数据（键名与 edge adapter 契约一致）：合成内参 / 深度比例。

        真实相机的内参由硬件标定（``RealsenseSensor.camera_info()``）；虚拟相机用固定值，
        便于端到端验证「像素 → 米」链路（反投影仍需 Phase 2 的外参）。
        """
        info = {
            "name": self.name,
            "width": int(self.width),
            "height": int(self.height),
            "intrinsics": {
                "fx": float(self.FOCAL_PX),
                "fy": float(self.FOCAL_PX),
                "cx": self.width / 2.0,
                "cy": self.height / 2.0,
            },
            "depth": None,
        }
        if self.enable_depth:
            info["depth"] = {"scale": float(self.DEPTH_SCALE), "aligned_to_color": True}
        return info

    def get_information(self):
        """读取完整观测（color 恒有；enable_depth 时额外合深度图），不做 collect_info 过滤。"""
        image = {}

        self.timestep += 1  # 每帧增加
        t = self.timestep * 0.05
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        for x in range(self.width):
            # 开始是红色，随着时间变化，颜色在红、绿、蓝之间循环
            # 红色通道：正弦变化
            r = int(128 + 127 * np.cos(t + x * 0.02 + self.seed * 3.14))
            # 绿色通道：余弦变化（偏移）
            g = int(128 + 127 * np.cos(t + 3.14 + x * 0.02 + self.seed * 3.14))
            # 蓝色通道：另一个相位
            # b = int(128 + 127 * np.cos(t + 3.14 + x * 0.02))
            b = 0

            img[:, x, 0] = r
            img[:, x, 1] = g
            img[:, x, 2] = b
        if self.is_jpeg:
            # 生成随机 JPEG 图像
            img = img[:, :, [2, 1, 0]]  # 转换为 BGR 格式，cv2 使用 BGR，而不是 RGB
            _, jpeg_image = cv2.imencode(".jpg", img)
            image["color"] = jpeg_image
        else:
            image["color"] = img

        if self.enable_depth:
            # 合成深度（uint16 毫米）：与彩色同尺寸、逐列线性——像素 u 处的深度恒定可预测
            column = np.arange(self.width, dtype=np.uint16) + np.uint16(self.DEPTH_MM_AT_U0)
            image["depth"] = np.repeat(column[None, :], self.height, axis=0)

        return image

    def disconnect(self):
        debug_print(self.name, "disconnect success", "INFO")
