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

"""HttpShmAdapter —— HTTP + 共享内存中间件 adapter 的共享基类。

面向「受控子进程」机器人：硬件初始化与连接由独立 SDK 进程自维护，Edge 侧 adapter 只做
**薄客户端**，经两条通道通信：

- **HTTP 指令下行**：``execute`` / ``rollout`` / ``safe_stop`` / ``reset`` /
  ``set_teleop`` / 采集回合控制；
- **共享内存观测上行**：读取 qpos 与 raw RGB 相机帧，编码为 Edge 契约的 JPEG；
- **状态查询**：``health`` 实时查询进程，``data_status`` / ``capture_status`` 查询
  采集状态。

**子类只需声明类常量**（身份 / 能力 / 连接参数 / 臂布局 / 相机布局），本基类提供全部
通用实现（``__init__`` + 指令 / 观测 / 状态方法）。身份由 discover 解析传入（``name``）；
能力与连接参数由类级常量定义，不随 discover 传输、不接收 Edge 配置；运行时可由 Edge
配置（``adapter`` 段）裁剪——``configure()`` 只启用指定臂 / 相机，未启用臂动作用
``HOME_QPOS`` 填充。
"""

import cv2
import httpx
import numpy as np

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_QPOS,
    Action,
    CaptureData,
    CaptureStatus,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_CONTROL_HZ,
    FIELD_DATA_DIR,
    FIELD_DATA_FILES,
    FIELD_DETAIL,
    FIELD_MEASURED_HZ,
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


def _as_opt_float(value) -> float | None:
    """health 频率字段：缺失 / 非数值 → None。"""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


class HttpShmAdapter(RobotAdapter):
    """HTTP + 共享内存中间件 adapter（子类声明类常量，行为全在此）。"""

    def __init__(self, name: str = ""):
        """中间件实例：由 discover 解析出的身份（name）参数化。

        - ``name``：机器人进程 discover 解析出的名称（展示用；缺省回退类常量）。
        - ``type`` 由类常量 ``ADAPTER_TYPE`` 确定（entry point 类型，用于实例化）。
        - 能力（动作维度 / 相机布局）与连接参数由类级常量定义；运行时经 ``configure()``
          应用 Edge 配置（启用臂 / 相机 / home_qpos）。
        """
        super().__init__(name=name)
        self.name = name or self.NAME
        # 能力：类级常量（自包含，不随 discover 传输）；基类 __init__ 已初始化
        # enabled_arms / _home_qpos / _full_image_names
        self.action_dim = self._compute_action_dim()  # 缺省全臂
        self.images = list(self.IMAGES)  # 相机名列表（IMAGES 字典的键；configure 可裁剪）
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
        debug_print(self.name, f"{type(self).__name__} released.", "INFO")

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
        """健康检查：实时 ``GET /v1/health``（SDK 自维护硬件；Edge 只查询）。

        附带 robot 名义 / 实测控制频率（control_hz / measured_hz，robot env 上报）。
        """
        data = {}
        try:
            resp = self._client().get(PATH_HEALTH)
            data = resp.json() if resp.status_code == 200 else {}
            ok = resp.status_code == 200 and bool(data.get(FIELD_OK, False))
        except Exception as exc:  # noqa: BLE001 进程失联
            debug_print(self.name, f"health check failed: {exc}", "WARNING")
            ok = False
        self._running = ok
        return HealthStatus(
            ok=ok,
            detail=str(data.get(FIELD_DETAIL) or ""),
            control_hz=_as_opt_float(data.get(FIELD_CONTROL_HZ)),
            measured_hz=_as_opt_float(data.get(FIELD_MEASURED_HZ)),
        )

    # ---- 指令（经 HTTP 转发 SDK 进程）-------------------------------------------
    def reset(self) -> None:
        """程序复位到 home（非阻塞）：HTTP 转发 SDK 进程。"""
        self.reset_calls += 1
        self._client().post(PATH_RESET)

    def execute(self, action: Action) -> None:
        """直接下发动作指令（raw）：本地记录 + HTTP 转发 SDK 进程。

        经 ``_expand_action`` 校验维度（启用臂数）并把动作展开回完整空间（未启用臂 home
        填充）再发送。
        """
        target = self._expand_action(action, "execute")
        self.executed.append(target.tolist())
        debug_print(self.name, f"execute sent: {target.tolist()}", "INFO")
        self._client().post(PATH_EXECUTE, json={FIELD_ACTION: target.tolist()})

    def set_teleop(self, enabled: bool) -> None:
        """设置遥操作开关（true=遥操作 / false=程控）：本地记录 + HTTP 转发 SDK 进程。"""
        self.teleop_enabled = bool(enabled)
        debug_print(self.name, f"teleop set to {self.teleop_enabled}", "INFO")
        self._client().post(PATH_TELEOP, json={FIELD_TELEOP_ENABLED: self.teleop_enabled})

    def rollout(self, action: Action) -> None:
        """推理闭环：经 ``_expand_action`` 校验 / 展开后 HTTP 转发（SDK 侧设为限速目标并逐帧靠近）。"""
        target = self._expand_action(action, "rollout")
        self.rollout_calls += 1
        self._client().post(PATH_ROLLOUT, json={FIELD_ACTION: target.tolist()})

    def safe_stop(self) -> None:
        """安全停止（幂等、失败安全）：本地记录 + HTTP 转发 SDK 进程。"""
        self.safe_stop_calls += 1
        try:
            self._client().post(PATH_SAFE_STOP)
        except Exception as exc:  # noqa: BLE001 安全停止失败只记录，不覆盖原始故障
            debug_print(self.name, f"safe_stop failed: {exc}", "ERROR")

    # ---- 采集数据状态 / 回合控制 ------------------------------------------------
    def data_status(self) -> CaptureData | None:
        """采集数据状态：数据目录 + 本次采集得到的数据列表（查询 SDK 进程）。

        数据采集（录制写盘）由 SDK 进程自维护；本方法只查询 / 上报结果。
        """
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
        """开始一轮采集：HTTP 转发 SDK 进程（episode 开始）。"""
        debug_print(self.name, "capture episode start", "INFO")
        self._client().post(PATH_CAPTURE_START)

    def end_capture(self) -> None:
        """结束一轮采集：HTTP 转发 SDK 进程（episode 结束）。"""
        debug_print(self.name, "capture episode end", "INFO")
        self._client().post(PATH_CAPTURE_END)

    # ---- observe（共享内存观测上行）---------------------------------------------
    def observe(self) -> dict | None:
        """读取共享内存最新观测帧（SDK 进程产出），图像编码为 JPEG（Edge 契约）。

        只返回启用臂 qpos 与启用相机（configure 裁剪）；SDK 进程把观测填充到共享内存，
        observe 只读取、不推进 SDK 运行。尚无首帧时返回 ``None``。
        """
        if self._shm is None:
            self._shm = ObsShmReader(self.shm_name)  # 惰性 attach（首次观测时）
        frame = self._shm.read()
        if frame is None:
            return None  # SDK 尚未产出首帧：瞬态无帧，不是空观测
        qpos = self._select_qpos(np.asarray(frame["qpos"], dtype=np.float32))
        obs = {
            KEY_QPOS: qpos,
            KEY_ACTION: qpos.copy(),  # 测试：action = 当前执行位置
        }
        full = {name: self._encode_jpeg(img) for name, img in zip(self._full_image_names, frame["images"])}
        for name in self.images:  # 只暴露启用相机
            obs[f"{CAMERA_PREFIX}{name}"] = full[name]
        return obs

    @staticmethod
    def _encode_jpeg(rgb: np.ndarray) -> bytes:
        """RGB ndarray → JPEG bytes（观测缓存图像编码；SDK 产出原图尺寸）。"""
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("Failed to encode image as JPEG")
        return buf.tobytes()
