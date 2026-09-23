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

"""TestRobot —— 虚拟测试机器人（**无硬件依赖**，离线联调 Edge 侧 TestRobotAdapter）。

结构对齐 ``DualPiperRobot``：**只做装配（控制器 + 相机）、取数与下发**——关节目标与位姿目标都只是
“转发”：运动学（FK / IK / 限速域）在 ``TestArmController``（虚拟：位姿 = ``POSE_MAP @ q`` 线性映射）。

- **控制器**：左/右臂 ``TestArmController``（内部有界随机游走，模拟关节反馈 + 虚拟运动学）；
- **视觉传感器**：3 相机 ``TestVisionSensor``（每帧生成移动 RGB 渐变，JPEG 输出）；
- 身份 / 共享内存名（``type="test_robot"`` / ``SHM="test_robot_obs"``）对齐 ``TestRobotAdapter``；
  换真实 SDK 进程即可无缝替换。
"""

import cv2
import numpy as np
from utils.base.data_handler import debug_print  # noqa: E402

from robot.base_robot import BaseRobot, CartesianActionError
from robot.controller.test_arm_controller import TestArmController  # noqa: E402
from robot.sensor.test_vision_sensor import TestVisionSensor  # noqa: E402


class TestRobot(BaseRobot):
    NAME = "test_robot"
    ADAPTER_TYPE = "test_robot"
    ROBOT_MODEL_ID = "test-robot"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES = {
        "capture": True,
        "execute": True,
        "streaming": True,
    }
    # 双臂：左 + 右臂，各 6 关节（joint 空间 12）；夹爪独立为 gripper 空间（每臂 1，共 2）
    QPOS = 12
    GRIPPER = 2
    # ---- 臂接线：左/右臂各一个 TestArmController（虚拟执行 + 虚拟运动学）----
    ARM_NAMES = ("left", "right")
    ARM_CONTROLLERS = {"left": "left_arm", "right": "right_arm"}
    JOINTS_PER_ARM = 6  # 每臂关节数（joint 空间每臂维度）
    POSE_DIM_PER_ARM = 6  # 每臂位姿维数（xyz + rpy）
    # 末端位姿：每臂 6 维（xyz + rpy），双臂共 12（对齐 TestRobotAdapter.POSE_DIM_PER_ARM = 6）
    POSE = 12
    # 动作空间：关节 + 位姿（绝对 / 增量）+ 夹爪（位姿由控制器的虚拟运动学解算，与 edge 侧
    # TestRobotAdapter 一致）
    ACTION_SPACES = (
        BaseRobot.ACTION_SPACE_JOINT,
        BaseRobot.ACTION_SPACE_POSE,
        BaseRobot.ACTION_SPACE_POSE_DELTA,
        BaseRobot.ACTION_SPACE_GRIPPER,
    )
    IMAGE_NAMES = ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    IMAGES = {name: (640, 480) for name in IMAGE_NAMES}
    SHM_NAME = "test_robot_obs"

    def __init__(self, robot_config: dict | None = None):
        super().__init__(robot_config)
        # 控制器：左/右臂 test 控制器（执行）；传感器：3 个 test 视觉传感器（相机）
        # 结构对齐 DualPiperRobot（无 master 主手：测试机器人不做遥操作）
        self.controllers: dict = {
            "left_arm": TestArmController("left_arm"),  # 左臂，执行
            "right_arm": TestArmController("right_arm"),  # 右臂，执行
        }
        self.sensors: dict = {
            "cam_head": TestVisionSensor("cam_head"),
            "cam_left_wrist": TestVisionSensor("cam_left_wrist"),
            "cam_right_wrist": TestVisionSensor("cam_right_wrist"),
        }
        self._pose_read_error: str | None = None  # 位姿不可用原因（同原因只告警一条，不刷屏）

    # ---- 取数：关节 qpos / 末端位姿（同一拍关节角正解）----------------------------------
    def _controller_for_arm(self, arm: str) -> TestArmController:
        """按 ``ARM_CONTROLLERS`` 取该臂的控制器（装配缺失 → 响亮报错）。"""
        key = self.ARM_CONTROLLERS.get(arm)
        if key is None or key not in self.controllers:
            raise RuntimeError(f"{self.name}: controller {key!r} for arm {arm!r} is not assembled")
        return self.controllers[key]

    def get_observation_qpos(self) -> np.ndarray:
        """当前帧**关节角**（扁平 ``QPOS`` 维 = 每臂 6）：“读取”退化为控制器内部状态（虚拟随机游走）。"""
        return self._read_joints()

    def get_observation_pose(self, qpos: np.ndarray | None = None) -> np.ndarray | None:
        """末端位姿（扁平 ``POSE`` 维）：每臂由虚拟运动学正解，与位姿目标的解算同一模型。

        关节读数缺失 / 非法 → **NaN 向量**（下游丢弃该帧位姿）；同一原因只告警一条。
        """
        try:
            joints = self._read_joints() if qpos is None else np.asarray(qpos, dtype=np.float64).reshape(-1)
            if joints.shape[0] < self.QPOS or not np.all(np.isfinite(joints[: self.QPOS])):
                raise ValueError(f"joint reading invalid (dim={joints.shape[0]})")
        except Exception as exc:  # noqa: BLE001 读数不可用：本拍不出位姿，下一拍重试
            reason = str(exc) or exc.__class__.__name__
            if reason != self._pose_read_error:
                self._pose_read_error = reason
                debug_print(self.name, f"pose unavailable ({reason}); publishing NaN pose", "WARNING")
            return np.full(self.POSE, np.nan)
        self._pose_read_error = None  # 恢复正常：下次失败重新告警一条
        return self._pose_of_joints(joints)

    def _pose_of_joints(self, joints: np.ndarray) -> np.ndarray:
        """逐臂虚拟正解 → 扁平位姿（``POSE`` 维）；关节值非法 → NaN 向量。"""
        values = np.asarray(joints, dtype=np.float64).reshape(-1)
        if values.shape[0] < self.QPOS or not np.all(np.isfinite(values[: self.QPOS])):
            return np.full(self.POSE, np.nan)
        arms = values[: self.QPOS].reshape(len(self.ARM_NAMES), self.JOINTS_PER_ARM)
        return np.concatenate([TestArmController.joint_to_pose(arm) for arm in arms])

    def get_target_pose(self) -> np.ndarray | None:
        """**目标位姿** = ``FK(关节段目标)``（与 ``get_observation_pose()`` 同一套虚拟正解）。"""
        return self._pose_of_joints(self._target_joints())

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``GRIPPER`` 维：每臂 1）——虚拟控制器直接给当前值。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._controller_for_arm(arm).get_gripper()
            values.append(np.nan if gripper is None else float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float64)

    def _read_joints(self) -> np.ndarray:
        """逐臂“读”关节角（扁平 ``QPOS`` 维，顺序对齐 ``ARM_NAMES``）；读不到 → RuntimeError。"""
        parts: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            joint = self._controller_for_arm(arm).get_joint()
            if joint is None:
                raise RuntimeError(f"{self.name}: arm {arm!r} joint read failed (None)")
            parts.append(np.asarray(joint, dtype=np.float64).reshape(-1)[: self.JOINTS_PER_ARM])
        return np.concatenate(parts)

    # ---- 下发：关节段（限速跟踪）/ 夹爪段（直接跟随）------------------------------
    def _apply_action(self, action: np.ndarray):
        """把底层目标向量 ``[关节段 | 夹爪段]`` 逐臂下发（控制线程每拍；限速已在 ``step()`` 完成）。"""
        values = np.asarray(action, dtype=np.float64).reshape(-1)
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.JOINTS_PER_ARM
            controller = self._controller_for_arm(arm)
            controller.set_joint(values[start : start + self.JOINTS_PER_ARM].copy())
            controller.set_gripper(float(values[self.QPOS + index]))

    def _prepare_target(self, qpos: np.ndarray, action_space: str | None) -> np.ndarray:
        """准备**关节段目标**：``joint`` 直通；``pose`` / ``pose_delta`` 逐臂解算（虚拟线性运动学）。

        ``pose_delta`` 由基类叠加成绝对目标位姿（基准 = 关节段目标的正解位姿）后再解算。
        """
        space = self.normalize_action_space(action_space)
        values = np.asarray(qpos, dtype=np.float64).reshape(-1)
        if space == self.ACTION_SPACE_JOINT:
            if values.shape[0] != self.QPOS:
                raise ValueError(f"{self.name}: joint action dim {values.shape[0]} != QPOS {self.QPOS}")
            return values
        target = self._absolute_pose_target(space, values)
        seed = self._target_joints() if space == self.ACTION_SPACE_POSE_DELTA else self.current_action()
        return self._solve_pose(target, seed)

    def _solve_pose(self, pose: np.ndarray, seed: np.ndarray) -> np.ndarray:
        """绝对目标位姿（每臂 ``xyz + rpy``）→ 关节段目标（逐臂虚拟逆解，起点 ``seed``）。"""
        start_joints = np.asarray(seed, dtype=np.float64).reshape(-1)
        home = np.asarray(self.init_joint, dtype=np.float64).reshape(-1)
        joints: list[np.ndarray] = []
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.POSE_DIM_PER_ARM
            joint_start = index * self.JOINTS_PER_ARM
            span = slice(joint_start, joint_start + self.JOINTS_PER_ARM)
            block = pose[start : start + self.POSE_DIM_PER_ARM]
            result = TestArmController.pose_to_joint(block, start_joints[span], fallback_seeds=(home[span],))
            if not result.ok:
                raise CartesianActionError(f"{self.name}: {arm} arm pose target rejected ({result.describe()})")
            joints.append(np.asarray(result.q, dtype=np.float64))
        return np.concatenate(joints)

    def connect(self):
        """连接 test 控制器与视觉传感器（虚拟，无硬件）。"""
        for ctrl in self.controllers.values():
            ctrl.connect()
        self.sensors["cam_head"].connect(is_jpeg=True, seed=0)
        self.sensors["cam_left_wrist"].connect(is_jpeg=True, seed=1)
        self.sensors["cam_right_wrist"].connect(is_jpeg=True, seed=2)
        self.ready = True
        debug_print(self.name, "TestRobot connected (virtual).", "INFO")

    def disconnect(self):
        """断开控制器与传感器（幂等，重复调用安全）。"""
        self.ready = False
        for name, ctrl in self.controllers.items():
            ctrl.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")
        for name, sensor in self.sensors.items():
            sensor.disconnect()
            debug_print(self.name, f"Disconnect sensor {name} done", "INFO")

    # ---- 相机（取数的一部分：由 capture_images() 组装为契约键）-------------------------

    def get_observation_images(self) -> list:
        """读取各相机 raw RGB 帧（顺序对齐 IMAGE_NAMES）。

        从 test 视觉传感器读 color（JPEG），解码为 raw RGB——与真实 piper 接入位一致。
        """
        images = []
        for name in self.IMAGE_NAMES:
            info = self.sensors[name].get_information()
            color = info.get("color") if info else None
            if color is None:
                raise RuntimeError(f"TestRobot.get_observation_images: 相机 {name} 无帧")
            decoded = cv2.imdecode(color, cv2.IMREAD_COLOR)
            if decoded is None:
                raise RuntimeError(f"TestRobot.get_observation_images: 相机 {name} JPEG 解码失败")
            images.append(decoded[:, :, ::-1])  # BGR → RGB
        return images
