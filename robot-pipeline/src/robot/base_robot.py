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

"""机器人控制骨架。

所有输入最终收敛为关节目标：``pose`` 先做 TCP→法兰换算和 IK，再进入与 ``joint`` 相同的
限速、MIT 下发链。原始观测按 ``joint`` / ``gripper`` / ``pose`` 物理量发布；融合的 qpos / action
由 server 在跨进程边界组装。
"""

import time
from dataclasses import dataclass

import numpy as np
from utils.data_handler import debug_print

from motrix_edge.adapter.http_contract import FIELD_ARM, FIELD_MOUNT, VALUE_MOUNT_FIXED, VALUE_MOUNT_WRIST
from robot.kinematics import tcp, wrap_angles


class CartesianActionError(ValueError):
    """位姿动作（每臂 ``xyz + rpy``）不可用 / 解算失败（robot server 映射为 HTTP 422，**不改既有目标**）。"""


@dataclass(frozen=True)
class LayoutPlan:
    """一次下发的**只读校验结果**：段序列 + 作用域 + 重塑后的逐臂块。"""

    layout: str
    segments: tuple[tuple[str, int], ...]
    scope: list[str]
    blocks: np.ndarray


class BaseRobot:
    # ---- 身份 / 能力（对齐 adapter 契约；子类覆盖）----
    NAME = "base_robot"
    ADAPTER_TYPE = "base_robot"  # entry point 名
    ROBOT_MODEL_ID = "base-robot"
    ROBOT_MODEL_VERSION = "0.0.0"
    CAPABILITIES: dict[str, bool] = {}

    # ---- 布局 / 特性（子类覆盖）----
    # 只声明每臂宽度；整机维度由 ARM_NAMES 推导。
    JOINT_DIM_PER_ARM = 6
    POSE_DIM_PER_ARM = 0  # 0 = 该机型不提供位姿
    ARM_NAMES: tuple[str, ...] = ()  # 物理臂序（所有具体机型必须声明）
    PROFILE_ARM_NAMES: tuple[str, ...] = ()  # 共享机器档案可声明的臂；空 = 与 ARM_NAMES 相同
    ARM_CONTROLLERS: dict[str, str] = {}  # 臂名 -> controllers 键（子类声明）
    MASTER_CONTROLLERS: dict[str, str] = {}  # 臂名 -> 遥操作输入控制器键（有主手的子类声明）
    IMAGE_NAMES: list[str] = []  # 相机名（observations/images/<name>）
    IMAGES: dict[str, tuple[int, int]] = {name: (640, 480) for name in IMAGE_NAMES}  # 相机名 -> (w, h)
    DEPTH_CAMERAS: tuple[str, ...] = ()  # 具备深度的相机（IMAGE_NAMES 子序；硬件事实）
    WRIST_CAMERAS: dict[str, str] = {}  # 腕部相机 -> 承载臂（装配事实；装在法兰上，随臂动）
    SHM_NAME = "robot_obs"  # 观测共享内存名（server 侧发布）

    # ---- 动作空间（取值与 edge 契约 ActionSpace 同名）----
    ACTION_SPACE_JOINT = "joint"  # 每臂关节角（绝对目标）
    ACTION_SPACE_JOINT_DELTA = "joint_delta"  # 每臂关节角增量（基准为当前关节目标）
    ACTION_SPACE_POSE = "pose"  # 每臂 xyz + rpy（绝对目标）
    ACTION_SPACE_POSE_DELTA = "pose_delta"  # 每臂 xyz + rpy（增量，基准为关节段目标的正解位姿）
    ACTION_SPACE_GRIPPER = "gripper"  # 每臂夹爪值（归一化 [0, 1]）
    ACTION_SPACE_GRIPPER_DELTA = "gripper_delta"  # 每臂夹爪增量（基准为当前夹爪目标）
    MOTION_ACTION_SPACES = (ACTION_SPACE_JOINT, ACTION_SPACE_JOINT_DELTA, ACTION_SPACE_POSE, ACTION_SPACE_POSE_DELTA)
    GRIPPER_ACTION_SPACES = (ACTION_SPACE_GRIPPER, ACTION_SPACE_GRIPPER_DELTA)
    ACTION_SPACES: tuple[str, ...] = (
        ACTION_SPACE_JOINT,
        ACTION_SPACE_JOINT_DELTA,
        ACTION_SPACE_GRIPPER,
        ACTION_SPACE_GRIPPER_DELTA,
    )

    # ---- robot 原始观测键（跨进程契约由 server 转换）----
    KEY_JOINT = "joint"  # 实测绝对关节角
    KEY_GRIPPER = "gripper"  # 实测夹爪开合
    KEY_POSE = "pose"  # 实测末端位姿（pose_dim() > 0；每臂 xyz + rpy）
    KEY_JOINT_ACTION = "joint_action"  # 当前关节目标；无目标时回退实测
    KEY_GRIPPER_ACTION = "gripper_action"  # 当前夹爪目标；无目标时回退实测
    KEY_JOINT_ERROR = "joint_error"  # 关节跟踪误差 = 目标 - 实测
    KEY_GRIPPER_ERROR = "gripper_error"  # 夹爪跟踪误差 = 目标 - 实测
    KEY_POSE_ACTION = "pose_action"  # 关节目标的 FK 位姿（pose_dim() > 0）
    KEY_TIMESTAMP = "timestamp"  # 观测帧时刻（观测线程打点）
    CAMERA_PREFIX = "observations/images/"  # 彩色图像键前缀
    DEPTH_PREFIX = "observations/depth/"  # 深度图键前缀（uint16，已对齐同名彩色图；仅生效的深度相机）

    JOINT_LABELS = ("j1", "j2", "j3", "j4", "j5", "j6")

    # ---- 遥操作模式（取值与 /v1/teleop 的 mode 同名）----
    TELEOP_MODE_ABSOLUTE = "absolute"  # 主臂绝对读数直连从臂 target
    TELEOP_MODE_DELTA = "delta"  # 锚点增量：target = slave_ref + (master_now − master_ref)
    TELEOP_MODES = (TELEOP_MODE_ABSOLUTE, TELEOP_MODE_DELTA)
    TELEOP_FOLLOW_JOINT = ACTION_SPACE_JOINT
    TELEOP_FOLLOW_POSE = ACTION_SPACE_POSE
    TELEOP_FOLLOW_SPACES = (TELEOP_FOLLOW_JOINT, TELEOP_FOLLOW_POSE)
    TELEOP_POSE_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

    def __init__(self, robot_config: dict | None = None):
        self._validate_dimensions()
        self.robot_config = dict(robot_config or {})
        self.name = str(self.robot_config.get("name", self.NAME))
        self.step_rad = float(self.robot_config.get("step_rad", 0.1))  # 每拍最大关节增量（rad）
        self.ready = False
        self.last_error = None

        # command = 本控制拍实际下发值；target = 最新请求的最终目标。
        self.joint_command: np.ndarray | None = None
        self.joint_target: np.ndarray | None = None
        self.gripper_command: np.ndarray | None = None
        self.gripper_target: np.ndarray | None = None
        self.init_joint = self._init_vector("init_joint", self.joint_dim(), required=True)
        self.init_gripper = self._init_vector("init_gripper", self.gripper_dim(), required=False, fill=1.0)

        # 遥操作跟随空间 / 位姿轴白名单 + 主臂关节、位姿常量偏置。
        self.teleop_follow_space, self.teleop_follow_axes = self._resolve_teleop_follow()
        self._configured_teleop_follow_space = self.teleop_follow_space
        self._configured_teleop_follow_axes = self.teleop_follow_axes
        self.teleop_joint_offset = self._resolve_teleop_joint_offset()
        self.teleop_pose_offset = self._resolve_teleop_pose_offset()

        # 遥操作：开关 + 映射模式 + 增量锚点（锚点在接管后首拍采样，见 _capture_teleop_anchor）
        self.teleop_enabled = False
        self.teleop_mode = self.TELEOP_MODE_ABSOLUTE
        self.teleop_master_ref: np.ndarray | None = None  # 主臂关节锚点（delta 模式）
        self.teleop_slave_ref: np.ndarray | None = None  # 从臂关节锚点（delta 模式）
        self.teleop_master_pose_ref: np.ndarray | None = None  # 主臂位姿锚点（pose 跟随）
        self.teleop_slave_pose_ref: np.ndarray | None = None  # 从臂位姿锚点（pose 跟随 / 锁轴参考）
        self.teleop_master_gripper_ref: np.ndarray | None = None  # 主臂夹爪锚点（delta 模式）
        self.teleop_slave_gripper_ref: np.ndarray | None = None  # 从臂夹爪锚点（delta 模式）
        # 主臂读数不可用时的累计计数 + 限流告警（同一原因只告警一条）
        self.teleop_read_failures = 0
        self._teleop_read_error: str | None = None  # 最近一次原因
        self.teleop_mapping_failures = 0
        self._teleop_mapping_error: str | None = None  # 最近一次 FK / IK / 位姿合成失败原因
        # 最近一次成功的主臂读数（关节 / 夹爪）；开关遥操作时随锚点一起清空
        self.teleop_master_now: np.ndarray | None = None
        self.teleop_master_gripper_now: np.ndarray | None = None

        self.seq = 0  # 帧计数（sample_state() 每帧自增）

        # 控制器 / 传感器（真实机器人填充；虚拟机器人为空）
        self.controllers: dict = {}
        self.sensors: dict = {}

        # 机械臂侧状态缓存（控制线程每拍 sample_state() 覆盖；观测线程只读）
        self.motion_state: dict | None = None

        # 生效的深度相机（顺序 = IMAGE_NAMES 子序；空 = 不开深度），由 robot.depth 决定
        self.depth_cameras = self._resolve_depth_cameras()

        # 末端 TCP（robot.tcp，可选）：位姿读写原点从法兰切到工具末端
        tcp_section = self.robot_config.get(tcp.SECTION)
        self.tcp_offsets = tcp.resolve_tcp(tcp_section, tuple(self.ARM_NAMES))
        self.pose_frame = tcp.FRAME_TCP if self.tcp_offsets else tcp.FRAME_FLANGE
        if bool((tcp_section or {}).get("enabled", False)) and not (self.tcp_offsets and self.pose_dim() > 0):
            debug_print(self.name, f"robot.{tcp.SECTION} 已开启但不生效（不提供位姿 / 未声明臂名），已忽略", "WARNING")

    # ---- 硬件设备类型（机器档案探测用：键 = PORT_ROLES / IMAGE_NAMES；值 = config.probe 的 kind）----
    PORT_KINDS: dict[str, str] = {}
    CAMERA_KINDS: dict[str, str] = {}

    # ---- 遥操作配置（robot.teleop：跟随空间 / 位姿轴 / 主臂常量偏置）----
    def _resolve_teleop_follow(self) -> tuple[str, tuple[str, ...]]:
        """解析 ``follow_space`` / ``follow_axes``；缺省为既有的 joint 跟随。

        ``follow_axes`` 只对 pose 跟随有效，采用白名单语义：未列出的轴锁在 teleop 首拍的从臂
        位姿锚点。配置错误在构造期响亮失败，避免真机以错误语义启动。
        """
        section = self.robot_config.get("teleop") or {}
        allowed = {"joint_offset", "pose_offset", "follow_space", "follow_axes"}
        unknown = [key for key in section if key not in allowed]
        if unknown:
            debug_print(self.name, f"robot.teleop 未知键 {unknown}（可用：{sorted(allowed)}），已忽略", "WARNING")
        follow_space = str(section.get("follow_space", self.TELEOP_FOLLOW_JOINT)).strip()
        if follow_space not in self.TELEOP_FOLLOW_SPACES:
            raise ValueError(
                f"{self.name}: robot.teleop.follow_space {follow_space!r} invalid (expect {self.TELEOP_FOLLOW_SPACES})"
            )
        raw_axes = section.get("follow_axes")
        if follow_space == self.TELEOP_FOLLOW_JOINT:
            if raw_axes is not None:
                raise ValueError(f"{self.name}: robot.teleop.follow_axes is only valid for follow_space='pose'")
            return follow_space, ()
        if self.ACTION_SPACE_POSE not in self.ACTION_SPACES or self.pose_dim() <= 0:
            raise ValueError(f"{self.name}: robot.teleop.follow_space='pose' needs pose action capability")
        expected_pose = self.POSE_DIM_PER_ARM * len(self.ARM_NAMES)
        if self.POSE_DIM_PER_ARM != len(self.TELEOP_POSE_AXES) or self.pose_dim() != expected_pose:
            raise ValueError(
                f"{self.name}: pose teleop needs {len(self.TELEOP_POSE_AXES)} pose dims per arm"
                f" and pose_dim()={expected_pose} (got per_arm={self.POSE_DIM_PER_ARM}, pose_dim()={self.pose_dim()})"
            )
        if raw_axes is not None and not isinstance(raw_axes, (list, tuple)):
            raise ValueError(f"{self.name}: robot.teleop.follow_axes must be a list (got {raw_axes!r})")
        axes = self.TELEOP_POSE_AXES if raw_axes is None else tuple(str(axis).strip() for axis in raw_axes)
        if not axes or len(set(axes)) != len(axes) or any(axis not in self.TELEOP_POSE_AXES for axis in axes):
            raise ValueError(
                f"{self.name}: robot.teleop.follow_axes must be a nonempty unique subset of"
                f" {self.TELEOP_POSE_AXES} (got {raw_axes!r})"
            )
        return follow_space, tuple(axes)

    def _resolve_teleop_joint_offset(self) -> np.ndarray:
        """解析逐关节 rad 偏置；缺省全 0。"""
        return self._resolve_teleop_offset("joint_offset", self.JOINT_DIM_PER_ARM, self.joint_dim())

    def _resolve_teleop_pose_offset(self) -> np.ndarray:
        """解析每臂 ``xyz + rpy`` 位姿偏置；缺省全 0。"""
        section = self.robot_config.get("teleop") or {}
        if section.get("pose_offset") is not None and (
            self.ACTION_SPACE_POSE not in self.ACTION_SPACES
            or self.POSE_DIM_PER_ARM != len(self.TELEOP_POSE_AXES)
            or self.pose_dim() <= 0
        ):
            raise ValueError(f"{self.name}: robot.teleop.pose_offset needs pose action capability")
        return self._resolve_teleop_offset("pose_offset", self.POSE_DIM_PER_ARM, self.pose_dim())

    def _resolve_teleop_offset(self, key: str, per_arm: int, total_dim: int) -> np.ndarray:
        """解析可全臂共用或按臂给出的 teleop 常量偏置。"""
        section = self.robot_config.get("teleop") or {}
        raw = section.get(key)
        if raw is None:
            return np.zeros(total_dim)
        arms = self.state_arm_count()
        if isinstance(raw, dict):
            known_arms = self.PROFILE_ARM_NAMES or tuple(self.state_arms())
            extra = [str(arm) for arm in raw if str(arm) not in known_arms]
            if extra:
                raise ValueError(f"{self.name}: robot.teleop.{key} 含未知臂 {extra}（可用：{list(known_arms)}）")
            per_arm_values = {arm: raw.get(arm) for arm in self.state_arms()}
        else:
            per_arm_values = dict.fromkeys(self.state_arms(), raw)
        offset = np.zeros(total_dim)
        for index, arm in enumerate(self.state_arms()):
            values = per_arm_values[arm]
            if values is None:
                continue
            block = np.asarray(values, dtype=np.float64).reshape(-1)
            if block.shape[0] != per_arm:
                raise ValueError(
                    f"{self.name}: robot.teleop.{key}[{arm}] dim {block.shape[0]} != {per_arm}"
                    f"（每臂维数 × {arms} 臂 = {total_dim}）"
                )
            if not np.all(np.isfinite(block)):
                raise ValueError(f"{self.name}: robot.teleop.{key}[{arm}] contains non-finite values")
            offset[index * per_arm : (index + 1) * per_arm] = block
        return offset

    # ---- 硬件接线（robot.ports / robot.cameras：值必填，代码内不存现场值）----
    def _required_devices(
        self,
        section: str,
        keys: tuple[str, ...],
        *,
        known_keys: tuple[str, ...] | None = None,
    ) -> dict[str, str]:
        """读取必填的硬件接线 ``robot.<section>.<key>``；缺失 / 空串 → ValueError。

        端口与相机设备只在配置里给，代码不存现场值。``known_keys`` 可包含同一机器档案中当前
        机型不使用的资源；它们不返回也不告警，真正的未知键仍打 WARNING。
        """
        values = self.robot_config.get(section) or {}
        allowed = known_keys or keys
        unknown = [key for key in values if key not in allowed]
        if unknown:
            debug_print(self.name, f"robot.{section} 未知键 {unknown}（可用：{list(allowed)}），已忽略", "WARNING")
        missing = [key for key in keys if not str(values.get(key) or "").strip()]
        if missing:
            raise ValueError(f"配置缺少 robot.{section}：{missing}（必填，无代码缺省值；请按现场接线填写）")
        return {key: str(values[key]).strip() for key in keys}

    # ---- 布局 / 解析（无 profile；obs/action 形态由类常量固定）------------------
    def _init_vector(self, key: str, dim: int, *, required: bool, fill: float = 0.0) -> np.ndarray:
        """读复位向量 ``init_joint`` / ``init_gripper``；长度必须等于 ``joint_dim()`` / ``gripper_dim()``。"""
        raw = self.robot_config.get(key)
        if raw is None:
            if required:
                raise ValueError(f"Robot {self.name} {key} is not set in robot_config.")
            return np.full(dim, float(fill))
        values = np.asarray(raw, dtype=np.float64).reshape(-1)
        if values.shape[0] != dim:
            raise ValueError(f"{self.name}: {key} dim {values.shape[0]} != {dim}")
        return values

    @classmethod
    def arm_count(cls) -> int:
        """物理臂数；具体机型必须用 ``ARM_NAMES`` 明确声明。"""
        if not cls.ARM_NAMES:
            raise ValueError(f"{cls.__name__}: ARM_NAMES must declare at least one physical arm")
        return len(cls.ARM_NAMES)

    @classmethod
    def _validate_dimensions(cls) -> None:
        """校验机型的维度声明，避免能力与物理布局不一致。"""
        cls.arm_count()
        if len(set(cls.ARM_NAMES)) != len(cls.ARM_NAMES):
            raise ValueError(f"{cls.__name__}: ARM_NAMES must be unique")
        if cls.PROFILE_ARM_NAMES and not set(cls.ARM_NAMES) <= set(cls.PROFILE_ARM_NAMES):
            raise ValueError(f"{cls.__name__}: ARM_NAMES must be a subset of PROFILE_ARM_NAMES")
        if set(cls.ARM_CONTROLLERS) != set(cls.ARM_NAMES):
            raise ValueError(f"{cls.__name__}: ARM_CONTROLLERS keys must exactly match ARM_NAMES")
        if len(set(cls.ARM_CONTROLLERS.values())) != len(cls.ARM_CONTROLLERS):
            raise ValueError(f"{cls.__name__}: ARM_CONTROLLERS values must be unique")
        if cls.MASTER_CONTROLLERS and set(cls.MASTER_CONTROLLERS) != set(cls.ARM_NAMES):
            raise ValueError(f"{cls.__name__}: MASTER_CONTROLLERS keys must exactly match ARM_NAMES")
        if len(set(cls.MASTER_CONTROLLERS.values())) != len(cls.MASTER_CONTROLLERS):
            raise ValueError(f"{cls.__name__}: MASTER_CONTROLLERS values must be unique")
        if int(cls.JOINT_DIM_PER_ARM) <= 0:
            raise ValueError(f"{cls.__name__}: JOINT_DIM_PER_ARM must be positive")
        pose_declared = {cls.ACTION_SPACE_POSE, cls.ACTION_SPACE_POSE_DELTA} & set(cls.ACTION_SPACES)
        if pose_declared and int(cls.POSE_DIM_PER_ARM) <= 0:
            raise ValueError(f"{cls.__name__}: pose action spaces require POSE_DIM_PER_ARM > 0")

    @classmethod
    def joint_dim(cls) -> int:
        """整机关节维度。"""
        return int(cls.JOINT_DIM_PER_ARM) * cls.arm_count()

    @classmethod
    def pose_dim(cls) -> int:
        """整机位姿维度；``POSE_DIM_PER_ARM = 0`` 时不提供位姿。"""
        return int(cls.POSE_DIM_PER_ARM) * cls.arm_count()

    @classmethod
    def gripper_dim(cls) -> int:
        """整机夹爪维度；契约固定每臂一个开合值。"""
        return cls.arm_count()

    def _controller_for_arm(self, arm: str):
        """按机型声明取得物理臂的执行控制器。"""
        key = self.ARM_CONTROLLERS.get(arm)
        if key is None or key not in self.controllers:
            raise RuntimeError(f"{self.name}: controller {key!r} for arm {arm!r} is not assembled")
        return self.controllers[key]

    def _master_controller_for_arm(self, arm: str):
        """按机型声明取得物理臂对应的遥操作输入控制器。"""
        key = self.MASTER_CONTROLLERS.get(arm)
        if key is None or key not in self.controllers:
            raise RuntimeError(f"{self.name}: master controller {key!r} for arm {arm!r} is not assembled")
        return self.controllers[key]

    def _joint_block(self, value, *, source: str) -> np.ndarray:
        """规范化单臂关节读数；不足声明宽度时立即报错。"""
        values = np.asarray(value, dtype=np.float64).reshape(-1)
        if values.shape[0] < self.JOINT_DIM_PER_ARM:
            raise RuntimeError(
                f"{self.name}: {source} joint dim {values.shape[0]} < JOINT_DIM_PER_ARM {self.JOINT_DIM_PER_ARM}"
            )
        return values[: self.JOINT_DIM_PER_ARM].copy()

    @classmethod
    def action_dim(cls, action_space: str | None = None) -> int:
        """该动作空间的整机值维度（缺省 ``joint``）。

        ``joint_delta`` 与 ``joint``、``pose_delta`` 与 ``pose``、``gripper_delta`` 与 ``gripper``
        分别同形，靠空间名区分。
        """
        space = cls.normalize_action_space(action_space)
        return {
            cls.ACTION_SPACE_JOINT: cls.joint_dim(),
            cls.ACTION_SPACE_JOINT_DELTA: cls.joint_dim(),
            cls.ACTION_SPACE_POSE: cls.pose_dim(),
            cls.ACTION_SPACE_POSE_DELTA: cls.pose_dim(),
            cls.ACTION_SPACE_GRIPPER: cls.gripper_dim(),
            cls.ACTION_SPACE_GRIPPER_DELTA: cls.gripper_dim(),
        }[space]

    @classmethod
    def action_dims(cls) -> dict[str, int]:
        """本机器人已声明的各动作空间维度（按 ``ACTION_SPACES`` 顺序），供能力上报。"""
        return {space: cls.action_dim(space) for space in cls.ACTION_SPACES}

    # ---- 下发布局（layout）：每臂块内的段序列，如 joint+gripper（书写顺序即块内顺序）----
    # 段类型（kind）= 动作空间名；约束：至多一个运动段 + 至多一个夹爪段
    LAYOUT_SEPARATOR = "+"

    @classmethod
    def segment_dims(cls) -> dict[str, int]:
        """每个段类型的每臂维数（只含本机器人已声明的段）。"""
        dims = {
            cls.ACTION_SPACE_JOINT: cls.JOINT_DIM_PER_ARM,
            cls.ACTION_SPACE_JOINT_DELTA: cls.JOINT_DIM_PER_ARM,
            cls.ACTION_SPACE_POSE: cls.POSE_DIM_PER_ARM,
            cls.ACTION_SPACE_POSE_DELTA: cls.POSE_DIM_PER_ARM,
            cls.ACTION_SPACE_GRIPPER: 1,
            cls.ACTION_SPACE_GRIPPER_DELTA: 1,
        }
        return {kind: int(dim) for kind, dim in dims.items() if kind in cls.ACTION_SPACES and int(dim) > 0}

    @classmethod
    def action_layouts(cls) -> dict[str, tuple[tuple[str, int], ...]]:
        """本机器人支持的 layout → 段序列 ``((kind, 每臂维数), ...)``（供能力上报）。

        规范名 = 单段 layout +「运动段 + 夹爪段」组合；``parse_layout()`` 另接受任意段序。
        """
        dims = cls.segment_dims()
        layouts: dict[str, tuple[tuple[str, int], ...]] = {kind: ((kind, dim),) for kind, dim in dims.items()}
        for motion in cls.MOTION_ACTION_SPACES:
            if motion not in dims:
                continue
            for gripper in cls.GRIPPER_ACTION_SPACES:
                if gripper in dims:
                    layouts[f"{motion}{cls.LAYOUT_SEPARATOR}{gripper}"] = (
                        (motion, dims[motion]),
                        (gripper, dims[gripper]),
                    )
        return layouts

    @classmethod
    def parse_layout(cls, layout: str | None) -> tuple[tuple[str, int], ...]:
        """layout 名 → 段序列；缺省 / 空串 → ``joint``；非法（未知段 / 重复段 / 两个值段 / 空）→ ``ValueError``。

        校验只读、不依赖实例；错误信息附可用 layout 清单。
        """
        name = cls.ACTION_SPACE_JOINT if not layout else str(layout).strip()
        parts = [part.strip() for part in name.split(cls.LAYOUT_SEPARATOR)]
        dims = cls.segment_dims()
        motion_kinds = set(cls.MOTION_ACTION_SPACES) & dims.keys()
        gripper_kinds = set(cls.GRIPPER_ACTION_SPACES) & dims.keys()
        segments: list[tuple[str, int]] = []
        invalid: str | None = None
        if not parts or any(not part for part in parts):
            invalid = "empty segment"
        elif len(set(parts)) != len(parts):
            invalid = "duplicate segment"
        elif any(part not in dims for part in parts):
            invalid = f"unknown segment (available: {sorted(dims)})"
        elif len([part for part in parts if part in motion_kinds]) > 1:
            invalid = "at most one motion segment"
        elif len([part for part in parts if part in gripper_kinds]) > 1:
            invalid = "at most one gripper segment"
        else:
            segments = [(part, dims[part]) for part in parts]
        if invalid is not None:
            raise ValueError(
                f"{cls.__name__}: invalid layout {name!r} ({invalid}; available: {sorted(cls.action_layouts())})"
            )
        return tuple(segments)

    @classmethod
    def normalize_action_space(cls, action_space: str | None) -> str:
        """校验动作空间名（缺省 / None → ``joint``）；不在 ``ACTION_SPACES`` 内 → ``ValueError``。

        各空间维度不同，故空间校验与维度校验（``action_dim()``）成对做。
        """
        space = cls.ACTION_SPACE_JOINT if action_space is None else str(action_space)
        if space not in cls.ACTION_SPACES:
            raise ValueError(f"action space {space!r} not supported (available: {list(cls.ACTION_SPACES)})")
        return space

    # ---- 关节观测布局 ------------------------------------------------------------
    @classmethod
    def state_arm_count(cls) -> int:
        """物理臂数。"""
        return cls.arm_count()

    @classmethod
    def state_value_dim_per_arm(cls) -> int:
        """每臂关节数。"""
        return int(cls.JOINT_DIM_PER_ARM)

    @classmethod
    def state_vector_dim(cls) -> int:
        """采集边界的 qpos / action 维数（每臂 joint + gripper）。"""
        return cls.joint_dim() + cls.gripper_dim()

    def state_arms(self) -> list[str]:
        """状态向量的物理臂顺序。"""
        return [str(arm) for arm in self.ARM_NAMES]

    def state_layout(self) -> dict:
        """采集边界 qpos / action 的逐维含义。"""
        arms = self.state_arms()
        per_arm = self.state_value_dim_per_arm()
        labels = self.JOINT_LABELS[:per_arm]
        dims: list[dict] = []
        for arm_index, arm in enumerate(arms):
            base = arm_index * (per_arm + 1)
            dims.extend(
                {"index": base + offset, "arm": arm, "kind": "joint", "name": name}
                for offset, name in enumerate(labels)
            )
            dims.append({"index": base + per_arm, "arm": arm, "kind": "gripper", "name": "gripper"})

        return {
            "state_space": self.ACTION_SPACE_JOINT,
            "state_dims": dims,
            "action_space": self.ACTION_SPACE_JOINT,
            "action_dims": list(dims),
        }

    def control_layout(self) -> dict:
        """各执行控制器的控制模式：``{"control_mode": <聚合>, "arms": {<控制器名>: {...}}}``。

        写进采集 JSON（``control_mode`` / ``control``）与 ``/v1/capture/status`` 的 ``meta``；
        模式取值 ``mit`` / ``mit+gravity`` / ``joint``。只记录 ``role == "follower"`` 且提供
        ``control_detail`` 的控制器（主手与无控制通路的控制器跳过）；执行臂模式不同 → ``mixed``，
        没有可记录的控制器 → ``None``。
        """
        arms: dict = {}
        for name, ctrl in self.controllers.items():
            detail = getattr(ctrl, "control_detail", None)
            if isinstance(detail, dict) and detail.get("role") == "follower":
                arms[str(name)] = detail
        modes = {entry["mode"] for entry in arms.values()}
        return {"control_mode": next(iter(modes)) if len(modes) == 1 else ("mixed" if modes else None), "arms": arms}

    # ---- 控制：HTTP（reset/execute/rollout/safe_stop）只修改两段目标 ----------------
    def reset(self):
        """程序复位到 home（非阻塞）：关节段 = ``init_joint``，夹爪段 = ``init_gripper``。"""
        self.disable_teleop()
        self.set_joint_target(self.init_joint)
        self.set_gripper_target(self.init_gripper)

    # ---- 下发内核（唯一写目标处）：layout + arms → 目标向量 ----------------
    def plan_layout(self, action, layout: str | None = None, arms: list[str] | None = None) -> LayoutPlan:
        """只读校验一次下发：返回「段序列 + 作用域 + 逐臂块」，不碰目标 / 遥操作状态。

        Raises:
            ValueError: layout 非法（见 ``parse_layout()``）、``arms`` 为空 / 重复 / 含未知臂、
                动作长度 != 段宽合计 × 臂数，或含非有限值。
        """
        segments = self.parse_layout(layout)
        names = self.state_arms()
        scope = list(names) if arms is None else [str(arm) for arm in arms]
        if not scope or len(set(scope)) != len(scope) or any(arm not in names for arm in scope):
            raise ValueError(f"{self.name}: arms must be a nonempty unique subset of {names} (got {arms})")
        width = sum(dim for _, dim in segments)
        values = np.asarray(action, dtype=np.float64).reshape(-1)
        expected = width * len(scope)
        if values.shape[0] != expected:
            raise ValueError(
                f"{self.name}: action dim {values.shape[0]} != layout {self.layout_name(segments)!r}"
                f" ({width}/arm) × arms {len(scope)} = {expected}"
            )
        if not np.all(np.isfinite(values)):
            raise ValueError(f"{self.name}: action contains non-finite values (NaN / inf)")
        return LayoutPlan(
            layout=self.layout_name(segments),
            segments=segments,
            scope=scope,
            blocks=values.reshape(len(scope), width),
        )

    @classmethod
    def layout_name(cls, segments) -> str:
        """段序列 → layout 名（保持请求里写的段序）。"""
        return cls.LAYOUT_SEPARATOR.join(str(kind) for kind, _ in segments)

    def apply_layout(self, action, layout: str | None = None, arms: list[str] | None = None) -> None:
        """按 ``layout`` / ``arms`` 写目标。

        - 运动段写关节目标：绝对段覆盖，delta 段叠加在当前目标；位姿类先完成 FK / IK；
        - 夹爪段写夹爪目标：绝对段覆盖，delta 段叠加，最终钳到 ``[0, 1]``；
        - ``joint_target`` / ``gripper_target`` 仅在所有转换成功后一起提交。

        解算失败（超限位 / 不收敛）→ ``CartesianActionError`` 且不改目标。
        """
        plan = self.plan_layout(action, layout, arms)
        joint_target = self._joint_target_snapshot()
        gripper_target = self._gripper_target_snapshot()
        offsets: dict[str, tuple[int, int]] = {}
        cursor = 0
        for kind, dim in plan.segments:
            offsets[kind] = (cursor, dim)
            cursor += dim
        names = self.state_arms()
        joint_per_arm = self.joint_dim() // len(names)
        motion_kind = next((kind for kind, _ in plan.segments if kind in self.MOTION_ACTION_SPACES), None)
        if motion_kind is not None:
            offset, dim = offsets[motion_kind]
            if motion_kind in (self.ACTION_SPACE_JOINT, self.ACTION_SPACE_JOINT_DELTA):
                full = joint_target.copy()
            elif motion_kind == self.ACTION_SPACE_POSE:
                pose = self.get_pose_action()
                if pose is None:
                    raise CartesianActionError(f"{self.name}: pose target base is unavailable")
                full = np.asarray(pose, dtype=np.float64).reshape(-1).copy()
            else:
                # pose_delta 的未选臂增量必须是 0；不能把该臂的关节目标误当成位姿增量。
                full = np.zeros(self.pose_dim(), dtype=np.float64)
            for row, arm in enumerate(plan.scope):
                index = names.index(arm)
                values = plan.blocks[row][offset : offset + dim]
                span = slice(index * dim, (index + 1) * dim)
                if motion_kind == self.ACTION_SPACE_JOINT_DELTA:
                    full[span] += values
                else:
                    full[span] = values
            solved = full if motion_kind == self.ACTION_SPACE_JOINT_DELTA else self._prepare_target(full, motion_kind)
            for arm in plan.scope:
                index = names.index(arm)
                joint_target[index * joint_per_arm : (index + 1) * joint_per_arm] = solved[
                    index * joint_per_arm : (index + 1) * joint_per_arm
                ]
        gripper_kind = next((kind for kind, _ in plan.segments if kind in self.GRIPPER_ACTION_SPACES), None)
        if gripper_kind is not None:
            offset, _ = offsets[gripper_kind]
            for row, arm in enumerate(plan.scope):
                index = names.index(arm)
                value = float(plan.blocks[row][offset])
                if gripper_kind == self.ACTION_SPACE_GRIPPER_DELTA:
                    value += gripper_target[index]
                gripper_target[index] = float(np.clip(value, 0.0, 1.0))
        self.joint_target = joint_target
        self.gripper_target = gripper_target
        debug_print(self.name, f"layout {plan.layout} applied (arms={plan.scope})", "DEBUG")

    def execute(
        self,
        action: np.ndarray,
        *,
        layout: str | None = None,
        arms: list[str] | None = None,
    ):
        """直接下发动作指令（程控抢回：会结束遥操作）：按 ``layout`` 写目标。

        - ``layout``（缺省 ``joint``）：每臂块内的段序列；绝对段覆盖目标，``*_delta`` 叠加目标，
          ``pose`` / ``pose_delta`` 先解算成关节目标，各段同一拍落地。
        - ``arms``（缺省 = 全部臂）：只写这些臂，其余臂目标原样保留；给出时 ``action`` 维度按
          所选臂算（要「只控部分臂」必须显式给 ``arms``）。
        """
        self.apply_layout(action, layout, arms)
        self.disable_teleop()  # 仅成功落地后抢回；非法动作 / IK 失败保持原遥操作状态

    def rollout(
        self,
        action: np.ndarray,
        *,
        layout: str | None = None,
        arms: list[str] | None = None,
    ) -> bool:
        """推理闭环：遥操作进行中则拒绝（返回 False）。

        遥操作开启即人工接管（不分 ``absolute`` / ``delta``）：从臂 target 由人工决定，推理下发
        让位——不改 target、也不退出遥操作（与 ``execute()`` 的「程控抢回」相反），遥操作关闭
        后自动恢复。``layout`` / ``arms`` 语义同 ``execute()``。
        """
        if self.teleop_enabled:
            debug_print(self.name, "rollout refused: teleop (human takeover) active.", "WARNING")
            return False
        self.execute(action, layout=layout, arms=arms)
        return True

    def safe_stop(self):
        """安全停止（幂等）：退出遥操作并清空目标，``step()`` 不再推进。

        这是「软停」：停止下发新目标并保持当前位姿（关节仍带力矩），不断电；硬件急停
        （``PiperController.emergency_stop``）不在这条路径上。
        """
        self.disable_teleop()  # 退出遥操作（含清空增量锚点）
        self.joint_target = None
        self.gripper_target = None
        debug_print(self.name, "Safe stop executed.", "WARNING")

    def _prepare_target(self, motion_target: np.ndarray, kind: str | None) -> np.ndarray:
        """把 joint / pose / pose_delta 统一转换为关节目标。"""
        space = self.normalize_action_space(kind)
        values = np.asarray(motion_target, dtype=np.float64).reshape(-1)
        if space == self.ACTION_SPACE_JOINT:
            if values.shape[0] != self.joint_dim():
                raise ValueError(f"{self.name}: joint action dim {values.shape[0]} != joint_dim() {self.joint_dim()}")
            return values
        if space not in (self.ACTION_SPACE_POSE, self.ACTION_SPACE_POSE_DELTA):
            raise CartesianActionError(f"{self.name}: cannot convert {space!r} to joints")
        target_pose = self._absolute_pose_target(space, values)
        seed = (
            self._joint_target_or_command()
            if space == self.ACTION_SPACE_POSE_DELTA
            else self._joint_command_or_fallback()
        )
        return self._solve_pose(target_pose, seed)

    # ---- 末端 TCP 帧换算（位姿读 / 写各自的唯一进出口）----
    def tcp_offset_of(self, arm: str) -> np.ndarray | None:
        """该臂的 TCP 偏移（法兰系，米）；未开启 TCP / 该臂未配置 → ``None``（= 恒等）。"""
        return self.tcp_offsets.get(str(arm))

    def pose_to_tcp(self, pose: np.ndarray) -> np.ndarray:
        """法兰系扁平位姿 → TCP 系（读路径唯一收口：实测位姿与目标位姿共用）。

        未开启 ``robot.tcp`` → 原值返回。
        """
        if not self.tcp_offsets:
            return np.asarray(pose, dtype=np.float64).reshape(-1).copy()
        offsets = [self.tcp_offset_of(arm) for arm in self.ARM_NAMES]
        return tcp.to_tcp(pose, offsets, per_arm=self.POSE_DIM_PER_ARM)

    def tcp_to_flange(self, pose: np.ndarray) -> np.ndarray:
        """TCP 系扁平位姿 → 法兰系（写路径唯一收口：逐臂解算前还原）。

        未开启 ``robot.tcp`` → 原值返回；IK / 限位因此只看法兰。
        """
        if not self.tcp_offsets:
            return np.asarray(pose, dtype=np.float64).reshape(-1).copy()
        offsets = [self.tcp_offset_of(arm) for arm in self.ARM_NAMES]
        return tcp.from_tcp(pose, offsets, per_arm=self.POSE_DIM_PER_ARM)

    def _tcp_section(self, enabled: bool, offset=None) -> dict:
        """运行期切换用的配置段：yml 的 ``robot.tcp`` + 运行期覆盖（``enabled`` / ``offset``）。

        ``offset`` 是运行期值，不写回 yml。
        """
        section = dict(self.robot_config.get(tcp.SECTION) or {})
        if offset is not None:
            section["offset"] = list(offset)
        section["enabled"] = bool(enabled)
        return section

    def set_tcp(self, enabled: bool, offset=None) -> None:
        """运行期切换位姿系（可开关 / 改偏移）：非法配置 → ``ValueError``（零副作用）。

        只改语义（``pose`` / ``pose_delta`` 与两个位姿观测键的原点），不动机目标——同一串数值
        换个原点，机械臂不会跳。切换需离开会话，同一 episode 内语义不能前后不一致。

        先校验再动状态：偏移非法（非 3 个数 / 非有限 / 未知键）→ ``ValueError`` 且运行位不变。
        """
        offsets = tcp.resolve_tcp(self._tcp_section(enabled, offset), tuple(self.ARM_NAMES))
        self.tcp_offsets, self.pose_frame = offsets, (tcp.FRAME_TCP if offsets else tcp.FRAME_FLANGE)
        detail = {arm: [round(float(v), 4) for v in off] for arm, off in offsets.items()}
        debug_print(
            self.name, f"位姿系已切换为 {self.pose_frame}" + (f"（offset={detail}）" if offsets else ""), "INFO"
        )

    def tcp_status(self) -> dict:
        """TCP 运行位（→ ``/v1/health``）：位姿系名 + 逐臂偏移（未开启 → ``tcp_offset`` 为 None）。"""
        return {
            "pose_frame": self.pose_frame,
            "tcp_offset": None
            if not self.tcp_offsets
            else {str(arm): [float(value) for value in tuple(offset)[:3]] for arm, offset in self.tcp_offsets.items()},
        }

    def set_gravity(self, enabled: bool, alpha: float | None = None) -> list[str]:
        """运行期开关重力前馈（只装在执行臂）：返回实际生效的控制器键。

        ``enabled=False`` → 卸载模型（``t_ff`` 下一拍起回到 0）；``alpha`` 给定时覆盖配置的比例
        （``0`` = 保留装载但不补偿）。参数 / 限幅仍来自 ``robot.gravity``。

        找不到可装载的执行控制器 → ``ValueError``（robot server 回 422），且不动任何状态。
        """
        if alpha is not None and not np.isfinite(float(alpha)):
            raise ValueError(f"{self.name}: invalid alpha {alpha!r}")
        controllers = self.controllers
        applied = [
            str(key)
            for key in self.ARM_CONTROLLERS.values()
            if callable(getattr(controllers.get(key), "enable_gravity_compensation", None))
        ]
        if not applied:
            raise ValueError(f"{self.name}: no controller supports gravity compensation")
        section = dict(self.robot_config.get("gravity") or {})
        section["enabled"] = bool(enabled)
        if alpha is not None:
            section["alpha"] = float(alpha)
        for key in applied:
            controllers[key].enable_gravity_compensation(section)
        return applied

    # ---- 位姿类动作的公共实现（``pose`` / ``pose_delta``；子类提供 FK / IK 接入）----
    def _absolute_pose_target(self, space: str, values: np.ndarray) -> np.ndarray:
        """位姿类动作 → 绝对目标位姿（每臂 ``xyz + rpy``，扁平 ``pose_dim()`` 维）。

        - ``pose``：值就是绝对目标，直用；
        - ``pose_delta``：``target = FK(关节段目标) + [Δxyz, wrap(Δrpy)]``；基准不可用 →
          ``CartesianActionError``。
        """
        expected = self.POSE_DIM_PER_ARM * len(self.ARM_NAMES)
        if values.shape[0] != expected:
            raise CartesianActionError(
                f"{self.name}: {space} action dim {values.shape[0]} != {expected}"
                f" ({self.POSE_DIM_PER_ARM} per arm × {len(self.ARM_NAMES)} arms)"
            )
        if not np.all(np.isfinite(values)):
            raise CartesianActionError(f"{self.name}: {space} action contains non-finite values")
        if space == self.ACTION_SPACE_POSE:
            return values
        base = self.get_pose_action()
        if base is None or not np.all(np.isfinite(np.asarray(base).reshape(-1)[:expected])):
            raise CartesianActionError(
                f"{self.name}: {space} needs a valid target pose as its base (FK of the joint target)"
            )
        target = np.asarray(base, dtype=np.float64).reshape(-1)[:expected].copy()
        for index in range(len(self.ARM_NAMES)):
            start = index * self.POSE_DIM_PER_ARM
            span = slice(start, start + self.POSE_DIM_PER_ARM)
            target[span] = target[span] + values[span]
            target[start + 3 : start + self.POSE_DIM_PER_ARM] = wrap_angles(
                target[start + 3 : start + self.POSE_DIM_PER_ARM]
            )
        return target

    def _joint_target_or_command(self) -> np.ndarray:
        """关节目标（未下发过指令时取当前关节指令）。

        增量动作的基准与逆解起点都取它（不是实测关节角）。
        """
        if self.joint_target is not None:
            return np.array(self.joint_target, dtype=np.float64)
        return np.asarray(self._joint_command_or_fallback(), dtype=np.float64)

    def set_joint_target(self, joint_target: np.ndarray | None):
        """写关节段目标（绝对值）：``step()`` 每帧把关节段朝它限速移动。

        ``None`` = 清空整条目标（安全停止）。增量（人工接管）写入用 ``set_joint_target_delta()``。
        """
        if joint_target is None:
            self.joint_target = None
            return
        values = np.asarray(joint_target, dtype=np.float64).reshape(-1)
        if values.shape[0] != self.joint_dim():
            raise ValueError(f"{self.name}: joint action dim {values.shape[0]} != joint_dim() {self.joint_dim()}")
        self.joint_target = values.copy()

    def set_gripper_target(self, gripper_target: np.ndarray | None):
        """写夹爪段目标（绝对值，每臂 1 维）：``step()`` 每拍直接跟随（不插值）。

        维度必须等于 ``gripper_dim()``；取值钳到 ``[0, 1]``（钳到时告警一条）。
        """
        if gripper_target is None:
            return  # None = 不改夹爪段（要复位用 reset()）
        values = np.asarray(gripper_target, dtype=np.float64).reshape(-1)
        if values.shape[0] != self.gripper_dim():
            raise ValueError(f"{self.name}: gripper action dim {values.shape[0]} != gripper_dim() {self.gripper_dim()}")
        clipped = np.clip(values, 0.0, 1.0)
        if not np.allclose(clipped, values):
            debug_print(self.name, f"gripper target clipped to [0, 1]: {values.tolist()}", "WARNING")
        self.gripper_target = clipped.copy()

    def _joint_target_snapshot(self) -> np.ndarray:
        """构造本次下发的局部目标副本；不修改 ``joint_target``。

        ``apply_layout()`` 在局部副本上完成补段与 FK / IK，全部成功后才一次提交。这样首条命令
        解算失败时，``joint_target`` 仍保持 ``None``，符合失败零副作用契约。
        """
        return self.joint_target.copy() if self.joint_target is not None else self._joint_command_or_fallback()

    def _gripper_target_snapshot(self) -> np.ndarray:
        """构造夹爪目标副本，供组合 layout 原子提交。"""
        return self.gripper_target.copy() if self.gripper_target is not None else self._gripper_command_or_fallback()

    # ---- 遥操作（模式 / 锚点）----
    def resolve_teleop_follow(self, follow_space=None, follow_axes=None) -> tuple[str, tuple[str, ...]]:
        """校验本次 teleop 的跟随空间与 pose 轴；``None`` 沿用启动配置。"""
        space = self._configured_teleop_follow_space if follow_space is None else str(follow_space).strip()
        if space not in self.TELEOP_FOLLOW_SPACES:
            raise ValueError(f"{self.name}: follow_space must be one of {self.TELEOP_FOLLOW_SPACES} (got {space!r})")
        if space == self.TELEOP_FOLLOW_JOINT:
            if follow_axes is not None:
                raise ValueError(f"{self.name}: follow_axes is only valid for pose teleop")
            return space, ()
        if self.ACTION_SPACE_POSE not in self.ACTION_SPACES or self.pose_dim() <= 0:
            raise ValueError(f"{self.name}: follow_space='pose' needs pose action capability")
        expected_pose = self.POSE_DIM_PER_ARM * len(self.ARM_NAMES)
        if self.POSE_DIM_PER_ARM != len(self.TELEOP_POSE_AXES) or self.pose_dim() != expected_pose:
            raise ValueError(
                f"{self.name}: pose teleop needs {len(self.TELEOP_POSE_AXES)} pose dims per arm"
                f" and pose_dim()={expected_pose} (got per_arm={self.POSE_DIM_PER_ARM}, pose_dim()={self.pose_dim()})"
            )
        if follow_axes is None:
            axes = (
                self._configured_teleop_follow_axes
                if self._configured_teleop_follow_space == self.TELEOP_FOLLOW_POSE
                else self.TELEOP_POSE_AXES
            )
            return space, axes
        if not isinstance(follow_axes, (list, tuple)):
            raise ValueError(f"{self.name}: follow_axes must be a list (got {follow_axes!r})")
        axes = tuple(str(axis).strip() for axis in follow_axes)
        if not axes or len(set(axes)) != len(axes) or any(axis not in self.TELEOP_POSE_AXES for axis in axes):
            raise ValueError(
                f"{self.name}: follow_axes must be a nonempty unique subset of"
                f" {self.TELEOP_POSE_AXES} (got {follow_axes!r})"
            )
        return space, axes

    def enable_teleop(self, mode: str = TELEOP_MODE_ABSOLUTE, follow_axes=None, follow_space=None) -> bool:
        """启用遥操作（``mode`` 决定主臂读数 → 从臂 target 的映射）。

        ``robot.teleop.follow_space`` 决定关节映射或 pose FK→IK；``mode`` 决定 absolute / delta。
        pose 的未跟随轴锁在启用后首拍的从臂位姿锚点。HTTP / Edge 契约仍只有既有 ``mode``。

        返回是否切换成功（模式名非法 → False，保持原模式与 target 不变）。
        """
        if mode not in self.TELEOP_MODES:
            debug_print(self.name, f"Unknown teleop mode {mode!r} (expect {self.TELEOP_MODES})", "ERROR")
            return False
        space, axes = self.resolve_teleop_follow(follow_space, follow_axes)
        self.teleop_mode = mode
        self.teleop_follow_space = space
        self.teleop_follow_axes = axes
        self._clear_teleop_anchor()
        self.teleop_enabled = True
        axes = f", axes={list(self.teleop_follow_axes)}" if self.teleop_follow_space == self.TELEOP_FOLLOW_POSE else ""
        debug_print(self.name, f"Teleop enabled (space={self.teleop_follow_space}, mode={mode}{axes})", "INFO")
        return True

    def disable_teleop(self):
        """关闭遥操作：回到程序控制，不再由主臂刷新目标（并清空增量锚点）。"""
        if not self.teleop_enabled:
            return  # reset / execute / rollout 每次都会调用：未开启时静默
        self.teleop_enabled = False
        self._clear_teleop_anchor()
        debug_print(self.name, f"Teleop disabled (mode={self.teleop_mode})", "INFO")

    def set_joint_target_delta(self, delta: np.ndarray) -> bool:
        """在增量锚点（接管瞬间的从臂关节角）上叠加增量，写关节段目标。

        ``delta`` = 主臂读数 − 主臂锚点；限速仍由 ``step()`` 完成，本接口不限幅。
        锚点未采样时返回 False 且不写 target。
        """
        if self.teleop_slave_ref is None:
            return False
        self.set_joint_target(self.teleop_slave_ref + np.asarray(delta, dtype=np.float64))
        return True

    def set_gripper_target_delta(self, delta: np.ndarray) -> bool:
        """在夹爪锚点上叠加增量（与关节段同一套语义）；锚点未采样 → False。"""
        if self.teleop_slave_gripper_ref is None:
            return False
        self.set_gripper_target(self.teleop_slave_gripper_ref + np.asarray(delta, dtype=np.float64))
        return True

    def _joint_command_or_fallback(self) -> np.ndarray:
        """当前关节命令；尚未产生控制拍时依次回退目标与初始值。

        供增量接管采样从臂锚点：接管后 target 从该位姿出发，此前残留的推理 target 被丢弃。
        """
        if self.joint_command is not None:
            return self.joint_command.copy()
        if self.joint_target is not None:
            return self.joint_target.copy()
        return np.asarray(self.init_joint, dtype=np.float64)

    def _gripper_command_or_fallback(self) -> np.ndarray:
        """当前夹爪命令；尚未产生控制拍时依次回退目标与初始值。"""
        if self.gripper_command is not None:
            return self.gripper_command.copy()
        if self.gripper_target is not None:
            return self.gripper_target.copy()
        return np.asarray(self.init_gripper, dtype=np.float64)

    def _capture_teleop_anchor(self, master_joints: np.ndarray, master_gripper: np.ndarray):
        """采样遥操作锚点：主 / 从关节、夹爪，以及 pose 跟随需要的两侧位姿。

        所有值先计算 / 校验再一次写入；FK 失败不会留下半套锚点。joint+absolute 不需要锚点，
        其余组合均在首次有效读数拍调用。
        """
        master_ref = np.asarray(master_joints, dtype=np.float64).reshape(-1).copy()
        slave_ref = self._joint_command_or_fallback()
        master_gripper_ref = np.asarray(master_gripper, dtype=np.float64).reshape(-1).copy()
        slave_gripper_ref = self._gripper_command_or_fallback()
        master_pose_ref = slave_pose_ref = None
        if self.teleop_follow_space == self.TELEOP_FOLLOW_POSE:
            master_pose_ref = self._teleop_master_pose(master_ref)
            slave_pose_ref = self._valid_teleop_pose(self._pose_of_joints(slave_ref), "slave")
        self.teleop_master_ref = master_ref
        self.teleop_slave_ref = slave_ref
        self.teleop_master_pose_ref = master_pose_ref
        self.teleop_slave_pose_ref = slave_pose_ref
        self.teleop_master_gripper_ref = master_gripper_ref
        self.teleop_slave_gripper_ref = slave_gripper_ref
        debug_print(
            self.name,
            f"Teleop anchor captured (space={self.teleop_follow_space}, mode={self.teleop_mode}).",
            "INFO",
        )

    def _valid_teleop_pose(self, pose, side: str) -> np.ndarray:
        """校验 teleop FK 结果为完整、有限的 ``pose_dim()`` 维向量。"""
        values = np.asarray(pose, dtype=np.float64).reshape(-1)
        if values.shape[0] != self.pose_dim() or not np.all(np.isfinite(values)):
            raise CartesianActionError(
                f"{self.name}: teleop {side} pose invalid (dim={values.shape[0]}, expected={self.pose_dim()})"
            )
        return values.copy()

    def _teleop_master_pose(self, master_joints: np.ndarray) -> np.ndarray:
        """主臂 FK 位姿叠加配置偏置，并规范化各臂 RPY。"""
        pose = self._valid_teleop_pose(self._pose_of_joints(master_joints), "master")
        pose += self.teleop_pose_offset
        for arm_index in range(len(self.ARM_NAMES)):
            start = arm_index * self.POSE_DIM_PER_ARM
            pose[start + 3 : start + self.POSE_DIM_PER_ARM] = wrap_angles(
                pose[start + 3 : start + self.POSE_DIM_PER_ARM]
            )
        return pose

    def _pose_teleop_target(self, master_joints: np.ndarray) -> np.ndarray:
        """按映射模式与轴白名单合成目标位姿并 IK，返回关节段目标。

        未跟随轴保留从臂首拍位姿；姿态增量与结果按现有 RPY chart wrap。IK seed 使用上一关节
        目标，使连续 teleop 拍优先落在同一解支。
        """
        if self.teleop_master_pose_ref is None or self.teleop_slave_pose_ref is None:
            raise CartesianActionError(f"{self.name}: pose teleop anchor is unavailable")
        master_pose = self._teleop_master_pose(master_joints)
        target = self.teleop_slave_pose_ref.copy()
        followed = set(self.teleop_follow_axes)
        per_arm = self.POSE_DIM_PER_ARM
        for arm_index in range(len(self.ARM_NAMES)):
            start = arm_index * per_arm
            for axis_index, axis in enumerate(self.TELEOP_POSE_AXES):
                if axis not in followed:
                    continue
                index = start + axis_index
                if self.teleop_mode == self.TELEOP_MODE_DELTA:
                    delta = master_pose[index] - self.teleop_master_pose_ref[index]
                    if axis_index >= 3:
                        delta = float(wrap_angles([delta])[0])
                    target[index] = self.teleop_slave_pose_ref[index] + delta
                else:
                    target[index] = master_pose[index]
            target[start + 3 : start + per_arm] = wrap_angles(target[start + 3 : start + per_arm])
        solved = np.asarray(self._solve_pose(target, self._joint_target_or_command()), dtype=np.float64).reshape(-1)
        if solved.shape[0] != self.joint_dim() or not np.all(np.isfinite(solved)):
            raise CartesianActionError(
                f"{self.name}: pose teleop IK returned invalid joints "
                f"(dim={solved.shape[0]}, expected={self.joint_dim()})"
            )
        return solved

    def _clear_teleop_anchor(self):
        """清空增量锚点与最近主臂读数（开启 / 关闭遥操作时调用；锚点在接管期间固定不变）。"""
        self.teleop_master_ref = None
        self.teleop_slave_ref = None
        self.teleop_master_pose_ref = None
        self.teleop_slave_pose_ref = None
        self.teleop_master_gripper_ref = None
        self.teleop_slave_gripper_ref = None
        self.teleop_master_now = None
        self.teleop_master_gripper_now = None
        self._teleop_mapping_error = None

    def _refresh_teleop_targets(self):
        """本拍遥操作 target 刷新（``step()`` 调用；未开启 / 读不到 / 读取异常 → 保持原 target）。

        joint 跟随沿用既有 absolute / delta；pose 跟随先 FK，再按轴白名单合成位姿并 IK。锚点尚未
        采样时本拍先原子采样。主臂读不到或位姿映射失败都只让本拍保持原目标，不置 ``last_error``。

        关节偏置在 FK 前、位姿偏置在 FK 后叠加；两者都早于锚点，故 ``absolute`` 下可见、
        ``delta`` 下被锚点抵消。位姿偏置不参与 joint 跟随。
        """
        if not self.teleop_enabled:
            return
        try:
            master = self._read_teleop_joint()
            master_gripper = self._read_teleop_gripper()
        except Exception as exc:  # noqa: BLE001 主臂读取异常：本拍保持原 target，下一拍重试
            self._note_teleop_read_failure(exc)
            return
        if master is None or master_gripper is None:
            self._note_teleop_read_failure("master reading unavailable (None)")
            return  # 主臂某段读不到：保持原 target，下一拍重试
        self._teleop_read_error = None  # 恢复正常：下次失败重新告警
        # 常量偏置叠在读数上（先叠再取锚点：delta 模式因此自动抵消，偏置不进入增量）
        master = np.asarray(master, dtype=np.float64) + self.teleop_joint_offset
        master_gripper = np.asarray(master_gripper, dtype=np.float64)
        # 最近一次成功读数：采集侧「帧头跳过」用它判断主臂是否已有效移动
        self.teleop_master_now = master
        self.teleop_master_gripper_now = master_gripper
        needs_anchor = self.teleop_mode == self.TELEOP_MODE_DELTA or self.teleop_follow_space == self.TELEOP_FOLLOW_POSE
        if needs_anchor and self.teleop_master_ref is None:
            try:
                self._capture_teleop_anchor(master, master_gripper)
            except Exception as exc:  # noqa: BLE001 FK / 锚点失败：整拍保持，下一拍重试
                self._note_teleop_mapping_failure(exc)
                return
        try:
            if self.teleop_follow_space == self.TELEOP_FOLLOW_POSE:
                self.set_joint_target(self._pose_teleop_target(master))
            elif self.teleop_mode == self.TELEOP_MODE_DELTA:
                self.set_joint_target_delta(master - self.teleop_master_ref)
            else:
                self.set_joint_target(master)
        except Exception as exc:  # noqa: BLE001 IK / 位姿合成失败：保持上一目标，下一拍重试
            self._note_teleop_mapping_failure(exc)
            return
        self._teleop_mapping_error = None
        if self.teleop_mode != self.TELEOP_MODE_DELTA:
            self.set_gripper_target(master_gripper)
            return
        self.set_gripper_target_delta(master_gripper - self.teleop_master_gripper_ref)

    def _note_teleop_mapping_failure(self, reason) -> None:
        """FK / IK / 位姿合成失败：累计 + 同原因限流告警，不改变目标与 health。"""
        self.teleop_mapping_failures += 1
        text = str(reason)
        if text != self._teleop_mapping_error:
            self._teleop_mapping_error = text
            debug_print(
                self.name,
                f"teleop pose mapping unavailable: {text}（本拍保持原 target，下一拍重试）",
                "WARNING",
            )

    def _note_teleop_read_failure(self, reason) -> None:
        """主臂读数不可用（``None`` / 异常）：累计计数 + 同一原因只告警一条。

        接管中读不到主臂是预期状态，因此不置 ``last_error``、不影响 health，但要可观测。
        计数在 ``teleop_read_failures``（累计，恢复后不清零）。
        """
        self.teleop_read_failures += 1
        text = str(reason)
        if text != self._teleop_read_error:
            self._teleop_read_error = text
            debug_print(
                self.name,
                f"teleop master read unavailable: {text}（本拍保持原 target，下一拍重试）",
                "WARNING",
            )

    def teleop_master_sample(self) -> tuple[np.ndarray, np.ndarray] | None:
        """最近一次成功的主臂读数 ``(关节, 夹爪)``；未开启遥操作 / 还没读到 → ``None``。

        与 ``_read_teleop_joint()`` 同源（控制线程每拍刷新），供采集侧「帧头跳过」复用。
        返回的数组只读使用。
        """
        if not self.teleop_enabled or self.teleop_master_now is None or self.teleop_master_gripper_now is None:
            return None
        return self.teleop_master_now, self.teleop_master_gripper_now

    def _read_teleop_joint(self) -> np.ndarray | None:
        """遥操作关节段目标源：返回主臂关节角读数；默认 None（无遥操作源）。

        子类只需实现「读主臂」，``absolute`` / ``delta`` 的差异全部由本基类映射。
        """
        return None

    def _read_teleop_gripper(self) -> np.ndarray | None:
        """遥操作夹爪段目标源：返回主臂夹爪读数（每臂 1 维）；默认 None。"""
        return None

    # ---- 每帧推进 --------------------------------------------------------------------
    def step(self):
        """刷新目标，关节限速前进，夹爪直接跟随，再下发本拍指令。"""
        self._refresh_teleop_targets()
        if self.joint_target is None and self.gripper_target is None:
            return
        if self.joint_command is None:
            self.joint_command = self.get_observation_joint()
        if self.gripper_command is None:
            self.gripper_command = self.get_observation_gripper()
        if self.joint_target is not None:
            self.joint_command = self._step_toward(self.joint_command, self.joint_target, self.step_rad)
        if self.gripper_target is not None:
            self.gripper_command = self.gripper_target.copy()
        self._apply_commands(self.joint_command, self.gripper_command)

    def _apply_commands(self, joints: np.ndarray, gripper: np.ndarray):
        """下发本拍关节与夹爪指令。"""
        raise NotImplementedError

    # ---- 每拍状态采样 / 观测组装（**原始数据，无契约键**；由 robot server 组装 standard_obs）----
    def sample_state(self) -> dict:
        """采样并缓存分离的关节、夹爪及其目标；位姿由同拍关节正解。"""
        self.seq += 1
        joint = self.get_observation_joint()
        gripper = self.get_observation_gripper()
        joint_target = self.get_joint_target()
        gripper_target = self.get_gripper_target()
        joint_action = joint.copy() if joint_target is None else joint_target
        gripper_action = gripper.copy() if gripper_target is None else gripper_target
        state = {
            self.KEY_JOINT: joint,
            self.KEY_GRIPPER: gripper,
            self.KEY_JOINT_ACTION: joint_action,
            self.KEY_GRIPPER_ACTION: gripper_action,
            self.KEY_JOINT_ERROR: joint_action - joint,
            self.KEY_GRIPPER_ERROR: gripper_action - gripper,
        }
        pose = self.get_observation_pose(joint)
        if pose is not None:
            state[self.KEY_POSE] = pose
        target_pose = self.get_pose_action()
        if target_pose is not None:
            state[self.KEY_POSE_ACTION] = target_pose
        self.motion_state = state
        return self.motion_state

    # ---- 相机与深度（彩色 + 深度观测；相机元数据供 robot server 上报）------------------
    def _resolve_depth_cameras(self) -> list[str]:
        """解析 ``robot.depth`` → 生效的深度相机（顺序 = ``IMAGE_NAMES`` 子序）。

        - ``enabled: false`` → 空（不开深度流）；
        - ``cameras`` 缺省 / 空 → 全部具备深度的相机（``DEPTH_CAMERAS``）；
        - 给了则必须是 ``DEPTH_CAMERAS`` 的子集，否则 ``ValueError``；
        - 未知键打 WARNING。
        """
        depth_config = self.robot_config.get("depth") or {}
        unknown = [key for key in depth_config if key not in ("enabled", "cameras")]
        if unknown:
            debug_print(self.name, f"robot.depth 未知键 {unknown}（可用：['enabled', 'cameras']），已忽略", "WARNING")
        if not bool(depth_config.get("enabled", True)):
            return []
        requested = [str(name).strip() for name in (depth_config.get("cameras") or [])]
        if not requested:
            return [name for name in self.IMAGE_NAMES if name in self.DEPTH_CAMERAS]
        unsupported = [name for name in requested if name not in self.DEPTH_CAMERAS]
        if unsupported:
            raise ValueError(
                f"{self.name}: robot.depth.cameras 含无深度能力的相机 {unsupported}（可用：{list(self.DEPTH_CAMERAS)}）"
            )
        return [name for name in self.IMAGE_NAMES if name in requested]

    def depth_camera_names(self) -> list[str]:
        """生效的深度相机名（顺序 = ``IMAGE_NAMES`` 子序）：配置开关 ∩ 具备深度的相机。

        取帧（``connect()`` 传 ``enable_depth``）与观测发布（``capture_frames()``）共用。
        """
        return list(self.depth_cameras)

    def get_camera_info(self, name: str) -> dict:
        """单个相机的静态元数据（键名与 edge adapter 契约一致）——子类按需覆盖。

        缺省问 sensor（``RealsenseSensor.camera_info()``：尺寸 / 彩色内参 / 深度比例）；
        无 sensor 时回退 ``IMAGES`` 的尺寸，内参为空。
        """
        sensor = self.sensors.get(name)
        if sensor is not None and hasattr(sensor, "camera_info"):
            return dict(sensor.camera_info())
        width, height = self.IMAGES.get(name, (0, 0))
        return {"name": name, "width": int(width), "height": int(height), "intrinsics": {}, "depth": None}

    def camera_mount(self, name: str) -> tuple[str, str | None]:
        """相机的安装方式 → ``(mount, arm)``（装配事实，取自 ``WRIST_CAMERAS``）：

        - ``("wrist", "<臂名>")``：装在末端法兰上，世界位姿随臂动（外参 = ``T_flange_cam``）；
        - ``("fixed", None)``：装在工作台 / 机身，外参是常数 ``T_world_cam``。
        """
        arm = self.WRIST_CAMERAS.get(name)
        return (VALUE_MOUNT_WRIST, str(arm)) if arm else (VALUE_MOUNT_FIXED, None)

    def camera_meta(self) -> list[dict]:
        """各相机静态元数据（顺序 = ``IMAGE_NAMES``）——robot server 的 ``GET /v1/cameras`` 用。

        深度段只对生效的深度相机保留（其余 ``depth: null``），另附安装方式（``mount`` / ``arm``）。
        标定外参另由 ``frames``（frames.json 原样）承载。
        """
        depth_names = self.depth_camera_names()
        infos = []
        for name in self.IMAGE_NAMES:
            info = self.get_camera_info(name)
            if name not in depth_names:
                info["depth"] = None
            mount, arm = self.camera_mount(name)
            info[FIELD_MOUNT] = mount
            info[FIELD_ARM] = arm
            infos.append(info)
        return infos

    def build_observation(self) -> dict | None:
        """组装完整观测 = 最新缓存机械臂状态 + 本拍相机帧（彩色 + 深度）——观测线程调用。

        ``motion_state`` 为空（启动瞬间 / 机械臂读取失败）时返回 None，本拍不出观测。
        """
        state = self.motion_state
        if state is None:
            return None
        timestamp = time.time()  # 观测拍时刻（取帧前打点；取帧 / 落盘耗时不计入）
        return {**state, **self.capture_frames(), self.KEY_TIMESTAMP: timestamp}

    def get_observation(self) -> dict:
        """完整观测 = 现场采样机械臂状态 + 相机帧（彩色 + 深度，单线程调用）。

        相机取帧会阻塞，勿在控制线程调用（会拖慢机械臂步进）；供单线程脚本 / 调试整体取一帧。
        """
        state = self.sample_state()
        timestamp = time.time()  # 帧时刻（取帧前打点，与 build_observation 同口径）
        return {**state, **self.capture_frames(), self.KEY_TIMESTAMP: timestamp}

    def get_observation_joint(self) -> np.ndarray:
        """当前帧关节角（扁平 ``joint_dim()`` 维，每臂 6）——子类实现。

        返回值写入原始观测的 ``joint`` 键；读不到可信读数时应抛 ``RuntimeError``。
        """
        raise NotImplementedError

    def get_observation_pose(self, joint: np.ndarray | None = None) -> np.ndarray | None:
        """末端位姿（扁平 ``pose_dim()`` 维：各臂 ``xyz + rpy``）——子类按需实现，缺省不提供。

        提供位姿的机器人用同一拍关节角让控制器的静态正解函数正算；关节读数缺失 / 非法时可返回
        NaN 向量（下游丢弃该帧位姿）。不提供位姿的机器人返回 ``None``（观测里没有该键）。
        """
        return None

    def get_pose_action(self) -> np.ndarray | None:
        """目标位姿 = ``FK(关节段目标)``（扁平 ``pose_dim()`` 维）——子类按需实现，缺省不提供。

        与 ``get_observation_pose()`` 同一套静态正解，喂进去的是 ``_joint_target_or_command()``；与
        ``observations/pose`` 同系可比，「目标 − 实测」即稳态误差。``pose_delta`` 的基准与解算
        结果都靠它。不提供位姿的机器人返回 ``None``。
        """
        return None

    def _pose_of_joints(self, joints: np.ndarray) -> np.ndarray:
        """关节向量 → 位姿向量的 teleop/FK 钩子；支持 pose 的机器人子类覆盖。"""
        raise CartesianActionError(f"{self.name}: joint-to-pose conversion is not supported")

    def _solve_pose(self, pose: np.ndarray, seed: np.ndarray) -> np.ndarray:
        """位姿向量 → 关节向量的 teleop/IK 钩子；支持 pose 的机器人子类覆盖。"""
        raise CartesianActionError(f"{self.name}: pose-to-joint conversion is not supported")

    def get_observation_gripper(self) -> np.ndarray:
        """当前帧夹爪（扁平 ``gripper_dim()`` 维 = 每臂 1，分段排布）——子类实现。

        返回值写入原始观测的 ``gripper`` 键，之后由 server 与 joint 组装为采集 qpos。读不到时可
        返回 NaN 向量。
        """
        raise NotImplementedError

    def get_observation_frames(self) -> list:
        """读取各相机本拍帧（list，顺序对齐 ``IMAGE_NAMES``；子类实现）。

        每项 ``{"color": <RGB uint8 HxWx3>, "depth": <uint16 HxW> | None}``——彩色恒有，深度仅在
        该相机开启深度时有值。一次取帧同时得到彩色与深度（取帧是阻塞的，不能为深度再读一次）。
        """
        raise NotImplementedError

    def capture_frames(self) -> dict:
        """读取各相机本拍帧并组装契约键（彩色 + 深度）——观测线程调用。

        ``observations/images/<cam>`` 恒有；``observations/depth/<cam>`` 只对生效的深度相机
        （``depth_camera_names()``）且该拍确实拿到深度时写入。
        """
        depth_names = self.depth_camera_names()
        frames = self.get_observation_frames()
        out: dict = {}
        for name, frame in zip(self.IMAGE_NAMES, frames, strict=True):
            out[f"{self.CAMERA_PREFIX}{name}"] = frame["color"]
            depth = frame.get("depth")
            if depth is not None and name in depth_names:
                out[f"{self.DEPTH_PREFIX}{name}"] = depth
        return out

    def get_joint_target(self) -> np.ndarray | None:
        """当前关节目标；位姿输入已在写目标前转换为关节。"""
        if self.joint_target is None:
            return None
        return self.joint_target.copy()

    def get_gripper_target(self) -> np.ndarray | None:
        """当前夹爪目标；未下发过则返回 None。"""
        if self.gripper_target is None:
            return None
        return self.gripper_target.copy()

    # ---- 生命周期（env / server 调用）----------------------------------------------------
    def connect(self):
        """连接硬件（子类实现；虚拟机器人直接 ready）。"""
        self.ready = True

    def disconnect(self):
        """断开硬件 / 释放资源（子类实现）。"""
        pass

    # ---- 通用限位插值 ------------------------------------------------------------------
    @staticmethod
    def _step_toward(current: np.ndarray, target: np.ndarray, max_step: float) -> np.ndarray:
        """限位插值：每步最多向目标靠近 max_step，防止关节数据跳变。"""
        current = np.asarray(current, dtype=np.float64)
        target = np.asarray(target, dtype=np.float64)
        delta = target - current
        step = np.clip(delta, -max_step, max_step)
        return np.where(np.abs(delta) <= max_step, target, current + step)
