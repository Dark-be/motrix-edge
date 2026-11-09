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
from utils.data_handler import debug_print

from robot.kinematics import IkResult, normalize_ik_config, normalize_ik_seeds

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
        values = np.asarray(q, dtype=np.float64).reshape(-1)
        if values.shape != (6,) or not np.all(np.isfinite(values)):
            raise ValueError("joint vector must contain exactly six finite values")
        return TestArmController.POSE_MAP @ values

    @staticmethod
    def pose_to_joint(
        pose,
        seed=None,
        fallback_seeds=(),
        *,
        pos_tol=1e-4,
        rot_tol=1e-3,
        max_iters=200,
        damping=1e-2,
        step_size=0.5,
    ) -> IkResult:
        """位姿 → 关节（线性可逆；任何关节越界都失败，容差不能掩盖不可达目标）。"""
        try:
            values = np.asarray(pose, dtype=np.float64).reshape(-1)
        except (TypeError, ValueError, OverflowError):
            return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")
        if values.shape != (6,) or not np.all(np.isfinite(values)):
            return IkResult(False, None, float("inf"), float("inf"), 0, "bad_input")
        config = normalize_ik_config(pos_tol, rot_tol, max_iters, damping, step_size)
        if config is None:
            return IkResult(False, None, float("inf"), float("inf"), 0, "bad_config")
        start = np.zeros(6) if seed is None else seed
        if normalize_ik_seeds(start, fallback_seeds, 6) is None:
            return IkResult(False, None, float("inf"), float("inf"), 0, "bad_seed")
        pos_tol, rot_tol, _, _, _ = config
        joints = np.diag(1.0 / np.diag(TestArmController.POSE_MAP)) @ values
        clipped = np.clip(joints, TestArmController.JOINT_MIN, TestArmController.JOINT_MAX)
        achieved = TestArmController.joint_to_pose(clipped)
        pos_err = float(np.linalg.norm(values[:3] - achieved[:3]))
        rot_err = float(np.linalg.norm(values[3:] - achieved[3:]))
        if not np.array_equal(clipped, joints):
            return IkResult(False, None, pos_err, rot_err, 1, "joint_limit")
        if pos_err <= pos_tol and rot_err <= rot_tol:
            return IkResult(True, joints, pos_err, rot_err, 1, "", 0.0)
        return IkResult(False, None, pos_err, rot_err, 1, "stalled")

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
