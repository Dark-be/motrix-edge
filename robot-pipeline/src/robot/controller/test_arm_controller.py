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

"""TestArmController —— 虚拟机械臂控制器（测试 / 离线联调用）。

- 关节 / 夹爪：``get_*`` 做**有界随机游走**（模拟硬件反馈持续变化），``set_*`` 直接覆写合成状态；
- **虚拟运动学**：位姿 = ``POSE_MAP @ q``（对角线性映射，x←j1 … rz←j6）；与真机 ``PiperController``
  同形的**静态** ``joint_to_pose`` / ``pose_to_joint``（无实例状态），所以机器人层无需区分真假。
"""

import numpy as np
from utils.base.data_handler import debug_print

from robot.kinematics import IkResult

from .arm_controller import ArmController


# 基于ArmController的测试机械臂控制器类，包含机械臂状态获取和控制方法的简单实现，用于测试和调试
class TestArmController(ArmController):
    # 「手搓 FK」：6 关节 → 6 维末端位姿的固定可逆线性映射（对角：x←j1 y←j2 z←j3，
    # rx←j4 ry←j5 rz←j6；平移 0.2 m/rad）。保证位姿与关节严格一致（关节动则 EEF 跟着动）。
    POSE_MAP = np.diag([0.2, 0.2, 0.2, 1.0, 1.0, 1.0])
    JOINT_MIN = 0.0  # 关节值域（与随机游走边界一致）
    JOINT_MAX = 2.0

    def __init__(self, name="test_arm"):
        super().__init__(name)

    # ---- 虚拟运动学（**静态**，与 PiperController 同形：上层只拿「位姿 ↔ 关节」的结果）--------
    @staticmethod
    def joint_to_pose(q: np.ndarray) -> np.ndarray:
        """正解：关节 → 末端位姿（线性的 ``POSE_MAP @ q``）。"""
        return TestArmController.POSE_MAP @ np.asarray(q, dtype=np.float64).reshape(-1)[:6]

    @staticmethod
    def pose_to_joint(pose, seed=None, fallback_seeds=(), **overrides) -> IkResult:
        """位姿 → 关节（线性可逆 → 恒成功，只解算不下发）。"""
        values = np.asarray(pose, dtype=np.float64).reshape(-1)[:6]
        joints = np.diag(1.0 / np.diag(TestArmController.POSE_MAP)) @ values
        return IkResult(
            ok=True,
            q=np.clip(joints, TestArmController.JOINT_MIN, TestArmController.JOINT_MAX),
            pos_err=0.0,
            rot_err=0.0,
            iterations=1,
            reason="",
        )

    def connect(self):
        debug_print(self.name, "setup success", "INFO")

    def disconnect(self):
        debug_print(self.name, "disconnect success", "INFO")

    def _walk(self, key, shape, low=0.0, high=2.0):
        """对 self.state[key] 做一次有界随机游走：随机加减 0.05 以内。"""
        cur = self.state.get(key)
        if cur is None:
            cur = np.zeros(shape, dtype=np.float64)
        cur = np.asarray(cur, dtype=np.float64)
        delta = np.random.uniform(-0.05, 0.05, size=cur.shape)
        self.state[key] = np.clip(cur + delta, low, high).copy()
        return self.state[key]

    def get_joint(self):
        return self._walk("joint", (6,))

    def get_position(self):
        return self._walk("pose", (6,))

    def get_gripper(self):
        # 双臂 qpos 中第 7、14 个元素对应夹爪，范围限制为 [0, 1]。
        return self._walk("gripper", (1,), low=0.0, high=1.0)

    def set_position(self, position: np.ndarray):
        if position.shape[0] == 6:
            debug_print(self.name, f"using EULER set position to {position}", "DEBUG")
        elif position.shape[0] == 7:
            debug_print(self.name, f"using QUATERNION set position to {position}", "DEBUG")
        else:
            debug_print(self.name, "set_position input size should be 6 -> EULER or 7 -> QUATERNION", "ERROR")

        self.state["pose"] = position

    def set_joint(self, joint: np.ndarray):
        debug_print(self.name, f"set joint to {joint}", "DEBUG")

        self.state["joint"] = joint

    # The input gripper value is in the range [0, 1], representing the degree of opening.
    def set_gripper(self, gripper: float):
        # 与真实控制器一致：越界裁切到 [0, 1] 后仍然记录，不丢指令
        self.state["gripper"] = np.clip(gripper, 0, 1)
