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

"""policy 包单元测试 —— 格式契约 / 动作块 / 工厂，无硬件、无网络可跑。"""

import cv2
import numpy as np
import pytest

from motrix_edge.policy import POLICY_REGISTRY, get_policy
from motrix_edge.policy.broker import ActionChunkBroker
from motrix_edge.policy.contract import (
    KEY_ACTION,
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    build_observation,
    encode_image,
    extract_action,
    resize_with_pad,
    to_rgb_uint8,
)


@pytest.fixture
def qpos():
    return np.zeros(14, dtype=np.float32)


def test_resize_with_pad_center_pads():
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    out = resize_with_pad(img, 224, 224)
    assert out.shape == (224, 224, 3)
    assert out.dtype == np.uint8


def test_build_observation_uint8(qpos):
    images = {"cam_head": np.zeros((100, 100, 3), dtype=np.uint8)}
    obs = build_observation(qpos, images, (224, 224), image_format="uint8")
    assert np.array_equal(obs[KEY_OBS_QPOS], qpos)
    img = obs[f"{KEY_OBS_IMAGE_PREFIX}cam_head"]
    assert img.shape == (224, 224, 3)
    assert img.dtype == np.uint8


def test_build_observation_jpeg(qpos):
    images = {"cam_head": np.zeros((100, 100, 3), dtype=np.uint8)}
    obs = build_observation(qpos, images, (224, 224), image_format="jpeg")
    raw = obs[f"{KEY_OBS_IMAGE_PREFIX}cam_head"]
    assert isinstance(raw, bytes)
    assert to_rgb_uint8(raw).shape == (224, 224, 3)


def test_build_observation_from_jpeg_bytes(qpos):
    # 直接传入 jpeg bytes（机器人传感器默认产出），契约层负责解码 + 缩放补零 + 重编码
    ok, buf = cv2.imencode(".jpg", np.zeros((80, 160, 3), dtype=np.uint8))
    assert ok
    images = {"cam_head": buf.tobytes()}
    obs = build_observation(qpos, images, (224, 224), image_format="jpeg")
    assert to_rgb_uint8(obs[f"{KEY_OBS_IMAGE_PREFIX}cam_head"]).shape == (224, 224, 3)


def test_jpeg_roundtrip_preserves_rgb_channels():
    # 已知 RGB 纯色块：验证 cv2 解码/编码路径没有把 R/B 通道对调（openpi 模型要求 RGB）
    rgb = np.zeros((16, 16, 3), dtype=np.uint8)
    rgb[..., 0] = 200  # R 强
    rgb[..., 1] = 100
    rgb[..., 2] = 50  # B 弱
    jpeg = encode_image(rgb, (16, 16), image_format="jpeg")
    decoded = to_rgb_uint8(jpeg)
    # jpeg 有损，允许容差；若 R/B 对调则 R 均值≈50、B≈200，必然失败
    assert abs(decoded[..., 0].mean() - 200) < 20
    assert abs(decoded[..., 2].mean() - 50) < 20


def test_extract_action():
    resp = {KEY_ACTION: np.array([[0.1, 0.2], [0.3, 0.4]])}
    assert np.array_equal(extract_action(resp), np.array([[0.1, 0.2], [0.3, 0.4]]))


def test_extract_action_error():
    with pytest.raises(RuntimeError):
        extract_action({"error": "boom"})


def test_extract_action_missing_key():
    with pytest.raises(KeyError):
        extract_action({"foo": 1})


def test_broker_chunk_slicing():
    """feed 新块后逐帧切片；块耗尽后 empty → 需重新 feed。"""
    broker = ActionChunkBroker(action_horizon=2)
    chunk = np.array([[1.0, 2.0], [3.0, 4.0]])
    assert broker.empty  # 初始无块
    broker.feed(chunk)
    assert not broker.empty
    assert np.array_equal(broker.step(), np.array([1.0, 2.0]))
    assert np.array_equal(broker.step(), np.array([3.0, 4.0]))
    # 块耗尽后 empty → 需重新 feed 新块（调用方向推理端请求的时机）
    assert broker.empty
    new_chunk = np.array([[5.0, 6.0], [7.0, 8.0]])
    broker.feed(new_chunk)
    assert np.array_equal(broker.step(), np.array([5.0, 6.0]))


def test_broker_single_step_passthrough():
    """单步动作（[dim]）透传不切片，消耗后即空。"""
    broker = ActionChunkBroker(action_horizon=2)
    broker.feed(np.array([0.1, 0.2]))
    action = broker.step()
    assert np.array_equal(action, np.array([0.1, 0.2]))
    assert broker.empty


def test_broker_uses_actual_short_chunk_length():
    """服务端返回短于协商 horizon 的末块时，按实际块长耗尽，不越界。"""
    broker = ActionChunkBroker(action_horizon=4)
    broker.feed(np.array([[1.0, 2.0], [3.0, 4.0]]))

    assert np.array_equal(broker.step(), np.array([1.0, 2.0]))
    assert np.array_equal(broker.step(), np.array([3.0, 4.0]))
    assert broker.empty


def test_broker_preserves_actual_long_chunk_length():
    """服务端返回长于协商 horizon 的块时，不静默丢弃尾部动作。"""
    broker = ActionChunkBroker(action_horizon=2)
    chunk = np.array([[1.0], [2.0], [3.0]])
    broker.feed(chunk)

    assert [broker.step().item() for _ in range(3)] == [1.0, 2.0, 3.0]
    assert broker.empty


def test_broker_reset():
    broker = ActionChunkBroker(action_horizon=2)
    chunk = np.array([[1.0, 2.0], [3.0, 4.0]])
    broker.feed(chunk)
    broker.reset()
    assert broker.empty  # reset 清空缓存
    broker.feed(chunk)
    assert np.array_equal(broker.step(), np.array([1.0, 2.0]))


def test_broker_step_without_feed_raises():
    """未 feed 直接 step：明确报错（调用方应先检查 empty）。"""
    broker = ActionChunkBroker(action_horizon=2)
    with pytest.raises(RuntimeError):
        broker.step()


class _FakeTransport:
    """计数 transport：支持 connect/close，并返回固定动作块 [horizon, dim]。"""

    def __init__(self, horizon=2, dim=2, metadata=None):
        self.calls = 0
        self.close_calls = 0
        self.horizon = horizon
        self.dim = dim
        self.server_metadata = metadata

    def connect(self):
        pass

    def close(self):
        self.close_calls += 1

    def request(self, payload):
        self.calls += 1
        return {"action": np.ones((self.horizon, self.dim), dtype=np.float32)}


def test_openpi_connect_closes_transport_when_action_horizon_missing():
    """metadata / config 均缺 action_horizon 时，必须关闭已建立的 WebSocket。"""
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({})
    transport = _FakeTransport(metadata={})
    client._transport = transport

    with pytest.raises(ValueError, match="action_horizon"):
        client.connect()

    assert transport.close_calls == 1
    assert client.server_metadata == {}
    assert client._broker is None
    assert client._action_horizon is None


def test_policy_clients_expose_server_metadata():
    from motrix_edge.policy.act.client import ACTClient
    from motrix_edge.policy.openpi.client import OpenPIClient

    metadata = {"model": "test-policy", "action_horizon": 16, "action_dim": 14}
    for client in (OpenPIClient({}), ACTClient({})):
        client._transport = _FakeTransport(metadata=metadata)
        client.connect()
        assert client.server_metadata == metadata
        client.disconnect()
        assert client.server_metadata == {}


def test_openpi_infer_requests_only_when_chunk_empty():
    """OpenPIClient.infer：**仅当 broker 块耗尽时才向推理端请求**（其余步骤消耗缓存块）。

    一个动作块（[horizon, dim]）应支撑 horizon 步推理，期间不再访问推理端。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 2})
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    client._broker = ActionChunkBroker(2)

    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
    # 第 1 步：块为空 → 请求 1 次，消耗缓存第 1 步
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 1
    # 第 2 步：块未耗尽 → **不请求**，直接消耗缓存第 2 步
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 1
    # 第 3 步：块耗尽（empty）→ 再请求 1 次
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 2


def test_act_client_uses_640x480_image_size_by_default():
    from motrix_edge.policy.act.client import ACTClient

    client = ACTClient({})
    assert client.image_size == (480, 640)


def test_act_infer_requests_only_when_chunk_empty():
    from motrix_edge.policy.act.client import ACTClient

    client = ACTClient({"action_horizon": 2})
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    client._broker = ActionChunkBroker(2)

    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 1
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 1
    assert np.array_equal(client.infer(obs), np.array([1.0, 1.0]))
    assert transport.calls == 2


def test_policy_registry_has_openpi_and_act():
    assert "openpi" in POLICY_REGISTRY
    assert "act" in POLICY_REGISTRY


def test_validate_policy_type():
    from motrix_edge.policy import validate_policy_type

    assert validate_policy_type("act") == "act"
    with pytest.raises(ValueError, match="nonexistent"):
        validate_policy_type("nonexistent")


def test_get_policy_unknown_type():
    with pytest.raises(ValueError):
        get_policy({"policy": {"type": "nonexistent"}})


def test_get_policy_default_openpi():
    policy = get_policy({})
    assert policy.__class__.__name__ == "OpenPIClient"


def test_get_policy_act():
    policy = get_policy({"policy": {"type": "act"}})
    assert policy.__class__.__name__ == "ACTClient"
    assert policy.image_size == (480, 640)


def test_get_policy_uses_only_shared_endpoint_config():
    policy = get_policy({"policy": {"host": "127.0.0.1", "port": 8765}}, policy_type="act")
    assert policy.policy_config["host"] == "127.0.0.1"
    assert policy.policy_config["port"] == 8765
    assert policy.image_size == (480, 640)
    assert policy.image_format == "jpeg"
