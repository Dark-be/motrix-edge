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
CAMERA_PREFIX = "observations/images/"

# execute / rollout 的一维动作输入：CLI/HTTP 常用 list，policy 常用 ndarray。
Action = Sequence[float] | np.ndarray


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
    """适配器声明的能力（数据布局声明）。"""

    robot_model_id: str = "unknown"
    robot_model_version: str = "0.0.0"
    action_dim: int = 0
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
    （``_select_qpos`` / ``_expand_action`` 为通用臂映射助手）。
    """

    # 本 adapter 的 entry point 类型（类确定，用于匹配 discover 的 type 加载类）；子类覆盖。
    ADAPTER_TYPE: str = ""

    # 能力声明（类级 dict，供「不实例化」按能力列出 / 过滤 adapter；子类覆盖）。
    # 实例 ``capabilities`` 属性把本声明并入 RobotCapabilities.capabilities。
    CAPABILITIES: dict[AdapterCapability, bool] = {}

    # ---- 动作布局声明（子类覆盖；供 configure 的臂裁剪 / qpos 挑选 / 动作展开）----
    # **完整**动作维度（如双臂 14）；启用臂下的实际维度见 ``action_dim``
    ACTION_DIM: int = 0
    # 每臂动作维度（如 7；无臂概念 = 0）
    ACTION_DIM_PER_ARM: int = 0
    # 臂名（物理顺序，如 ("left", "right")；空 = 无臂概念，不做臂裁剪）
    ARM_NAMES: tuple[str, ...] = ()
    # 臂名 → qpos 切片（如 {"left": slice(0, 7), "right": slice(7, 14)}）
    ARM_QPOS_SLICES: dict[str, slice] = {}
    # 全动作 home 位姿（长度 = ACTION_DIM；未启用臂动作填充用）
    HOME_QPOS: list[float] = []
    # 缺省启用臂（物理顺序；空 = 默认全部 ARM_NAMES）
    DEFAULT_ENABLED_ARMS: tuple[str, ...] = ()
    # 相机布局：{相机名: 分辨率 (width, height)}（configure 校验 / 挑选用）
    IMAGES: dict[str, tuple[int, int]] = {}

    def __init_subclass__(cls, **kwargs):
        """子类布局声明自洽性校验（导入期报错，不让错误拖到运行时的数组赋值）。

        - ``ARM_QPOS_SLICES`` 的键必须与 ``ARM_NAMES`` 一致；
        - 每个切片的长度必须等于 ``ACTION_DIM_PER_ARM``；
        - ``HOME_QPOS``（非空时）长度必须等于 ``ACTION_DIM``。
        """
        super().__init_subclass__(**kwargs)
        names = tuple(cls.ARM_NAMES)
        if names and set(cls.ARM_QPOS_SLICES) != set(names):
            raise TypeError(
                f"{cls.__name__}: ARM_QPOS_SLICES keys {sorted(cls.ARM_QPOS_SLICES)} != ARM_NAMES {list(names)}"
            )
        if names and cls.ACTION_DIM_PER_ARM:
            for arm, segment in cls.ARM_QPOS_SLICES.items():
                length = (segment.stop or 0) - (segment.start or 0)
                if segment.stop is not None and length != cls.ACTION_DIM_PER_ARM:
                    raise TypeError(
                        f"{cls.__name__}: ARM_QPOS_SLICES[{arm!r}] length != ACTION_DIM_PER_ARM"
                        f" ({cls.ACTION_DIM_PER_ARM})"
                    )
        if cls.HOME_QPOS and cls.ACTION_DIM and len(cls.HOME_QPOS) != cls.ACTION_DIM:
            raise TypeError(f"{cls.__name__}: HOME_QPOS length {len(cls.HOME_QPOS)} != ACTION_DIM {cls.ACTION_DIM}")

    def __init__(self, name: str = "", *, endpoint: str | None = None, shm_name: str | None = None):
        """身份与连接参数由 discover 赋予（缺省为空 = 进程内测试，回退类常量）。

        ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（不随 discover 传输）；能力由子类类常量
        定义；``endpoint`` / ``shm_name`` 为进程自报的连接参数（指令地址 / 共享内存名），
        子类缺省回退自己的类常量（``SDK_URL`` / ``SHM_NAME``）。此处初始化能力裁剪状态
        （启用臂 / 启用相机 / home），后续由 ``configure()`` 裁剪；
        ``action_dim`` 为按当前启用臂推导的只读属性。
        """
        self.name = name
        self.type = self.ADAPTER_TYPE
        self.endpoint = endpoint
        self.shm_name = shm_name
        # 能力裁剪状态（configure 应用）：启用臂 / 启用相机均**按声明顺序**排列，
        # 未启用臂动作用类常量 HOME_QPOS 填充
        self.enabled_arms = list(self.DEFAULT_ENABLED_ARMS or self.ARM_NAMES)
        self.enabled_images = list(self.IMAGES)
        self._home_qpos = np.asarray(self.HOME_QPOS or [0.0] * self.ACTION_DIM, dtype=np.float64)

    # ---- 能力裁剪（configure：启用臂 / 相机；home 固定由 HOME_QPOS 定义）----------
    def configure(self, enabled_arms=None, enabled_cameras=None) -> None:
        """应用 Edge 配置（``adapter`` 段）裁剪能力：启用臂 / 相机。

        - ``enabled_arms``：启用的臂（``ARM_NAMES`` 子集，如 right / left）；**缺省不
          改变当前设置**（部分更新语义；初始值取 ``DEFAULT_ENABLED_ARMS``，如双臂）。
          运行时 ``action_dim = 启用臂数 × ACTION_DIM_PER_ARM``，动作段按**物理顺序**
          （``ARM_NAMES``）映射，未启用臂用类常量 ``HOME_QPOS`` 填充（home 位姿**固定由
          adapter 定义**，不可运行时覆盖）；
        - ``enabled_cameras``：启用的相机（``IMAGES`` 子集）；**缺省不改变当前设置**
          （初始值 = 全部相机）；结果按 ``IMAGES`` 声明顺序排列（与臂同口径，
          不随调用方传参顺序变化）。

        无臂概念（``ARM_NAMES`` 为空）时 ``enabled_arms`` 忽略（声明即全臂）。参数
        **原子校验**：任一非法（未知臂 / 未知相机）→ ``ValueError``，不改变当前能力。
        只影响本实例的维度 / 观测布局，不重启 / 不新建连接。

        校验与归一化在 ``normalize_capability_config()``（类方法）——未绑定 adapter 时
        （只有类常量、无实例）也用它预先校验运行时配置。
        """
        arms, cameras = self.normalize_capability_config(enabled_arms, enabled_cameras)
        # 校验通过后应用（顺序已归一化到声明顺序）
        if arms is not None:
            self.enabled_arms = arms
        if cameras is not None:
            self.enabled_images = cameras

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
            arms = [a for a in cls.ARM_NAMES if a in requested]
        cameras = None
        if enabled_cameras is not None:
            requested_cameras = [str(c).strip() for c in enabled_cameras]
            unknown = [c for c in requested_cameras if c not in cls.IMAGES]
            if unknown:
                raise ValueError(f"unknown camera(s): {unknown} (available: {list(cls.IMAGES)})")
            cameras = [c for c in cls.IMAGES if c in requested_cameras]
        return arms, cameras

    def enabled_map(self) -> dict[str, dict[str, bool]]:
        """能力启用状态（分组字典，前端勾选展示 / 同步用）。

        返回 ``{"arms": {臂名: 是否启用}, "cameras": {相机名: 是否启用}}``，由当前
        ``enabled_arms`` / ``enabled_images``（``configure()`` 应用后）推导。
        """
        return {
            "arms": {arm: arm in self.enabled_arms for arm in self.ARM_NAMES},
            "cameras": {cam: cam in self.enabled_images for cam in self.IMAGES},
        }

    @property
    def action_dim(self) -> int:
        """当前启用臂下的动作维度（``configure()`` 应用后）；无臂概念 → 完整维度。"""
        return self._dim_for(self.enabled_arms)

    @classmethod
    def _dim_for(cls, arms) -> int:
        """给定启用臂的动作维度；无臂概念 → 完整维度。"""
        if cls.ARM_NAMES and cls.ACTION_DIM_PER_ARM:
            return cls.ACTION_DIM_PER_ARM * len(arms)
        return cls.ACTION_DIM

    def _select_qpos(self, qpos) -> np.ndarray:
        """按启用臂（物理顺序）挑选 / 拼接 qpos（观测口径，统一 float32）；无臂概念 → 原样。

        观测三件套（qpos / action / pose）共用本方法，保证同一裁剪口径；下发方向的
        维度校验 / 展开用 ``_expand_action``（float64，做数值计算）。
        """
        qpos = np.asarray(qpos, dtype=np.float32)
        if not self.ARM_NAMES:
            return qpos
        parts = [qpos[self.ARM_QPOS_SLICES[arm]] for arm in self.enabled_arms]
        return np.concatenate(parts) if parts else np.asarray([], dtype=np.float32)

    def _expand_action(self, action: Action, operation: str) -> np.ndarray:
        """校验启用臂维度并展开回完整动作空间（未启用臂用 home_qpos 填充，float64）。

        无臂概念 / 全臂启用 → 原样返回（动作即完整维度）；仅启用部分臂时，动作段
        按物理顺序（``ARM_NAMES``）写入对应切片，其余切片填充 ``self._home_qpos``。
        """
        target = np.asarray(action, dtype=np.float64)
        if target.ndim != 1 or target.shape[0] != self.action_dim:
            actual = target.shape[0] if target.ndim > 0 else 0
            raise ValueError(f"{operation} action dim {actual} != action_dim {self.action_dim}")
        if not self.ARM_NAMES or len(self.enabled_arms) == len(self.ARM_NAMES):
            return target
        full = self._home_qpos.copy()
        cursor = 0
        for arm in self.enabled_arms:
            full[self.ARM_QPOS_SLICES[arm]] = target[cursor : cursor + self.ACTION_DIM_PER_ARM]
            cursor += self.ACTION_DIM_PER_ARM
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
    def execute(self, action: Action) -> None:
        """直接下发一维 array-like 动作指令（raw 指令，立即执行）。"""
        raise NotImplementedError

    # ---- teleop（遥操作开关）--------------------------------------------------
    def set_teleop(self, enabled: bool) -> None:
        """设置遥操作开关（``True``=遥操作 / ``False``=程控 / 推理控制）。

        默认 no-op；支持遥操作的子类按需覆盖（如经 HTTP 转发机器人进程 /v1/teleop）。
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
    def rollout(self, action: Action) -> None:
        """接收一维 array-like 模型动作，按 capabilities.action_dim 解析并推进一帧。"""
        raise NotImplementedError

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
