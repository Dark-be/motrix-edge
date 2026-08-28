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
  rollout / safe_stop / data_status）。

本进程自包含模拟硬件逻辑（``SimRobotCore``：关节推进 / 相机帧生成），与 Edge 共享的
唯一部分是两份契约：**共享内存布局**（``motrix_edge.adapter.shm_contract``）与
**HTTP 指令**（``motrix_edge.adapter.http_contract``），不 import ``motrix_edge`` 其它模块。
**不实现数据采集 / 录制逻辑**——真实机器人 SDK 由自身自维护硬件与采集，本脚本只保留
与 Edge 交互会用到的部分（观测上行 + 指令下行 + 数据状态查询）。既可独立运行（进程
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
from fastapi import FastAPI

# 唯一允许依赖的 Edge 部分：共享内存布局契约（shm）+ HTTP 指令契约（http_contract），
# 保证与 adapter 的读取 / 调用逻辑统一（见 motrix_edge/adapter/shm_contract.py、http_contract.py）
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_ACTION_DIM,
    FIELD_CAPABILITIES,
    FIELD_CONTROLLERS,
    FIELD_DATA_DIR,
    FIELD_DATA_FILES,
    FIELD_DETAIL,
    FIELD_ENDPOINT,
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
    FIELD_TYPE,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
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

# 观测键（standard_obs 契约；与 adapter/base.py 的 KEY_QPOS / KEY_ACTION 一致）
_KEY_QPOS = "observations/qpos"
_KEY_ACTION = "action"

# 随机游走参数（模拟遥操作输入）：每帧目标有界随机游走
_RANDOM_STEP = 0.05
_RANDOM_RANGE = (0.0, 2.0)

# 默认 SDK 监听 / 共享内存名（TestRobotAdapter 中间件类常量与此对齐）
DEFAULT_SDK_HOST = "127.0.0.1"
DEFAULT_SDK_PORT = 8090
DEFAULT_SHM_NAME = "test_robot_obs"
DEFAULT_RUN_HZ = 30


class SimRobotCore:
    """模拟机器人硬件核心（本进程自包含，无 IO）。

    - ``step()``：推进一帧运动（目标随机游走，qpos 限速靠近目标）。
    - ``frame()``：当前观测帧 ``{KEY_QPOS, KEY_ACTION, "images": [raw RGB, ...]}``。

    **不实现数据采集 / 录制逻辑**：真实机器人 SDK 由自身自维护硬件与采集；本脚本只保留
    与 Edge 交互会用到的部分（观测上行 + 指令下行 + 数据状态查询）。
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
    STEP_RAD = 0.05  # 每帧限速步长
    DATA_DIR = "data/test_task"  # 数据目录（供 data_status 上报；采集由真实 SDK 自维护）
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
    ):
        self.action_dim = action_dim
        self.images = list(images or self.IMAGES)
        self.step_rad = step_rad
        self.camera_size = tuple(camera_size)
        self.init_qpos = init_qpos

        self._qpos = np.zeros(action_dim, dtype=np.float64)
        self._target: np.ndarray | None = None  # reset 设 home；随机游走 / rollout 设模型 action
        self._rng = np.random.default_rng(0)
        self._data_dir: str | None = data_dir  # 数据目录（data_status 上报）

        # 测试断言用记录（供 SDK 状态 / 服务测试）
        self.executed: list = []
        self.rollout_calls = 0
        self.safe_stop_calls = 0
        self.reset_calls = 0
        self.teleop_enabled = False  # 遥操作开关（teleop 指令设置）
        self.capturing = False  # 采集回合进行中（capture episode start / end）
        self.episode_count = 0  # 已开始采集的回合数（测试断言用）
        self._run_time = 0.0  # fake image 相位推进时间

    # ---- 运动推进 -------------------------------------------------------------
    def step(self) -> None:
        """推进一帧运动：目标随机游走（模拟遥操作），qpos 限速靠近目标。"""
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

    def set_teleop(self, enabled: bool) -> None:
        """设置遥操作开关（true=遥操作 / false=程控）。"""
        self.teleop_enabled = bool(enabled)

    def start_capture(self) -> None:
        """开始一轮采集（episode 开始）：置 capturing 标志。"""
        self.capturing = True
        self.episode_count += 1

    def end_capture(self) -> None:
        """结束一轮采集（episode 结束）：清 capturing 标志。"""
        self.capturing = False

    def safe_stop(self) -> None:
        """安全停止（幂等、失败安全）：清空目标。"""
        self.safe_stop_calls += 1
        self._target = None

    def rollout(self, action) -> None:
        """推理闭环：把模型 action 设为限速目标（维度校验）。"""
        target = np.asarray(action, dtype=np.float64)
        if target.shape[0] != self.action_dim:
            raise ValueError(f"rollout action dim {target.shape[0]} != action_dim {self.action_dim}")
        self.rollout_calls += 1
        self._target = target

    # ---- 观测帧 ---------------------------------------------------------------
    def frame(self) -> dict:
        """当前观测帧：qpos / action + 相机帧（raw RGB ndarray，CAMERA_SIZE）。"""
        self._run_time += 1 / 30.0  # 图像相位推进（随 SDK 服务步进）
        return {
            _KEY_QPOS: self._qpos.astype(np.float32),
            _KEY_ACTION: self._qpos.astype(np.float32),  # 测试：action = 当前执行位置
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

    # ---- 数据状态（供 data_status 上报；采集由真实 SDK 自维护，本脚本不实现录制）----
    @property
    def data_dir(self) -> Path | None:
        """数据目录（data_status 上报）。"""
        return Path(self._data_dir) if self._data_dir else None

    @property
    def data_files(self) -> list[str]:
        """数据目录下已有的数据文件路径（data_status 上报；真实 SDK 自行填充采集产物）。"""
        data_dir = self.data_dir
        if data_dir is None or not data_dir.exists():
            return []
        return [str(p) for p in sorted(data_dir.iterdir()) if p.is_file()]

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
        name=shm_name, image_count=len(core.images), image_size=core.camera_size, qpos_dim=core.action_dim
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
                writer.write(frame[_KEY_QPOS], frame["images"])
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
                FIELD_OBSERVATION_KEYS: [_KEY_QPOS] + [f"observations/images/{img}" for img in core.images],
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
        """设置遥操作开关（true=遥操作 / false=程控）。"""
        core.set_teleop(bool(body.get(FIELD_TELEOP_ENABLED, False)))
        return {FIELD_STATUS: VALUE_STATUS_ACCEPTED}

    @app.post(PATH_ROLLOUT)
    def rollout(body: dict):
        core.rollout(np.asarray(body[FIELD_ACTION], dtype=np.float64))
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

    @app.get(PATH_DATA_STATUS)
    def data_status():
        """采集数据状态：数据目录 + 本次采集得到的数据列表（数据由进程自维护）。"""
        return {
            FIELD_DATA_DIR: str(core.data_dir) if core.data_dir else None,
            FIELD_DATA_FILES: core.data_files,
            FIELD_RUNNING: True,
        }

    return app


def run_sdk_server(
    host: str = DEFAULT_SDK_HOST,
    port: int = DEFAULT_SDK_PORT,
    shm_name: str = DEFAULT_SHM_NAME,
    data_dir: str | None = None,
    run_hz: int = DEFAULT_RUN_HZ,
    log_level: str = "info",
) -> None:
    """阻塞运行 SDK 服务器（进程入口）：组装核心 + HTTP 服务并启动 uvicorn。"""
    core = SimRobotCore(data_dir=data_dir)
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
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args()

    run_sdk_server(
        host=args.host,
        port=args.port,
        shm_name=args.shm,
        data_dir=args.data_dir,
        run_hz=args.run_hz,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
