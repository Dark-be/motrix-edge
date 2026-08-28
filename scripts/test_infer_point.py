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

"""Test Infer Point —— 独立运行的模拟 openpi 推理服务端（虚拟推理端点）。

**不进行真实推理**：作为联调用的虚拟策略服务端，验证「edge → 推理端」的传输契约
（见 ``motrix_edge/policy/contract.py`` 与 ``msgpack_numpy.py``）。运行在指定 ip / 端口：

- 连接建立后**先下发首条 metadata**（msgpack，含 ``action_horizon``）；
- 每个请求接收 msgpack 观测 ``{"observations/qpos": ndarray, "observations/images/*": ...}``，
  返回一段**有界随机游走**的 action chunk ``{"action": ndarray}``（``[horizon, dim]``，块间连续），
  供 ``ActionChunkBroker`` 逐帧切片验证。

与 Edge 的耦合**仅限 wire 契约**（``motrix_edge.policy.contract`` / ``msgpack_numpy``），
不 import ``motrix_edge`` 的业务 / 硬件逻辑。既可独立运行（进程入口），也可被测试进程内
复用（``create_server`` 组装 WebSocket 服务端）。

独立运行::

    uv run python scripts/test_infer_point.py [--host 0.0.0.0] [--port 8765] \
        [--action-dim 14] [--action-horizon 16] [--step 0.05] [--range -1 1] [--seed 0]
"""

from __future__ import annotations

import argparse

import numpy as np
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

# 唯一允许依赖的 Edge 部分：wire 契约（消息 key 常量）+ msgpack-numpy 序列化，
# 保证与 policy 客户端的收发逻辑统一（见 motrix_edge/policy/contract.py、msgpack_numpy.py）。
from motrix_edge.policy.contract import KEY_ACTION, KEY_OBS_QPOS
from motrix_edge.policy.msgpack_numpy import packb, unpackb

# 默认参数（SimInferCore 类常量与此对齐）
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_ACTION_DIM = 14
DEFAULT_ACTION_HORIZON = 16
DEFAULT_STEP = 0.05  # 随机游走单步幅（有界）
DEFAULT_RANGE = (-1.0, 1.0)  # 动作值域（有界）
DEFAULT_SEED = 0  # 随机种子（None = 随机）


class SimInferCore:
    """模拟推理核心（本进程自包含，无真实模型）。

    ``chunk()``：返回下一个动作块 ``[action_horizon, action_dim]``，为**有界随机游走**
    的连续轨迹（每块首步衔接上一块末步，使 ``ActionChunkBroker`` 跨块取动作时无跳变）。
    单步幅 ``step``、值域 ``range`` 可配置；``reset()`` 复位游走起点。
    """

    MODEL = "test-infer-point"  # metadata 的 model 标识（虚拟模型名）

    def __init__(
        self,
        action_dim: int = DEFAULT_ACTION_DIM,
        action_horizon: int = DEFAULT_ACTION_HORIZON,
        step: float = DEFAULT_STEP,
        action_range: tuple[float, float] = DEFAULT_RANGE,
        seed: int | None = DEFAULT_SEED,
    ):
        self.action_dim = action_dim
        self.action_horizon = action_horizon
        self.step = step
        self.action_range = tuple(action_range)
        self.seed = seed
        self._rng = np.random.default_rng(seed)
        self._current = np.zeros(action_dim, dtype=np.float64)  # 上一块末步（游走起点）

    def chunk(self) -> np.ndarray:
        """下一个动作块 ``[action_horizon, action_dim]``（连续随机游走轨迹）。"""
        out = np.zeros((self.action_horizon, self.action_dim), dtype=np.float64)
        pos = self._current
        for i in range(self.action_horizon):
            pos = self._walk(pos)
            out[i] = pos
        self._current = pos  # 块间衔接：下一块首步从本块末步继续游走
        return out

    def chunk_for(self, obs) -> np.ndarray:
        """按观测生成下一个动作块；观测携带 ``observations/qpos`` 时按其维数适配 ``action_dim``。"""
        qpos = obs.get(KEY_OBS_QPOS) if isinstance(obs, dict) else None
        if qpos is not None:
            qpos = np.asarray(qpos)
            if qpos.ndim == 1 and qpos.shape[0] != self.action_dim:
                self.action_dim = qpos.shape[0]
                self._current = np.zeros(self.action_dim, dtype=np.float64)  # 维数变化 → 复位游走
        return self.chunk()

    def reset(self) -> None:
        """复位游走起点（全零）。"""
        self._current = np.zeros(self.action_dim, dtype=np.float64)

    # ---- 内部 ---------------------------------------------------------------
    def _walk(self, pos: np.ndarray) -> np.ndarray:
        """有界随机游走一步：在 [-step, step] 内随机增量，裁剪到值域。"""
        pos = pos + self._rng.uniform(-self.step, self.step, size=self.action_dim)
        return np.clip(pos, *self.action_range)


def build_metadata(core: SimInferCore) -> dict:
    """连接后下发的首条 metadata（msgpack）：含 ``action_horizon``（客户端必读）。"""
    return {
        "model": core.MODEL,
        "action_horizon": core.action_horizon,
        "action_dim": core.action_dim,
    }


def handle_connection(conn, core: SimInferCore) -> None:
    """处理单条 WebSocket 连接：连接先发 metadata，再一问一答回动作块。

    遵循传输契约（``MsgpackTransport``）：服务端以**文本**回包表示错误；客户端观测
    解析失败 / 消息异常 → 文本错误回包，不中断连接。
    """
    conn.send(packb(build_metadata(core)))
    while True:
        try:
            raw = conn.recv()
        except ConnectionClosed:
            break  # 客户端关闭 / 连接断开
        if raw is None:
            break
        if isinstance(raw, str):
            conn.send(f"error: unexpected text message: {raw!r}")  # 契约：文本 = 错误
            continue
        try:
            obs = unpackb(raw)
        except Exception as exc:  # noqa: BLE001 解析失败 → 文本错误回包
            conn.send(f"error: failed to unpack observation: {exc}")
            continue
        action = core.chunk_for(obs)
        conn.send(packb({KEY_ACTION: action}))


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, core: SimInferCore | None = None):
    """创建虚拟推理 WebSocket 服务端（``websockets.sync.server.serve`` 返回的 Server 上下文管理器）。

    ``Server.serve_forever()`` 阻塞运行（可放入独立线程）；退出上下文自动关闭。端口传 ``0``
    由系统分配（测试用），实际端口经 ``server.socket.getsockname()`` 读取（如有）。
    """
    core = core or SimInferCore()

    def handler(conn):
        handle_connection(conn, core)

    return serve(handler, host, port, max_size=None)  # max_size=None：容纳图像观测


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, core: SimInferCore | None = None) -> None:
    """阻塞运行虚拟推理端点（进程入口）：Ctrl-C 退出。"""
    with create_server(host, port, core) as server:
        print(f"Test infer point (virtual openpi server) listening on ws://{host}:{port}  (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nTest infer point stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start a virtual openpi inference server (no real inference; returns random-walk action chunks)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="WebSocket server bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="WebSocket server bind port")
    parser.add_argument("--action-dim", type=int, default=DEFAULT_ACTION_DIM, help="Action dimension")
    parser.add_argument("--action-horizon", type=int, default=DEFAULT_ACTION_HORIZON, help="Action chunk horizon")
    parser.add_argument("--step", type=float, default=DEFAULT_STEP, help="Random-walk step magnitude")
    parser.add_argument("--range", type=float, nargs=2, default=list(DEFAULT_RANGE), help="Action bounds (min max)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Random seed (None = random)")
    args = parser.parse_args()

    core = SimInferCore(
        action_dim=args.action_dim,
        action_horizon=args.action_horizon,
        step=args.step,
        action_range=tuple(args.range),
        seed=args.seed,
    )
    run_server(host=args.host, port=args.port, core=core)


if __name__ == "__main__":
    main()
