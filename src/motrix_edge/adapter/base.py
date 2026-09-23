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

"""RobotAdapter —— 机器人硬件抽象层（HAL）契约。

核心（session / server / CLI）只依赖本接口与 entry point 发现，不引用具体机器人实现。
具体机器人（及 controller / sensor / profile）由外部 SDK / 包实现本接口并通过
``motrix_edge.adapters`` entry point 注册接入。

职责面（角色）：
  discover/health  发现并检查硬件（discover / health / ready / release）
  capabilities    声明能力（动作维度 / 观测布局 / 相机）
  observe         读取最新观测缓存（JPEG 图像 + qpos；**不推进 / 不影响适配器运行**）
  execute         执行动作指令（直接下发）
  capture_status  采集状态（运行位 + 元信息 + 数据目录/列表；进程自维护）
  rollout         推理闭环（被推理任务消费）
  safe_stop       安全停止（幂等、失败安全）

设计取舍：
  - **适配器独立运行**：适配器自身持续运行（常驻运行线程 / 硬件控制循环）推进运动并更新
    最新观测缓存；``observe()`` **只读取缓存**（JPEG 图像 + qpos），不推进、不驱动适配器。
    ``rollout()`` 设置目标，由适配器运行循环限速靠近。
  - **观测图像为 JPEG**：观测缓存中的摄像头帧为 **JPEG 编码**（adapter 提供，如 640x480）；
    Edge 侧可解码 / 降采样后用于预览与 WebRTC 推流。
  - **采集下沉、无回合控制**：数据采集（录制写盘）由适配器 / 机器人进程自维护——Edge
    进入采集会话后只读共享内存观测并展示，**不驱动回合**。adapter 只预留一个**采集状态**
    接口（``capture_status()``）返回运行位（进程是否正在采集）+ 元信息 + 数据目录 / 列表，
    供 server 状态上报。观测键契约（KEY_QPOS / KEY_ACTION / CAMERA_PREFIX）在此单点定义。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

# ---- 观测键契约（standard_obs 字典的键名，与 ACT 采集格式一致）----------------
KEY_QPOS = "observations/qpos"
KEY_ACTION = "action"
# 夹爪观测键（每臂 1 维，物理顺序同 ``ARM_NAMES``）：夹爪是**独立动作空间**（``gripper``），
# 故观测也独立成键，不混进 ``observations/qpos``。
KEY_GRIPPER = "observations/gripper"
# 末端位姿（每臂 6 维：xyz + rpy，物理顺序同 ``ARM_NAMES``；单位米 / 弧度）——位姿原语的输入；
# 机器人不提供位姿时该键不出现（``POSE = 0``）。
KEY_POSE = "observations/pose"
# **目标位姿** = ``FK(关节段目标)``——底层位姿目标（与 ``KEY_POSE`` 同一套 FK、同一拍）。
# 用途：``pose_delta`` 的解算结果对上位可见（``settle`` 判到位拿它当参考，不必自己攒基准）；
# 「目标 − 实测」即 MIT 稳态误差。同样随位姿存在（``POSE = 0`` 时无此键）。
KEY_POSE_TARGET = "observations/pose_target"
CAMERA_PREFIX = "observations/images/"

# execute / rollout 的一维动作输入：CLI/HTTP 常用 list，policy 常用 ndarray。
Action = Sequence[float] | np.ndarray


class ActionSpace(str, Enum):
    """动作空间（flat 动作向量的语义）——策略声明、adapter 校验、机器人进程最终解释。

    四个空间**各自只表达一件事**，值都按 ``ARM_NAMES`` 逐臂展开（每臂等长）：

    - ``JOINT``（缺省）：每臂 6 关节角，**绝对目标**；
    - ``POSE``：每臂 ``xyz + rpy`` 末端位姿**绝对目标**；由机器人侧求解器转成关节目标后执行
      （运动学归机器人侧，edge 不解释机器人结构）；
    - ``POSE_DELTA``：每臂 ``xyz + rpy`` 位姿**增量**；机器人侧叠加在**当前关节段目标**的正解
      位姿上再解一次（基准取目标而非实测——底层 MIT 有稳态误差，以实测为基准会把误差写进
      新目标、逐步累积）；
    - ``GRIPPER``：每臂 1 夹爪开合（归一化 ``[0, 1]``）。
    """

    JOINT = "joint"
    POSE = "pose"
    POSE_DELTA = "pose_delta"
    GRIPPER = "gripper"


# 与机器人侧（``BaseRobot.ACTION_SPACE_*``）同名同值的字符串别名：共享内存 / 机器进程
# 契约层（不依赖枚举实例的场合）直接用字符串常量。
ACTION_SPACE_JOINT = ActionSpace.JOINT.value
ACTION_SPACE_POSE = ActionSpace.POSE.value
ACTION_SPACE_POSE_DELTA = ActionSpace.POSE_DELTA.value
ACTION_SPACE_GRIPPER = ActionSpace.GRIPPER.value


class AdapterCapability(str, Enum):
    """适配器能力标识（能力描述 dict 的键）。

    会话按能力选择适配器：CaptureSession 要求 CAPTURE，InferSession 要求 EXECUTE。
    """

    CAPTURE = "capture"  # 支持数据采集（数据生产者，被采集控制）
    EXECUTE = "execute"  # 支持动作执行（推理闭环，被推理任务消费）
    STREAMING = "streaming"  # 支持视频流（遥操作预览 / 只读流，供后续 WebRTC 使用）


@dataclass
class HealthStatus:
    """健康检查结果：ok=False 时 detail 说明原因。

    ``control_hz`` = 机器人名义控制频率（env 控制线程 ``HZ``）；``measured_hz`` = 实测
    控制线程帧率（robot server 最近窗口统计）；未知时为 None。
    """

    ok: bool
    detail: str = ""
    control_hz: float | None = None  # 名义控制频率（Hz）
    measured_hz: float | None = None  # 实测控制线程帧率（Hz）


@dataclass
class CaptureStatus:
    """采集状态（合并原「采集数据状态」+「采集元信息」）：运行位 + 元信息 + 数据目录。

    数据采集（录制写盘）由适配器 / 机器人进程自维护（Edge **不承担保存职责**）；
    元信息由 ``capture sync --meta`` 从 console / web 同步到进程，进程保存一轮数据时
    附加——**只有 ``meta`` 一个载体**（采集员 / 任务名等是其中的键，分类可拓展），不另设
    同义顶层字段。Edge 周期查询（``capture_status()``）并缓存，供 server 状态上报与前端
    展示；**数据文件列表不在本状态里**——本地数据的扫描 / 选择 / 打包由
    [UploadSession](./upload_session.py) 直接读目录完成。
    """

    running: bool = False  # 进程当前是否正在采集（episode 开→关）
    meta: dict = field(default_factory=dict)  # 采集元信息全集（capture sync 同步；含 operator / task_name 等键）
    data_dir: str | None = None  # 数据目录（进程自维护；edge 只读）


def image_names_of(keys) -> list[str]:
    """从观测键里挑出相机名（``observations/images/<name>`` → ``<name>``）。

    ``keys`` 可以是 adapter 声明的观测键清单，也可以是某一帧观测的键（同一口径）——
    ``RobotCapabilities.image_names`` 与 ``server/preview`` 共用本函数。
    """
    return [k[len(CAMERA_PREFIX) :] for k in keys if k.startswith(CAMERA_PREFIX)]


@dataclass
class RobotCapabilities:
    """适配器声明的能力（数据布局声明）。

    ``action_spaces`` = **支持的动作空间**列表（缺省仅关节空间）：各个空间的值都按臂展开、
    维度**各不相同**（joint / pose / pose_delta = 每臂 6，gripper = 每臂 1），故维度以
    ``action_dims`` 字典给出（``action_dim`` 保留为关节空间维度的兼容字段）。

    观测**常驻**（与动作空间无关）：``observations/qpos`` = 关节角、``observations/gripper``
    = 夹爪、``observations/pose`` = 实测末端位姿、``observations/pose_target`` = 目标位姿
    （机器人不提供位姿时后两个键不出现）。
    """

    robot_model_id: str = "unknown"
    robot_model_version: str = "0.0.0"
    action_dim: int = 0  # 关节空间维度（兼容字段）
    action_dims: dict[str, int] = field(default_factory=dict)  # 各动作空间维度
    action_spaces: list[str] = field(default_factory=lambda: [ActionSpace.JOINT.value])
    # 观测键（如 observations/qpos、observations/images/cam_head）
    observation_keys: list[str] = field(default_factory=list)
    # 能力描述 dict：capability -> 是否支持（子类必须显式声明；缺省不支持任何能力）
    capabilities: dict[AdapterCapability, bool] = field(default_factory=dict)

    @property
    def image_names(self) -> list[str]:
        """从观测键推导相机名（observations/images/<name>）。"""
        return image_names_of(self.observation_keys)

    def supports(self, cap: AdapterCapability) -> bool:
        """该适配器是否支持给定能力。"""
        return self.capabilities.get(cap, False)


@dataclass
class DiscoveredRobot:
    """机器人进程 discover 结果 —— 身份 + 进程自报的连接参数。

    身份用于实例化 adapter：``type`` 为 adapter 类 entry point 名（加载并实例化），
    ``name`` 供展示；``endpoint`` / ``shm_name`` 是**进程自报**的连接参数（HTTP 指令
    地址 / 观测共享内存名），adapter 优先采用（None = 无 discover，回退类常量）——
    避免「改了服务端端口后 discover 成功、指令仍发往下写死的地址」。能力（动作维度 /
    相机 / capabilities）仍由 adapter 内部类常量定义。
    """

    name: str
    type: str  # adapter 类型（entry point 名，用于加载 adapter 类）
    endpoint: str | None = None  # 进程自报的 SDK HTTP 指令地址（None = 用 adapter 类常量）
    shm_name: str | None = None  # 进程自报的观测共享内存名（None = 用 adapter 类常量）


class RobotAdapter(ABC):
    """机器人硬件抽象层接口。

    由 **discover 参数**（进程解析出的 ``name`` / ``endpoint`` / ``shm_name``；``type``
    由类常量确定）参数化；能力（动作维度 / 相机 / capabilities）由 adapter 内部类常量定义，
    连接参数缺省回退类常量（``SDK_URL`` / ``SHM_NAME``）。adapter 只负责
    **连接进程**并转发指令 / 读取观测——不自带 discover / probe（发现由 ``discover_adapter``
    完成）。运行时可经 ``configure()`` 应用 Edge 配置（``adapter`` 段）裁剪能力：启用臂 /
    相机、未启用臂用 home 填充——只影响维度 / 观测布局，不重启、不新建连接
    （``_select_arm_segments`` / ``_expand_action`` 为通用臂映射助手）。
    """

    # 本 adapter 的 entry point 类型（类确定，用于匹配 discover 的 type 加载类）；子类覆盖。
    ADAPTER_TYPE: str = ""

    # 能力声明（类级 dict，供「不实例化」按能力列出 / 过滤 adapter；子类覆盖）。
    # 实例 ``capabilities`` 属性把本声明并入 RobotCapabilities.capabilities。
    CAPABILITIES: dict[AdapterCapability, bool] = {}

    # ---- 动作布局声明（子类覆盖；供 configure 的臂裁剪 / 动作展开）----
    # **按动作空间**声明每臂维度：``{"joint": 6, "pose": 6, "gripper": 1}``——三个空间
    # 各自只表达一件事，值都按 ``ARM_NAMES`` 逐臂展开（每臂等长），故臂段切片可由
    # 「每臂维度」直接算出（无需再单独维护切片表）。只有已声明的空间才可下发。
    ACTION_DIM_PER_ARM: dict[str, int] = {}
    # 各空间的**全臂 home**（长度 = 每臂维度 × 臂数）：未启用臂用**同空间**的 home 填充
    # （关节 home 填关节、夹爪 home 填夹爪；``pose`` 不给 home —— 未启用臂时直接拒绝，见
    # ``_require_full_arms_for_cartesian``）。
    HOME: dict[str, list[float]] = {}
    # 本适配器接受的动作空间（缺省仅关节空间）；不支持的空间 → rollout / execute 报错
    ACTION_SPACES: tuple[ActionSpace, ...] = (ActionSpace.JOINT,)
    # ``pose`` 值所在的坐标系（如 "flange" / "tcp" / "fk"）：下发的位姿目标与读到的位姿
    # **必须同系**，否则 ``move_delta`` 这类「当前位姿 + 增量」会偏一个常量。
    POSE_FRAME: str = "unknown"
    # 启用的臂（物理顺序，如 ("left", "right")；空 = 无臂概念，不做臂裁剪）
    ARM_NAMES: tuple[str, ...] = ()
    # 缺省启用臂（物理顺序；空 = 默认全部 ARM_NAMES）
    DEFAULT_ENABLED_ARMS: tuple[str, ...] = ()
    # 相机布局：{相机名: 分辨率 (width, height)}（configure 校验 / 挑选用）
    IMAGES: dict[str, tuple[int, int]] = {}

    def __init__(self, name: str = "", *, endpoint: str | None = None, shm_name: str | None = None):
        """身份与连接参数由 discover 赋予（缺省为空 = 进程内测试，回退类常量）。

        ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（不随 discover 传输）；能力由子类类常量
        定义；``endpoint`` / ``shm_name`` 为进程自报的连接参数（指令地址 / 共享内存名），
        子类缺省回退自己的类常量（``SDK_URL`` / ``SHM_NAME``）。此处初始化能力裁剪状态
        （启用臂 / home / 完整相机顺序）；``action_dim`` / ``images`` 由子类初始化
        （部分测试替身用只读 property，此处不写）。
        """
        self.name = name
        self.type = self.ADAPTER_TYPE
        self.endpoint = endpoint
        self.shm_name = shm_name
        # 能力裁剪状态（configure 应用）：启用臂（物理顺序）/ 完整相机顺序
        self.enabled_arms = list(self.DEFAULT_ENABLED_ARMS or self.ARM_NAMES)
        self._home: dict[str, np.ndarray] = {
            str(space): np.asarray(values, dtype=np.float64) for space, values in self.HOME.items()
        }
        for space, values in self._home.items():
            expected = self.ACTION_DIM_PER_ARM.get(space, 0) * max(len(self.ARM_NAMES), 1)
            if values.shape[0] != expected:
                raise ValueError(f"{self.type}: HOME[{space!r}] dim {values.shape[0]} != {expected}")
        self._full_image_names = list(self.IMAGES)
        # 各空间**启用臂**维度（serve / 配置 / 前端）＋ 兼容字段 ``action_dim`` = 关节空间维度
        self.action_dims = {space.value: self.action_dim_for(space) for space in self.ACTION_SPACES}
        self.action_dim = self.action_dims.get(ActionSpace.JOINT.value, 0)
        # 遥操作 / 人工接管状态：**由真正支持遥操作的子类**在 set_teleop 里记录（基类 no-op
        # 不写——否则「不支持遥操作的适配器」会被上报成遥操作中）；server 据此在状态里
        # 暴露 ``teleop`` / ``teleop_mode``（见 server/state.py）。
        self.teleop_enabled = False
        self.teleop_mode: str | None = None

    # ---- 能力裁剪（configure：启用臂 / 相机；各空间 home 固定由 HOME 定义）----------
    def configure(self, enabled_arms=None, enabled_cameras=None) -> None:
        """应用 Edge 配置（``adapter`` 段）裁剪能力：启用臂 / 相机。

        - ``enabled_arms``：启用的臂（``ARM_NAMES`` 子集，如 right / left）；缺省启用全部
          （``DEFAULT_ENABLED_ARMS``，如双臂）。运行时 ``action_dim = 启用臂数 ×
          ACTION_DIM_PER_ARM``，动作段按**物理顺序**（``ARM_NAMES``）映射，未启用臂用
          类常量 ``HOME[space]`` 填充（各空间 home**固定由 adapter 定义**，不可运行时覆盖）；
        - ``enabled_cameras``：启用的相机（``IMAGES`` 子集）；缺省全部（如三相机）。

        无臂概念（``ARM_NAMES`` 为空）时 ``enabled_arms`` 忽略。参数**原子校验**：任一非法
        （未知臂 / 未知相机）→ ``ValueError``，不改变当前能力。只影响本实例的维度 /
        观测布局，不重启 / 不新建连接。

        校验与归一化在 ``normalize_capability_config()``（类方法）——未绑定 adapter 时
        （只有类常量、无实例）也用它预先校验运行时配置。
        """
        arms, cameras = self.normalize_capability_config(enabled_arms, enabled_cameras)
        # 校验通过后应用（顺序已归一化到声明顺序）
        if arms is not None:
            self.enabled_arms = arms
        if cameras is not None:
            self.images = cameras
        self.action_dims = {space.value: self.action_dim_for(space) for space in self.ACTION_SPACES}
        self.action_dim = self.action_dims.get(ActionSpace.JOINT.value, 0)

    @classmethod
    def normalize_capability_config(
        cls, enabled_arms=None, enabled_cameras=None
    ) -> tuple[list[str] | None, list[str] | None]:
        """静态校验并归一化能力配置（**不依赖实例**）→ ``(arms, cameras)``。

        ``None`` = 该键缺省（调用方保持当前设置）；否则是按类常量声明顺序
        （``ARM_NAMES`` / ``IMAGES``）归一化的列表。**原子校验**：未知臂 / 未知相机 /
        空 ``enabled_arms`` → ``ValueError``。无臂概念（``ARM_NAMES`` 为空）时
        ``enabled_arms`` 忽略 → ``(None, cameras)``。

        仅供能力裁剪与**未绑定时的预校验**调用（类常量足以判未知臂 / 相机，无需实例化）。
        """
        arms = None
        if enabled_arms is not None and cls.ARM_NAMES:
            requested = [str(a).strip().lower() for a in enabled_arms]
            for a in requested:
                if a not in cls.ARM_NAMES:
                    raise ValueError(f"unknown arm: {a!r} (available: {list(cls.ARM_NAMES)})")
            if not requested:
                raise ValueError("enabled_arms must not be empty")
            arms = [a for a in cls.ARM_NAMES if a in requested]  # 物理顺序
        cameras = None
        if enabled_cameras is not None:
            requested_cameras = [str(c).strip() for c in enabled_cameras]
            unknown = [c for c in requested_cameras if c not in cls.IMAGES]
            if unknown:
                raise ValueError(f"unknown camera(s): {unknown} (available: {list(cls.IMAGES)})")
            cameras = [c for c in cls.IMAGES if c in requested_cameras]  # 声明顺序
        return arms, cameras

    def enabled_map(self) -> dict[str, dict[str, bool]]:
        """能力启用状态（分组字典，前端勾选展示 / 同步用）。

        返回 ``{"arms": {臂名: 是否启用}, "cameras": {相机名: 是否启用}}``，由当前
        ``enabled_arms`` / ``images``（``configure()`` 应用后）推导。
        """
        return {
            "arms": {arm: arm in self.enabled_arms for arm in self.ARM_NAMES},
            "cameras": {cam: cam in self.images for cam in self.IMAGES},
        }

    @classmethod
    def default_enabled_map(cls) -> dict[str, dict[str, bool]]:
        """类级**默认**能力启用字典（不依赖实例 / 绑定状态）。

        基于类常量推导：臂 = ``DEFAULT_ENABLED_ARMS``（缺省 ``ARM_NAMES``，如双臂），
        相机 = ``IMAGES`` 全部启用（如三相机）。供**未绑定 adapter** 时前端 / CLI 展示
        默认勾选（刷新即可见）。与 ``enabled_map()``（实例实际生效）区分。
        """
        arms = cls.DEFAULT_ENABLED_ARMS or cls.ARM_NAMES
        return {
            "arms": {arm: arm in arms for arm in cls.ARM_NAMES},
            "cameras": {cam: True for cam in cls.IMAGES},
        }

    def action_dim_for(self, action_space: ActionSpace | str | None = None) -> int:
        """**启用臂**下该动作空间的值维度（= 每臂维度 × 启用臂数）；无臂概念 → 每臂维度。"""
        space = self.normalize_action_space(action_space)
        per_arm = self.ACTION_DIM_PER_ARM.get(space.value, 0)
        if not per_arm:
            raise ValueError(f"{self.type}: action space {space.value!r} has no dimension declared")
        if self.ARM_NAMES:
            return per_arm * len(self.enabled_arms)
        return per_arm

    def _select_arm_segments(self, values, per_arm_dim: int) -> np.ndarray:
        """按启用臂挑选**等长分段**状态（每臂 ``per_arm_dim`` 维，物理顺序 = ``ARM_NAMES``）。

        各动作空间的值都按臂等长展开，故一份助手同时服务关节角 / 夹爪 / 位姿；
        无臂概念 / 维数不足 → 原样返回（不猜布局）。
        """
        values = np.asarray(values, dtype=np.float32)
        per_arm_dim = int(per_arm_dim)
        if not self.ARM_NAMES or per_arm_dim <= 0 or values.shape[0] < len(self.ARM_NAMES) * per_arm_dim:
            return values
        parts = []
        for arm in self.enabled_arms:
            start = self.ARM_NAMES.index(arm) * per_arm_dim
            parts.append(values[start : start + per_arm_dim])
        return np.concatenate(parts) if parts else np.asarray([], dtype=np.float32)

    def _expand_action(
        self, action: Action, operation: str, action_space: ActionSpace | str | None = None
    ) -> np.ndarray:
        """校验维度并展开回该空间的**完整**动作（未启用臂用**同空间 home** 填充）。

        无臂概念 / 全臂启用 → 原样返回（动作即完整维度）；仅启用部分臂时，动作段按物理
        顺序（``ARM_NAMES``）写入对应臂块，其余臂块填 ``HOME[space]``（同空间，不会把关节
        值当位姿发）。
        """
        space = self.normalize_action_space(action_space)
        expected = self.action_dim_for(space)
        target = np.asarray(action, dtype=np.float64)
        if target.ndim != 1 or target.shape[0] != expected:
            actual = target.shape[0] if target.ndim > 0 else 0
            raise ValueError(f"{operation} {space.value} action dim {actual} != {expected}")
        per_arm = self.ACTION_DIM_PER_ARM[space.value]
        if not self.ARM_NAMES or len(self.enabled_arms) == len(self.ARM_NAMES):
            return target
        home = self._home.get(space.value)
        if home is None:
            raise ValueError(
                f"{operation} {space.value} requires all arms enabled (enabled: {list(self.enabled_arms)})"
            )
        full = home.copy()
        for cursor, arm in enumerate(self.enabled_arms):
            start = self.ARM_NAMES.index(arm) * per_arm
            full[start : start + per_arm] = target[cursor * per_arm : (cursor + 1) * per_arm]
        return full

    # ---- health / release（硬件由 SDK 进程自维护，Edge 只查询 / 释放本地资源）-----
    def release(self) -> None:
        """释放资源（原 disconnect）。默认 no-op，子类按需实现。"""
        pass

    @abstractmethod
    def health(self) -> HealthStatus:
        """健康检查：就绪 / 状态 / 错误详情。"""
        raise NotImplementedError

    @property
    def ready(self) -> bool:
        """是否就绪（可开始任务）。默认取 health().ok。"""
        return self.health().ok

    # ---- capabilities（声明能力）--------------------------------------------
    @property
    @abstractmethod
    def capabilities(self) -> RobotCapabilities:
        """声明能力：动作维度 / 观测布局 / 相机。"""
        raise NotImplementedError

    def effective_pose_dim_per_arm(self) -> int:
        """**生效**的每臂位姿维数（0 = 本机/本适配器不提供位姿）。

        位姿是 ``observations/pose``（每臂 ``xyz + rpy``），与动作空间无关、随时可读；
        故这里 = 适配器声明的 ``pose`` 每臂维数；未声明 ``pose`` → 0。
        """
        if ActionSpace.POSE not in self.ACTION_SPACES:
            return 0
        return int(self.ACTION_DIM_PER_ARM.get(ActionSpace.POSE.value, 0) or 0)

    @property
    def pose_dim(self) -> int:
        """位姿值维度（启用臂 × 每臂位姿维数；不提供位姿的适配器为 0）。

        位姿不再是独立观测键——本值只用于声明「能不能吃 / 给位姿值」。
        """
        per_arm = self.effective_pose_dim_per_arm()
        if per_arm <= 0:
            return 0
        if not self.ARM_NAMES:
            return per_arm
        return per_arm * len(self.enabled_arms)

    # ---- observe（读取最新观测缓存，被预览 / policy 推理消费）------------------
    @abstractmethod
    def observe(self) -> dict | None:
        """返回适配器维护的最新观测缓存；尚无首帧时返回 ``None``。

        - 图像为 **JPEG 编码**（adapter 提供，如 640x480）；qpos / action 为状态缓存。
        - **observe 不推进 / 不影响适配器运行**——适配器自身持续运行（控制循环 /
          采集程序）更新缓存，observe 只是取出缓存。
        - ``None`` 表示瞬态无帧，session 应跳过本轮，不得升级为任务错误。
        - 被「预览（摄像头 + 状态）」与「policy 推理」消费；不采集。
        键见模块级契约（KEY_QPOS / CAMERA_PREFIX / KEY_ACTION）。
        """
        raise NotImplementedError

    # ---- execute（执行动作指令）-----------------------------------------------
    @abstractmethod
    def execute(self, action: Action, action_space: ActionSpace | str | None = None) -> None:
        """直接下发一维 array-like 动作指令（raw 指令，立即执行）。

        ``action_space`` = 动作语义（缺省 / ``None`` → ``ActionSpace.JOINT``），与 ``rollout``
        同口径：不在本适配器 ``ACTION_SPACES`` 内的空间 → ``ValueError``；笛卡尔动作的 IK 归
        机器人进程（机器人在收到目标时解算一次，失败则拒绝该条指令）。
        """
        raise NotImplementedError

    # ---- teleop（遥操作 / 人工接管）-------------------------------------------
    def set_teleop(self, enabled: bool, mode: str | None = None) -> None:
        """设置遥操作（``True``=遥操作 / ``False``=程控 / 推理控制）。

        ``mode`` 为遥操作映射模式（``absolute`` 缺省 / ``delta`` = **人工接管**：以接管瞬间
        的主 / 从位姿为锚点、只叠加主臂增量），**仅在 ``enabled=True`` 时有意义**；``None``
        表示沿用进程侧缺省（等价 ``absolute``）。遥操作开启即视为人工接管，进程侧会拒绝
        ``rollout``（见 `wiki/design/robot_pipeline_teleop.md`）。

        默认 no-op（**不支持遥操作的适配器不记录状态**，保持 ``teleop_enabled=False``）；
        支持遥操作的子类按需覆盖（如经 HTTP 转发机器人进程 /v1/teleop），并在生效后同步
        ``teleop_enabled`` / ``teleop_mode`` 供 server 状态上报。
        """
        pass

    # ---- 采集状态（进程自维护：运行位 + 元信息 + 数据落盘）-----------------------
    # Edge 进入采集会话后只读共享内存观测并展示，不驱动回合；adapter 只预留一个
    # capture_status() 接口（供 server 周期轮询上报）。
    def capture_status(self) -> CaptureStatus | None:
        """采集状态：运行位（是否正在采集）+ 元信息（采集员 / 任务名等）+ 数据目录 / 列表。

        数据采集与元信息均由适配器 / 机器人进程自维护，本方法只查询 / 上报结果。
        未启用采集 / 未知 → 返回 ``None``。子类按需覆盖。
        """
        return None

    def sync_capture_meta(self, meta: dict) -> None:
        """把采集元信息（采集员 / 任务名等）同步到机器人进程；进程保存数据时附加。

        默认 no-op；采集由机器人进程自维护的适配器按需覆盖（如经 HTTP 转发
        机器人进程 /v1/capture/sync）。
        """
        pass

    # ---- 采集回合控制（capture episode start / end）---------------------------
    def start_capture(self) -> None:
        """开始一轮采集（episode 开始）：通知适配器 / 机器人进程开启录制。

        默认 no-op；采集由适配器 / 机器人进程自维护的适配器按需覆盖（如经 HTTP 转发
        机器人进程 /v1/capture/start）。
        """
        pass

    def end_capture(self) -> None:
        """结束一轮采集（episode 结束）：通知适配器 / 机器人进程停止录制。

        默认 no-op；采集由适配器 / 机器人进程自维护的适配器按需覆盖（如经 HTTP 转发
        机器人进程 /v1/capture/end）。
        """
        pass

    # ---- rollout（推理闭环，被推理任务消费）-----------------------------------
    @abstractmethod
    def rollout(self, action: Action, action_space: ActionSpace | str | None = None) -> bool:
        """接收一维 array-like 模型动作，按 capabilities.action_dim 解析并推进一帧。

        ``action_space`` = 动作语义（缺省 / ``None`` → ``ActionSpace.JOINT``，与仅下发关节
        动作的调用方等价）；不在本适配器 ``ACTION_SPACES`` 内的空间 → ``ValueError``
        （调用方应回执拒绝，不要猜语义下发）。笛卡尔动作的 IK 由机器人进程负责。

        返回是否**已下发**：``False`` = 进程侧拒绝本拍（遥操作 / 人工接管中，见
        `wiki/design/robot_pipeline_teleop.md`），调用方应跳过本拍、下拍重试（遥操作关闭后
        自动恢复）；``True`` / 无返回（旧实现）均视为已下发。
        """
        raise NotImplementedError

    # ---- 动作空间校验（子类在 execute / rollout 内调用）------------------------
    @classmethod
    def normalize_action_space(cls, action_space: ActionSpace | str | None) -> ActionSpace:
        """规范化并校验动作空间：``None`` → ``JOINT``；不支持 → ``ValueError``。"""
        space = ActionSpace.JOINT if action_space is None else ActionSpace(action_space)
        if space not in cls.ACTION_SPACES:
            raise ValueError(
                f"action space {space.value!r} not supported (available: {[s.value for s in cls.ACTION_SPACES]})"
            )
        return space

    # ---- safe_stop（安全停止）-------------------------------------------------
    @abstractmethod
    def safe_stop(self) -> None:
        """安全停止（幂等、失败安全）：停发指令并保持位姿——**软停，不断电**。

        「断电急停」（硬件 e-stop）不属本契约：由现场急停按钮 / 作业流程负责，详见
        wiki/design/motrix_edge_adapter.md。
        """
        raise NotImplementedError

    # ---- 生命周期辅助 ----------------------------------------------------------
    def reset(self) -> None:
        """程序复位到 home（非阻塞）：设置 home 目标，由后续 observe()/rollout() 推进。"""
        pass
