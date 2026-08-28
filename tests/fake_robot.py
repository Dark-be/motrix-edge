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
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_QPOS,
    AdapterCapability,
    CaptureData,
    CaptureStatus,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)


class FakeRobotAdapter(RobotAdapter):
    """内存态假适配器：模拟观测缓存 / 数据状态 / 健康状态，无硬件无网络。

    - ``observe`` 返回有限 qpos / action（含图像键，JPEG bytes）；
    - ``data_status`` 返回采集数据状态（数据目录 + 本次采集得到的数据列表；数据由
      SDK 自维护，无回合控制）；
    - ``health`` / ``ready`` / ``release`` 反映进程可用性（SDK 自维护硬件与连接）。
    """

    NAME = "test_robot"
    ROBOT_MODEL_ID = "test-robot"
    ACTION_DIM = 14
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
        self._data_files: list[str] = []
        self.reset_calls = 0
        self.safe_stop_calls = 0
        self.release_calls = 0
        self.executed: list = []  # execute / rollout 记录（供测试断言）
        self.teleop_values: list[bool] = []  # set_teleop 记录（供测试断言）
        self.capture_episodes: list[str] = []  # start_capture / end_capture 记录（供测试断言）
        self.capture_running = False  # 进程是否在采集（录制中）
        self.capture_meta: dict = {}  # 同步的采集元信息（保存数据时附加；供测试断言）
        self._qpos = np.zeros(self.ACTION_DIM, dtype=float)
        self._action = np.zeros(self.ACTION_DIM, dtype=float)

    # -- 能力 ---------------------------------------------------------------
    @property
    def capabilities(self) -> RobotCapabilities:
        keys = [KEY_QPOS, KEY_ACTION] + [f"{CAMERA_PREFIX}{img}" for img in self.IMAGES]
        return RobotCapabilities(
            robot_model_id=self.ROBOT_MODEL_ID,
            action_dim=self.ACTION_DIM,
            observation_keys=keys,
            capabilities={
                AdapterCapability.CAPTURE: True,
                AdapterCapability.EXECUTE: True,
            },
        )

    @property
    def images(self) -> list[str]:
        return list(self.IMAGES)

    @property
    def action_dim(self) -> int:
        return self.ACTION_DIM

    # -- 健康 / 释放 ----------------------------------------------------------
    def release(self) -> None:
        self.release_calls += 1

    def health(self) -> HealthStatus:
        return HealthStatus(ok=self.available)

    # -- 观测 ---------------------------------------------------------------
    def observe(self) -> dict:
        obs = {KEY_QPOS: self._qpos.copy(), KEY_ACTION: self._action.copy()}
        for img in self.IMAGES:
            frame = np.full((64, 64, 3), 128, dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", frame)
            obs[f"{CAMERA_PREFIX}{img}"] = buf.tobytes() if ok else b""
        return obs

    # -- 执行 / 推理 -----------------------------------------------------------
    def execute(self, action) -> None:
        self._action = np.asarray(action, dtype=float)
        self.executed.append(np.asarray(action, dtype=float).tolist())

    def rollout(self, action) -> None:
        self._action = np.asarray(action, dtype=float)
        self.executed.append(np.asarray(action, dtype=float).tolist())

    def set_teleop(self, enabled: bool) -> None:
        self.teleop_values.append(bool(enabled))

    def start_capture(self) -> None:
        self.capture_episodes.append("start")
        self.capture_running = True

    def end_capture(self) -> None:
        self.capture_episodes.append("end")
        self.capture_running = False

    # -- 采集状态 / 元信息同步（进程自维护；供测试断言）--------------------------
    def capture_status(self):
        """采集状态：进程当前采集元信息（采集员 / 任务名等）+ 运行位。"""
        return CaptureStatus(
            running=self.capture_running,
            operator=self.capture_meta.get("operator"),
            task_name=self.capture_meta.get("task_name"),
            meta=dict(self.capture_meta),
        )

    def sync_capture_meta(self, meta) -> None:
        """同步采集元信息到进程（保存一轮数据时附加）。"""
        self.capture_meta = dict(meta or {})

    # -- 采集数据状态（数据由 SDK 自维护，无回合控制）----------------------------
    def data_status(self):
        """采集数据状态：数据目录 + 本次采集得到的数据列表。"""
        return CaptureData(data_dir=self._data_dir, data_files=list(self._data_files))

    # -- 复位 / 安全停止 -------------------------------------------------------
    def reset(self) -> None:
        self.reset_calls += 1
        self._qpos = np.zeros(self.ACTION_DIM, dtype=float)

    def safe_stop(self) -> None:
        self.safe_stop_calls += 1
