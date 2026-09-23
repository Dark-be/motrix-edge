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
**无相机**（obs 只有 qpos，无 images）。

- 动作维度：Follower 单臂 6 关节（``QPOS=6``，joint 空间）+ 1 夹爪（``GRIPPER=1``，gripper 空间）——
  夹爪不拼在关节向量里。
- 遥操作：``_get_teleop_target()`` 读 Leader 主臂关节角度（``get_leader_joint_angles()``），
  ``_get_teleop_gripper()`` 给夹爪段（同构，直接下发）。
- 硬件 SDK（pyAgxArm）**仅在机器人端安装**；obs/action 形态由类常量固定（无 profile）。
"""

import numpy as np
from utils.base.data_handler import debug_print  # noqa: E402

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

    QPOS = 6  # Follower 单臂：6 关节角（joint 空间）
    GRIPPER = 1  # Follower 单臂：1 夹爪（gripper 空间）
    IMAGE_NAMES: list[str] = []  # 无相机
    IMAGES: dict[str, tuple[int, int]] = {}
    SHM_NAME = "single_piper_obs"

    # 默认复位目标（config 未提供时使用）：6 关节 0 + 夹爪张开 1
    DEFAULT_INIT_JOINT = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    DEFAULT_INIT_GRIPPER = [1.0]
    # ---- 硬件接线键清单（**值必填、只在配置里给**：robot.ports）----
    PORT_ROLES = ("leader", "follower")  # 主 / 从臂 CAN 接口名

    def __init__(self, robot_config: dict | None = None):
        robot_config = dict(robot_config or {})
        robot_config.setdefault("init_joint", self.DEFAULT_INIT_JOINT)
        robot_config.setdefault("init_gripper", self.DEFAULT_INIT_GRIPPER)
        super().__init__(robot_config)
        # 控制器：Leader 主臂（只读取，遥操作输入）+ Follower 从臂（执行）
        # 端口在 robot.ports 里**必填**；固件版本可经 robot_config 配置（*_firmware，缺省 v188）
        self.ports = self._required_devices("ports", self.PORT_ROLES)
        self.leader_firmware = str(self.robot_config.get("leader_firmware", "v188"))
        self.follower_firmware = str(self.robot_config.get("follower_firmware", "v188"))
        self.controllers: dict = {
            "leader": PiperController("leader"),  # Leader 主臂，只读取
            "follower": PiperController("follower"),  # Follower 从臂，执行
        }

    def connect(self):
        """连接：Leader 主臂（role=leader）+ Follower 从臂（role=follower，执行）。"""
        self.controllers["leader"].connect(port=self.ports["leader"], role="leader", firmware=self.leader_firmware)
        self.controllers["follower"].connect(
            port=self.ports["follower"], role="follower", firmware=self.follower_firmware
        )
        debug_print(self.name, "Setup controllers done", "INFO")
        self.ready = True

    def disconnect(self):
        """断开 Leader/Follower 控制器（幂等，重复调用安全）。"""

        self.ready = False
        for name, ctrl in self.controllers.items():
            ctrl.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")

    def get_observation_qpos(self) -> np.ndarray:
        """当前帧**关节角**（扁平 ``QPOS`` 维 = 6 关节）——从 Follower 从臂控制器取。

        观测中的 ``observations/qpos`` 始终是关节角；夹爪走 ``get_observation_gripper()``（独立键）。
        """
        joint = self.controllers["follower"].get_joint()
        if joint is None:
            raise RuntimeError("SinglePiperRobot.get_observation_qpos: 从臂控制器读取失败（返回 None）")
        return np.asarray(joint, dtype=np.float64).reshape(-1)[: self.QPOS]

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``GRIPPER`` 维 = 1）——读不到时给 NaN（下游丢弃该帧）。"""
        gripper = self.controllers["follower"].get_gripper()
        if gripper is None:
            return np.full(self.GRIPPER, np.nan)
        return np.asarray([float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0])], dtype=np.float64)

    def get_observation_images(self) -> list:
        """无相机（IMAGE_NAMES 为空），返回空列表。"""
        return []

    def _apply_action(self, action: np.ndarray):
        """把底层目标向量 ``[关节段(6) | 夹爪段(1)]`` 下发到 Follower 从臂（限速已在 step() 内完成）。

        set_joint 内部会 clip 关节角度（原地修改），故传 copy 避免改动 self.action。
        """
        values = np.asarray(action, dtype=np.float64).reshape(-1)
        follower = self.controllers["follower"]
        follower.set_joint(values[: self.QPOS].copy())
        follower.set_gripper(float(values[self.QPOS]))

    def _get_teleop_target(self) -> np.ndarray | None:
        """遥操作**关节段**接入位：Leader 主臂 → Follower 从臂。

        读 Leader 主臂关节角度（``get_leader_joint_angles()``）；主臂读取失败 / 未连接时返回
        None（step() 保持原 target 不刷新）。
        """
        joint = self.controllers["leader"].get_leader_joint_angles()
        if joint is None:
            return None
        return np.asarray(joint, dtype=np.float64).reshape(-1)[: self.QPOS].astype(np.float32)

    def _get_teleop_gripper(self) -> np.ndarray | None:
        """遥操作**夹爪段**接入位：主臂夹爪读数（此机型固定给 1.0 = 张开）。"""
        # gripper = self.controllers["leader"].get_gripper()
        return np.asarray([1.0], dtype=np.float32)
