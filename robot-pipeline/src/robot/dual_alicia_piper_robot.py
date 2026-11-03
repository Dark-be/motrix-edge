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

"""DualPiperRobot —— 真实双臂机器人骨架（**接入位预留，未接入硬件**）。

结构对齐原 ``alicia_piper_teleop_robot``：master（alicia 主手）+ slave（piper 从手）
双控制器 + 3 相机。本骨架只声明布局 / 控制状态机（**无 profile**，obs/action 形态由
类常量固定，与 DualPiperAdapter 协定一致）；硬件 SDK（alicia_d_sdk / pyAgxArm /
pyrealsense2 / v4l2）**仅在机器人端安装**，接入时在 ``connect()`` / ``get_observation_joint()`` /
``get_observation_frames()`` / ``_apply_commands()`` 中填充。

遥操作：``teleop_enabled`` 默认 False（adapter 通讯控制中暂时均处于 false）；
接入位见 ``_read_teleop_joint()``（master 主手 → slave 从手）。
"""

import cv2
import numpy as np
from utils.data_handler import debug_print  # noqa: E402

from robot.base_robot import BaseRobot
from robot.controller.alicia_teach_controller import AliciaTeachController  # noqa: E402
from robot.controller.piper_controller import PiperController  # noqa: E402
from robot.sensor.cv2_sensor import Cv2Sensor  # noqa: E402
from robot.sensor.realsense_sensor import RealsenseSensor  # noqa: E402
from robot.sensor.v4l2_sensor import V4l2Sensor  # noqa: E402


class DualAliciaPiperRobot(BaseRobot):
    NAME = "dual_alicia_piper"
    ADAPTER_TYPE = "dual_piper"
    ROBOT_MODEL_ID = "dual-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES = {
        "capture": True,
        "execute": True,
        "streaming": True,
    }

    ARM_NAMES = ("left", "right")
    ARM_CONTROLLERS = {"left": "left_arm", "right": "right_arm"}
    MASTER_CONTROLLERS = {"left": "left_master", "right": "right_master"}
    IMAGE_NAMES = ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    IMAGES = {name: (640, 480) for name in IMAGE_NAMES}
    # 头部 = RealSense（序列号）；腕部 = USB 相机（V4L2 稳定软链 /dev/v4l/by-id/*）
    CAMERA_KINDS = {"cam_head": "realsense", "cam_left_wrist": "v4l2", "cam_right_wrist": "v4l2"}
    # 具备深度的相机：**只有** cam_head（RealSense）；两腕是网络摄像头，无深度
    DEPTH_CAMERAS: tuple[str, ...] = ("cam_head",)
    # 腕部相机 → 承载臂（装配事实）：两路腕相机为网络摄像头（无深度），但仍随臂动
    WRIST_CAMERAS: dict[str, str] = {"cam_left_wrist": "left", "cam_right_wrist": "right"}
    SHM_NAME = "dual_piper_obs"
    # ---- 硬件接线键清单（**值必填、只在配置里给**：robot.ports / robot.cameras）----
    PORT_ROLES = ("left_master", "right_master", "left", "right")  # 控制器端口（主手 / 从臂）
    # Alicia 示教臂 = USB 串口（/dev/serial/by-id/*）；Piper 从臂 = CAN 接口名
    PORT_KINDS = {"left_master": "serial", "right_master": "serial", "left": "can", "right": "can"}

    def __init__(self, robot_config: dict | None = None):
        super().__init__(robot_config)
        self.ports = self._required_devices("ports", self.PORT_ROLES)
        self.camera_devices = self._required_devices("cameras", self.IMAGE_NAMES)
        # ---- 控制器 / 传感器接入位（硬件 SDK 仅在机器人端；接入时实例化）----
        # 参照原 src/robot/alicia_piper_teleop_robot.py：
        #   left/right_master = AliciaTeachController（主手，遥操作输入）
        #   left/right_arm    = PiperController（从手，执行）
        #   cam_head = RealsenseSensor；cam_left/right_wrist = Cv2Sensor
        self.controllers: dict = {
            "left_arm": PiperController("left"),  # slave 从手
            "right_arm": PiperController("right"),  # slave 从手
            "left_master": AliciaTeachController("left_master"),  # master 主手
            "right_master": AliciaTeachController("right_master"),  # master 主手
        }
        self.sensors: dict = {
            "cam_head": RealsenseSensor("cam_head"),
            "cam_left_wrist": Cv2Sensor("cam_left_wrist"),
            "cam_right_wrist": Cv2Sensor("cam_right_wrist"),
        }

    def connect(self):
        """连接真实双臂 SDK：master 主手（遥操作输入）+ slave 从手（执行） + 相机。

        主手只作输入、不下发指令；slave 从手执行。端口 / 相机设备取自配置
        （``robot.ports`` / ``robot.cameras``，**必填**，构造时已校验）。硬件 SDK 仅在机器人
        端安装。
        """
        for arm in self.ARM_NAMES:
            master_name = self.MASTER_CONTROLLERS[arm]
            self._master_controller_for_arm(arm).connect(port=self.ports[master_name])
        # 重力前馈只装在**执行**臂（Alicia 从手）：主手只读，不会被 set_joint 下发
        gravity = self.robot_config.get("gravity")
        for arm in self.ARM_NAMES:
            self._controller_for_arm(arm).connect(port=self.ports[arm], gravity=gravity)
        debug_print(self.name, "Setup controllers done", "INFO")
        self.sensors["cam_head"].connect(
            device=self.camera_devices["cam_head"],
            pixel_format="jpg",
            enable_depth="cam_head" in self.depth_cameras,  # 三路里只有头部是 RealSense
        )
        self.sensors["cam_left_wrist"].connect(device=self.camera_devices["cam_left_wrist"], pixel_format="jpg")
        self.sensors["cam_right_wrist"].connect(device=self.camera_devices["cam_right_wrist"], pixel_format="jpg")
        debug_print(self.name, "Setup sensors done", "INFO")
        self.ready = True

    def disconnect(self):
        """断开 master/slave 控制器与相机（幂等，重复调用安全）。"""

        self.ready = False
        for name, ctrl in self.controllers.items():
            ctrl.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")
        for name, sensor in self.sensors.items():
            sensor.disconnect()
            debug_print(self.name, f"Disconnect sensor {name} done", "INFO")

    def get_observation_joint(self) -> np.ndarray:
        """当前帧关节角（扁平 ``joint_dim()`` 维），从执行臂读取并按 ``ARM_NAMES`` 展开。

        发布为 robot 原始键 ``joint``；夹爪由 ``gripper`` 发布。
        """
        parts: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            joint = self._controller_for_arm(arm).get_joint()
            if joint is None:
                raise RuntimeError(f"{self.name}: arm {arm!r} joint read failed (None)")
            parts.append(self._joint_block(joint, source=f"arm {arm!r}"))
        return np.concatenate(parts)

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``gripper_dim()`` 维 = 每臂 1）——读不到时该臂给 NaN。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._controller_for_arm(arm).get_gripper()
            values.append(np.nan if gripper is None else float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float64)

    def get_observation_frames(self) -> list:
        """读取各相机**本拍**帧（顺序对齐 IMAGE_NAMES）——彩色恒有，深度仅在开启时有。

        cam_head（RealSense）开启深度时，彩色与深度来自**同一次取帧**（深度已对齐到彩色图）；
        两腕是网络摄像头，``depth`` 恒为 ``None``。相机按 sensor.pixel_format 区分：
        jpg → color 为 JPEG bytes（此处 imdecode）；raw → 已解码为 RGB ndarray（直接用）。
        """
        frames = []
        for name in self.IMAGE_NAMES:
            sensor = self.sensors[name]
            info = sensor.get_information()
            color = info.get("color") if info else None
            if color is None:
                raise RuntimeError(f"{self.name}.get_observation_frames: 相机 {name} 无帧")
            if getattr(sensor, "pixel_format", V4l2Sensor.PIXEL_FORMAT_JPG) != V4l2Sensor.PIXEL_FORMAT_JPG:
                decoded = color  # raw（V4l2/Realsense）：已解码为 RGB（HxWx3）
            else:
                decoded = cv2.imdecode(color, cv2.IMREAD_COLOR)
                if decoded is None:
                    debug_print(self.name, f"相机 {name} JPEG 解码失败，返回空帧", "WARNING")
                    decoded = np.zeros((self.IMAGES[name][1], self.IMAGES[name][0], 3), dtype=np.uint8)
                decoded = decoded[:, :, ::-1]  # BGR → RGB
            frames.append({"color": decoded, "depth": info.get("depth")})
        return frames

    def _apply_commands(self, joints: np.ndarray, gripper: np.ndarray):
        """逐臂下发 slave 的关节和夹爪。"""
        joints = np.asarray(joints, dtype=np.float64).reshape(-1)
        gripper = np.asarray(gripper, dtype=np.float64).reshape(-1)
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.JOINT_DIM_PER_ARM
            controller = self._controller_for_arm(arm)
            controller.set_joint(joints[start : start + self.JOINT_DIM_PER_ARM].copy())
            controller.set_gripper(float(gripper[index]))

    def _read_teleop_joint(self) -> np.ndarray | None:
        """遥操作**关节段**接入位：master 主手 → slave 从手（左→左、右→右）。

        从 master 主手读取并返回扁平 ``joint_dim()`` 维主臂关节角；主手读取失败 / 未连接时返回 None
        （step() 保持原 target 不刷新）。
        """
        joints: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            master = self._master_controller_for_arm(arm).get_joint()
            if master is None:
                return None
            joints.append(self._joint_block(master, source=f"master arm {arm!r}"))
        return np.concatenate(joints).astype(np.float32)

    def _read_teleop_gripper(self) -> np.ndarray | None:
        """遥操作**夹爪段**接入位：master 主手夹爪读数（每臂 1 维）；读不到 → None。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._master_controller_for_arm(arm).get_gripper()
            if gripper is None:
                return None
            values.append(float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float32)
