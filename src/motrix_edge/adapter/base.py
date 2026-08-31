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
  data_status     采集数据状态（数据目录 + 本次采集得到的数据列表；采集会话预留）
  rollout         推理闭环（被推理任务消费）
  safe_stop       安全停止（幂等、失败安全）

设计取舍：
  - **适配器独立运行**：适配器自身持续运行（常驻运行线程 / 硬件控制循环）推进运动并更新
    最新观测缓存；``observe()`` **只读取缓存**（JPEG 图像 + qpos），不推进、不驱动适配器。
    ``rollout()`` 设置目标，由适配器运行循环限速靠近。
  - **观测图像为 JPEG**：观测缓存中的摄像头帧为 **JPEG 编码**（adapter 提供，如 640x480）；
    Edge 侧可解码 / 降采样后用于预览与 WebRTC 推流。
  - **采集下沉、无回合控制**：数据采集（录制写盘）由适配器 / 机器人进程自维护——Edge
    进入采集会话后只读共享内存观测并展示，**不驱动回合**。adapter 只预留一个**数据状态**
    接口（``data_status()``）返回数据目录 + 本次采集得到的数据列表，供 server 状态
    上报。观测键契约（KEY_QPOS / KEY_ACTION / CAMERA_PREFIX）在此单点定义。
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
    """健康检查结果：ok=False 时 detail 说明原因。"""

    ok: bool
    detail: str = ""


@dataclass
class CaptureData:
    """采集数据状态（采集会话预留）：数据目录 + 本次采集得到的数据列表。

    数据采集（录制写盘）由适配器 / 机器人进程自维护（Edge **不承担保存职责**，只负责
    收集 / 上传）；adapter 通过 ``data_status()`` 返回本状态，供 server 状态上报。
    """

    data_dir: str | None = None  # 数据目录（未启用 / 未知 → None）
    data_files: list[str] = field(default_factory=list)  # 本次采集得到的数据列表


@dataclass
class CaptureStatus:
    """采集状态（采集会话预留）：机器人进程当前采集元信息 + 运行位。

    元信息（采集员姓名 / 任务名称等）由 ``capture sync --meta`` 从 console / web 同步到
    机器人进程，进程保存一轮数据时附加；Edge 在采集会话期间周期查询（``capture_status()``）
    并缓存，供 server 状态上报。
    """

    running: bool = False  # 进程当前是否在采集（录制中）
    operator: str | None = None  # 采集员姓名
    task_name: str | None = None  # 任务名称
    meta: dict = field(default_factory=dict)  # 通用元信息（保存数据时附加）


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
        return [k[len(CAMERA_PREFIX) :] for k in self.observation_keys if k.startswith(CAMERA_PREFIX)]

    def supports(self, cap: AdapterCapability) -> bool:
        """该适配器是否支持给定能力。"""
        return self.capabilities.get(cap, False)


@dataclass
class DiscoveredRobot:
    """机器人进程 discover 结果 —— 只保留身份（name / type）。

    身份用于实例化 adapter：``type`` 为 adapter 类 entry point 名（加载并实例化），
    ``name`` 供展示。能力（动作维度 / 相机 / capabilities）与连接参数（SDK 地址 /
    共享内存名）**全部由 adapter 内部类常量定义**，不随 discover 传输——discover 只
    回答「找到了哪个类型的机器人进程」。
    """

    name: str
    type: str  # adapter 类型（entry point 名，用于加载 adapter 类）


class RobotAdapter(ABC):
    """机器人硬件抽象层接口。

    由 **身份参数**（discover 解析出的 ``name`` / ``id``，`type` 由类常量确定）参数化；
    能力与连接参数（SDK 地址 / 共享内存名）由 adapter 内部类常量定义。adapter 只负责
    **连接进程**并转发指令 / 读取观测。运行时可经 ``configure()`` 应用 Edge 配置（``adapter``
    段）裁剪能力：启用臂 / 相机、未启用臂用 home 填充——只影响维度 / 观测布局，不重启、
    不新建连接（``_select_qpos`` / ``_expand_action`` 为通用臂映射助手）。
    """

    # 本 adapter 的 entry point 类型（类确定，用于匹配 discover 的 type 加载类）；子类覆盖。
    ADAPTER_TYPE: str = ""

    # 能力声明（类级 dict，供「不实例化」按能力列出 / 过滤 adapter；子类覆盖）。
    # 实例 ``capabilities`` 属性把本声明并入 RobotCapabilities.capabilities。
    CAPABILITIES: dict[AdapterCapability, bool] = {}

    # ---- 动作布局声明（子类覆盖；供 configure 的臂裁剪 / qpos 挑选 / 动作展开）----
    # 完整动作维度（如双臂 14）
    ACTION_DIM: int = 0
    # 每臂动作维度（如 7；无臂概念 = 0）
    ACTION_DIM_PER_ARM: int = 0
    # 启用的臂（物理顺序，如 ("left", "right")；空 = 无臂概念，不做臂裁剪）
    ARM_NAMES: tuple[str, ...] = ()
    # 臂名 → qpos 切片（如 {"left": slice(0, 7), "right": slice(7, 14)}）
    ARM_QPOS_SLICES: dict[str, slice] = {}
    # 全动作 home 位姿（长度 = ACTION_DIM；未启用臂动作填充用）
    HOME_QPOS: list[float] = []
    # 缺省启用臂（物理顺序；空 = 默认全部 ARM_NAMES）
    DEFAULT_ENABLED_ARMS: tuple[str, ...] = ()
    # 相机布局：{相机名: 分辨率 (width, height)}（configure 校验 / 挑选用）
    IMAGES: dict[str, tuple[int, int]] = {}

    def __init__(self, name: str = ""):
        """身份参数化：``name`` 由 discover 赋予（缺省为空 = 进程内测试）。

        ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（不随 discover 传输）；能力与连接参数
        由子类类常量定义。此处初始化能力裁剪状态（启用臂 / home / 完整相机顺序）；
        ``action_dim`` / ``images`` 由子类初始化（部分测试替身用只读 property，此处不写）。
        """
        self.name = name
        self.type = self.ADAPTER_TYPE
        # 能力裁剪状态（configure 应用）：启用臂（物理顺序）/ 未启用臂 home / SDK 观测帧相机顺序
        self.enabled_arms = list(self.DEFAULT_ENABLED_ARMS or self.ARM_NAMES)
        self._home_qpos = np.asarray(self.HOME_QPOS or [0.0] * self.ACTION_DIM, dtype=np.float64)
        self._full_image_names = list(self.IMAGES)

    # ---- 能力裁剪（configure：启用臂 / 相机 / home；通用臂映射助手）---------------
    def configure(self, enabled_arms=None, enabled_cameras=None, home_qpos=None) -> None:
        """应用 Edge 配置（``adapter`` 段）裁剪能力：启用臂 / 相机 / 未启用臂 home。

        - ``enabled_arms``：启用的臂（``ARM_NAMES`` 子集，如 right / left）；缺省启用全部。
          运行时 ``action_dim = 启用臂数 × ACTION_DIM_PER_ARM``，动作段按**物理顺序**
          （``ARM_NAMES``）映射，未启用臂用 home 填充；
        - ``enabled_cameras``：启用的相机（``IMAGES`` 子集）；缺省全部；
        - ``home_qpos``：未启用臂的 home 位姿（长度 = ``ACTION_DIM``）；缺省 ``HOME_QPOS``。

        无臂概念（``ARM_NAMES`` 为空）时 ``enabled_arms`` 忽略。参数**原子校验**：任一非法
        （未知臂 / 未知相机 / 维度错误）→ ``ValueError``，不改变当前能力。只影响本实例的
        维度 / 观测布局，不重启 / 不新建连接。
        """
        arms = None
        if enabled_arms is not None and self.ARM_NAMES:
            arms = [str(a).strip().lower() for a in enabled_arms]
            for a in arms:
                if a not in self.ARM_NAMES:
                    raise ValueError(f"unknown arm: {a!r} (available: {list(self.ARM_NAMES)})")
            if not arms:
                raise ValueError("enabled_arms must not be empty")
        cameras = None
        if enabled_cameras is not None:
            cameras = [str(c).strip() for c in enabled_cameras]
            unknown = [c for c in cameras if c not in self.IMAGES]
            if unknown:
                raise ValueError(f"unknown camera(s): {unknown} (available: {list(self.IMAGES)})")
        home = None
        if home_qpos is not None:
            home = np.asarray(home_qpos, dtype=np.float64)
            if home.ndim != 1 or home.shape[0] != self.ACTION_DIM:
                raise ValueError(f"home_qpos must be {self.ACTION_DIM}-dim, got {home.shape}")
        # 全部校验通过后应用
        if arms is not None:
            self.enabled_arms = [a for a in self.ARM_NAMES if a in arms]  # 保持物理顺序
        if cameras is not None:
            self.images = list(cameras)
        if home is not None:
            self._home_qpos = home.copy()
        self.action_dim = self._compute_action_dim()

    def _compute_action_dim(self) -> int:
        """启用臂下的动作维度；无臂概念 → 完整维度。"""
        if self.ARM_NAMES and self.ACTION_DIM_PER_ARM:
            return self.ACTION_DIM_PER_ARM * len(self.enabled_arms)
        return self.ACTION_DIM

    def _select_qpos(self, qpos) -> np.ndarray:
        """按启用臂（物理顺序）挑选 / 拼接 qpos；无臂概念 → 原样返回。"""
        qpos = np.asarray(qpos, dtype=np.float32)
        if not self.ARM_NAMES:
            return qpos
        parts = [qpos[self.ARM_QPOS_SLICES[arm]] for arm in self.enabled_arms]
        return np.concatenate(parts) if parts else np.asarray([], dtype=np.float32)

    def _expand_action(self, action: Action, operation: str) -> np.ndarray:
        """校验启用臂维度并展开回完整动作空间（未启用臂用 home_qpos 填充）。

        无臂概念 / 全臂启用 → 原样返回（动作即完整维度）；仅启用部分臂时，动作段按物理
        顺序（``ARM_NAMES``）写入对应切片，其余切片填充 ``self._home_qpos``。
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

    # ---- 采集数据状态（采集会话预留：数据由适配器 / 进程自维护，无回合控制）-----
    # Edge 进入采集会话后只读共享内存观测并展示，不驱动回合；adapter 只预留一个
    # data_status() 接口返回数据目录 + 本次采集得到的数据列表（供 server 上报）。
    def data_status(self) -> CaptureData | None:
        """采集数据状态：数据目录 + 本次采集得到的数据列表。

        数据采集（录制写盘）由适配器 / 机器人进程自维护（Edge **不承担保存职责**）；
        本方法只查询 / 上报结果。未启用采集 / 数据未知 → 返回 ``None``。子类按需覆盖。
        """
        return None

    # ---- 采集状态（采集会话预留：采集员 / 任务名等元信息，进程自维护）----------
    def capture_status(self) -> CaptureStatus | None:
        """采集状态：机器人进程当前采集元信息（采集员 / 任务名等）+ 运行位。

        元信息由 ``capture sync --meta`` 从 console / web 同步到进程，进程保存一轮
        数据时附加；本方法只查询 / 上报结果。未启用采集 / 未知 → 返回 ``None``。
        子类按需覆盖。
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
        """安全停止（幂等、失败安全）。"""
        raise NotImplementedError

    # ---- 生命周期辅助 ----------------------------------------------------------
    def reset(self) -> None:
        """程序复位到 home（非阻塞）：设置 home 目标，由后续 observe()/rollout() 推进。"""
        pass
