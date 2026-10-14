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

"""SinglePiperRobot —— 单臂双 Piper 遥操作机器人（Leader 主臂 + Follower 从臂）。

由**两个 Piper 机械臂**组成：一个为 Leader（主臂，只读取，经 ``set_leader_mode()`` 进入
主臂模式，用 ``get_leader_joint_angles()`` 读关节），一个为 Follower（从臂，执行动作）。
**无相机**（robot 原始观测只有 ``joint`` / ``gripper``，无图像）。

- 动作维度：Follower 单臂 6 关节（``joint_dim()=6``，joint 空间）+ 1 夹爪（``gripper_dim()=1``，gripper 空间）——
  夹爪不拼在关节向量里。
- 遥操作：``_read_teleop_joint()`` 读 Leader 主臂关节角度（``get_leader_joint_angles()``），
  ``_read_teleop_gripper()`` 给夹爪段（同构，直接下发）。
- 硬件 SDK（pyAgxArm）**仅在机器人端安装**；与双臂机型共用机器档案，仅取
  ``left_master`` / ``left`` 这一对端口。
"""

import numpy as np
from utils.data_handler import debug_print  # noqa: E402

from robot.base_robot import BaseRobot
from robot.controller.piper_controller import PiperController  # noqa: E402


class SinglePiperRobot(BaseRobot):
    NAME = "single_piper"
    ADAPTER_TYPE = "single_piper"
    ROBOT_MODEL_ID = "single-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES = {
        "capture": True,
        "execute": True,
        "streaming": False,
    }

    # 单臂模式直接选择双臂机器档案的左侧一对，不另造 leader / follower 接线名。
    ARM_NAMES = ("left",)
    PROFILE_ARM_NAMES = ("left", "right")
    ARM_CONTROLLERS = {"left": "left_arm"}
    MASTER_CONTROLLERS = {"left": "left_master"}
    IMAGE_NAMES: list[str] = []  # 无相机
    IMAGES: dict[str, tuple[int, int]] = {}
    CAMERA_KINDS: dict[str, str] = {}
    SHM_NAME = "single_piper_obs"

    # 默认复位目标（config 未提供时使用）：6 关节 0 + 夹爪张开 1
    DEFAULT_INIT_JOINT = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    DEFAULT_INIT_GRIPPER = [1.0]
    # ---- 硬件接线键清单（**值必填、只在配置里给**：robot.ports）----
    PORT_ROLES = ("left_master", "left")  # 当前机型实际使用的左主手 / 左从臂
    PROFILE_PORT_ROLES = ("left_master", "right_master", "left", "right")  # 共用双臂机器档案
    PORT_KINDS = {role: "can" for role in PORT_ROLES}

    def __init__(self, robot_config: dict | None = None):
        robot_config = dict(robot_config or {})
        robot_config.setdefault("init_joint", self.DEFAULT_INIT_JOINT)
        robot_config.setdefault("init_gripper", self.DEFAULT_INIT_GRIPPER)
        super().__init__(robot_config)
        # 控制器：Leader 主臂（只读取，遥操作输入）+ Follower 从臂（执行）
        # 端口在 robot.ports 里**必填**；固件版本可经 robot_config 配置（*_firmware，缺省 v188）
        self.ports = self._required_devices("ports", self.PORT_ROLES, known_keys=self.PROFILE_PORT_ROLES)
        self.leader_firmware = str(self.robot_config.get("leader_firmware", "v188"))
        self.follower_firmware = str(self.robot_config.get("follower_firmware", "v188"))
        self.controllers: dict = {
            "left_master": PiperController("left_master"),  # Leader 主臂，只读取
            "left_arm": PiperController("left"),  # Follower 从臂，执行；名称对齐重力 arms.left
        }

    def connect(self):
        """连接：Leader 主臂（role=leader）+ Follower 从臂（role=follower，执行）。

        重力前馈（``robot.gravity``）只装在**执行**臂上：主臂只读，不会被 ``set_joint`` 下发。
        """
        arm = self.ARM_NAMES[0]
        master_name = self.MASTER_CONTROLLERS[arm]
        self._master_controller_for_arm(arm).connect(
            port=self.ports[master_name], role="leader", firmware=self.leader_firmware
        )
        self._controller_for_arm(arm).connect(
            port=self.ports[arm],
            role="follower",
            firmware=self.follower_firmware,
            gravity=self.robot_config.get("gravity"),
        )
        debug_print(self.name, "Setup controllers done", "INFO")
        self.ready = True

    def disconnect(self):
        """断开 Leader/Follower 控制器（幂等，重复调用安全）。"""

        self.ready = False
        for name, ctrl in self.controllers.items():
            ctrl.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")

    def get_observation_joint(self) -> np.ndarray:
        """当前帧**关节角**（扁平 ``joint_dim()`` 维 = 6 关节）——从 Follower 从臂控制器取。

        发布为 robot 原始键 ``joint``；夹爪由 ``gripper`` 发布。
        """
        arm = self.ARM_NAMES[0]
        joint = self._controller_for_arm(arm).get_joint()
        if joint is None:
            raise RuntimeError("SinglePiperRobot.get_observation_joint: 从臂控制器读取失败（返回 None）")
        return self._joint_block(joint, source=f"arm {arm!r}")

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``gripper_dim()`` 维 = 1）——读不到时给 NaN（下游丢弃该帧）。"""
        gripper = self._controller_for_arm(self.ARM_NAMES[0]).get_gripper()
        if gripper is None:
            return np.full(self.gripper_dim(), np.nan)
        return np.asarray([float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0])], dtype=np.float64)

    def get_observation_frames(self) -> list:
        """无相机（IMAGE_NAMES 为空），返回空列表。"""
        return []

    def _apply_commands(self, joints: np.ndarray, gripper: np.ndarray):
        """下发 Follower 的关节和夹爪。"""
        follower = self._controller_for_arm(self.ARM_NAMES[0])
        follower.set_joint(np.asarray(joints, dtype=np.float64).reshape(-1).copy())
        follower.set_gripper(float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))

    def _read_teleop_joint(self) -> np.ndarray | None:
        """遥操作**关节段**接入位：Leader 主臂 → Follower 从臂。

        读 Leader 主臂关节角度（``get_leader_joint_angles()``）；主臂读取失败 / 未连接时返回
        None（step() 保持原 target 不刷新）。
        """
        joint = self._master_controller_for_arm(self.ARM_NAMES[0]).get_leader_joint_angles()
        if joint is None:
            return None
        return self._joint_block(joint, source="leader").astype(np.float32)

    def _read_teleop_gripper(self) -> np.ndarray | None:
        """遥操作**夹爪段**接入位：左主手夹爪读数；读不到时返回 None。"""
        gripper = self._master_controller_for_arm(self.ARM_NAMES[0]).get_gripper()
        if gripper is None:
            return None
        return np.asarray([float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0])], dtype=np.float32)
