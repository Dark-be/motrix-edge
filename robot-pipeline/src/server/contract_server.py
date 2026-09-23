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

env（BaseEnv）只控制 robot：控制线程 30Hz 限速步进 + 观测线程取帧发布、状态切换、接收
控制方法（robot_reset / robot_execute ...）。本模块把 /v1 指令桥接到 env，并在每拍回调
（env.on_frame，由 env 观测线程触发）中读取 env 保留的观测副本（= 控制线程采样的机械臂
状态 + 本拍相机帧）组装 standard_obs、写入共享内存、缓存供 /observe 调试。

端点（前缀 /v1）:
    POST /v1/discover      自描述探活
    GET  /v1/health        {ok, detail}
    POST /v1/reset         复位到 home（非阻塞）
    POST /v1/execute       raw 动作 {action}
    POST /v1/rollout       推理动作 {action}（**人工接管中 → 409**）
    POST /v1/teleop        遥操作 {enabled: bool, mode?: absolute|delta}
    POST /v1/safe_stop     急停
    POST /v1/capture/start 开始一轮采集（episode 开始）
    POST /v1/capture/end   结束一轮采集（episode 结束）
    POST /v1/capture/sync  同步采集元信息 {meta}（采集员 / 任务名等）
    GET  /v1/capture/status {running, meta, data_dir}

采集状态单一来源：原 ``GET /v1/data_status``（数据目录 + 数据列表）已**合并**进
``/v1/capture/status``（数据目录随采集状态一并上报）；数据文件列表不经 HTTP 上报——
本地数据的扫描 / 选择 / 打包由 Edge 的 UploadSession 直接读目录完成。

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
from env.base_env import TakeoverActiveError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from utils.base.data_handler import debug_print

from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_GRIPPER, KEY_POSE, KEY_POSE_TARGET, KEY_QPOS
from motrix_edge.adapter.http_contract import (
    DEFAULT_TELEOP_MODE,
    FIELD_ACTION_DIM,
    FIELD_ACTION_DIMS,
    FIELD_CAPABILITIES,
    FIELD_CONTROL_HZ,
    FIELD_DATA_DIR,
    FIELD_DETAIL,
    FIELD_ENDPOINT,
    FIELD_MEASURED_HZ,
    FIELD_META,
    FIELD_NAME,
    FIELD_OBSERVATION_KEYS,
    FIELD_OK,
    FIELD_ROBOT,
    FIELD_ROBOT_MODEL_ID,
    FIELD_ROBOT_MODEL_VERSION,
    FIELD_RUNNING,
    FIELD_SHM_NAME,
    FIELD_STATUS,
    FIELD_SUPPORTED_ADAPTERS,
    FIELD_TYPE,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_CAPTURE_STATUS,
    PATH_CAPTURE_SYNC,
    PATH_DISCOVER,
    PATH_EXECUTE,
    PATH_HEALTH,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
    TELEOP_MODES,
    VALUE_STATUS_ACCEPTED,
)
from motrix_edge.adapter.shm_contract import ObsShmWriter

# ----------------------------------------------------------------------------
# 契约（复用 adapter 包定义：观测键 / HTTP 端点 / 共享内存布局）
# ----------------------------------------------------------------------------
FIELD_ID = "id"


def _robot_name(robot) -> str:
    """进程展示名：实例 ``name``（= 配置 ``robot.name`` 覆盖类常量 ``NAME``）。

    discover（``id`` / ``name``）、``/`` 调试端点与启动日志共用，保证「配置即上报」；
    非 ``BaseRobot`` 的替身按 实例 ``name`` → 类常量 ``NAME`` → 类名 依次回退。
    """
    return str(getattr(robot, "name", None) or getattr(robot, "NAME", None) or type(robot).__name__)


def _robot_action_dims(robot) -> dict[str, int]:
    """各**已声明**动作空间的维度（joint / pose / gripper）——机器人自己算（``action_dims()``）。"""
    action_dims = getattr(robot, "action_dims", None)
    if callable(action_dims):
        return {str(key): int(value) for key, value in action_dims().items()}
    raise AttributeError("robot action dimensions are not available")


def _robot_qpos_dim(robot) -> int:
    """观测 qpos / action 区宽度（关节角，每臂 6 维 × 臂数）。"""
    return int(getattr(robot, "QPOS", 0) or 0)


def _robot_gripper_dim(robot) -> int:
    """夹爪区宽度（每臂 1 维；夹爪是独立动作空间，故不并进 qpos）。"""
    return int(getattr(robot, "GRIPPER", 0) or 0)


def _robot_pose_dim(robot) -> int:
    """位姿区宽度（每臂 xyz + rpy）；0 = 本机不提供位姿（不占位姿区）。"""
    return int(getattr(robot, "POSE", 0) or 0)


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
    action: list[float] = Field(
        ...,
        description=("动作（按臂展开）：joint = 各臂 6 关节角；pose = 各臂 xyz+rpy；gripper = 各臂 1 夹爪"),
    )
    action_space: str | None = Field(
        default=None,
        description=(
            "动作空间：joint（缺省，关节角绝对目标）/ pose（末端位姿绝对目标，机器人侧解算）/ "
            "pose_delta（末端位姿**增量**，叠加在关节段目标的正解位姿上）/ "
            "gripper（每臂 1 夹爪，只改夹爪目标）"
        ),
    )


class TeleopRequest(BaseModel):
    enabled: bool = Field(..., description="是否启用遥操作（true=遥操作 / false=程控）")
    mode: str = Field(
        default=DEFAULT_TELEOP_MODE,
        description="遥操作映射模式：absolute=主臂绝对位姿直连 / delta=人工接管（锚点增量）",
    )


class CaptureSyncRequest(BaseModel):
    meta: dict = Field(..., description="采集元信息（operator / task_name / description 等）")


class _ShmPublisher:
    """server 侧观测发布器：组装 standard_obs 并写入共享内存（env 不碰共享内存）。

    env 观测线程每拍回调（on_frame）把保留的观测副本传入 publish()；本类按 adapter 契约
    组装 standard_obs、写入 ObsShmWriter，并缓存供 /observe 调试。
    """

    def __init__(self, robot):
        self.robot = robot
        self._writer: ObsShmWriter | None = None
        self.last_obs: dict = {}  # 最新 standard_obs（/observe 调试用）

    def publish(self, obs):
        """发布一帧观测（obs 来自 env 观测线程保留的副本，键已按契约）。

        写入 qpos（**关节角**）+ action（关节段目标）+ gripper（夹爪）+ pose（实测位姿）+
        pose_target（目标位姿 = ``FK(关节段目标)``，增量动作的解算结果靠它对上位可见）+
        raw RGB 图像；位姿（与目标位姿）仅在机器人提供（``POSE > 0``）时写入（否则不占该区）。
        """
        if obs is None or obs.get(KEY_QPOS) is None:
            return
        image_names = _robot_image_names(self.robot)
        standard = {
            KEY_QPOS: obs[KEY_QPOS],
            KEY_ACTION: obs.get(KEY_ACTION) if obs.get(KEY_ACTION) is not None else obs[KEY_QPOS].copy(),
            KEY_GRIPPER: obs.get(KEY_GRIPPER),
            "seq": getattr(self.robot, "seq", 0),
        }
        if obs.get(KEY_POSE) is not None:
            standard[KEY_POSE] = obs[KEY_POSE]  # 末端位姿（笛卡尔原语 / 预览的输入）
        if obs.get(KEY_POSE_TARGET) is not None:
            standard[KEY_POSE_TARGET] = obs[KEY_POSE_TARGET]  # 目标位姿（= FK(关节段目标)）
        for name in image_names:
            standard[f"{CAMERA_PREFIX}{name}"] = obs[f"{CAMERA_PREFIX}{name}"]
        self.last_obs = standard
        if self._writer is None:
            self._writer = self._create_writer()
        self._writer.write(
            qpos=standard[KEY_QPOS],
            action=standard[KEY_ACTION],
            gripper=standard[KEY_GRIPPER],
            pose=standard.get(KEY_POSE),
            pose_target=standard.get(KEY_POSE_TARGET),
            images=[standard[f"{CAMERA_PREFIX}{n}"] for n in image_names],
        )

    def _create_writer(self) -> ObsShmWriter:
        """创建共享内存写者（上次进程残留 → attach 后 unlink 重建）。

        区域宽度：qpos / action = 关节角维度；gripper = 臂数；pose / pose_target = 每臂 6 × 臂数
        （不提供位姿 → 0）。
        """
        # 相机尺寸（假设各相机一致）；无相机机器人（IMAGES 为空）用占位尺寸，image_count=0 无图像数据
        image_size = next(iter(getattr(self.robot, "IMAGES", {}).values()), (640, 480))
        image_names = _robot_image_names(self.robot)
        qpos_dim = _robot_qpos_dim(self.robot)

        def build() -> ObsShmWriter:
            return ObsShmWriter(
                name=self.robot.SHM_NAME,
                image_count=len(image_names),
                image_size=image_size,
                qpos_dim=qpos_dim,
                action_dim=qpos_dim,
                gripper_dim=_robot_gripper_dim(self.robot),
                pose_dim=_robot_pose_dim(self.robot),
                pose_target_dim=_robot_pose_dim(self.robot),  # 目标位姿随实测位姿同生共死
            )

        try:
            writer = build()
        except FileExistsError:
            # 上次进程残留：attach 后 unlink 再重建（幂等清理）
            stale = shared_memory.SharedMemory(name=self.robot.SHM_NAME)
            stale.close()
            stale.unlink()
            writer = build()
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
    """为已构造的机器人运行环境构建 /v1 契约应用（服务器不读配置；控制 / 观测线程都在 env 内）。

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
            debug_print("SERVER", f"{_robot_name(robot)} not ready — check config / SDK.", "WARNING")
        debug_print("SERVER", f"{_robot_name(robot)} process server started (pid={os.getpid()})", "INFO")
        yield
        debug_print("SERVER", "shutting down ...", "INFO")
        env.stop()
        publisher.close()

    app = FastAPI(title=f"{_robot_name(robot)} robot process server", version="0.1.0", lifespan=lifespan)
    app.state.robot = robot  # 供测试/中间件访问内部机器人
    app.state.env = env

    def _require_ready():
        if not robot.ready:
            raise HTTPException(status_code=503, detail="robot not ready (connect first)")

    # ---------------------------------------------------------------- 信息（调试）
    @app.get("/")
    def root():
        return {
            "name": f"{_robot_name(robot)}_process_server",
            "action_dims": _robot_action_dims(robot),
            "action_spaces": list(getattr(robot, "ACTION_SPACES", None) or ("joint",)),
            "endpoints": [
                PATH_DISCOVER,
                PATH_HEALTH,
                PATH_RESET,
                PATH_EXECUTE,
                PATH_ROLLOUT,
                PATH_TELEOP,
                PATH_SAFE_STOP,
                PATH_CAPTURE_START,
                PATH_CAPTURE_END,
            ],
        }

    # ---------------------------------------------------------------- 自描述探活
    @app.post(PATH_DISCOVER)
    def discover(request: Request):
        """自描述探活（不初始化）：Edge discover_adapter 消费（id / name / type / running）。

        ``endpoint`` 上报**可连地址**：取请求头 ``Host``（客户端实际用的地址），避免把绑定
        地址（如 0.0.0.0）当成指令目标；Edge 采用该值后，「discover 可达」即「指令可达」。

        ``id`` / ``name`` 取**进程展示名**（配置 ``robot.name``，缺省回退机器人类常量 ``NAME``）：
        Edge 侧作为 adapter 展示名（控制台 / 状态接口），故同型号多机靠配置区分（如
        ``dual_piper_pc16``）。
        """
        image_names = _robot_image_names(robot)
        endpoint = f"http://{request.headers.get('host') or f'{host}:{port}'}"
        name = _robot_name(robot)
        return {
            FIELD_STATUS: VALUE_STATUS_ACCEPTED,
            FIELD_ROBOT: {
                FIELD_ID: name,
                FIELD_NAME: name,
                FIELD_TYPE: robot.ADAPTER_TYPE,
                FIELD_RUNNING: True,
                FIELD_SUPPORTED_ADAPTERS: [robot.ADAPTER_TYPE],
                FIELD_ROBOT_MODEL_ID: robot.ROBOT_MODEL_ID,
                FIELD_ROBOT_MODEL_VERSION: robot.ROBOT_MODEL_VERSION,
                FIELD_ACTION_DIM: _robot_action_dims(robot).get("joint", 0),
                FIELD_ACTION_DIMS: _robot_action_dims(robot),
                # 观测键 = standard_obs **实际产出**的键（qpos + 关节段目标 action + 夹爪 + 位姿），
                # 与 Edge 侧 ``observe()`` / ``capabilities.observation_keys`` 同口径
                FIELD_OBSERVATION_KEYS: [KEY_QPOS, KEY_ACTION, KEY_GRIPPER]
                + ([KEY_POSE, KEY_POSE_TARGET] if _robot_pose_dim(robot) > 0 else [])
                + [f"{CAMERA_PREFIX}{n}" for n in image_names],
                FIELD_CAPABILITIES: _robot_capabilities(robot),
                FIELD_ENDPOINT: endpoint,
                FIELD_SHM_NAME: robot.SHM_NAME,
            },
        }

    # ---------------------------------------------------------------- 健康检查
    @app.get(PATH_HEALTH)
    def health():
        """健康检查 {ok, detail, control_hz, measured_hz}：Edge adapter.health 消费。"""
        h = env.health()
        ok = bool(h.get("ready"))  # ready 已含两线程存活与无错误（env 单点定义）
        detail = "" if ok else (h.get("last_error") or "robot not ready")
        return {
            FIELD_OK: ok,
            FIELD_DETAIL: detail,
            FIELD_CONTROL_HZ: h.get("control_hz"),
            FIELD_MEASURED_HZ: h.get("measured_hz"),
        }

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
            env.robot_execute(req.action, req.action_space)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_EXECUTE)
    def execute(req: ActionRequest):
        """直接下发 raw 动作（与 rollout 一样只修改唯一目标，主循环限速跟踪）。

        ``action_space=pose`` 时由机器人侧解算一次（失败 → 422，不改既有目标）。
        """
        return _apply_action(req)

    @app.post(PATH_ROLLOUT)
    def rollout(req: ActionRequest):
        """推理闭环：**遥操作（人工接管）进行中 → 409**。

        遥操作期间从臂 target 由人工决定，推理下发必须让位（与 execute / reset 的「程控抢回」
        相反：被拒的 rollout 不会结束遥操作）；Edge 侧 adapter 据此跳过本拍，遥操作关闭后自动恢复。
        ``action_space`` 语义同 ``/v1/execute``。
        """
        _require_ready()
        try:
            env.robot_rollout(req.action, req.action_space)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except TakeoverActiveError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_TELEOP)
    def teleop(req: TeleopRequest):
        """设置遥操作：``enabled``（true=遥操作 / false=程控）+ ``mode``（映射模式）。

        ``mode``：``absolute``（缺省，主臂绝对位姿直连从臂 target）/ ``delta``（**人工接管**——
        以接管瞬间的主 / 从位姿为锚点，只把主臂增量叠加到从臂 target，从臂不会突变）；
        取值单点定义在 ``motrix_edge.adapter.http_contract``，非法取值返回 422。
        """
        _require_ready()
        if req.mode not in TELEOP_MODES:
            raise HTTPException(status_code=422, detail=f"unknown teleop mode {req.mode!r} (expect {TELEOP_MODES})")
        env.robot_set_teleop(req.enabled, req.mode)
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_SAFE_STOP)
    def safe_stop():
        """安全停止（软停：停发指令并保持位姿，不断电）：Edge adapter.safe_stop 消费。"""
        env.robot_safe_stop()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    # ---------------------------------------------------------------- 采集（episode）
    @app.post(PATH_CAPTURE_START)
    def capture_start():
        """开始一轮采集（episode 开始）：env 置 capturing=True，观测线程记录观测。"""
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
        """采集状态（运行位 / 元信息 / 数据目录）：Edge adapter.capture_status 消费。

        ``{running, meta, data_dir}``——``running`` 是 **env 真实采集位**（capture/start↔end
        之间为 True）；元信息为 ``meta`` 全集（采集员 / 任务名等是其键，不另设同义顶层
        字段）；数据目录随状态一并上报（原 ``/v1/data_status`` 已合入本端点）。
        """
        cs = env.capture_status()
        return {
            FIELD_RUNNING: bool(cs.get("running")),
            FIELD_META: cs.get("meta") or {},
            FIELD_DATA_DIR: cs.get("data_dir"),
        }

    # ---------------------------------------------------------------- 调试（非契约）
    @app.get("/observe")
    def observe_debug():
        """调试用：最新观测 qpos（关节角）+ 夹爪 + 位姿 + 关节段目标 + 相机 JPEG(base64)。

        Edge 侧实际经共享内存读观测（本端点为调试用）。
        """
        obs = publisher.last_obs
        if not obs:
            return {"ready": False, "data": None}
        out = {"ready": True, "qpos": np.asarray(obs[KEY_QPOS], dtype=np.float64).tolist(), "seq": obs.get("seq")}
        if obs.get(KEY_GRIPPER) is not None:
            out["gripper"] = np.asarray(obs[KEY_GRIPPER], dtype=np.float64).tolist()
        if obs.get(KEY_POSE) is not None:
            out["pose"] = np.asarray(obs[KEY_POSE], dtype=np.float64).tolist()
        out["action"] = np.asarray(obs[KEY_ACTION], dtype=np.float64).tolist()
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
