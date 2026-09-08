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


class _FakeTransport:
    """计数 transport：支持 connect/close，记录最近 payload，返回固定动作块 [horizon, dim]。"""

    def __init__(self, horizon=2, dim=2, metadata=None):
        self.calls = 0
        self.close_calls = 0
        self.horizon = horizon
        self.dim = dim
        self.server_metadata = metadata
        self.last_payload = None

    def connect(self):
        pass

    def close(self):
        self.close_calls += 1

    def request(self, payload):
        self.calls += 1
        self.last_payload = payload
        return {"actions": np.ones((self.horizon, self.dim), dtype=np.float32)}


def test_openpi_defaults_action_horizon_when_metadata_missing():
    """官方服务端 metadata 不提供 action_horizon → 回退 policy_config（缺省 50），不再报错。

    连接失败清理（refresh 语义：close 既有/半开连接）仍须保持。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({})  # 无 metadata action_horizon、无 config action_horizon
    transport = _FakeTransport(metadata={"model": "piper", "action_dim": 14})
    client._transport = transport
    client.connect()
    assert client.server_metadata == {"model": "piper", "action_dim": 14}
    assert client._action_horizon == 50  # 缺省 50（配置键 policy.action_horizon）
    client.disconnect()
    assert client.server_metadata == {}

    # metadata 有则优先；config 其次
    client2 = OpenPIClient({"action_horizon": 40})
    client2._transport = _FakeTransport(metadata={"action_horizon": 8})
    client2.connect()
    assert client2._action_horizon == 8  # metadata 优先
    client2.disconnect()
    client3 = OpenPIClient({"action_horizon": 40})
    client3._transport = _FakeTransport(metadata={})
    client3.connect()
    assert client3._action_horizon == 40  # config 兜底
    client3.disconnect()


def test_openpi_exposes_server_metadata():
    from motrix_edge.policy.openpi.client import OpenPIClient

    metadata = {"model": "test-policy", "action_horizon": 16, "action_dim": 14}
    client = OpenPIClient({})
    client._transport = _FakeTransport(metadata=metadata)
    client.connect()
    assert client.server_metadata == metadata
    assert client._action_horizon == 16
    client.disconnect()
    assert client.server_metadata == {}


def test_openpi_infer_requests_only_when_chunk_empty():
    """OpenPIClient.infer：**仅当缓存块耗尽时才向推理端请求**（其余步骤消耗缓存块）。

    一个动作块（[horizon, dim]）应支撑 horizon 步推理，期间不再访问推理端。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 2})
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    client.connect()  # 以 config action_horizon=2 初始化空缓存

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


def _jpeg_bytes(rgb):
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok
    return buf.tobytes()


def test_openpi_sends_official_flat_observation():
    """OpenPIClient 按官方 flat 契约组装观测：state=qpos、图像为 uint8 数组（非 jpeg bytes）、
    相机集来自 bind_adapter（adapter 启用的相机名，非 edge.yml）、rename_cameras 改名、
    prompt 动态携带。"""
    from motrix_edge.policy.contract import OPENPI_KEY_IMAGES, OPENPI_KEY_PROMPT, OPENPI_KEY_STATE
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient(
        {
            "action_horizon": 2,
            "image_size": 16,
            "rename_cameras": {"cam_head": "base_0_rgb"},
            "prompt": "open the box",
        }
    )
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    # 布局来自 adapter 运行时配置（InferSession 进入时 bind_adapter）：只启用 cam_head + cam_right_wrist
    client.bind_adapter(action_dim=7, camera_names=["cam_head", "cam_right_wrist"])
    client.connect()

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[..., 0] = 200
    obs = {
        "observations/qpos": np.arange(2, dtype=np.float32),
        "observations/images/cam_head": _jpeg_bytes(rgb),  # jpeg bytes → 改名 base_0_rgb
        "observations/images/cam_left_wrist": _jpeg_bytes(rgb),  # 未启用（不在 bind 相机集）→ 过滤
        "observations/images/cam_right_wrist": rgb,  # 数组直传
    }
    assert client.infer(obs) is not None

    payload = transport.last_payload
    assert np.array_equal(payload[OPENPI_KEY_STATE], obs["observations/qpos"])
    assert payload[OPENPI_KEY_PROMPT] == "open the box"
    images = payload[OPENPI_KEY_IMAGES]
    assert set(images) == {"base_0_rgb", "cam_right_wrist"}  # bind 相机集过滤 + 改名
    for img in images.values():
        assert isinstance(img, np.ndarray)
        assert img.dtype == np.uint8
        assert img.shape == (16, 16, 3)  # uint8 HWC（非 jpeg bytes）


def test_openpi_camera_layout_comes_from_bind_adapter_not_config():
    """openpi **不读 edge.yml 的相机名**：仅 bind_adapter 决定要下发的相机（未绑定 = 透传全部）。"""
    from motrix_edge.policy.contract import OPENPI_KEY_IMAGES
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 1, "image_size": 8})  # policy_config 不带任何相机列表
    transport = _FakeTransport(horizon=1, dim=1)
    client._transport = transport
    client.connect()
    obs = {
        "observations/qpos": np.zeros(1, dtype=np.float32),
        "observations/images/cam_head": np.zeros((4, 4, 3), dtype=np.uint8),
        "observations/images/cam_left_wrist": np.zeros((4, 4, 3), dtype=np.uint8),
    }
    client.infer(obs)  # 未绑定 → 透传观测内全部相机
    assert set(transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head", "cam_left_wrist"}

    client.bind_adapter(action_dim=7, camera_names=["cam_head"])  # adapter 只启用 cam_head（双相机→单相机）
    client.infer(obs)  # 上一块（horizon=1）已耗尽 → 再请求
    assert set(transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head"}


def test_openpi_prompt_dynamic_per_request():
    """prompt 运行时动态可换（会话侧 set policy.prompt 后，下一请求携带新文本）。"""
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 1, "image_size": 8})
    transport = _FakeTransport(horizon=1, dim=1)
    client._transport = transport
    client.connect()
    obs = {"observations/qpos": np.zeros(1, dtype=np.float32)}

    client.infer(obs)  # 块为空 → 请求
    assert "prompt" not in transport.last_payload  # 缺省 prompt=None → 不发（服务端 default_prompt 兜底）

    client.prompt = "switch tasks now"
    client.infer(obs)  # 块已耗尽（horizon=1）→ 再请求
    assert transport.last_payload["prompt"] == "switch tasks now"


# act 策略已改为 lerobot gRPC 流式客户端（见 tests/test_act_grpc_client.py），
# 不再使用 ws / ActionChunkBroker / image_size；相关旧断言已移除。


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
    # act 走 lerobot gRPC：不再有 ws 的 image_size / image_format
    assert not hasattr(policy, "image_size")


def test_get_policy_uses_only_shared_endpoint_config():
    policy = get_policy({"policy": {"host": "127.0.0.1", "port": 8765}}, policy_type="act")
    assert policy.policy_config["host"] == "127.0.0.1"
    assert policy.policy_config["port"] == 8765
    assert policy._transport._host == "127.0.0.1"
    assert policy._transport._port == 8765
