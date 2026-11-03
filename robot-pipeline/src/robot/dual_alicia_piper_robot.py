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

"""Alicia 串口主手 + Piper 双执行臂，共用 DualPiperRobot 的执行与采集行为。

仅主手不同：相机与执行臂均沿用 Piper 装配；主手 SDK 映射后的关节读数
进入同一条 Piper 遥操作通路，观测 / FK / IK / TCP / 夹爪死区均继承 Piper 实现。
"""

from robot.controller.alicia_teach_controller import AliciaTeachController
from robot.dual_piper_robot import DualPiperRobot


class DualAliciaPiperRobot(DualPiperRobot):
    NAME = "dual_alicia_piper"
    PORT_KINDS = {"left_master": "serial", "right_master": "serial", "left": "can", "right": "can"}

    def __init__(self, robot_config: dict | None = None):
        super().__init__(robot_config)
        # 构造阶段尚未连接设备；只替换不同的主手。
        for name in self.MASTER_CONTROLLERS.values():
            self.controllers[name] = AliciaTeachController(name)

    def _connect_masters(self):
        """Alicia 主手使用串口连接，无 Piper 的 leader 参数。"""
        for arm in self.ARM_NAMES:
            master_name = self.MASTER_CONTROLLERS[arm]
            self._master_controller_for_arm(arm).connect(port=self.ports[master_name])
