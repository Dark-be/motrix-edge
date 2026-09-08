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

"""虚拟推理端点（scripts/test_infer_point.py）测试。

覆盖：随机游走动作块形态 / 有界 / 块间连续、metadata 契约，以及真实 ``MsgpackTransport``
连接虚拟端点的 wire 契约（metadata → 观测 → 动作块）。无硬件、无需真实推理。
"""

import socket
import sys
import threading
from pathlib import Path

import numpy as np

from motrix_edge.transport import MsgpackTransport

# scripts/ 非安装包：把仓库根加入 sys.path 以便导入虚拟端点模块（与 test_robot_sdk 同为
# 独立运行的联调脚本，非 src-layout 包内模块）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.test_infer_point import (  # noqa: E402
    DEFAULT_RANGE,
    DEFAULT_STEP,
    SimInferCore,
    build_metadata,
    create_server,
)


def _free_port() -> int:
    """向系统申请一个空闲端口（bind 后关闭，供测试监听复用）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# SimInferCore：有界随机游走动作块
# ---------------------------------------------------------------------------


def test_chunk_shape_and_bounds():
    core = SimInferCore(action_dim=14, action_horizon=16, seed=0)
    chunk = core.chunk()
    assert chunk.shape == (16, 14)
    assert chunk.dtype == np.float64
    # 有界：落在动作值域内
    assert np.all(chunk >= DEFAULT_RANGE[0]) and np.all(chunk <= DEFAULT_RANGE[1])


def test_chunks_are_continuous_random_walk():
    """块间连续：下一块首步从上一块末步继续游走（单步增量 ≤ step）。"""
    core = SimInferCore(action_dim=14, action_horizon=16, step=DEFAULT_STEP, seed=1)
    first = core.chunk()
    second = core.chunk()
    assert first.shape == second.shape
    # 连续性：second[0] 与 first[-1] 每维增量不超过单步幅（有界随机游走）
    delta = np.abs(second[0] - first[-1])
    assert np.all(delta <= DEFAULT_STEP + 1e-9)
    # 轨迹在动（非静止）：至少有些维度移动
    assert np.any(np.abs(second[0] - first[-1]) > 1e-9)


def test_chunk_adapts_to_observation_state_dim():
    """观测携带 state（openpi 官方 flat 键）时按 state 维数适配动作维（单臂 7 维）。"""
    core = SimInferCore(action_dim=14, action_horizon=16, seed=0)
    obs = {"state": np.zeros(7, dtype=np.float32)}
    chunk = core.chunk_for(obs)
    assert chunk.shape == (16, 7)
    assert core.action_dim == 7


def test_reset_zeros_walk_start():
    core = SimInferCore(action_dim=14, action_horizon=16, seed=0)
    core.chunk()
    core.reset()
    chunk = core.chunk()
    # 复位后从全零开始：首步增量 ≤ step（相对 0）
    assert np.all(np.abs(chunk[0]) <= DEFAULT_STEP + 1e-9)


def test_build_metadata_defaults_without_action_horizon():
    """官方 metadata 默认不含 action_horizon（客户端从 policy_config 兜底）；可开关发布。"""
    core = SimInferCore(action_dim=14, action_horizon=16)
    meta = build_metadata(core)
    assert meta == {"model": "test-infer-point", "action_dim": 14}
    assert "action_horizon" not in meta

    core2 = SimInferCore(action_dim=14, action_horizon=16, publish_action_horizon=True)
    meta2 = build_metadata(core2)
    assert meta2["action_horizon"] == 16
    assert meta2["action_dim"] == 14
    assert meta2["model"] == "test-infer-point"


# ---------------------------------------------------------------------------
# wire 契约：真实 MsgpackTransport ↔ 虚拟端点（连接 → metadata → 观测 → 动作块）
# ---------------------------------------------------------------------------


def _serve_in_thread(core):
    """在后台线程运行虚拟端点，返回 (server, port)。"""
    port = _free_port()
    server = create_server("127.0.0.1", port, core)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def test_wire_contract_metadata_and_action_chunk():
    """MsgpackTransport 连接虚拟端点：收到 metadata（无 action_horizon）→ 发官方 flat 观测
    → 收 ``actions`` 动作块。"""
    core = SimInferCore(action_dim=14, action_horizon=16, seed=0)
    server, port = _serve_in_thread(core)
    transport = MsgpackTransport(host="127.0.0.1", port=port, connect_timeout=5.0)
    try:
        transport.connect()
        assert transport.server_metadata["model"] == "test-infer-point"
        assert transport.server_metadata["action_dim"] == 14
        assert "action_horizon" not in transport.server_metadata  # 官方默认不带

        obs = {"state": np.zeros(14, dtype=np.float32), "prompt": "do something"}
        resp = transport.request(obs)
        action = np.asarray(resp["actions"])
        assert action.shape == (16, 14)
        assert np.all(action >= DEFAULT_RANGE[0]) and np.all(action <= DEFAULT_RANGE[1])

        # 第二次请求：动作块从上一块末步继续（随机游走连续）
        resp2 = transport.request(obs)
        action2 = np.asarray(resp2["actions"])
        assert action2.shape == (16, 14)
        delta = np.abs(action2[0] - action[-1])
        assert np.all(delta <= DEFAULT_STEP + 1e-9)
    finally:
        transport.close()
        server.shutdown()


def test_wire_contract_adapts_to_state_dim():
    """虚拟端点按观测 state 维数返回对应维度的动作块。"""
    core = SimInferCore(action_dim=14, action_horizon=16, seed=0)
    server, port = _serve_in_thread(core)
    transport = MsgpackTransport(host="127.0.0.1", port=port, connect_timeout=5.0)
    try:
        transport.connect()
        resp = transport.request({"state": np.zeros(7, dtype=np.float32)})
        action = np.asarray(resp["actions"])
        assert action.shape == (16, 7)
    finally:
        transport.close()
        server.shutdown()


def test_openpi_client_e2e_official_flat_wire():
    """OpenPIClient ↔ 官方契约 mock 端到端：metadata 无 action_horizon（配置兜底 50）、
    ``actions`` 响应键、官方 flat 观测、prompt 动态可换。"""
    from motrix_edge.policy.openpi.client import OpenPIClient

    core = SimInferCore(action_dim=14, action_horizon=4, seed=0)
    server, port = _serve_in_thread(core)
    client = OpenPIClient({"host": "127.0.0.1", "port": port, "action_horizon": 50, "prompt": "pick and place"})
    try:
        client.connect()
        assert client.server_metadata["model"] == "test-infer-point"
        assert "action_horizon" not in client.server_metadata  # 官方默认不带
        assert client._action_horizon == 50  # 配置兜底

        obs = {"observations/qpos": np.zeros(14, dtype=np.float32)}
        actions = [client.infer(obs) for _ in range(20)]  # 跨动作块逐帧消费（每块 4 步）
        assert all(a is not None and np.ndim(a) == 1 and len(a) == 14 for a in actions)

        client.prompt = "now do a different task"  # 动态 prompt 可换
        assert np.ndim(client.infer(obs)) == 1
    finally:
        client.disconnect()
        server.shutdown()
