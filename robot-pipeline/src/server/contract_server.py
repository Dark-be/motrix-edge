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
    GET  /v1/cameras       相机静态元数据（尺寸 / 彩色内参 / 深度比例）
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
from config import get_log_dir
from env.base_env import TakeoverActiveError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from robot.calibration.store import load_frames
from utils.data_handler import debug_print, file_log_enabled
from utils.logging import uvicorn_log_config

from motrix_edge.adapter.base import CAMERA_PREFIX, DEPTH_PREFIX, KEY_ACTION, KEY_POSE, KEY_POSE_TARGET, KEY_QPOS
from motrix_edge.adapter.http_contract import (
    DEFAULT_TELEOP_MODE,
    FIELD_ACTION_DIM,
    FIELD_ACTION_DIMS,
    FIELD_ACTION_LAYOUTS,
    FIELD_ALIGNED_TO_COLOR,
    FIELD_ARM,
    FIELD_CAMERAS,
    FIELD_CAPABILITIES,
    FIELD_CONTROL_HZ,
    FIELD_DATA_DIR,
    FIELD_DEPTH,
    FIELD_DEPTH_SCALE,
    FIELD_DETAIL,
    FIELD_ENDPOINT,
    FIELD_FRAMES,
    FIELD_HEAD_SKIP,
    FIELD_HEIGHT,
    FIELD_INTRINSICS,
    FIELD_MEASURED_HZ,
    FIELD_META,
    FIELD_MOUNT,
    FIELD_NAME,
    FIELD_OBSERVATION_KEYS,
    FIELD_OK,
    FIELD_ROBOT,
    FIELD_ROBOT_MODEL_ID,
    FIELD_ROBOT_MODEL_VERSION,
    FIELD_RUNNING,
    FIELD_SHM_NAME,
    FIELD_STATE_ARMS,
    FIELD_STATUS,
    FIELD_SUPPORTED_ADAPTERS,
    FIELD_TYPE,
    FIELD_WIDTH,
    INTRINSICS_KEYS,
    PATH_CAMERAS,
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
    VALUE_MOUNT_FIXED,
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


def _robot_action_layouts(robot) -> list[dict]:
    """本机器人支持的**下发布局**（``name`` / ``per_arm`` / ``segments``）——供 Edge / 前端渲染与校验。"""
    action_layouts = getattr(robot, "action_layouts", None)
    if not callable(action_layouts):
        return []
    return [
        {
            "name": str(name),
            "per_arm": sum(int(dim) for _, dim in segments),
            "segments": [{"kind": str(kind), "dim": int(dim)} for kind, dim in segments],
        }
        for name, segments in action_layouts().items()
    ]


def _robot_state_arms(robot) -> list[str]:
    """物理臂序（``arms`` 缺省时的块序）；无臂概念的机器人返回空列表。"""
    state_arms = getattr(robot, "state_arms", None)
    return [str(arm) for arm in state_arms()] if callable(state_arms) else []


def _robot_state_dim(robot) -> int:
    """状态向量（``observations/qpos`` / ``action``）宽度：每臂「值 + 夹爪」× 臂数（双臂 14）。

    值段当前是关节角（``STATE_SPACE = joint``）；机器人整体切位姿时维度不变、语义由
    ``state_layout()`` 自描述（采集 JSON 的 ``state_space`` / ``state_dims``）。
    """
    return int(robot.state_vector_dim())


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


def _robot_depth_names(robot) -> list[str]:
    """机器人**生效**的深度相机名（顺序 = 深度流在共享内存里的顺序）。

    取机器人的 ``depth_camera_names()``（配置开关 ∩ 具备深度的相机）——无深度 / 未实现的
    替身机器人 → 空列表（不占深度区）。
    """
    names = getattr(robot, "depth_camera_names", None)
    if callable(names):
        return [str(name) for name in names()]
    return []


def _camera_payload(info: dict) -> dict:
    """相机元数据 → HTTP 契约形状（**键名单点在这里收口**）。

    机器人层只管给值（尺寸 / 内参 / 深度比例 / 安装方式），键名与嵌套形状在服务边界按契约常量拼装：
    多余 / 缺失的键不会漂到网络上（缺失内参 → 0.0；无深度 → ``depth: null``）。
    标定外参**不在这里逐相机重复**：整份产物另有一个载体（响应级 ``frames``，见 :func:`cameras`）。
    """
    intrinsics = info.get(FIELD_INTRINSICS) or {}
    depth = info.get(FIELD_DEPTH) or None
    return {
        FIELD_NAME: str(info.get(FIELD_NAME) or ""),
        FIELD_WIDTH: int(info.get(FIELD_WIDTH) or 0),
        FIELD_HEIGHT: int(info.get(FIELD_HEIGHT) or 0),
        FIELD_INTRINSICS: {key: float(intrinsics.get(key) or 0.0) for key in INTRINSICS_KEYS},
        FIELD_DEPTH: None
        if not depth
        else {
            FIELD_DEPTH_SCALE: float(depth.get(FIELD_DEPTH_SCALE) or 0.0),
            FIELD_ALIGNED_TO_COLOR: bool(depth.get(FIELD_ALIGNED_TO_COLOR, False)),
        },
        # 安装方式（装配事实）：fixed = 外参常数；wrist = 随臂动（外参只能是 T_flange_cam）
        FIELD_MOUNT: str(info.get(FIELD_MOUNT) or VALUE_MOUNT_FIXED),
        FIELD_ARM: None if info.get(FIELD_ARM) is None else str(info.get(FIELD_ARM)),
    }


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
    model_config = ConfigDict(extra="forbid")  # 旧键（如 action_space）显式 422，不静默按 joint 解释

    action: list[float] = Field(
        ...,
        description=("动作（按臂展开）：joint = 各臂 6 关节角；pose = 各臂 xyz+rpy；gripper = 各臂 1 夹爪"),
    )
    layout: str | None = Field(
        default=None,
        description=(
            "下发布局：每臂块内的段序列，段名用 '+' 连接、书写顺序即块内顺序，"
            "如 joint / gripper / joint+gripper / pose+gripper（缺省 joint）"
        ),
    )
    arms: list[str] | None = Field(
        default=None,
        description="作用域：缺省 = 全部臂（值按全部臂展开）；给出 = 只写这些臂，其余臂目标保持不动",
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

        写入 qpos（**状态向量**：每臂「值 + 夹爪」）+ action（**同维同布局**的目标向量）+
        pose（实测位姿）+ pose_target（目标位姿 = ``FK(关节段目标)``，增量动作的解算结果靠它对上位
        可见）+ raw RGB 图像 + 深度图（机器人开启深度时；已对齐到彩色图）；位姿（与目标位姿）
        仅在机器人提供（``POSE > 0``）时写入，深度仅在**生效的深度相机**有值时写入（否则不占该区）。
        """
        if obs is None or obs.get(KEY_QPOS) is None:
            return
        image_names = _robot_image_names(self.robot)
        depth_names = _robot_depth_names(self.robot)
        standard = {
            KEY_QPOS: obs[KEY_QPOS],
            KEY_ACTION: obs.get(KEY_ACTION) if obs.get(KEY_ACTION) is not None else obs[KEY_QPOS].copy(),
            "seq": getattr(self.robot, "seq", 0),
        }
        if obs.get(KEY_POSE) is not None:
            standard[KEY_POSE] = obs[KEY_POSE]  # 末端位姿（笛卡尔原语 / 预览的输入）
        if obs.get(KEY_POSE_TARGET) is not None:
            standard[KEY_POSE_TARGET] = obs[KEY_POSE_TARGET]  # 目标位姿（= FK(关节段目标)）
        for name in image_names:
            standard[f"{CAMERA_PREFIX}{name}"] = obs[f"{CAMERA_PREFIX}{name}"]
        depths = []
        for name in depth_names:
            depth = obs.get(f"{DEPTH_PREFIX}{name}")
            if depth is not None:
                standard[f"{DEPTH_PREFIX}{name}"] = depth
                depths.append(depth)
        self.last_obs = standard
        if self._writer is None:
            self._writer = self._create_writer()
        self._writer.write(
            qpos=standard[KEY_QPOS],
            action=standard[KEY_ACTION],
            pose=standard.get(KEY_POSE),
            pose_target=standard.get(KEY_POSE_TARGET),
            images=[standard[f"{CAMERA_PREFIX}{n}"] for n in image_names],
            # 深度区宽度固定：路数对不上（某相机本拍无深度）就不传，宁可本帧深度停在上一次
            depths=depths if len(depths) == len(depth_names) else None,
        )

    def _create_writer(self) -> ObsShmWriter:
        """创建共享内存写者（上次进程残留 → attach 后 unlink 重建）。

        区域宽度：qpos / action = **状态向量维数**（每臂「值 + 夹爪」，双臂 14）；
        pose / pose_target = 每臂 6 × 臂数（不提供位姿 → 0）；深度区 = **生效的深度相机**数
        （无深度 → 0，不占区）。
        """
        # 相机尺寸（假设各相机一致）；无相机机器人（IMAGES 为空）用占位尺寸，image_count=0 无图像数据
        image_size = next(iter(getattr(self.robot, "IMAGES", {}).values()), (640, 480))
        image_names = _robot_image_names(self.robot)
        depth_names = _robot_depth_names(self.robot)
        qpos_dim = _robot_state_dim(self.robot)

        def build() -> ObsShmWriter:
            return ObsShmWriter(
                name=self.robot.SHM_NAME,
                image_count=len(image_names),
                image_size=image_size,
                qpos_dim=qpos_dim,
                action_dim=qpos_dim,
                pose_dim=_robot_pose_dim(self.robot),
                pose_target_dim=_robot_pose_dim(self.robot),  # 目标位姿随实测位姿同生共死
                depth_count=len(depth_names),
                depth_size=image_size,  # 深度已对齐到彩色图 → 同尺寸
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
                PATH_CAMERAS,
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
                FIELD_ACTION_LAYOUTS: _robot_action_layouts(robot),
                FIELD_STATE_ARMS: _robot_state_arms(robot),
                # 观测键 = standard_obs **实际产出**的键（状态向量 qpos + 目标向量 action + 位姿），
                # 与 Edge 侧 ``observe()`` / ``capabilities.observation_keys`` 同口径
                FIELD_OBSERVATION_KEYS: [KEY_QPOS, KEY_ACTION]
                + ([KEY_POSE, KEY_POSE_TARGET] if _robot_pose_dim(robot) > 0 else [])
                + [f"{CAMERA_PREFIX}{n}" for n in image_names]
                + [f"{DEPTH_PREFIX}{n}" for n in _robot_depth_names(robot)],
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

    # ---------------------------------------------------------------- 相机元数据（静态）
    @app.get(PATH_CAMERAS)
    def cameras():
        """相机静态元数据 ``{cameras: [...], frames: ...}``：尺寸 / **彩色内参** / 深度比例 /
        安装方式 + **统一坐标系外参产物**。

        深度图已**对齐到彩色图**，故反投影只需这一套内参；无深度的相机 ``depth`` 为 ``null``。
        静态数据（不随帧变，进不了共享内存的逐帧区），Edge adapter 惰性查询一次并缓存，
        供 ``GET /v1/depth`` 把像素深度换算成米与 ``world`` 坐标。未就绪时 503（sensor 尚无可读
        内参——宁可不给，也不报 0 值内参）。

        ``frames`` = 标定产物 ``<根>/config/calibration/frames.json`` **原样**（单点形状在
        ``motrix_edge.geometry.FrameSet``）；未标定 / 产物非法 → ``null``（坐标功能关闭，其余照常）。
        """
        _require_ready()
        meta = getattr(robot, "camera_meta", None)
        infos = meta() if callable(meta) else []
        frames = load_frames()
        return {
            FIELD_CAMERAS: [_camera_payload(info) for info in infos],
            FIELD_FRAMES: None if frames is None else frames.to_payload(),
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
            env.robot_execute(req.action, layout=req.layout, arms=req.arms)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_EXECUTE)
    def execute(req: ActionRequest):
        """直接下发 raw 动作（与 rollout 一样只修改唯一目标，主循环限速跟踪）。

        ``layout="pose"`` 时由机器人侧解算一次（失败 → 422，不改既有目标）。
        """
        return _apply_action(req)

    @app.post(PATH_ROLLOUT)
    def rollout(req: ActionRequest):
        """推理闭环：**遥操作（人工接管）进行中 → 409**。

        遥操作期间从臂 target 由人工决定，推理下发必须让位（与 execute / reset 的「程控抢回」
        相反：被拒的 rollout 不会结束遥操作）；Edge 侧 adapter 据此跳过本拍，遥操作关闭后自动恢复。
        ``layout`` / ``arms`` 语义同 ``/v1/execute``。
        """
        _require_ready()
        try:
            env.robot_rollout(req.action, layout=req.layout, arms=req.arms)
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

        ``{running, meta, data_dir, head_skip}``——``running`` 是 **env 真实采集位**（capture/start↔end
        之间为 True）；元信息为 ``meta`` 全集（采集员 / 任务名等是其键，不另设同义顶层
        字段）；数据目录随状态一并上报（原 ``/v1/data_status`` 已合入本端点）；``head_skip``
        为帧头跳过进度（``{"skipped": n}``；``null`` = 未在跳过）——供操作员/脚本区分
        「正在等主臂移动」与「已开始记录」（阈值见 ``collector.skip_until_motion``）。
        """
        cs = env.capture_status()
        return {
            FIELD_RUNNING: bool(cs.get("running")),
            FIELD_META: cs.get("meta") or {},
            FIELD_DATA_DIR: cs.get("data_dir"),
            FIELD_HEAD_SKIP: cs.get("head_skip"),
        }

    # ---------------------------------------------------------------- 调试（非契约）
    @app.get("/observe")
    def observe_debug():
        """调试用：最新观测状态向量（每臂「值 + 夹爪」）+ 位姿 + 目标向量 + 相机 JPEG(base64)。

        Edge 侧实际经共享内存读观测（本端点为调试用）；逐维含义见 ``state_layout()``
        （采集 JSON 的 ``state_space`` / ``state_dims``）。
        """
        obs = publisher.last_obs
        if not obs:
            return {"ready": False, "data": None}
        out = {
            "ready": True,
            "qpos": np.asarray(obs[KEY_QPOS], dtype=np.float64).tolist(),
            "seq": obs.get("seq"),
            "state_layout": robot.state_layout(),
        }
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
        # 深度（生效的深度相机）：调试端点不回传整张深度图，只给形状与原始值范围
        for name in _robot_depth_names(robot):
            depth = obs.get(f"{DEPTH_PREFIX}{name}")
            if depth is None:
                out[f"depth/{name}"] = None
                continue
            values = np.asarray(depth, dtype=np.uint16)
            out[f"depth/{name}"] = {
                "shape": list(values.shape),
                "min_raw": int(values.min()),
                "max_raw": int(values.max()),
            }
        return out

    return app


def serve(app_obj, host: str | None = None, port: int | None = None) -> None:
    host = host or _DEFAULT_HOST
    port = int(port or _DEFAULT_PORT)
    # uvicorn 日志与 motrix_edge 侧同构：access 缺省静默（防长期运行刷屏）；开启
    # MOTRIX_EDGE_LOG_FILE 后只写 <根>/logs/uvicorn.log（<根> = $MOTRIX_ROBOT_PIPELINE_DIR）
    log_dir = get_log_dir()
    file_logging = file_log_enabled()
    if file_logging:  # 关闭时不建目录（不留空 logs/）
        os.makedirs(log_dir, exist_ok=True)
    debug_print(
        "SERVER",
        f"uvicorn: http://{host}:{port} | file_logging={'ON' if file_logging else 'OFF (MOTRIX_EDGE_LOG_FILE=0)'}",
        "INFO",
    )
    uvicorn.run(
        app_obj,
        host=host,
        port=port,
        log_level="info",
        log_config=uvicorn_log_config(str(log_dir / "uvicorn.log"), file_logging),
    )
