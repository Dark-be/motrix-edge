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

"""DualPiperRobot —— 真实双臂 Piper 机器人（master 主手 + slave 从手 + 3 路 RealSense）。

装配硬件（``connect`` / ``disconnect``）与取数 / 下发：位姿目标由本层逐臂解算成关节目标
（``PiperController.pose_to_joint``，静态纯函数）后走关节通路；运动学（FK / IK / 限位）都在
控制器里，位姿观测由同一模型的静态正解给出。

- 物理臂与每臂宽度由类常量声明，整机维度自动推导并与 ``DualPiperAdapter`` 一致；
  ``robot.cartesian.ik`` 给解算参数，``robot.tcp`` 切换位姿系。
- 遥操作接入位见 ``_read_teleop_joint()`` / ``_read_teleop_gripper()``（主手 → 从手）；
  主臂常量偏置走 ``robot.teleop.joint_offset``（基类统一叠加）。

硬件 SDK（pyAgxArm / pyrealsense2 / v4l2）仅在机器人端安装。
"""

import cv2
import numpy as np
from utils.data_handler import debug_print  # noqa: E402

from robot.base_robot import BaseRobot, CartesianActionError
from robot.controller.piper_controller import PiperController  # noqa: E402
from robot.sensor.realsense_sensor import RealsenseSensor  # noqa: E402
from robot.sensor.v4l2_sensor import V4l2Sensor  # noqa: E402


class DualPiperRobot(BaseRobot):
    NAME = "dual_piper"
    ADAPTER_TYPE = "dual_piper"
    ROBOT_MODEL_ID = "dual-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES = {
        "capture": True,
        "execute": True,
        "streaming": True,
    }

    GRIPPER_DEADZONE = 0.2  # piper 抓取死区：小于 0.2 视为闭合（0）
    IMAGE_NAMES = ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    IMAGES = {name: (640, 480) for name in IMAGE_NAMES}
    CAMERA_KINDS = {name: "realsense" for name in IMAGE_NAMES}  # 三路均为 RealSense（配置填序列号）
    DEPTH_CAMERAS: tuple[str, ...] = tuple(IMAGE_NAMES)  # 三路均具备深度（是否启用由 robot.depth 决定）
    # 腕部相机 -> 承载臂（装配事实）：两路腕相机随臂动，外参只能是 T_flange_cam；cam_head 固定在头部
    WRIST_CAMERAS: dict[str, str] = {"cam_left_wrist": "left", "cam_right_wrist": "right"}
    SHM_NAME = "dual_piper_obs"
    # 硬件接线（值必填、只在配置里给：robot.ports / robot.cameras）
    PORT_ROLES = ("left_master", "right_master", "left", "right")  # 主手 / 从臂端口
    PORT_KINDS = {role: "can" for role in PORT_ROLES}  # 均为 CAN 接口名

    ARM_NAMES = ("left", "right")
    ARM_CONTROLLERS = {"left": "left_arm", "right": "right_arm"}  # 臂名 -> 执行控制器
    MASTER_CONTROLLERS = {"left": "left_master", "right": "right_master"}  # 臂名 -> 遥操作输入控制器
    POSE_DIM_PER_ARM = 6

    # 动作空间：关节 / 位姿（绝对）/ 位姿增量 / 夹爪——位姿类由控制器静态求解器解算
    ACTION_SPACES = (
        BaseRobot.ACTION_SPACE_JOINT,
        BaseRobot.ACTION_SPACE_JOINT_DELTA,
        BaseRobot.ACTION_SPACE_POSE,
        BaseRobot.ACTION_SPACE_POSE_DELTA,
        BaseRobot.ACTION_SPACE_GRIPPER,
        BaseRobot.ACTION_SPACE_GRIPPER_DELTA,
    )

    def __init__(self, robot_config: dict | None = None):
        super().__init__(robot_config)
        # 位姿解算参数（robot.cartesian.ik，可选）
        self.ik_config = dict((self.robot_config.get("cartesian") or {}).get("ik") or {})
        self.ports = self._required_devices("ports", self.PORT_ROLES)
        self.camera_devices = self._required_devices("cameras", self.IMAGE_NAMES)
        # 主手只读（遥操作输入）；左/右臂执行；三路 RealSense 按序列号区分
        self.controllers: dict = {
            "left_arm": PiperController("left"),
            "right_arm": PiperController("right"),
            "left_master": PiperController("left_master"),
            "right_master": PiperController("right_master"),
        }
        self.sensors: dict = {name: RealsenseSensor(name) for name in self.IMAGE_NAMES}
        self._pose_read_error: str | None = None  # 位姿不可用原因（同原因只告警一条）

    # ---- 取数：关节 / 末端位姿（同一拍关节角正解）----
    def get_observation_joint(self) -> np.ndarray:
        """当前帧关节角（扁平 ``joint_dim()`` 维），逐臂读取并按 ``ARM_NAMES`` 展开。

        发布为 robot 原始键 ``joint``；夹爪由 ``gripper`` 发布。任一臂读不到时抛 ``RuntimeError``。
        """
        return self._read_joints()

    def get_observation_pose(self, joint: np.ndarray | None = None) -> np.ndarray | None:
        """末端位姿（扁平 ``pose_dim()`` 维：各臂 ``xyz + rpy``）——由同一拍关节角正解。

        与位姿目标的解算共用同一运动学模型，读到的与下发的同系可直接比对（现场标定见
        ``scripts/verify_cartesian.py``）。关节读数缺失 / 非法 → NaN 向量（下游丢弃该帧），
        同一原因只告警一条。位姿系由 ``robot.tcp`` 决定（见基类 ``pose_to_tcp()``）。
        """
        try:
            joints = self._read_joints() if joint is None else np.asarray(joint, dtype=np.float64).reshape(-1)
            if joints.shape[0] < self.joint_dim() or not np.all(np.isfinite(joints[: self.joint_dim()])):
                raise ValueError(f"joint reading invalid (dim={joints.shape[0]})")
        except Exception as exc:  # noqa: BLE001 读数不可用：本拍不出位姿，下一拍重试
            reason = str(exc) or exc.__class__.__name__
            if reason != self._pose_read_error:
                self._pose_read_error = reason
                debug_print(self.name, f"pose unavailable ({reason}); publishing NaN pose", "WARNING")
            return np.full(self.pose_dim(), np.nan)
        self._pose_read_error = None  # 恢复正常：下次失败重新告警
        return self._pose_of_joints(joints)

    def _pose_of_joints(self, joints: np.ndarray) -> np.ndarray:
        """逐臂静态正解 → 扁平位姿（``pose_dim()`` 维）；关节值非法 → NaN 向量。

        实测位姿与目标位姿共用这一个换算层，两个观测键因此始终同系可比。正解给的是法兰系位姿，
        末尾统一经 ``pose_to_tcp()`` 换到位姿系。
        """
        values = np.asarray(joints, dtype=np.float64).reshape(-1)
        if values.shape[0] < self.joint_dim() or not np.all(np.isfinite(values[: self.joint_dim()])):
            return np.full(self.pose_dim(), np.nan)
        arms = values[: self.joint_dim()].reshape(len(self.ARM_NAMES), self.JOINT_DIM_PER_ARM)
        return self.pose_to_tcp(np.concatenate([PiperController.joint_to_pose(arm) for arm in arms]))

    def get_pose_action(self) -> np.ndarray | None:
        """目标位姿 = ``FK(关节段目标)``（每臂 ``xyz + rpy``，位姿系，米 / 弧度）。

        与 ``get_observation_pose()`` 同一套静态正解，喂进去的是 ``_joint_target_or_command()``，与
        ``observations/pose`` 同系可比、同一拍发布。
        """
        return self._pose_of_joints(self._joint_target_or_command())

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``gripper_dim()`` 维：每臂 1）；某臂读不到 → 该臂 NaN（下游丢弃该帧）。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._controller_for_arm(arm).get_gripper()
            values.append(np.nan if gripper is None else float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float64)

    def _read_joints(self) -> np.ndarray:
        """逐臂读关节角（扁平 ``joint_dim()`` 维，顺序对齐 ``ARM_NAMES``）；任一臂读不到 → ``RuntimeError``。"""
        parts: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            joint = self._controller_for_arm(arm).get_joint()
            if joint is None:
                raise RuntimeError(f"{self.name}: arm {arm!r} joint read failed (None)")
            parts.append(self._joint_block(joint, source=f"arm {arm!r}"))
        return np.concatenate(parts)

    # ---- 下发：关节段（限速跟踪）/ 夹爪段（直接跟随）----
    def _apply_commands(self, joints: np.ndarray, gripper: np.ndarray):
        """逐臂下发关节和夹爪；Piper 夹爪死区按闭合处理。"""
        joints = np.asarray(joints, dtype=np.float64).reshape(-1)
        gripper = np.asarray(gripper, dtype=np.float64).reshape(-1)
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.JOINT_DIM_PER_ARM
            joint = joints[start : start + self.JOINT_DIM_PER_ARM].copy()
            grip = float(gripper[index])
            if not np.isfinite(grip) or grip < self.GRIPPER_DEADZONE:
                grip = 0.0
            controller = self._controller_for_arm(arm)
            # set_joint 按软限位就地裁切，传 copy 以免改到 self.joint_command
            controller.set_joint(joint)
            controller.set_gripper(grip)

    def _solve_pose(self, pose: np.ndarray, seed: np.ndarray) -> np.ndarray:
        """绝对目标位姿（每臂 ``xyz + rpy``）→ 关节段目标（逐臂静态逆解，起点 ``seed``）。

        ``pose`` 是位姿系的值，解算前经 ``tcp_to_flange()`` 还原成法兰，IK / 限位只看该系。
        逆解在每条命令落地时做一次（不进 30Hz 控制拍）。
        """
        flange_pose = self.tcp_to_flange(pose)
        start_joints = np.asarray(seed, dtype=np.float64).reshape(-1)
        home = np.asarray(self.init_joint, dtype=np.float64).reshape(-1)
        joints: list[np.ndarray] = []
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.POSE_DIM_PER_ARM
            joint_start = index * self.JOINT_DIM_PER_ARM
            span = slice(joint_start, joint_start + self.JOINT_DIM_PER_ARM)
            block = flange_pose[start : start + self.POSE_DIM_PER_ARM]
            result = PiperController.pose_to_joint(
                block, start_joints[span], fallback_seeds=(home[span],), **self.ik_config
            )
            if not result.ok:
                raise CartesianActionError(f"{self.name}: {arm} arm pose target rejected ({result.describe()})")
            joints.append(np.asarray(result.q, dtype=np.float64))
        return np.concatenate(joints)

    def connect(self):
        """连接双臂 SDK：主手（只作输入）+ 从手（执行）+ 3 路 RealSense。

        端口 / 相机序列号取自 ``robot.ports`` / ``robot.cameras``（构造时已校验必填）。
        """
        self._connect_masters()
        # 重力前馈只装在执行臂（主手只读）
        gravity = self.robot_config.get("gravity")
        for arm in self.ARM_NAMES:
            self._controller_for_arm(arm).connect(port=self.ports[arm], gravity=gravity)
        debug_print(self.name, "Setup controllers done", "INFO")
        for name in self.IMAGE_NAMES:
            self.sensors[name].connect(
                device=self.camera_devices[name],
                pixel_format="jpg",
                enable_depth=name in self.depth_cameras,  # 只有生效的深度相机开深度流
            )
        debug_print(self.name, "Setup sensors done", "INFO")
        self.ready = True

    def _connect_masters(self):
        """连接只读 Piper 主手；其他主手机型覆盖此硬件接入点。"""
        for arm in self.ARM_NAMES:
            master_name = self.MASTER_CONTROLLERS[arm]
            self._master_controller_for_arm(arm).connect(port=self.ports[master_name], role="leader")

    def disconnect(self):
        """断开 master/slave 控制器与相机（幂等，重复调用安全）。"""

        self.ready = False
        for name, ctrl in self.controllers.items():
            ctrl.disconnect()
            debug_print(self.name, f"Disconnect controller {name} done", "INFO")
        for name, sensor in self.sensors.items():
            sensor.disconnect()
            debug_print(self.name, f"Disconnect sensor {name} done", "INFO")

    def get_observation_frames(self) -> list:
        """读取各相机本拍帧（顺序对齐 ``IMAGE_NAMES``）——彩色恒有，深度仅在开启时有。

        每相机只调一次 ``get_information()``：彩色与深度同拍同源（深度已对齐彩色图）。
        按 ``sensor.pixel_format`` 区分：jpg → 解 JPEG；raw → 已是 RGB ndarray。
        """
        frames = []
        for name in self.IMAGE_NAMES:
            sensor = self.sensors[name]
            info = sensor.get_information()
            color = info.get("color") if info else None
            if color is None:
                raise RuntimeError(f"DualPiperRobot.get_observation_frames: 相机 {name} 无帧")
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

    def _read_teleop_joint(self) -> np.ndarray | None:
        """遥操作关节段接入位：主手 → 从手（左→左、右→右），返回扁平 ``joint_dim()`` 维主臂关节角。

        只返回读数本身；常量偏置由基类按 ``robot.teleop.joint_offset`` 统一叠加。
        读不到 / 未连接时返回 None（``step()`` 保持原 target）。
        """
        joints: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            master = self._master_controller_for_arm(arm).get_joint()
            if master is None:
                return None
            joints.append(self._joint_block(master, source=f"master arm {arm!r}"))
        return np.concatenate(joints).astype(np.float32)

    def _read_teleop_gripper(self) -> np.ndarray | None:
        """遥操作夹爪段接入位：主手夹爪读数（每臂 1 维）；读不到 → None。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._master_controller_for_arm(arm).get_gripper()
            if gripper is None:
                return None
            values.append(float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float32)
