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

"""lerobot-remote 策略客户端单元测试 —— 观测组装 / 相机映射 / 取块，无网络。

用一个假的 ``RemoteClient``（只暴露 ``capabilities`` / ``admit`` / ``infer``）替代 zenoh 传输，
覆盖「按服务端声明组装观测」和「torch 动作块 → edge ActionChunk」两条主路径。
"""

import numpy as np
import pytest

from motrix_edge.policy.lerobot_remote.client import LerobotRemoteClient


class _Caps:
    """Minimal stand-in for the server-advertised capabilities."""

    def __init__(self, features, action_shape=(7,)):
        from lerobot.policies import FeatureSpec

        self.features = tuple(features)
        self.action_feature = FeatureSpec(
            name="action",
            shape=action_shape,
            dtype="float32",
            names=tuple(f"a{i}" for i in range(action_shape[0])),
            semantics="joint-abs",
        )
        self.action_interval = 0.1
        self.execution_steps = 20

    @staticmethod
    def rgb(name, shape=(480, 640, 3)):
        from lerobot.policies import FeatureSpec

        return FeatureSpec(name=name, shape=shape, dtype="uint8", kind="rgb", semantics="rgb")

    @staticmethod
    def state(name, shape=(7,)):
        from lerobot.policies import FeatureSpec

        return FeatureSpec(name=name, shape=shape, dtype="float32", semantics="joint-abs")


def _remote_client(policy_config=None):
    config = {"endpoint": "tcp/127.0.0.1:7447", "deployment": "d", "semantics": "s", **(policy_config or {})}
    client = LerobotRemoteClient(config)
    client._client = type("FakeRemote", (), {})()  # 只用于暴露 capabilities
    client._client.capabilities = _Caps(
        [
            _Caps.state("observation.state"),
            _Caps.rgb("observation.images.piper_0_cam_head"),
            _Caps.rgb("observation.images.piper_0_cam_right_wrist"),
        ]
    )
    client._camera_map = client._resolve_cameras(client._client.capabilities, _remote_obs())
    return client


def _remote_obs():
    return {
        "observations/qpos": np.arange(7, dtype=np.float32),
        "observations/qpos_err": np.arange(7, dtype=np.float32) + 10,
        "observations/images/cam_head": np.zeros((8, 8, 3), dtype=np.uint8),
        "observations/images/cam_right_wrist": np.zeros((8, 8, 3), dtype=np.uint8),
    }


def test_remote_observation_features_match_server_declaration():
    """观测按**服务端声明的特征名/尺寸**组装：state 原样、图像 letterbox 到声明尺寸。"""
    client = _remote_client()
    features = client._observation_features(_remote_obs())
    assert set(features) == {
        "observation.state",
        "observation.images.piper_0_cam_head",
        "observation.images.piper_0_cam_right_wrist",
    }
    assert features["observation.state"].tolist() == list(range(7))
    assert features["observation.images.piper_0_cam_head"].shape == (480, 640, 3)
    assert features["observation.images.piper_0_cam_head"].dtype == np.uint8


def test_remote_qpos_error_layout_concatenates_state():
    client = _remote_client({"observation_layout": "qpos+qpos_err"})
    state = client._state_values(_remote_obs())
    assert state.tolist() == [*range(7), *range(10, 17)]


def test_remote_qpos_error_layout_requires_matching_robot_observation():
    client = LerobotRemoteClient({"deployment": "d", "semantics": "s", "observation_layout": "qpos+qpos_err"})
    with pytest.raises(KeyError, match="observations/qpos_err"):
        client._state_values({"observations/qpos": np.zeros(7)})
    with pytest.raises(ValueError, match="qpos_err shape"):
        client._state_values({"observations/qpos": np.zeros(7), "observations/qpos_err": np.zeros(6)})


def test_remote_camera_names_map_by_short_suffix():
    """``cam_head`` 自动匹配 ``observation.images.piper_0_cam_head``（短名后缀）。"""
    client = _remote_client()
    assert client._camera_map == {
        "observation.images.piper_0_cam_head": "cam_head",
        "observation.images.piper_0_cam_right_wrist": "cam_right_wrist",
    }


def test_remote_camera_map_override():
    client = LerobotRemoteClient(
        {
            "deployment": "d",
            "semantics": "s",
            "camera_map": {"head": "observation.images.piper_0_cam_head"},
        }
    )
    caps = _Caps(
        [
            _Caps.rgb("observation.images.piper_0_cam_head"),
            _Caps.rgb("observation.images.piper_0_cam_right_wrist"),
        ]
    )
    obs = {
        "observations/images/head": np.zeros((8, 8, 3), dtype=np.uint8),
        "observations/images/cam_right_wrist": np.zeros((8, 8, 3), dtype=np.uint8),
    }
    mapping = client._resolve_cameras(caps, obs)
    assert mapping["observation.images.piper_0_cam_head"] == "head"
    assert mapping["observation.images.piper_0_cam_right_wrist"] == "cam_right_wrist"


def test_remote_unmappable_camera_raises():
    client = LerobotRemoteClient({"deployment": "d", "semantics": "s"})
    caps = _Caps([_Caps.rgb("observation.images.front")])
    with pytest.raises(ValueError, match="cannot map server camera"):
        client._resolve_cameras(caps, {"observations/qpos": np.zeros(7)})


class _FakeRemote:
    """Stand-in for ``lerobot.remote_inference.RemoteClient`` (no transport)."""

    def __init__(self, caps, steps=20, dim=7):
        import torch

        self.capabilities = caps
        self.instance_id = "inst-1"
        self.artifact_identity = "artifact-1"
        self.admits = []
        self.requests = []
        self.closed = False
        self._torch = torch
        self._steps = steps
        self._dim = dim

    def admit(self, **kwargs):
        self.admits.append(kwargs)

    def infer(self, request):
        from lerobot.inference import ActionChunk, ActionProvenance

        self.requests.append(request)
        actions = self._torch.zeros((self._steps, self._dim), dtype=self._torch.float32)
        return ActionChunk(
            model_actions=None,
            canonical_actions=actions,
            provenance=ActionProvenance(request.observation.capture_time, request.observation.task),
            execution_steps=self._steps,
        )

    def close(self):
        self.closed = True


def _connected(policy_config=None, caps=None, steps=20):
    client = LerobotRemoteClient(
        {"endpoint": "tcp/127.0.0.1:7447", "deployment": "d", "semantics": "s", **(policy_config or {})}
    )
    fake = _FakeRemote(
        caps
        or _Caps(
            [
                _Caps.state("observation.state"),
                _Caps.rgb("observation.images.piper_0_cam_head"),
                _Caps.rgb("observation.images.piper_0_cam_right_wrist"),
            ]
        ),
        steps=steps,
    )
    client._client = fake
    return client, fake


def test_infer_chunk_admits_once_and_returns_edge_action_chunk():
    """admission 只做一次（用服务端声明的 features / 频率），取块转成 edge ``ActionChunk``。"""
    client, fake = _connected()
    chunk = client.infer_chunk(_remote_obs(), index=42)

    assert fake.admits and len(fake.admits) == 1
    admitted = fake.admits[0]
    assert admitted["features"] is fake.capabilities.features  # 直接采用服务端声明
    assert admitted["action_interval"] == fake.capabilities.action_interval
    assert admitted["semantics"] == "s" and admitted["mode"] == "chunk"

    assert chunk.actions.shape == (20, 7)
    assert np.isfinite(chunk.actions).all()
    assert chunk.start_index == 42  # 首步 = 请求的绝对步号
    assert client.observed_chunk_len == 20 and client.chunk_seen is True

    request = fake.requests[0]
    assert request.generation == 0 and request.delay == 0
    assert request.mode.value == "chunk"
    assert request.continuation.cursor == 42
    assert set(request.observation.features) == {
        "observation.state",
        "observation.images.piper_0_cam_head",
        "observation.images.piper_0_cam_right_wrist",
    }

    # 第二次取块不再重复 admission
    client.infer_chunk(_remote_obs(), index=43)
    assert len(fake.admits) == 1


def test_prepare_without_observation_is_noop():
    client, fake = _connected()
    client.prepare(None)
    assert fake.admits == []


def test_infer_without_connect_raises():
    client = LerobotRemoteClient({"deployment": "d", "semantics": "s"})
    with pytest.raises(RuntimeError, match="not connected"):
        client.infer_chunk(_remote_obs())


def test_disconnect_closes_client():
    client, fake = _connected()
    client.prepare(_remote_obs())
    client.disconnect()
    assert fake.closed is True and client.connected is False


def test_admit_rejects_state_dim_mismatch():
    client, _ = _connected(
        caps=_Caps([_Caps.state("observation.state", shape=(14,))]),
    )
    with pytest.raises(ValueError, match="does not match server feature"):
        client.infer_chunk(_remote_obs(), index=0)


def test_jpeg_encoding_requires_pillow(monkeypatch):
    """encoding=jpeg 需要 pillow；缺依赖时由 codec 懒加载给出明确安装提示。"""
    import lerobot.utils.import_utils as import_utils
    from lerobot.remote_inference.codec import RGBImage, encode_message
    from lerobot.remote_inference.protocol import Envelope, MessageType

    real = import_utils.is_package_available
    monkeypatch.setattr(
        import_utils,
        "is_package_available",
        lambda pkg, import_name=None: False if pkg == "pillow" else real(pkg, import_name=import_name),
    )
    with pytest.raises(ImportError, match="pillow"):
        encode_message(
            Envelope(
                MessageType.OBSERVATION,
                "inst",
                "sess",
                0,
                "req",
                {"image": RGBImage(np.zeros((4, 4, 3), dtype=np.uint8), "jpeg", 90)},
            )
        )
