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

"""进程内假机器人适配器 —— 只测 edge 包内独立逻辑，不启动 SDK 进程 / 网络 / 共享内存。

实现 ``RobotAdapter`` 契约（内存态）：观测缓存、采集数据状态、健康状态、复位 /
安全停止。供 session / server 测试注入（替代 TestRobotAdapter 中间件对 SDK 进程的依赖），
让测试套件完全自包含、快速稳定。
"""

import cv2
import numpy as np

from motrix_edge.adapter.base import (
    ACTION_SPACE_GRIPPER,
    ACTION_SPACE_JOINT,
    ACTION_SPACE_POSE,
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_GRIPPER,
    KEY_POSE,
    KEY_QPOS,
    ActionSpace,
    AdapterCapability,
    CaptureStatus,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)


class FakeRobotAdapter(RobotAdapter):
    """内存态假适配器：模拟观测缓存 / 数据状态 / 健康状态，无硬件无网络。

    - ``observe`` 返回有限 qpos / action（含图像键，JPEG bytes）；
    - ``capture_status`` 返回采集状态（运行位 + 元信息 + 数据目录 / 列表；数据由
      SDK 自维护，无回合控制）；
    - ``health`` / ``ready`` / ``release`` 反映进程可用性（SDK 自维护硬件与连接）。
    """

    NAME = "test_robot"
    ROBOT_MODEL_ID = "test-robot"
    # 无臂概念（``ARM_NAMES`` 空）→ 「每臂维度」即整机维度：12 关节（双臂量级）+ 2 夹爪；
    # 三个动作空间各自只表达一件事，故值都按空间取（joint 12 / pose 12 / gripper 2）。
    ACTION_DIM_PER_ARM = {ACTION_SPACE_JOINT: 12, ACTION_SPACE_POSE: 12, ACTION_SPACE_GRIPPER: 2}
    ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.POSE, ActionSpace.GRIPPER)
    # 各空间 home（未启用臂填充用；无臂概念时只是能力面上的声明值）
    HOME = {ACTION_SPACE_JOINT: [0.0] * 12, ACTION_SPACE_GRIPPER: [1.0] * 2}
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }

    def __init__(self, config=None, *, available=True):
        super().__init__()
        config = config or {}
        self.name = config.get("name", self.NAME)
        self.available = available
        self._data_dir = config.get("data_dir")
        self.reset_calls = 0
        self.safe_stop_calls = 0
        self.release_calls = 0
        self.executed: list = []  # execute / rollout 记录（供测试断言）
        self.rollout_spaces: list = []  # rollout 收到的动作空间（None = 未指定，按关节空间）
        self.execute_spaces: list = []  # execute 收到的动作空间（None = 未指定，按关节空间）
        self.teleop_calls: list[tuple[bool, str | None]] = []  # set_teleop 记录（enabled, mode）
        self.capture_episodes: list[str] = []  # start_capture / end_capture 记录（供测试断言）
        self.capture_running = False  # 进程是否在采集（录制中）
        self.capture_meta: dict = {}  # 同步的采集元信息（保存数据时附加；供测试断言）
        # 三样观测状态：关节角（12）+ 夹爪（2）+ 末端位姿（12，与关节角同源成正解）
        self._qpos = np.zeros(self.ACTION_DIM_PER_ARM[ACTION_SPACE_JOINT], dtype=float)
        self._gripper = np.ones(self.ACTION_DIM_PER_ARM[ACTION_SPACE_GRIPPER], dtype=float)
        self._pose = np.zeros(self.ACTION_DIM_PER_ARM[ACTION_SPACE_POSE], dtype=float)
        self._action = np.zeros(self.ACTION_DIM_PER_ARM[ACTION_SPACE_JOINT], dtype=float)

    # -- 能力 ---------------------------------------------------------------
    @property
    def capabilities(self) -> RobotCapabilities:
        keys = [KEY_QPOS, KEY_GRIPPER, KEY_POSE, KEY_ACTION] + [f"{CAMERA_PREFIX}{img}" for img in self.IMAGES]
        return RobotCapabilities(
            robot_model_id=self.ROBOT_MODEL_ID,
            action_dim=self.action_dims[ACTION_SPACE_JOINT],
            action_dims=dict(self.action_dims),
            action_spaces=[space.value for space in self.ACTION_SPACES],
            observation_keys=keys,
            capabilities={
                AdapterCapability.CAPTURE: True,
                AdapterCapability.EXECUTE: True,
            },
        )

    @property
    def images(self) -> list[str]:
        return list(self.IMAGES)

    # -- 健康 / 释放 ----------------------------------------------------------
    def release(self) -> None:
        self.release_calls += 1

    def health(self) -> HealthStatus:
        return HealthStatus(ok=self.available)

    # -- 观测 ---------------------------------------------------------------
    def observe(self) -> dict:
        obs = {
            KEY_QPOS: self._qpos.copy(),
            KEY_GRIPPER: self._gripper.copy(),
            KEY_POSE: self._pose.copy(),
            KEY_ACTION: self._action.copy(),
        }
        for img in self.IMAGES:
            frame = np.full((64, 64, 3), 128, dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", frame)
            obs[f"{CAMERA_PREFIX}{img}"] = buf.tobytes() if ok else b""
        return obs

    # -- 执行 / 推理 -----------------------------------------------------------
    def execute(self, action, action_space=None) -> None:
        self._write_space(action, action_space)
        self.executed.append(np.asarray(action, dtype=float).tolist())
        self.execute_spaces.append(None if action_space is None else str(action_space))

    def rollout(self, action, action_space=None) -> bool:
        self._write_space(action, action_space)
        self.executed.append(np.asarray(action, dtype=float).tolist())
        self.rollout_spaces.append(None if action_space is None else str(action_space))
        return True

    def _write_space(self, action, action_space) -> None:
        """按空间只写自己那一段（与机器人侧同语义：三个空间互不覆盖）。"""
        values = np.asarray(action, dtype=float).reshape(-1)
        space = ACTION_SPACE_JOINT if action_space is None else str(action_space)
        if space == ACTION_SPACE_JOINT:
            self._qpos = values.copy()
            self._action = values.copy()
        elif space == ACTION_SPACE_GRIPPER:
            self._gripper = values.copy()
        elif space == ACTION_SPACE_POSE:
            self._pose = values.copy()

    def set_teleop(self, enabled: bool, mode: str | None = None) -> None:
        self.teleop_calls.append((bool(enabled), mode))

    def start_capture(self) -> None:
        self.capture_episodes.append("start")
        self.capture_running = True

    def end_capture(self) -> None:
        self.capture_episodes.append("end")
        self.capture_running = False

    # -- 采集状态 / 元信息同步（进程自维护；供测试断言）--------------------------
    def capture_status(self):
        """采集状态：运行位 + 采集员 / 任务名等元信息 + 数据目录 / 列表。"""
        return CaptureStatus(
            running=self.capture_running,
            meta=dict(self.capture_meta),
            data_dir=self._data_dir,
        )

    def sync_capture_meta(self, meta) -> None:
        """同步采集元信息到进程（保存一轮数据时附加）。"""
        self.capture_meta = dict(meta or {})

    # -- 复位 / 安全停止 -------------------------------------------------------
    def reset(self) -> None:
        self.reset_calls += 1
        self._qpos = np.zeros(self.ACTION_DIM_PER_ARM[ACTION_SPACE_JOINT], dtype=float)

    def safe_stop(self) -> None:
        self.safe_stop_calls += 1
