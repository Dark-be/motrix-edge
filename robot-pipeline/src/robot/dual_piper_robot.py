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

"""DualPiperRobot —— 真实双臂 Piper 机器人（master 主手 + slave 从手 + 3 相机）。

**机器人类只做两件事**：装配硬件（``connect`` / ``disconnect``）与**取数 / 转发目标**——位姿目标或
关节角度目标都由本层逐臂交给 ``PiperController``；运动学（FK / 姿态求解 / 限位）全在控制器里，
机器人层不碰解算细节（见 ``wiki/design/robot_pipeline_action_spaces.md``）。

- 布局（``QPOS`` / ``POSE`` / ``GRIPPER`` / 相机 / 共享内存名）由类常量固定，与
  ``DualPiperAdapter`` 协定一致；
- 遥操作：``teleop_enabled`` 默认 False（adapter 通讯控制中暂时均处于 false）；接入位见
  ``_get_teleop_target()`` / ``_get_teleop_gripper()``（master 主手 → slave 从手）；
- 位姿动作：``action_space=pose``（每臂 ``xyz + rpy``，长度 12）由本层解算成关节目标
  （``PiperController.pose_to_joint``，静态纯函数），仍走 MIT 关节通路（**不经 ``move_p``**）；
  位姿观测由同一模型的静态正解给出（与解算同源）；
- 配置 ``robot.cartesian.ik``（求解参数）在这里解析后直接用于解算（限位无需配置：解算与下发
  共用 ``PIPER_JOINT_LIMITS``）。

硬件 SDK（pyAgxArm / pyrealsense2 / v4l2）**仅在机器人端安装**。
"""

import cv2
import numpy as np
from utils.base.data_handler import debug_print  # noqa: E402

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

    QPOS = 12  # joint 空间：每臂 6 关节角 × 2 臂（夹爪已独立为 gripper 空间）
    GRIPPER = 2  # gripper 空间：每臂 1 夹爪 × 2 臂
    GRIPPER_DEADZONE = 0.2  # piper 抓取死区：小于 0.2 视为闭合（0）
    IMAGE_NAMES = ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    IMAGES = {name: (640, 480) for name in IMAGE_NAMES}
    SHM_NAME = "dual_piper_obs"
    # ---- 硬件接线键清单（**值必填、只在配置里给**：robot.ports / robot.cameras）----
    PORT_ROLES = ("left_master", "right_master", "left", "right")  # 控制器端口（主手 / 从臂）

    # ---- 臂接线：左/右臂各一个 PiperController（执行）----
    # 本类只管两件事：**取数**（get_observation_qpos / get_observation_gripper）与**下发**；位姿目标在这里
    # 解算成关节角（:meth:`PiperController.pose_to_joint`，静态）后与 ``joint`` 目标走同一条控制通路。
    ARM_NAMES = ("left", "right")
    ARM_CONTROLLERS = {"left": "left_arm", "right": "right_arm"}
    JOINTS_PER_ARM = 6  # 每臂关节数（joint 空间每臂维度）
    POSE_DIM_PER_ARM = 6  # 每臂位姿维数（xyz + rpy）
    # 每臂 6 维 ``xyz + rpy``（米 / 弧度，法兰坐标系），双臂共 12——由控制器的静态正解算出后写进
    # ``observations/pose``（与关节角同一拍，机器人始终提供位姿）。
    POSE = 12

    # 动作空间：关节（每臂 6 关节角）/ 位姿（每臂 xyz + rpy，绝对）/ 位姿增量 /
    # 夹爪（每臂 1）——位姿（增量）由控制器的静态求解器解算
    ACTION_SPACES = (
        BaseRobot.ACTION_SPACE_JOINT,
        BaseRobot.ACTION_SPACE_POSE,
        BaseRobot.ACTION_SPACE_POSE_DELTA,
        BaseRobot.ACTION_SPACE_GRIPPER,
    )

    def __init__(self, robot_config: dict | None = None):
        super().__init__(robot_config)
        # 位姿动作的求解参数（``robot.cartesian.ik``，可选）：解算在本层做，参数就放在本层
        self.ik_config = dict((self.robot_config.get("cartesian") or {}).get("ik") or {})
        self.ports = self._required_devices("ports", self.PORT_ROLES)
        self.camera_devices = self._required_devices("cameras", self.IMAGE_NAMES)  # 三路均为 RealSense
        # ---- 控制器 / 传感器接入位（硬件 SDK 仅在机器人端；接入时实例化）----
        # 参照原 src/robot/alicia_piper_teleop_robot.py：
        #   left/right_master = PiperController（主手，遥操作输入）
        #   left/right_arm    = PiperController（从手，执行）
        #   三相机均为 RealsenseSensor（按序列号区分，序列号在配置 robot.cameras 里）
        self.controllers: dict = {
            "left_arm": PiperController("left"),
            "right_arm": PiperController("right"),
            "left_master": PiperController("left_master"),
            "right_master": PiperController("right_master"),
        }
        self.sensors: dict = {name: RealsenseSensor(name) for name in self.IMAGE_NAMES}
        self._pose_read_error: str | None = None  # 位姿不可用原因（同原因只告警一条，不刷屏）

    # ---- 取数：关节 qpos / 末端位姿（同一拍关节角正解）----------------------------------
    def _controller_for_arm(self, arm: str) -> PiperController:
        """按 ``ARM_CONTROLLERS`` 取该臂的**执行**控制器（装配缺失 → 响亮报错）。"""
        key = self.ARM_CONTROLLERS.get(arm)
        if key is None or key not in self.controllers:
            raise RuntimeError(f"{self.name}: controller {key!r} for arm {arm!r} is not assembled")
        return self.controllers[key]

    def get_observation_qpos(self) -> np.ndarray:
        """当前帧**关节角**（扁平 ``QPOS`` 维 = 每臂 6）——逐臂 ``get_joint``，顺序对齐 ``ARM_NAMES``。

        观测中的 ``observations/qpos`` 始终是关节角（数采 / VLA 要的就是它）；任一臂读不到
        （``None``）→ ``RuntimeError``（本拍没有可信观测，不拿半个观测充数）。
        """
        return self._read_joints()

    def get_observation_pose(self, qpos: np.ndarray | None = None) -> np.ndarray | None:
        """末端位姿（扁平 ``POSE`` 维：各臂 ``xyz + rpy``）——由**同一拍关节角**正解。

        与位姿目标的解算共用同一运动学模型，所以「读到的」与「下发的」同系可直接比对
        （现场标定见 ``scripts/verify_cartesian.py``）。
        关节读数缺失 / 非法 → **NaN 向量**（而非 ``None``）：下游防护会丢弃该帧位姿，比「留上一帧
        旧值」诚实；同一原因只告警一条（30Hz 不刷屏）。
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
        """逐臂静态正解 → 扁平位姿（``POSE`` 维）；关节值非法 → NaN 向量（下游丢弃该帧）。

        实测位姿（``get_observation_pose(qpos)``）与目标位姿（``get_target_pose()``）共用这一个
        换算层，保证「读到的」与「下发的目标」严格同系（同一 ``PiperKinematics`` 模型）。
        """
        values = np.asarray(joints, dtype=np.float64).reshape(-1)
        if values.shape[0] < self.QPOS or not np.all(np.isfinite(values[: self.QPOS])):
            return np.full(self.POSE, np.nan)
        arms = values[: self.QPOS].reshape(len(self.ARM_NAMES), self.JOINTS_PER_ARM)
        return np.concatenate([PiperController.joint_to_pose(arm) for arm in arms])

    def get_target_pose(self) -> np.ndarray | None:
        """**目标位姿** = ``FK(关节段目标)``（每臂 ``xyz + rpy``，法兰系，米 / 弧度）。

        与 ``get_observation_pose()`` 同一套静态正解，只是喂进去的是 ``_target_joints()``
        （未下发过指令 → 当前指令位置），因此与 ``observations/pose`` 同系可比、同一拍发布。
        """
        return self._pose_of_joints(self._target_joints())

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``GRIPPER`` 维：每臂 1）——读不到时该臂给 NaN（下游丢弃该帧）。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self._controller_for_arm(arm).get_gripper()
            values.append(np.nan if gripper is None else float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float64)

    def _read_joints(self) -> np.ndarray:
        """逐臂读关节角（扁平 ``QPOS`` 维，顺序对齐 ``ARM_NAMES``）。

        任一臂读不到（``None``）→ ``RuntimeError``（本拍没有可信观测，不拿半个观测充数）。
        """
        parts: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            joint = self._controller_for_arm(arm).get_joint()
            if joint is None:
                raise RuntimeError(f"{self.name}: arm {arm!r} joint read failed (None)")
            parts.append(np.asarray(joint, dtype=np.float64).reshape(-1)[: self.JOINTS_PER_ARM])
        return np.concatenate(parts)

    # ---- 下发：关节段（限速跟踪）/ 夹爪段（直接跟随）----------------------------------
    def _apply_action(self, action: np.ndarray):
        """把底层目标向量 ``[关节段 | 夹爪段]`` 逐臂下发（控制线程每拍；关节段限速已在 ``step()`` 完成）。

        夹爪小于 ``GRIPPER_DEADZONE`` 时按 0 处理（piper 抓取死区）。
        """
        values = np.asarray(action, dtype=np.float64).reshape(-1)
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.JOINTS_PER_ARM
            joint = values[start : start + self.JOINTS_PER_ARM].copy()
            grip = float(values[self.QPOS + index]) if index < self.GRIPPER else 0.0
            if not np.isfinite(grip) or grip < self.GRIPPER_DEADZONE:
                grip = 0.0
            controller = self._controller_for_arm(arm)
            # set_joint 会按软限位就地裁切（限位与解算同一份）：传 copy 以免改到 self.action
            controller.set_joint(joint)
            controller.set_gripper(grip)

    def _prepare_target(self, qpos: np.ndarray, action_space: str | None) -> np.ndarray:
        """准备**关节段目标**：``joint`` 直通；``pose`` / ``pose_delta`` 逐臂解算（本层编排）。

        ``pose_delta`` 先由基类 ``_absolute_pose_target()`` 叠成**绝对目标位姿**（基准 = 关节段
        目标的**正解**位姿，不是实测位姿），再与 ``pose`` 走同一条解算路径。解算起点：``pose``
        用当前指令位置（保持既有行为）、``pose_delta`` 用关节段目标（增量是相对目标的，起点也用
        目标才不甩关节）；兜底 ``init_joint`` 段。解算失败 → ``CartesianActionError``
        （robot server → 422）且**不改既有目标**。
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
        """绝对目标位姿（每臂 ``xyz + rpy``）→ 关节段目标（逐臂静态逆解，起点 ``seed``）。

        逆解在**控制线程**里、每条命令只做一次（不进 30Hz 控制拍）；失败只影响该条命令。
        """
        start_joints = np.asarray(seed, dtype=np.float64).reshape(-1)
        home = np.asarray(self.init_joint, dtype=np.float64).reshape(-1)
        joints: list[np.ndarray] = []
        for index, arm in enumerate(self.ARM_NAMES):
            start = index * self.POSE_DIM_PER_ARM
            joint_start = index * self.JOINTS_PER_ARM
            span = slice(joint_start, joint_start + self.JOINTS_PER_ARM)
            block = pose[start : start + self.POSE_DIM_PER_ARM]
            result = PiperController.pose_to_joint(
                block, start_joints[span], fallback_seeds=(home[span],), **self.ik_config
            )
            if not result.ok:
                raise CartesianActionError(f"{self.name}: {arm} arm pose target rejected ({result.describe()})")
            joints.append(np.asarray(result.q, dtype=np.float64))
        return np.concatenate(joints)

    def connect(self):
        """连接真实双臂 SDK：master 主手（遥操作输入）+ slave 从手（执行） + 3 路 RealSense。

        主手只作输入、不下发指令；slave 从手执行。端口 / 相机序列号取自配置
        （``robot.ports`` / ``robot.cameras``，**必填**，构造时已校验）——三路均为 RealSense，
        必须逐台按**序列号**指定。硬件 SDK 仅在机器人端安装。
        """
        self.controllers["left_master"].connect(port=self.ports["left_master"], role="leader")
        self.controllers["right_master"].connect(port=self.ports["right_master"], role="leader")
        self.controllers["left_arm"].connect(port=self.ports["left"])
        self.controllers["right_arm"].connect(port=self.ports["right"])
        debug_print(self.name, "Setup controllers done", "INFO")
        for name in self.IMAGE_NAMES:
            self.sensors[name].connect(device=self.camera_devices[name], pixel_format="jpg")
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

    def get_observation_images(self) -> list:
        """读取当前帧原始观测的 images——从相机传感器直接取 color。

        顺序对齐 IMAGE_NAMES（cam_head / cam_left_wrist / cam_right_wrist）；任一无帧时报错。
        相机按 sensor.pixel_format 区分：jpg → color 为 JPEG bytes（此处 imdecode）；
        raw → color 已解码为 RGB ndarray（直接使用）。
        """
        images = []
        for name in self.IMAGE_NAMES:
            sensor = self.sensors[name]
            info = sensor.get_information()
            color = info.get("color") if info else None
            if color is None:
                raise RuntimeError(f"DualPiperRobot.get_observation_images: 相机 {name} 无帧")
            if getattr(sensor, "pixel_format", V4l2Sensor.PIXEL_FORMAT_JPG) != V4l2Sensor.PIXEL_FORMAT_JPG:
                images.append(color)  # raw（V4l2/Realsense）：已解码为 RGB（HxWx3）
                continue
            decoded = cv2.imdecode(color, cv2.IMREAD_COLOR)
            if decoded is None:
                debug_print(self.name, f"相机 {name} JPEG 解码失败，返回空帧", "WARNING")
                decoded = np.zeros((self.IMAGES[name][1], self.IMAGES[name][0], 3), dtype=np.uint8)
            images.append(decoded[:, :, ::-1])  # BGR → RGB
        return images

    def _get_teleop_target(self) -> np.ndarray | None:
        """遥操作**关节段**接入位：master 主手 → slave 从手（左→左、右→右）。

        返回扁平 ``QPOS`` 维主臂关节角；主手读取失败 / 未连接时返回 None
        （step() 保持原 target 不刷新）。
        """
        joints: list[np.ndarray] = []
        for arm in self.ARM_NAMES:
            master = self.controllers[f"{arm}_master"].get_joint()
            if master is None:
                return None
            joints.append(np.asarray(master, dtype=np.float64).reshape(-1)[: self.JOINTS_PER_ARM].copy())
        parts = []
        for joint in joints:
            joint[1] += 0.1
            parts.append(joint)
        return np.concatenate(parts).astype(np.float32)

    def _get_teleop_gripper(self) -> np.ndarray | None:
        """遥操作**夹爪段**接入位：master 主手夹爪读数（每臂 1 维）；读不到 → None。"""
        values = []
        for arm in self.ARM_NAMES:
            gripper = self.controllers[f"{arm}_master"].get_gripper()
            if gripper is None:
                return None
            values.append(float(np.asarray(gripper, dtype=np.float64).reshape(-1)[0]))
        return np.asarray(values, dtype=np.float32)
