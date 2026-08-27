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

"""DualPiperAdapter 薄客户端测试，无真实 SDK 进程 / 共享内存。"""

import cv2
import numpy as np
import pytest

from motrix_edge.adapter.base import CAMERA_PREFIX, KEY_ACTION, KEY_QPOS, AdapterCapability
from motrix_edge.adapter.dual_piper_adapter import DualPiperAdapter
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_TELEOP_ENABLED,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_EXECUTE,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
)


class _Response:
    def __init__(self, body=None, status_code=200):
        self._body = body or {}
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    def __init__(self, get_body=None):
        self.posts = []
        self.get_body = get_body or {}
        self.closed = False

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Response()

    def get(self, url):
        return _Response(self.get_body.get(url, {}))

    def close(self):
        self.closed = True


def _adapter(http=None):
    adapter = DualPiperAdapter(name="Piper")
    adapter._http = http or _FakeHttp()
    return adapter


def test_identity_and_capabilities():
    adapter = DualPiperAdapter(name="Piper")
    caps = adapter.capabilities

    assert (adapter.name, adapter.type) == ("Piper", "dual_piper")
    assert caps.action_dim == 14
    assert caps.image_names == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert all(caps.supports(cap) for cap in AdapterCapability)


def test_execute_and_rollout_validate_then_forward():
    http = _FakeHttp()
    adapter = _adapter(http)

    with pytest.raises(ValueError, match="execute action dim"):
        adapter.execute([0.0] * 7)
    assert http.posts == []

    action = np.arange(14, dtype=np.float64)
    adapter.execute(action)
    adapter.rollout(action)

    expected = action.tolist()
    assert adapter.executed == [expected]
    assert adapter.rollout_calls == 1
    assert http.posts == [
        (PATH_EXECUTE, {FIELD_ACTION: expected}),
        (PATH_ROLLOUT, {FIELD_ACTION: expected}),
    ]


def test_control_and_capture_commands_forward():
    http = _FakeHttp()
    adapter = _adapter(http)

    adapter.reset()
    adapter.set_teleop(True)
    adapter.start_capture()
    adapter.end_capture()
    adapter.safe_stop()

    assert adapter.reset_calls == 1
    assert adapter.teleop_enabled is True
    assert adapter.safe_stop_calls == 1
    assert http.posts == [
        (PATH_RESET, None),
        (PATH_TELEOP, {FIELD_TELEOP_ENABLED: True}),
        (PATH_CAPTURE_START, None),
        (PATH_CAPTURE_END, None),
        (PATH_SAFE_STOP, None),
    ]


def test_health_and_data_status():
    http = _FakeHttp(
        {
            "/v1/health": {"ok": True},
            "/v1/data_status": {"data_dir": "/data/task", "data_files": ["a.hdf5", "b.json"]},
        }
    )
    adapter = _adapter(http)

    assert adapter.health().ok is True
    assert adapter.running is True
    data = adapter.data_status()
    assert data is not None
    assert data.data_dir == "/data/task"
    assert data.data_files == ["a.hdf5", "b.json"]


def test_observe_returns_none_until_first_frame(monkeypatch):
    class _Reader:
        def __init__(self, name):
            assert name == "dual_piper_obs"

        def read(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr("motrix_edge.adapter.dual_piper_adapter.ObsShmReader", _Reader)
    assert DualPiperAdapter().observe() is None


def test_observe_builds_edge_observation(monkeypatch):
    images = [np.full((8, 8, 3), channel, dtype=np.uint8) for channel in (40, 80, 120)]

    class _Reader:
        def __init__(self, name):
            pass

        def read(self):
            return {"qpos": np.arange(14, dtype=np.float64), "images": images}

        def close(self):
            pass

    monkeypatch.setattr("motrix_edge.adapter.dual_piper_adapter.ObsShmReader", _Reader)
    obs = DualPiperAdapter().observe()

    assert obs is not None
    assert obs[KEY_QPOS].dtype == np.float32
    assert np.array_equal(obs[KEY_ACTION], obs[KEY_QPOS])
    for name in ("cam_head", "cam_left_wrist", "cam_right_wrist"):
        encoded = obs[f"{CAMERA_PREFIX}{name}"]
        assert isinstance(encoded, bytes)
        assert cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


def test_release_closes_local_resources():
    class _Shm:
        closed = False

        def close(self):
            self.closed = True

    http = _FakeHttp()
    shm = _Shm()
    adapter = _adapter(http)
    adapter._shm = shm

    adapter.release()

    assert shm.closed is True
    assert http.closed is True
    assert adapter._shm is None
    assert adapter._http is None
