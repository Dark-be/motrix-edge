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

"""policy/act（lerobot gRPC 流式客户端）测试 —— 进程内 fake AsyncInference 服务端，无 lerobot 官方 server。

覆盖：connect（Ready 握手 + 延迟下发 PolicyInstructions）、动作块流式消费（块内不
重复上传观测 / 块耗尽才再上传）、must_go 语义、时序平滑（重叠预取 + 加权聚合）、
drain / reset / disconnect。
"""

import pickle
import queue
import threading
import time
from concurrent import futures

import grpc
import numpy as np
import pytest
import torch

from lerobot.async_inference.helpers import TimedAction
from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import python_object_to_bytes, receive_bytes_in_chunks
from motrix_edge.policy.act.client import ACTClient


class _FakeAsyncInferenceServicer(services_pb2_grpc.AsyncInferenceServicer):
    """模仿 lerobot policy_server 的 AsyncInference 语义（不做真推理）：

    - SendObservations 聚合分块后解 pickle 得到 TimedObservation，把 timestep 入队；
    - GetActions 弹出一个待推理观测，返回其起始 timestep 起的 actions_per_chunk 步
      TimedAction。每块的动作为同一标量：默认 1.0；传 ``block_values`` 时按请求顺序
      取用（用于验证平滑聚合 old/new 差异）。
    """

    def __init__(self, actions_per_chunk: int = 3, dim: int = 2, block_values=None):
        self.actions_per_chunk = actions_per_chunk
        self.dim = dim
        self.ready_calls = 0
        self.policy_calls = 0
        self.obs_calls = 0
        self.policy_data: bytes | None = None
        self.last_raw: dict | None = None
        self._obs_queue: "queue.Queue[int]" = queue.Queue()
        self._block_values = iter(block_values) if block_values is not None else None

    def Ready(self, request, context):  # noqa: N802
        self.ready_calls += 1
        return services_pb2.Empty()

    def SendPolicyInstructions(self, request, context):  # noqa: N802
        self.policy_calls += 1
        self.policy_data = request.data
        return services_pb2.Empty()

    def SendObservations(self, request_iterator, context):  # noqa: N802
        data = receive_bytes_in_chunks(request_iterator, None, threading.Event())
        timed = pickle.loads(data)  # noqa: S301 测试用（vendored 类）
        self.last_raw = timed.get_observation()
        self._obs_queue.put(timed.get_timestep())
        self.obs_calls += 1
        return services_pb2.Empty()

    def GetActions(self, request, context):  # noqa: N802
        try:
            timestep = self._obs_queue.get(timeout=2.0)
        except queue.Empty:
            return services_pb2.Actions(data=b"")
        if self._block_values is not None:
            value = next(self._block_values, 1.0)
            action = torch.full((self.dim,), value)
        else:
            action = torch.ones(self.dim)
        chunk = [
            TimedAction(timestamp=time.time(), timestep=timestep + i, action=action)
            for i in range(self.actions_per_chunk)
        ]
        return services_pb2.Actions(data=python_object_to_bytes(chunk))


@pytest.fixture()
def act_server():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = _FakeAsyncInferenceServicer(actions_per_chunk=3, dim=2)
    services_pb2_grpc.add_AsyncInferenceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    yield port, servicer
    server.stop(0)


def _make_client(port, **overrides):
    # 默认关闭时序平滑（smooth_overlap=0 → 纯「块耗尽才推理」），平滑语义由单独测试开启。
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "pretrained_name_or_path": "fake/act",
        "actions_per_chunk": 3,
        "smooth_overlap": 0,
    }
    cfg.update(overrides)
    return ACTClient(cfg)


def test_act_grpc_handshake_and_lazy_policy(act_server):
    """connect：Ready 握手成功；策略指令延后（首次 infer 才 SendPolicyInstructions）。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    assert servicer.ready_calls == 1
    assert servicer.policy_calls == 0  # 指令延后
    assert client.server_metadata["protocol"] == "lerobot/async-inference"
    client.disconnect()


def test_act_grpc_streams_action_chunks(act_server):
    """动作块流式：一个块（3 步）内不重复上传观测，块耗尽（第 4 步）才再上传。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

    actions = [client.infer(obs) for _ in range(4)]
    assert all(a.shape == (2,) for a in actions)
    assert all(np.allclose(a, [1.0, 1.0]) for a in actions)
    assert servicer.obs_calls == 2  # ts0 上传一次，ts3 再上传一次
    assert servicer.policy_calls == 1  # 仅首次 infer 下发策略指令
    assert servicer.policy_data is not None

    # drain：消费缓存动作（不发新推理请求 / 不再上传观测）
    drained = client.drain()
    assert drained is not None and np.allclose(drained, [1.0, 1.0])
    assert servicer.obs_calls == 2
    client.disconnect()


def test_act_grpc_reset_clears_cache(act_server):
    """reset：清空缓存动作（随后 infer 需再上传观测取新块）。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
    client.infer(obs)  # 块 {0,1,2}，缓存 1,2
    client.reset()
    assert client._actions == {}
    client.infer(obs)  # 需新观测（ts1）→ 再上传
    assert servicer.obs_calls == 2
    client.disconnect()


def test_act_grpc_requires_pretrained_model(act_server):
    """缺 pretrained_name_or_path：首次 infer 拒绝（策略指令需要模型标识）。"""
    port, _ = act_server
    client = _make_client(port, pretrained_name_or_path=None)
    client.connect()
    with pytest.raises(ValueError, match="pretrained_name_or_path"):
        client.infer({"observations/qpos": np.zeros(2, dtype=np.float32)})
    client.disconnect()


def test_act_grpc_image_letterboxed(act_server):
    """图像上传前被 letterbox 到 224×224：等比缩放 + 上下留黑边，内容不变形。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    # 640×360 横向纯红图
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    img[:, :, 2] = 255  # R
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32), "observations/images/cam": img}
    client.infer(obs)

    got = servicer.last_raw["cam"]
    assert got.shape == (224, 224, 3)
    assert not got[:40].any()  # 上黑边
    assert not got[-40:].any()  # 下黑边
    # 中部内容行（等比缩放后内容区高 126，居中于 [49, 175)）保持纯红
    assert np.all(got[112, :, 2] == 255)
    client.disconnect()


def test_act_grpc_smoothing_weighted_sequence(act_server):
    """平滑完整序列：K=3、overlap=1、块值 1..N，断言重叠步 = 0.3*旧 + 0.7*新。"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = _FakeAsyncInferenceServicer(actions_per_chunk=3, dim=2, block_values=[1, 2, 3, 4, 5, 6])
    services_pb2_grpc.add_AsyncInferenceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        client = _make_client(port, smooth_overlap=1)
        client.connect()
        obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
        actions = [client.infer(obs) for _ in range(8)]
        expected = [1.0, 1.0, 1.7, 2.0, 2.7, 3.0, 3.7, 4.0]
        assert all(np.allclose(a, [v, v]) for a, v in zip(actions, expected))
        # 预取请求发生在 ts0/2/4/6（每 K-overlap=2 步一次），非每步
        assert servicer.obs_calls == 4
        # drain 只消费已平滑缓存，不再触发推理
        drained = []
        while (a := client.drain()) is not None:
            drained.append(a)
        assert drained  # 消费了块3剩余 ts7→后…
        assert servicer.obs_calls == 4
        client.disconnect()
    finally:
        server.stop(0)


def test_act_grpc_unknown_aggregate_fn():
    """未知 aggregate_fn：构造即拒绝（配置错误尽早暴露）。"""
    with pytest.raises(ValueError, match="aggregate_fn"):
        ACTClient({"host": "127.0.0.1", "port": 1, "aggregate_fn": "bogus"})
