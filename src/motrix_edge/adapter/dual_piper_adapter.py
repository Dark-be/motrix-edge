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

"""DualPiperAdapter —— 双臂 Piper 机器人适配器（HTTP + 共享内存薄客户端）。

与 ``TestRobotAdapter`` 同构：机器人硬件初始化与连接由独立 SDK 进程自行维护，Edge 侧
adapter 只负责两条通信通道：

- **HTTP 指令下行**：``execute`` / ``rollout`` / ``safe_stop`` / ``reset`` /
  ``set_teleop`` / 采集回合控制；
- **共享内存观测上行**：读取双臂 qpos 与三路 raw RGB 相机帧，编码为 Edge 契约的 JPEG；
- **状态查询**：``health`` 实时查询进程，``data_status`` 查询采集产物状态。

动作维度为 14（左右臂各 6 关节 + 1 夹爪），相机布局为 ``cam_head`` /
``cam_left_wrist`` / ``cam_right_wrist``。身份由 discover 传入，能力与连接参数由类常量定义。
"""

import cv2
import httpx
import numpy as np

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_QPOS,
    Action,
    AdapterCapability,
    CaptureData,
    CaptureStatus,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_DATA_DIR,
    FIELD_DATA_FILES,
    FIELD_META,
    FIELD_OK,
    FIELD_OPERATOR,
    FIELD_RUNNING,
    FIELD_TASK_NAME,
    FIELD_TELEOP_ENABLED,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_CAPTURE_STATUS,
    PATH_CAPTURE_SYNC,
    PATH_DATA_STATUS,
    PATH_EXECUTE,
    PATH_HEALTH,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
)
from motrix_edge.adapter.shm_contract import ObsShmReader
from motrix_edge.utils.data_handler import debug_print


class DualPiperAdapter(RobotAdapter):
    # ---- 身份（discover 解析传入；缺省回退类常量）----
    NAME = "dual_piper"  # 实例名（debug_print 前缀）
    ADAPTER_TYPE = "dual_piper"  # 本 adapter 的 entry point 类型

    # ---- 能力 / 连接参数（类级常量，自包含，不随 discover 传输）----
    ROBOT_MODEL_ID = "dual-piper"
    ROBOT_MODEL_VERSION = "0.0.0"
    # 双臂 Piper：左 + 右臂，各 6 关节 + 1 夹爪 = 7，共 14
    ACTION_DIM = 14
    # 相机布局：{相机名: 分辨率 (width, height)}（SDK 产出 raw RGB；observe 编码 JPEG 原图）
    IMAGES: dict[str, tuple[int, int]] = {
        "cam_head": (640, 480),
        "cam_left_wrist": (640, 480),
        "cam_right_wrist": (640, 480),
    }

    CAPABILITIES: dict[AdapterCapability, bool] = {
        AdapterCapability.CAPTURE: True,
        AdapterCapability.EXECUTE: True,
        AdapterCapability.STREAMING: True,
    }

    # 中间件连接参数（与双臂 Piper SDK 进程约定）
    SDK_URL = "http://127.0.0.1:8090"
    SHM_NAME = "dual_piper_obs"
    HTTP_TIMEOUT = 3.0

    def __init__(self, name: str = ""):
        """中间件实例：由 discover 解析出的身份（name）参数化。

        - ``name``：机器人进程 discover 解析出的名称（展示用；缺省回退类常量）。
        - ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（entry point 类型，用于实例化）。
        - 能力（动作维度 / 相机布局）与连接参数**全部由类级常量定义**，不随 discover 传输、
          不接收 Edge 配置。
        """
        super().__init__(name=name)
        self.name = name or self.NAME
        # 能力：类级常量（自包含，不随 discover 传输）
        self.action_dim = self.ACTION_DIM
        self.images = list(self.IMAGES)  # 相机名列表（IMAGES 字典的键）
        self.robot_model_id = self.ROBOT_MODEL_ID
        self.robot_model_version = self.ROBOT_MODEL_VERSION
        self._capabilities = dict(self.CAPABILITIES)

        # 中间件连接参数：类级常量（不接收 Edge 配置）
        self.sdk_url = self.SDK_URL.rstrip("/")
        self.shm_name = self.SHM_NAME
        self.http_timeout = self.HTTP_TIMEOUT

        # 惰性连接资源：首次指令 / 观测时建立（SDK 自维护硬件与连接）
        self._http: httpx.Client | None = None  # SDK HTTP 客户端（指令下行）
        self._shm: ObsShmReader | None = None  # 共享内存观测读者（观测上行）
        self._running = False  # 机器人进程最近一次确认是否运行（health 实时刷新）

        # 本地记录（便于调试与无硬件测试）
        self.executed: list[list[float]] = []
        self.rollout_calls = 0
        self.safe_stop_calls = 0
        self.reset_calls = 0
        self.teleop_enabled = False

    @property
    def running(self) -> bool:
        """机器人进程最近一次确认是否运行（health 实时刷新）。"""
        return self._running

    def _client(self) -> httpx.Client:
        """惰性建立 SDK HTTP 客户端（首次指令 / 查询时）。"""
        if self._http is None:
            self._http = httpx.Client(base_url=self.sdk_url, timeout=self.http_timeout)
        return self._http

    def release(self):
        """释放 Edge 侧本地资源（SDK 连接由进程自维护）。"""
        if self._shm is not None:
            self._shm.close()
            self._shm = None
        if self._http is not None:
            self._http.close()
            self._http = None
        debug_print(self.name, "DualPiperAdapter released.", "INFO")

    # ---- capabilities ----------------------------------------------------------
    @property
    def capabilities(self) -> RobotCapabilities:
        obs_keys = [KEY_QPOS] + [f"{CAMERA_PREFIX}{img}" for img in self.images]
        return RobotCapabilities(
            robot_model_id=self.robot_model_id,
            robot_model_version=self.robot_model_version,
            action_dim=self.action_dim,
            observation_keys=obs_keys,
            capabilities=dict(self._capabilities),
        )

    # ---- health（实时查询 SDK 进程状态）-----------------------------------------
    def health(self) -> HealthStatus:
        try:
            resp = self._client().get(PATH_HEALTH)
            ok = resp.status_code == 200 and bool(resp.json().get(FIELD_OK, False))
        except Exception as exc:  # noqa: BLE001 进程失联
            debug_print(self.name, f"health check failed: {exc}", "WARNING")
            ok = False
        self._running = ok
        return HealthStatus(ok=ok)

    # ---- 指令（经 HTTP 转发 SDK 进程）-------------------------------------------
    def reset(self) -> None:
        self.reset_calls += 1
        self._client().post(PATH_RESET)

    def execute(self, action: Action) -> None:
        target = self._validate_action(action, "execute")
        self.executed.append(target.tolist())
        debug_print(self.name, f"execute sent: {target.tolist()}", "INFO")
        self._client().post(PATH_EXECUTE, json={FIELD_ACTION: target.tolist()})

    def set_teleop(self, enabled: bool) -> None:
        self.teleop_enabled = bool(enabled)
        debug_print(self.name, f"teleop set to {self.teleop_enabled}", "INFO")
        self._client().post(PATH_TELEOP, json={FIELD_TELEOP_ENABLED: self.teleop_enabled})

    def rollout(self, action: Action) -> None:
        target = self._validate_action(action, "rollout")
        self.rollout_calls += 1
        self._client().post(PATH_ROLLOUT, json={FIELD_ACTION: target.tolist()})

    def safe_stop(self) -> None:
        self.safe_stop_calls += 1
        try:
            self._client().post(PATH_SAFE_STOP)
        except Exception as exc:  # noqa: BLE001 安全停止失败只记录，不覆盖原始故障
            debug_print(self.name, f"safe_stop failed: {exc}", "ERROR")

    # ---- 采集数据状态 / 回合控制 ------------------------------------------------
    def data_status(self) -> CaptureData | None:
        try:
            resp = self._client().get(PATH_DATA_STATUS)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            debug_print(self.name, f"data status query failed: {exc}", "WARNING")
            return None
        return CaptureData(
            data_dir=body.get(FIELD_DATA_DIR),
            data_files=[str(path) for path in body.get(FIELD_DATA_FILES, [])],
        )

    def capture_status(self) -> CaptureStatus | None:
        """采集状态：机器人进程当前采集元信息 + 运行位（查询 SDK 进程）。"""
        try:
            resp = self._client().get(PATH_CAPTURE_STATUS)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            debug_print(self.name, f"capture status query failed: {exc}", "WARNING")
            return None
        return CaptureStatus(
            running=bool(body.get(FIELD_RUNNING, False)),
            operator=body.get(FIELD_OPERATOR),
            task_name=body.get(FIELD_TASK_NAME),
            meta=dict(body.get(FIELD_META, {}) or {}),
        )

    def sync_capture_meta(self, meta: dict) -> None:
        """同步采集元信息到 SDK 进程（进程保存一轮数据时附加）。"""
        debug_print(self.name, f"capture meta sync: {meta}", "INFO")
        self._client().post(PATH_CAPTURE_SYNC, json={FIELD_META: meta})

    def start_capture(self) -> None:
        debug_print(self.name, "capture episode start", "INFO")
        self._client().post(PATH_CAPTURE_START)

    def end_capture(self) -> None:
        debug_print(self.name, "capture episode end", "INFO")
        self._client().post(PATH_CAPTURE_END)

    # ---- observe（共享内存观测上行）---------------------------------------------
    def observe(self) -> dict | None:
        """读取双臂 qpos + 三路相机；尚无首帧时返回 ``None``。"""
        if self._shm is None:
            self._shm = ObsShmReader(self.shm_name)
        frame = self._shm.read()
        if frame is None:
            return None
        qpos = np.asarray(frame["qpos"], dtype=np.float32)
        obs = {KEY_QPOS: qpos, KEY_ACTION: qpos.copy()}
        for image, name in zip(frame["images"], self.images):
            obs[f"{CAMERA_PREFIX}{name}"] = self._encode_jpeg(image)
        return obs

    def _validate_action(self, action: Action, operation: str) -> np.ndarray:
        """动作转 float64 一维数组并校验双臂维度。"""
        target = np.asarray(action, dtype=np.float64)
        if target.ndim != 1 or target.shape[0] != self.action_dim:
            actual = target.shape[0] if target.ndim > 0 else 0
            raise ValueError(f"{operation} action dim {actual} != action_dim {self.action_dim}")
        return target

    @staticmethod
    def _encode_jpeg(rgb: np.ndarray) -> bytes:
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("Failed to encode image as JPEG")
        return buf.tobytes()
