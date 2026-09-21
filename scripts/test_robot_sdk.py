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

"""Test Robot SDK —— 独立运行的模拟机器人 SDK 进程。

与 Edge 的耦合**仅限两个通信通道**（不依赖 motrix_edge 的业务 / 硬件逻辑）：

- **共享内存（shm_contract）**：复用 ``motrix_edge.adapter.shm_contract`` 的共享内存布局契约
  （``ObsShmWriter``），按 ``run_hz`` 持续产出模拟图像（raw RGB）+ 关节数据，与
  Edge adapter 的 ``ObsShmReader`` 读取逻辑**布局统一**。
- **HTTP 服务器**：用 FastAPI 接受 adapter 指令（discover / health / reset / execute /
  rollout / safe_stop / capture start·end·sync·status）。

本进程自包含模拟硬件逻辑（``SimRobotCore``：关节推进 / 相机帧生成），与 Edge 共享的
唯一部分是两份契约：**共享内存布局**（``motrix_edge.adapter.shm_contract``）与
**HTTP 指令**（``motrix_edge.adapter.http_contract``），不 import ``motrix_edge`` 其它模块。
**不实现数据采集 / 录制逻辑**——真实机器人 SDK 由自身自维护硬件与采集，本脚本只保留
与 Edge 交互会用到的部分（观测上行 + 指令下行 + 采集状态查询）。既可独立运行（进程
入口），也可被测试进程内复用（``create_sdk_app`` 组装 FastAPI app）。

独立运行::

    uv run python scripts/test_robot_sdk.py [--port 8090] [--shm test_robot_obs]
"""

from __future__ import annotations

import argparse
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException

# 唯一允许依赖的 Edge 部分：共享内存布局契约（shm）+ HTTP 指令契约（http_contract），
# 保证与 adapter 的读取 / 调用逻辑统一（见 motrix_edge/adapter/shm_contract.py、http_contract.py）
from motrix_edge.adapter.http_contract import (
    DEFAULT_ACTION_SPACE,
    FIELD_ACTION,
    FIELD_ACTION_DIM,
    FIELD_ACTION_SPACE,
    FIELD_CAPABILITIES,
    FIELD_CONTROLLERS,
    FIELD_DATA_DIR,
    FIELD_DETAIL,
    FIELD_ENDPOINT,
    FIELD_META,
    FIELD_NAME,
    FIELD_OBSERVATION_KEYS,
    FIELD_OK,
    FIELD_ROBOT,
    FIELD_ROBOT_MODEL_ID,
    FIELD_ROBOT_MODEL_VERSION,
    FIELD_RUNNING,
    FIELD_SENSORS,
    FIELD_SHM_NAME,
    FIELD_STATUS,
    FIELD_SUPPORTED_ADAPTERS,
    FIELD_TELEOP_ENABLED,
    FIELD_TELEOP_MODE,
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
    VALUE_ACTION_SPACE_CARTESIAN_POSE,
    VALUE_STATUS_ACCEPTED,
)
from motrix_edge.adapter.shm_contract import ObsShmWriter

# 观测键（standard_obs 契约；与 adapter/base.py 的 KEY_QPOS / KEY_ACTION 一致）
_KEY_QPOS = "observations/qpos"
_KEY_ACTION = "action"
_KEY_POSE = "observations/pose"

# 随机游走参数（模拟遥操作输入）：每帧目标有界随机游走
_RANDOM_STEP = 0.05
_RANDOM_RANGE = (0.0, 2.0)

# ---- 简易运动学（模拟笛卡尔控制）---------------------------------------------
# 模拟臂没有真实运动学，用**线性任务空间映射**代替：pose = M @ q_joints（M 可逆）。
# FK / IK 互为逆，足以验证「笛卡尔指令 → IK → 关节限速执行」整条链路与契约。
_KIN_PER_ARM_DIM = 7  # 每臂：6 关节 + 夹爪
_KIN_JOINTS = 6
_POSE_DIM_PER_ARM = 6  # 每臂位姿：xyz + rpy


def _kin_matrix() -> np.ndarray:
    """模拟臂的任务空间映射矩阵（对角为主 + 小量耦合，保证可逆且非平凡）。"""
    matrix = np.diag([0.30, 0.30, 0.30, 1.00, 1.00, 1.00])
    matrix[0, 1] = 0.05
    matrix[1, 2] = -0.04
    matrix[2, 0] = 0.03
    matrix[3, 4] = 0.10
    matrix[4, 5] = -0.08
    matrix[5, 3] = 0.06
    return matrix


# 默认 SDK 监听 / 共享内存名（TestRobotAdapter 中间件类常量与此对齐）
DEFAULT_SDK_HOST = "127.0.0.1"
DEFAULT_SDK_PORT = 8090
DEFAULT_SHM_NAME = "test_robot_obs"
DEFAULT_RUN_HZ = 30


class TeleopActiveError(RuntimeError):
    """遥操作（人工接管）中：推理下发（rollout）被拒 → HTTP 409（与 robot-pipeline 契约一致）。"""


class SimRobotCore:
    """模拟机器人硬件核心（本进程自包含，无 IO）。

    - ``step()``：推进一帧运动（目标随机游走，qpos 限速靠近目标）。
    - ``frame()``：当前观测帧 ``{KEY_QPOS, KEY_ACTION, "images": [raw RGB, ...]}``。

    **不实现数据采集 / 录制逻辑**：真实机器人 SDK 由自身自维护硬件与采集；本脚本只保留
    与 Edge 交互会用到的部分（观测上行 + 指令下行 + 采集状态查询）。
    """

    # 行为参数（可经 __init__ 覆盖）
    NAME = "Test Robot"  # 机器人名称（discover 自描述）
    ROBOT_MODEL_ID = "test-robot"  # 机器人型号（discover 自描述）
    ROBOT_MODEL_VERSION = "0.0.0"
    SUPPORTED_ADAPTERS = ["test_robot"]  # 本进程支持被哪些 adapter 类型操作（discover 自描述）
    ADAPTER_TYPE = "test_robot"  # 机器人进程自描述：adapter 类型（entry point 名）
    CONTROLLERS = ["left_arm", "right_arm"]  # 机器人进程自描述：控制器列表
    SENSORS = ["encoder_0", "encoder_1"]  # 机器人进程自描述：传感器列表
    CAPABILITIES = {"capture": True, "execute": True, "streaming": True}  # 支持的角色
    ACTION_DIM = 14
    IMAGES = ["cam_head", "cam_left_wrist", "cam_right_wrist"]  # 相机布局
    POSE_DIM_PER_ARM = _POSE_DIM_PER_ARM  # 每臂位姿维数（xyz + rpy；位姿由简易运动学合成）
    STEP_RAD = 0.05  # 每帧限速步长
    DATA_DIR = "data/test_task"  # 数据目录（供 capture status 上报；采集由真实 SDK 自维护）
    INIT_QPOS = None  # 初始位姿（None → 全零 home）
    CAMERA_SIZE = (640, 480)  # (width, height)：观测图像尺寸（raw RGB）

    def __init__(
        self,
        action_dim: int = ACTION_DIM,
        images: list[str] | None = None,
        step_rad: float = STEP_RAD,
        data_dir: str | None = DATA_DIR,
        init_qpos=None,
        camera_size: tuple[int, int] = CAMERA_SIZE,
        random_walk: bool = True,
    ):
        self.action_dim = action_dim
        self.images = list(images or self.IMAGES)
        self.step_rad = step_rad
        self.camera_size = tuple(camera_size)
        self.init_qpos = init_qpos
        # 随机游走（模拟遥操作输入）：False → 目标只由指令决定（笛卡尔闭环 / 收敛验证用）
        self.random_walk = bool(random_walk)

        self._qpos = np.zeros(action_dim, dtype=np.float64)
        self._target: np.ndarray | None = None  # reset 设 home；随机游走 / rollout 设模型 action
        self._rng = np.random.default_rng(0)
        self._data_dir: str | None = data_dir  # 数据目录（capture status 上报）
        # 简易运动学：位姿 = M @ 关节段（每臂 6 关节）；IK = 逆映射
        self._kin = _kin_matrix()
        self._kin_inv = np.linalg.inv(self._kin)

        # 测试断言用记录（供 SDK 状态 / 服务测试）
        self.executed: list = []
        self.rollout_calls = 0
        self.rollout_spaces: list[str] = []  # 最近一轮 rollout 的动作空间（断言用）
        self.safe_stop_calls = 0
        self.reset_calls = 0
        self.teleop_enabled = False  # 遥操作开关（teleop 指令设置）
        self.teleop_mode: str | None = None  # 遥操作模式（absolute | delta；None = 未指定）
        self.capturing = False  # 采集回合进行中（capture episode start / end）
        self.capture_meta: dict = {}  # capture sync 同步的元信息（保存数据时附加）
        self.episode_count = 0  # 已开始采集的回合数（测试断言用）
        self._run_time = 0.0  # fake image 相位推进时间

    # ---- 运动推进 -------------------------------------------------------------
    def step(self) -> None:
        """推进一帧运动：目标随机游走（模拟遥操作；``random_walk=False`` 时不动目标），qpos 限速靠近目标。"""
        if self.random_walk:
            self._refresh_target()
        if self._target is not None:
            self._qpos = self._step_toward(self._qpos, self._target, self.step_rad)

    def reset(self) -> None:
        """程序复位到 home（非阻塞）：设 home 目标，由后续 step() 推进。"""
        self.reset_calls += 1
        init = self.INIT_QPOS if self.INIT_QPOS is not None else self.init_qpos
        self._target = np.asarray(init, dtype=np.float64) if init is not None else np.zeros(self.action_dim)
        self._qpos = self._target.copy()  # 开始即贴近目标，避免 start 时跳变

    def execute(self, action: list[float] | np.ndarray) -> None:
        """直接下发一维动作指令（raw）：记录调用，供测试断言。"""
        self.executed.append(action)

    def set_teleop(self, enabled: bool, mode: str | None = None) -> None:
        """设置遥操作（true=遥操作 / false=程控；mode=absolute|delta，开启时有意义）。"""
        self.teleop_enabled = bool(enabled)
        self.teleop_mode = mode if self.teleop_enabled else None

    def start_capture(self) -> None:
        """开始一轮采集（episode 开始）：置 capturing 标志。"""
        self.capturing = True
        self.episode_count += 1

    def end_capture(self) -> None:
        """结束一轮采集（episode 结束）：清 capturing 标志。"""
        self.capturing = False

    def set_capture_meta(self, meta: dict) -> None:
        """同步采集元信息（capture sync）：真实 SDK 在保存一轮数据时附加到描述文件。"""
        self.capture_meta = dict(meta or {})

    def safe_stop(self) -> None:
        """安全停止（幂等、失败安全）：清空目标。"""
        self.safe_stop_calls += 1
        self._target = None

    def rollout(self, action, action_space: str | None = None) -> None:
        """推理闭环：把模型 action 设为限速目标（维度校验）。

        ``action_space`` = ``joint``（缺省）或 ``cartesian_pose``：后者先经简易 IK 转成关节
        目标（模拟真实机器人侧 IK 分支）。遥操作（人工接管）中拒绝下发（与真实 robot-pipeline
        一致，见 /v1/rollout 契约）。
        """
        if self.teleop_enabled:  # 遥操作中：推理让位
            raise TeleopActiveError("teleop (human takeover) active: rollout refused")
        target = np.asarray(action, dtype=np.float64)
        if target.shape[0] != self.action_dim:
            raise ValueError(f"rollout action dim {target.shape[0]} != action_dim {self.action_dim}")
        self.rollout_calls += 1
        self.rollout_spaces.append(str(action_space or DEFAULT_ACTION_SPACE))
        if str(action_space or DEFAULT_ACTION_SPACE) == VALUE_ACTION_SPACE_CARTESIAN_POSE:
            target = self._ik_action(target)
        self._target = target

    # ---- 简易运动学（FK / IK）--------------------------------------------------
    def fk_pose(self, qpos: np.ndarray | None = None) -> np.ndarray:
        """正向运动学：qpos → 末端位姿（每臂 6 维 xyz + rpy，物理顺序同 ACTION_DIM 分段）。"""
        flat = np.asarray(self._qpos if qpos is None else qpos, dtype=np.float64)
        parts = []
        for index in range(self._arm_count()):
            start = index * _KIN_PER_ARM_DIM
            parts.append(self._kin @ flat[start : start + _KIN_JOINTS])
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float64)

    @property
    def pose_dim(self) -> int:
        """位姿观测维度（每臂 6 维 × 臂数）。"""
        return _POSE_DIM_PER_ARM * self._arm_count()

    def _arm_count(self) -> int:
        """臂数（按动作维度 / 每臂 7 维推导；无臂概念时为 0）。"""
        if self.action_dim <= 0 or self.action_dim % _KIN_PER_ARM_DIM:
            return 0
        return self.action_dim // _KIN_PER_ARM_DIM

    def _ik_action(self, pose_action: np.ndarray) -> np.ndarray:
        """笛卡尔动作 → 关节动作（模拟 IK）：每臂 xyz+rpy 反解 6 关节，夹爪透传。"""
        target = np.zeros(self.action_dim, dtype=np.float64)
        joints_per_arm = _POSE_DIM_PER_ARM + 1
        if pose_action.shape[0] != joints_per_arm * self._arm_count():
            raise ValueError(f"cartesian action dim {pose_action.shape[0]} != {joints_per_arm * self._arm_count()}")
        for index in range(self._arm_count()):
            src = pose_action[index * joints_per_arm : index * joints_per_arm + joints_per_arm]
            dst = index * _KIN_PER_ARM_DIM
            target[dst : dst + _KIN_JOINTS] = self._kin_inv @ src[:6]
            target[dst + _KIN_JOINTS] = float(np.clip(src[6], 0.0, 1.0))
        return target

    # ---- 观测帧 ---------------------------------------------------------------
    def frame(self) -> dict:
        """当前观测帧：qpos / action / pose（简易 FK） + 相机帧（raw RGB ndarray，CAMERA_SIZE）。"""
        self._run_time += 1 / 30.0  # 图像相位推进（随 SDK 服务步进）
        return {
            _KEY_QPOS: self._qpos.astype(np.float32),
            _KEY_ACTION: self._qpos.astype(np.float32),  # 测试：action = 当前执行位置
            _KEY_POSE: self.fk_pose().astype(np.float32),
            "images": [self.fake_image(self._run_time) for _ in self.images],
        }

    def fake_image(self, t: float) -> np.ndarray:
        """生成一帧模拟相机图（raw RGB，CAMERA_SIZE）：彩色渐变随 t 变化。"""
        w, h = self.camera_size

        # 1. 网格坐标：xx 沿水平（列）、yy 沿垂直（行），形状 (H, W)
        xx, yy = np.meshgrid(np.arange(w), np.arange(h))

        # 2. 各通道沿不同方向**独立渐变**，形成彩色渐变图
        freq = 2 * np.pi * 0.005
        r = np.sin(freq * xx + t * 2)  # 水平渐变
        g = np.sin(freq * yy + t * 2 + 2 * np.pi / 3)  # 垂直渐变
        b = np.sin(freq * (xx + yy) + t * 2 + 4 * np.pi / 3)  # 对角渐变

        # 3. 拼接为 (H, W, 3)，映射到 [50, 200] 并转 uint8
        stack = np.stack([r, g, b], axis=2)
        normalized = (stack + 1) / 2  # [0, 1]
        rgb = (50 + normalized * 150).astype(np.uint8)
        return rgb

    # ---- 采集状态（供 capture status 上报；采集由真实 SDK 自维护，本脚本不实现录制）----
    @property
    def data_dir(self) -> Path | None:
        """数据目录（capture status 上报）。"""
        return Path(self._data_dir) if self._data_dir else None

    # ---- 内部 ---------------------------------------------------------------
    def _refresh_target(self) -> None:
        """刷新目标为有界随机游走（模拟遥操作输入）。"""
        if self._target is None:
            self._target = self._qpos.copy()
        delta = self._rng.uniform(-_RANDOM_STEP, _RANDOM_STEP, size=self.action_dim)
        self._target = np.clip(self._target + delta, *_RANDOM_RANGE)

    @staticmethod
    def _step_toward(current: np.ndarray, target: np.ndarray, max_step: float) -> np.ndarray:
        """限速插值：每步最多向目标靠近 max_step，防止关节数据跳变。"""
        current = np.asarray(current, dtype=np.float64)
        target = np.asarray(target, dtype=np.float64)
        delta = target - current
        step = np.clip(delta, -max_step, max_step)
        return np.where(np.abs(delta) <= max_step, target, current + step)


def create_sdk_app(
    core: SimRobotCore,
    shm_name: str,
    run_hz: int = DEFAULT_RUN_HZ,
    endpoint: str = f"http://{DEFAULT_SDK_HOST}:{DEFAULT_SDK_PORT}",
) -> FastAPI:
    """创建 SDK HTTP 应用：启动硬件线程发布共享内存观测，暴露指令端点。

    - lifespan 启动：创建共享内存（``ObsShmWriter``）、启动硬件线程按 ``run_hz``
      推进 ``core.step()`` 并写入最新观测帧（qpos + raw RGB 图像）。
    - lifespan 关闭：停止硬件线程、释放并删除共享内存（幂等，可反复起停）。
    - 端点操作直接转发到 ``core``（采集由真实 SDK 自维护，本脚本不实现录制）。
    - ``endpoint``：本进程 HTTP 地址（discover 自描述返回给 Edge，供 adapter 连指令）。
    """
    writer = ObsShmWriter(
        name=shm_name,
        image_count=len(core.images),
        image_size=core.camera_size,
        qpos_dim=core.action_dim,
        action_dim=core.action_dim,  # 目标动作区（SHM 布局 v2：qpos + action + images）
        pose_dim=core.pose_dim,  # 末端位姿区（> 0 → 布局 v3；笛卡尔策略的观测输入）
    )

    class _State:
        hardware_running = False
        thread: threading.Thread | None = None

    state = _State()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        state.hardware_running = True
        writer.set_flags(running=True)

        def _hardware_loop() -> None:
            while state.hardware_running:
                core.step()  # 推进一帧运动（随机游走 / 限速靠近目标）
                frame = core.frame()
                writer.write(frame[_KEY_QPOS], frame[_KEY_ACTION], frame["images"], pose=frame[_KEY_POSE])
                time.sleep(1 / run_hz)

        state.thread = threading.Thread(target=_hardware_loop, name="sdk-hardware", daemon=True)
        state.thread.start()
        try:
            yield
        finally:
            state.hardware_running = False
            if state.thread is not None:
                state.thread.join(timeout=1.0)
            writer.set_flags(running=False)
            writer.close()
            writer.unlink()

    app = FastAPI(title="Test Robot SDK", lifespan=_lifespan)

    @app.post(PATH_DISCOVER)
    def discover():
        """机器人进程自描述探活（不初始化）：声明身份 + 能力 + 连接参数。"""
        return {
            FIELD_STATUS: VALUE_STATUS_ACCEPTED,
            FIELD_ROBOT: {
                FIELD_NAME: core.NAME,
                FIELD_TYPE: core.ADAPTER_TYPE,
                FIELD_ROBOT_MODEL_ID: core.ROBOT_MODEL_ID,
                FIELD_ROBOT_MODEL_VERSION: core.ROBOT_MODEL_VERSION,
                FIELD_ACTION_DIM: core.action_dim,
                FIELD_OBSERVATION_KEYS: [_KEY_QPOS, _KEY_POSE] + [f"observations/images/{img}" for img in core.images],
                FIELD_CONTROLLERS: list(core.CONTROLLERS),
                FIELD_SENSORS: list(core.SENSORS),
                FIELD_CAPABILITIES: dict(core.CAPABILITIES),
                FIELD_ENDPOINT: endpoint,
                FIELD_SHM_NAME: shm_name,
                FIELD_RUNNING: True,
                FIELD_SUPPORTED_ADAPTERS: list(core.SUPPORTED_ADAPTERS),
            },
        }

    @app.get(PATH_HEALTH)
    def health():
        return {FIELD_OK: True, FIELD_DETAIL: ""}

    @app.post(PATH_RESET)
    def reset():
        core.reset()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_EXECUTE)
    def execute(body: dict):
        core.execute(body[FIELD_ACTION])
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_TELEOP)
    def teleop(body: dict):
        """设置遥操作（true=遥操作 / false=程控；可选 mode=absolute|delta 人工接管）。"""
        core.set_teleop(bool(body.get(FIELD_TELEOP_ENABLED, False)), body.get(FIELD_TELEOP_MODE))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_ROLLOUT)
    def rollout(body: dict):
        """推理闭环：遥操作（人工接管）中 → 409（与真实 robot-pipeline 契约一致）。

        ``action_space``（缺省 ``joint``）：``cartesian_pose`` 时经模拟 IK 转关节目标。
        """
        try:
            core.rollout(
                np.asarray(body[FIELD_ACTION], dtype=np.float64),
                body.get(FIELD_ACTION_SPACE),
            )
        except (TeleopActiveError, ValueError) as exc:
            status = 409 if isinstance(exc, TeleopActiveError) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_SAFE_STOP)
    def safe_stop():
        core.safe_stop()
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_CAPTURE_START)
    def capture_start():
        """开始一轮采集（episode 开始）：置 capturing 标志（共享内存状态位同步）。"""
        core.start_capture()
        writer.set_flags(capturing=True)
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_CAPTURE_END)
    def capture_end():
        """结束一轮采集（episode 结束）：清 capturing 标志（共享内存状态位同步）。"""
        core.end_capture()
        writer.set_flags(capturing=False)
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.get(PATH_CAPTURE_STATUS)
    def capture_status():
        """采集状态（运行位 / 元信息 / 数据目录）：Edge adapter.capture_status 消费。

        ``running`` 是**本进程真实采集位**（capture/start ↔ end 之间为 True）；元信息为
        ``capture sync`` 同步的全集；数据目录为采集产物落地目录（进程自维护）。
        """
        return {
            FIELD_RUNNING: bool(core.capturing),
            FIELD_META: core.capture_meta,
            FIELD_DATA_DIR: str(core.data_dir) if core.data_dir else None,
        }

    @app.post(PATH_CAPTURE_SYNC)
    def capture_sync(body: dict):
        """同步采集元信息（Edge adapter.sync_capture_meta 调用；进程保存一轮数据时附加）。"""
        core.set_capture_meta(body.get(FIELD_META) or {})
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    return app


def run_sdk_server(
    host: str = DEFAULT_SDK_HOST,
    port: int = DEFAULT_SDK_PORT,
    shm_name: str = DEFAULT_SHM_NAME,
    data_dir: str | None = None,
    run_hz: int = DEFAULT_RUN_HZ,
    log_level: str = "info",
    random_walk: bool = True,
) -> None:
    """阻塞运行 SDK 服务器（进程入口）：组装核心 + HTTP 服务并启动 uvicorn。

    ``random_walk=False``：关闭模拟遥操作随机输入（目标只由指令决定）——笛卡尔闭环 /
    收敛联调时用，否则随机游走会持续抢走 target，机械臂不会走向指令目标。
    """
    core = SimRobotCore(data_dir=data_dir or SimRobotCore.DATA_DIR, random_walk=random_walk)
    app = create_sdk_app(core, shm_name=shm_name, run_hz=run_hz)
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    server.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a simulated robot SDK process (HTTP + shared memory).")
    parser.add_argument("--host", default=DEFAULT_SDK_HOST, help="HTTP server bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_SDK_PORT, help="HTTP server bind port")
    parser.add_argument("--shm", default=DEFAULT_SHM_NAME, help="Shared memory name for the observation channel")
    parser.add_argument("--data-dir", default=None, help="Capture data directory (default: SimRobotCore.DATA_DIR)")
    parser.add_argument("--run-hz", type=int, default=DEFAULT_RUN_HZ, help="Hardware observation rate (Hz)")
    parser.add_argument(
        "--no-random-walk",
        dest="random_walk",
        action="store_false",
        help="Disable the simulated teleop random walk (target follows commands only)",
    )
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args()

    run_sdk_server(
        host=args.host,
        port=args.port,
        shm_name=args.shm,
        data_dir=args.data_dir,
        run_hz=args.run_hz,
        log_level=args.log_level,
        random_walk=args.random_walk,
    )


if __name__ == "__main__":
    main()
