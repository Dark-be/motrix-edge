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

"""edge 契约 HTTP 服务器 + 契约（合并）：为任意机器人运行环境（env）提供 /v1 指令接口，
并在 server 侧**直接写入观测共享内存**。

**一个 adapter 对应一个 robot server**：观测键 / HTTP 端点 / 共享内存布局都由 adapter
（motrix_edge.adapter 契约）规定；本模块**复用**这些定义（
``motrix_edge.adapter.{base,http_contract,shm_contract}``，契约单点），并直接写
共享内存（ObsShmWriter）。env 不碰 HTTP / 共享内存，只负责控制 robot。

env（BaseEnv 子类）只控制 robot：30Hz 主循环、状态切换、接收控制方法（robot_reset /
robot_execute ...）。本模块把 /v1 指令桥接到 env，并在每帧回调（env.on_frame）中读取
env 保留的观测副本（30Hz 由 env 调用 robot.get_observation() 产生）组装 standard_obs、
写入共享内存、缓存供 /observe 调试。

端点（前缀 /v1）:
    POST /v1/discover      自描述探活
    GET  /v1/health        {ok, detail}
    POST /v1/reset         复位到 home（非阻塞）
    POST /v1/execute       raw 动作 {action}
    POST /v1/rollout       推理动作 {action}
    POST /v1/teleop        遥操作开关 {enabled: bool}
    POST /v1/safe_stop     急停
    GET  /v1/data_status   {data_dir, data_files, running}
    POST /v1/capture/start 开始一轮采集（episode 开始）
    POST /v1/capture/end   结束一轮采集（episode 结束）
    POST /v1/capture/sync  同步采集元信息 {meta}（operator / task_name 等）
    GET  /v1/capture/status {running, operator, task_name, meta}

入口见 server/robot_server.py（按 config 自动匹配机器人，由 robot.type 选择虚拟/真实接入位）。
"""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from multiprocessing import shared_memory

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from utils.base.data_handler import debug_print

from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_QPOS
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION_DIM,
    FIELD_CAPABILITIES,
    FIELD_DATA_FILES,
    FIELD_DETAIL,
    FIELD_ENDPOINT,
    FIELD_META,
    FIELD_NAME,
    FIELD_OBSERVATION_KEYS,
    FIELD_OK,
    FIELD_OPERATOR,
    FIELD_ROBOT,
    FIELD_ROBOT_MODEL_ID,
    FIELD_ROBOT_MODEL_VERSION,
    FIELD_RUNNING,
    FIELD_SHM_NAME,
    FIELD_STATUS,
    FIELD_SUPPORTED_ADAPTERS,
    FIELD_TASK_NAME,
    FIELD_TYPE,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_CAPTURE_STATUS,
    PATH_CAPTURE_SYNC,
    PATH_DATA_STATUS,
    PATH_DISCOVER,
    PATH_EXECUTE,
    PATH_HEALTH,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
    VALUE_STATUS_ACCEPTED,
)
from motrix_edge.adapter.shm_contract import ObsShmWriter

# ----------------------------------------------------------------------------
# 契约（复用 adapter 包定义：观测键 / HTTP 端点 / 共享内存布局）
# ----------------------------------------------------------------------------
FIELD_ID = "id"


def _robot_action_dim(robot) -> int:
    if hasattr(robot, "QPOS"):
        return int(robot.QPOS)
    if hasattr(robot, "action_dim"):
        action_dim = robot.action_dim
        return int(action_dim() if callable(action_dim) else action_dim)
    capabilities = getattr(robot, "capabilities", None)
    if capabilities is not None and hasattr(capabilities, "action_dim"):
        return int(capabilities.action_dim)
    raise AttributeError("robot action dimension is not available")


def _robot_image_names(robot) -> list[str]:
    names = list(getattr(robot, "IMAGE_NAMES", []) or [])
    if names:
        return names
    capabilities = getattr(robot, "capabilities", None)
    if capabilities is not None and hasattr(capabilities, "image_names"):
        return list(capabilities.image_names)
    return []


def _robot_capabilities(robot) -> dict[str, bool]:
    capabilities = getattr(robot, "CAPABILITIES", None)
    if isinstance(capabilities, dict) and capabilities:
        return {str(key): bool(value) for key, value in capabilities.items()}
    return {
        "capture": True,
        "execute": True,
        "streaming": bool(_robot_image_names(robot)),
    }


_DEFAULT_HOST = "0.0.0.0"  # 对齐 Edge 侧 adapter.SDK_HOST
_DEFAULT_PORT = 8090  # 对齐 Edge 侧 adapter.SDK_URL


class ActionRequest(BaseModel):
    action: list[float] = Field(..., description="动作 [左6关节, 左夹爪, 右6关节, 右夹爪]")


class TeleopRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用遥操作（true=遥操作 / false=程控）")


class CaptureSyncRequest(BaseModel):
    meta: dict = Field(..., description="采集元信息（operator / task_name / description 等）")


class _ShmPublisher:
    """server 侧观测发布器：组装 standard_obs 并写入共享内存（env 不碰共享内存）。

    env 每帧回调（on_frame）把 30Hz 保留的观测副本传入 publish()；本类按 adapter 契约
    组装 standard_obs、写入 ObsShmWriter，并缓存供 /observe 调试。
    """

    def __init__(self, robot):
        self.robot = robot
        self._writer: ObsShmWriter | None = None
        self.last_obs: dict = {}  # 最新 standard_obs（/observe 调试用）

    def publish(self, obs):
        """发布一帧观测（obs 来自 env 30Hz 保留的副本，键已按契约：observations/qpos + images/<cam>）。"""
        if obs is None or obs.get(KEY_QPOS) is None:
            return
        image_names = _robot_image_names(self.robot)
        standard = {
            KEY_QPOS: obs[KEY_QPOS],
            KEY_ACTION: obs.get("action") if obs.get("action") is not None else obs[KEY_QPOS].copy(),
            "seq": getattr(self.robot, "seq", 0),
        }
        for name in image_names:
            standard[f"{CAMERA_PREFIX}{name}"] = obs[f"{CAMERA_PREFIX}{name}"]
        self.last_obs = standard
        if self._writer is None:
            self._writer = self._create_writer()
        self._writer.write(
            qpos=standard[KEY_QPOS],
            images=[standard[f"{CAMERA_PREFIX}{n}"] for n in image_names],
        )

    def _create_writer(self) -> ObsShmWriter:
        # 相机尺寸（假设各相机一致）；无相机机器人（IMAGES 为空）用占位尺寸，image_count=0 无图像数据
        image_size = next(iter(getattr(self.robot, "IMAGES", {}).values()), (640, 480))
        image_names = _robot_image_names(self.robot)
        try:
            writer = ObsShmWriter(
                name=self.robot.SHM_NAME,
                image_count=len(image_names),
                image_size=image_size,
                qpos_dim=_robot_action_dim(self.robot),
            )
        except FileExistsError:
            # 上次进程残留：attach 后 unlink 再重建（幂等清理）
            stale = shared_memory.SharedMemory(name=self.robot.SHM_NAME)
            stale.close()
            stale.unlink()
            writer = ObsShmWriter(
                name=self.robot.SHM_NAME,
                image_count=len(image_names),
                image_size=image_size,
                qpos_dim=_robot_action_dim(self.robot),
            )
        writer.set_flags(running=True)
        return writer

    def close(self):
        """释放共享内存写者（server 停止时调用）。"""
        if self._writer is not None:
            try:
                self._writer.set_flags(running=False)
            finally:
                try:
                    self._writer.unlink()
                finally:
                    self._writer.close()
                    self._writer = None


def create_app(env, host: str | None = None, port: int | None = None) -> FastAPI:
    """为已构造的机器人运行环境构建 /v1 契约应用（服务器不读配置；主循环在 env 内）。

    ``host`` / ``port`` 用于 /v1/discover 上报 endpoint；None 时回退默认值。
    实际监听由 ``serve()``（或 uvicorn）决定，可来自配置 server 段或命令行。
    """
    host = host or _DEFAULT_HOST
    port = int(port or _DEFAULT_PORT)
    robot = env.robot
    # server 直接写共享内存：挂到 env 的每帧回调（env 不碰共享内存 / 契约）
    publisher = _ShmPublisher(robot)
    env.on_frame = publisher.publish

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        env.start()
        if not env.health().get("ready"):
            debug_print("SERVER", f"{robot.NAME} not ready — check config / SDK.", "WARNING")
        debug_print("SERVER", f"{robot.NAME} process server started (pid={os.getpid()})", "INFO")
        yield
        debug_print("SERVER", "shutting down ...", "INFO")
        env.stop()
        publisher.close()

    app = FastAPI(title=f"{robot.NAME} robot process server", version="0.1.0", lifespan=lifespan)
    app.state.robot = robot  # 供测试/中间件访问内部机器人
    app.state.env = env

    def _require_ready():
        if not robot.ready:
            raise HTTPException(status_code=503, detail="robot not ready (connect first)")

    # ---------------------------------------------------------------- 信息（调试）
    @app.get("/")
    def root():
        return {
            "name": f"{robot.NAME}_process_server",
            "action_dim": _robot_action_dim(robot),
            "endpoints": [
                PATH_DISCOVER,
                PATH_HEALTH,
                PATH_RESET,
                PATH_EXECUTE,
                PATH_ROLLOUT,
                PATH_TELEOP,
                PATH_SAFE_STOP,
                PATH_DATA_STATUS,
                PATH_CAPTURE_START,
                PATH_CAPTURE_END,
            ],
        }

    # ---------------------------------------------------------------- 自描述探活
    @app.post(PATH_DISCOVER)
    def discover():
        """自描述探活（不初始化）：Edge discover_adapter 消费（id / name / type / running）。"""
        image_names = _robot_image_names(robot)
        return {
            FIELD_STATUS: VALUE_STATUS_ACCEPTED,
            FIELD_ROBOT: {
                FIELD_ID: robot.NAME,
                FIELD_NAME: robot.NAME,
                FIELD_TYPE: robot.ADAPTER_TYPE,
                FIELD_RUNNING: True,
                FIELD_SUPPORTED_ADAPTERS: [robot.ADAPTER_TYPE],
                FIELD_ROBOT_MODEL_ID: robot.ROBOT_MODEL_ID,
                FIELD_ROBOT_MODEL_VERSION: robot.ROBOT_MODEL_VERSION,
                FIELD_ACTION_DIM: _robot_action_dim(robot),
                FIELD_OBSERVATION_KEYS: [KEY_QPOS] + [f"{CAMERA_PREFIX}{n}" for n in image_names],
                FIELD_CAPABILITIES: _robot_capabilities(robot),
                FIELD_ENDPOINT: f"http://{host}:{port}",
                FIELD_SHM_NAME: robot.SHM_NAME,
            },
        }

    # ---------------------------------------------------------------- 健康检查
    @app.get(PATH_HEALTH)
    def health():
        """健康检查 {ok, detail}：Edge adapter.health 消费。"""
        h = env.health()
        ok = bool(h.get("ready") and h.get("loop_alive"))
        detail = "" if ok else (h.get("last_error") or "robot not ready")
        return {FIELD_OK: ok, FIELD_DETAIL: detail}

    # ---------------------------------------------------------------- 指令
    @app.post(PATH_RESET)
    def reset():
        """程序复位到 home（非阻塞）：与 execute 一样只修改唯一目标。"""
        _require_ready()
        try:
            env.robot_reset()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    def _apply_action(req: ActionRequest) -> dict:
        _require_ready()
        try:
            env.robot_execute(req.action)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_EXECUTE)
    def execute(req: ActionRequest):
        return _apply_action(req)

    @app.post(PATH_ROLLOUT)
    def rollout(req: ActionRequest):
        # 当前与 execute 一致：都只修改唯一目标，主循环限速跟踪
        return _apply_action(req)

    @app.post(PATH_TELEOP)
    def teleop(req: TeleopRequest):
        """设置遥操作开关（true=遥操作 / false=程控）。"""
        _require_ready()
        env.robot_set_teleop(req.enabled)
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_SAFE_STOP)
    def safe_stop():
        env.robot_safe_stop()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    # ---------------------------------------------------------------- 采集（episode）
    @app.post(PATH_CAPTURE_START)
    def capture_start():
        """开始一轮采集（episode 开始）：env 置 capturing=True，主循环记录观测。"""
        _require_ready()
        env.robot_capture_start()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_CAPTURE_END)
    def capture_end():
        """结束一轮采集（episode 结束）：env 置 capturing=False，保存为一条 episode。"""
        _require_ready()
        env.robot_capture_end()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_CAPTURE_SYNC)
    def capture_sync(req: CaptureSyncRequest):
        """同步采集元信息（operator / task_name / description 等）到 collector。

        由 Edge adapter 的 ``sync_capture_meta`` 调用（POST /v1/capture/sync，
        body ``{meta: {...}}``）；collector 在结束一轮采集写同名 JSON 元信息时附加。
        """
        env.robot_capture_sync(req.meta)
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    # ---------------------------------------------------------------- 采集状态
    @app.get(PATH_CAPTURE_STATUS)
    def capture_status():
        """采集状态 {running, operator, task_name, meta}：Edge adapter.capture_status 消费。"""
        cs = env.capture_status()
        return {
            FIELD_RUNNING: bool(cs.get("running")),
            FIELD_OPERATOR: cs.get("operator"),
            FIELD_TASK_NAME: cs.get("task_name"),
            FIELD_META: cs.get("meta") or {},
        }

    # ---------------------------------------------------------------- 采集数据状态
    @app.get(PATH_DATA_STATUS)
    def data_status():
        """采集数据状态 {data_dir, data_files, running}：Edge adapter.data_status 消费。"""
        ds = env.data_status()
        return {"data_dir": ds.get("data_dir"), FIELD_DATA_FILES: ds.get("episodes", []), FIELD_RUNNING: True}

    # ---------------------------------------------------------------- 调试（非契约）
    @app.get("/observe")
    def observe_debug():
        """调试用：最新观测 qpos + 相机 JPEG(base64)（Edge 侧实际经共享内存读观测）。"""
        obs = publisher.last_obs
        if not obs:
            return {"ready": False, "data": None}
        out = {"ready": True, "qpos": obs[KEY_QPOS].tolist(), "seq": obs.get("seq")}
        for name in _robot_image_names(robot):
            rgb = obs.get(f"{CAMERA_PREFIX}{name}")
            if rgb is None:
                out[f"images/{name}"] = None
                continue
            ok, buf = cv2.imencode(".jpg", cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR))
            out[f"images/{name}"] = base64.b64encode(buf.tobytes()).decode("ascii") if ok else None
        return out

    return app


def serve(app_obj, host: str | None = None, port: int | None = None) -> None:
    host = host or _DEFAULT_HOST
    port = int(port or _DEFAULT_PORT)
    debug_print("SERVER", f"uvicorn: http://{host}:{port}", "INFO")
    uvicorn.run(app_obj, host=host, port=port, log_level="info")
