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

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    DEPTH_PREFIX,
    KEY_ACTION,
    KEY_POSE,
    KEY_QPOS,
    ActionSpace,
    AdapterCapability,
)
from motrix_edge.adapter.dual_piper_adapter import DualPiperAdapter
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_CAMERAS,
    FIELD_LAYOUT,
    FIELD_TELEOP_ENABLED,
    PATH_CAMERAS,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_EXECUTE,
    PATH_RESET,
    PATH_ROLLOUT,
    PATH_SAFE_STOP,
    PATH_TELEOP,
)

_GRIPPER = (0.5, 0.8)  # 每臂 1（顺序同 ARM_NAMES = left / right）


def _state_vector(joints, gripper) -> np.ndarray:
    """值段（每臂 6）+ 夹爪段（每臂 1）→ 状态向量（每臂「值 + 夹爪」交错，与机器人侧同规则）。

    输出与 ``adapter.observe()`` 一致（``float32``）——否则 0.8 这类值在 float32/float64 间
    不等，断言会假失败。
    """
    values = np.asarray(joints, dtype=np.float64).reshape(-1)
    grips = np.asarray(gripper, dtype=np.float64).reshape(-1)
    return np.concatenate(
        [np.concatenate([values[index * 6 : (index + 1) * 6], [grips[index]]]) for index in range(grips.size)]
    ).astype(np.float32)


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
        self.gets = []  # GET 调用记录（断言元数据只查一次）
        self.get_body = get_body or {}
        self.closed = False

    def post(self, url, json=None):
        self.posts.append((url, json))
        return _Response()

    def get(self, url):
        self.gets.append(url)
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
    assert caps.action_dim == 12  # 兼容字段 = 关节空间维度
    assert caps.action_dims == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
    assert caps.action_spaces == ["joint", "pose", "pose_delta", "gripper"]
    # 观测键 = observe() 实际产出的键（状态向量 qpos + 目标向量 action + 位姿），相机键在后
    assert caps.observation_keys[:3] == [KEY_QPOS, KEY_ACTION, KEY_POSE]
    assert caps.image_names == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert all(caps.supports(cap) for cap in AdapterCapability)


def test_execute_and_rollout_validate_then_forward():
    http = _FakeHttp()
    adapter = _adapter(http)

    with pytest.raises(ValueError, match="execute joint action dim"):
        adapter.execute([0.0] * 7)  # 7 != joint 12
    assert http.posts == []

    action = np.arange(12, dtype=np.float64)
    adapter.execute(action)
    adapter.rollout(action)

    expected = action.tolist()
    assert adapter.executed == [expected]
    assert adapter.rollout_calls == 1
    assert http.posts == [
        (PATH_EXECUTE, {FIELD_ACTION: expected, FIELD_LAYOUT: "joint"}),
        (PATH_ROLLOUT, {FIELD_ACTION: expected, FIELD_LAYOUT: "joint"}),
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


def test_health_and_capture_status():
    http = _FakeHttp(
        {
            "/v1/health": {"ok": True, "detail": "", "control_hz": 30.0, "measured_hz": 29.8},
            "/v1/capture/status": {
                "running": True,
                "meta": {"operator": "Yu", "task_name": "put bowls", "description": "demo"},
                "data_dir": "/data/task",
            },
        }
    )
    adapter = _adapter(http)

    health = adapter.health()
    assert health.ok is True
    assert health.control_hz == 30.0  # 名义控制频率（robot env HZ）
    assert health.measured_hz == 29.8  # 实测控制线程帧率
    assert adapter.running is True
    capture = adapter.capture_status()
    assert capture is not None
    assert capture.running is True
    assert capture.meta == {"operator": "Yu", "task_name": "put bowls", "description": "demo"}
    assert capture.data_dir == "/data/task"


def test_observe_returns_none_until_first_frame(monkeypatch):
    class _Reader:
        def __init__(self, name):
            assert name == "dual_piper_obs"

        def read(self):
            return None

        def close(self):
            pass

    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _Reader)
    assert DualPiperAdapter().observe() is None


def test_observe_builds_edge_observation(monkeypatch):
    images = [np.full((8, 8, 3), channel, dtype=np.uint8) for channel in (40, 80, 120)]

    class _Reader:
        def __init__(self, name):
            pass

        def read(self):
            return {
                "qpos": _state_vector(np.arange(12, dtype=np.float64), _GRIPPER),
                "action": _state_vector(np.arange(12, dtype=np.float64) + 100, _GRIPPER),  # 与 qpos 可区分
                "images": images,
            }

        def close(self):
            pass

    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _Reader)
    obs = DualPiperAdapter().observe()

    assert obs is not None
    assert obs[KEY_QPOS].dtype == np.float32
    assert obs[KEY_ACTION].dtype == np.float32
    assert obs[KEY_QPOS].shape == (14,)  # 每臂「值 6 + 夹爪 1」× 2 臂
    # action 取 SHM 里进程侧的目标向量，而不是 qpos 副本（preview 显示真实指令）
    assert np.array_equal(obs[KEY_ACTION], _state_vector(np.arange(12, dtype=np.float32) + 100, _GRIPPER))
    for name in ("cam_head", "cam_left_wrist", "cam_right_wrist"):
        encoded = obs[f"{CAMERA_PREFIX}{name}"]
        assert isinstance(encoded, bytes)
        assert cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


def _camera_meta_payload(depth_cameras=("cam_head", "cam_left_wrist", "cam_right_wrist")) -> dict:
    """机器人进程 ``GET /v1/cameras`` 的响应体（三路相机；只有给定相机有深度）。"""
    cameras = []
    for name in ("cam_head", "cam_left_wrist", "cam_right_wrist"):
        has_depth = name in depth_cameras
        cameras.append(
            {
                "name": name,
                "width": 640,
                "height": 480,
                "intrinsics": {"fx": 600.0, "fy": 600.0, "cx": 320.0, "cy": 240.0},
                "depth": {"scale": 0.001, "aligned_to_color": True} if has_depth else None,
            }
        )
    return {FIELD_CAMERAS: cameras}


def _depth_reader(depths, images=None):
    """假 ObsShmReader：帧里带深度区（``None`` = 该帧没有 ``depths`` 键）。"""
    payload = {
        "qpos": _state_vector(np.arange(12, dtype=np.float64), _GRIPPER),
        "action": _state_vector(np.arange(12, dtype=np.float64), _GRIPPER),
        "images": images or [np.full((8, 8, 3), 40, dtype=np.uint8) for _ in range(3)],
    }
    if depths is not None:
        payload["depths"] = depths

    class _Reader:
        def __init__(self, name):
            pass

        def read(self):
            return payload

        def close(self):
            pass

    return _Reader


def test_observe_reads_depth_and_caches_camera_meta(monkeypatch):
    """机器人开了深度：观测带 ``observations/depth/<cam>``（uint16 原样透传，不经 JPEG）。"""
    depths = [np.full((8, 8), 1000 + index, dtype=np.uint16) for index in range(3)]
    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _depth_reader(depths))
    http = _FakeHttp({PATH_CAMERAS: _camera_meta_payload()})
    adapter = _adapter(http)

    obs = adapter.observe()

    assert obs is not None
    for index, name in enumerate(("cam_head", "cam_left_wrist", "cam_right_wrist")):
        depth = obs[f"{DEPTH_PREFIX}{name}"]
        assert depth.dtype == np.uint16 and int(depth[0, 0]) == 1000 + index
    # 相机元数据（内参 / 深度比例）惰性查一次并缓存：第二次 observe 不再发请求
    assert adapter.depth_camera_names() == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert adapter.camera_infos()["cam_head"]["depth"]["scale"] == 0.001
    adapter.observe()
    assert http.gets == [PATH_CAMERAS]


def test_observe_drops_depth_when_stream_count_mismatch(monkeypatch):
    """深度流路数与进程上报的深度相机数不符 → 本次不透传深度（不静默错位到别的相机名）。"""
    monkeypatch.setattr(
        "motrix_edge.adapter.http_shm_adapter.ObsShmReader",
        _depth_reader([np.full((8, 8), 1000, dtype=np.uint16)]),  # 只 1 路，但上报告知 3 路有深度
    )
    adapter = _adapter(_FakeHttp({PATH_CAMERAS: _camera_meta_payload()}))

    obs = adapter.observe()

    assert obs is not None
    assert not [key for key in obs if key.startswith(DEPTH_PREFIX)]  # 路数不符：宁可不给
    assert f"{CAMERA_PREFIX}cam_head" in obs  # 图像不受影响


def test_observe_without_camera_metadata_keeps_images(monkeypatch):
    """旧版进程没有 ``/v1/cameras``（或查询失败）：深度不可用，但图像 / 状态观测照常。"""
    monkeypatch.setattr(
        "motrix_edge.adapter.http_shm_adapter.ObsShmReader",
        _depth_reader([np.full((8, 8), 1000, dtype=np.uint16)]),
    )
    adapter = _adapter(_FakeHttp())  # 元数据端点无内容

    obs = adapter.observe()

    assert obs is not None
    assert adapter.camera_infos() == {}
    assert adapter.depth_camera_names() == []
    assert not [key for key in obs if key.startswith(DEPTH_PREFIX)]
    assert f"{CAMERA_PREFIX}cam_head" in obs


def test_depth_follows_enabled_cameras(monkeypatch):
    """``configure()`` 裁剪相机后：只给启用相机发布深度（与图像同一口径）。"""
    depths = [np.full((8, 8), 1000 + index, dtype=np.uint16) for index in range(3)]
    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _depth_reader(depths))
    adapter = _adapter(_FakeHttp({PATH_CAMERAS: _camera_meta_payload()}))
    adapter.configure(enabled_cameras=["cam_head"])

    obs = adapter.observe()

    assert obs is not None
    assert [key for key in obs if key.startswith(DEPTH_PREFIX)] == [f"{DEPTH_PREFIX}cam_head"]
    assert [key for key in obs if key.startswith(CAMERA_PREFIX)] == [f"{CAMERA_PREFIX}cam_head"]


def _pose_reader(pose, pose_dim, images=None):
    """假 ObsShmReader：带 header 的 pose_dim 与可选位姿区（模拟机器人是否上位姿区）。"""
    payload = {
        "qpos": _state_vector(np.arange(12, dtype=np.float64), _GRIPPER),
        "action": _state_vector(np.arange(12, dtype=np.float64), _GRIPPER),
        "images": images or [np.full((8, 8, 3), 40, dtype=np.uint8) for _ in range(3)],
    }
    if pose is not None:
        payload["pose"] = np.asarray(pose, dtype=np.float64)

    class _Reader:
        def __init__(self, name):
            self.pose_dim = pose_dim  # header 里声明的布局（观察量，不决定能力）

        def read(self):
            return payload

        def close(self):
            pass

    return _Reader


def test_pose_dim_is_declared_not_probed(monkeypatch):
    """位姿能力由**声明**给出（不随 header 漂移）：每臂 6 维、双臂 12 维、观测键里有 pose。"""
    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _pose_reader(None, 12))
    adapter = DualPiperAdapter()
    adapter.observe()

    assert adapter.effective_pose_dim_per_arm() == 6
    assert adapter.pose_dim == 12
    assert KEY_POSE in adapter.capabilities.observation_keys
    assert DualPiperAdapter().effective_pose_dim_per_arm() == 6  # 未 attach 同理


def test_observe_keeps_valid_end_effector_pose(monkeypatch):
    """机器人上报合理位姿（米 / 弧度）→ 按启用臂切片透传（笛卡尔原语与 settle 靠它）。"""
    pose = np.array([0.3, -0.1, 0.25, 0.0, 0.0, 0.2, 0.3, 0.1, 0.25, 0.0, 0.0, -0.2])
    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _pose_reader(pose, 12))
    obs = DualPiperAdapter().observe()

    assert obs is not None and KEY_POSE in obs
    assert np.allclose(obs[KEY_POSE], pose.astype(np.float32))


def test_observe_drops_pose_with_wrong_units(monkeypatch):
    """量纲写错（如 0.001mm 整数当米）→ 丢弃该拍位姿，不让垃圾值进闭环。"""
    bad = np.array([123456, -78901, 5000, 0, 0, 0, 1, 2, 3, 0, 0, 0], dtype=float)
    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _pose_reader(bad, 12))
    obs = DualPiperAdapter().observe()

    assert obs is not None and KEY_POSE not in obs  # qpos / 图像不受影响
    assert obs[KEY_QPOS].shape == (14,)


def test_observe_drops_pose_with_wrong_shape(monkeypatch):
    """位姿长度与（臂数 × 每臂维数）不符 → 丢弃（不猜布局）。"""
    monkeypatch.setattr(
        "motrix_edge.adapter.http_shm_adapter.ObsShmReader",
        _pose_reader(np.zeros(6), 12),  # 声称 12 维却只给了 6 维
    )
    assert KEY_POSE not in DualPiperAdapter().observe()


def test_pose_frame_declares_flange():
    """读（位姿观测）与写（笛卡尔目标）必须同系：dual piper 声明 flange，供上游判断语义。"""
    assert DualPiperAdapter.POSE_FRAME == "flange"


def test_pose_layout_is_advertised_and_forwarded():
    """四个空间都已接入：宣称 joint / pose / pose_delta / gripper，且 rollout / execute 的 body 带 ``layout``。

    joint / pose / pose_delta 扁平维度相同，漏传就是静默误解释。
    """
    http = _FakeHttp()
    adapter = _adapter(http)
    assert adapter.capabilities.action_spaces == ["joint", "pose", "pose_delta", "gripper"]
    assert adapter.normalize_action_space("pose") is ActionSpace.POSE

    action = np.arange(12, dtype=np.float64)
    adapter.rollout(action, layout="pose")
    adapter.execute(action, layout="pose")

    assert http.posts == [
        (PATH_ROLLOUT, {FIELD_ACTION: action.tolist(), FIELD_LAYOUT: "pose"}),
        (PATH_EXECUTE, {FIELD_ACTION: action.tolist(), FIELD_LAYOUT: "pose"}),
    ]


def test_gripper_layout_is_forwarded():
    """夹爪布局：每臂 1 维，同样带 layout 下发（机器人只写夹爪段）。"""
    http = _FakeHttp()
    adapter = _adapter(http)

    adapter.execute(np.array([0.2, 0.9]), layout="gripper")

    assert http.posts == [(PATH_EXECUTE, {FIELD_ACTION: [0.2, 0.9], FIELD_LAYOUT: "gripper"})]


def test_pose_requires_all_arms_enabled():
    """臂裁剪下位姿动作被拒：未启用臂用 ``HOME['joint']``（关节值）填充，不能当位姿下发。"""
    http = _FakeHttp()
    adapter = _adapter(http)
    adapter.configure(enabled_arms=["right"])

    with pytest.raises(ValueError, match="requires all arms"):
        adapter.rollout(np.zeros(adapter.action_dim), layout="pose")
    with pytest.raises(ValueError, match="requires all arms"):
        adapter.execute(np.zeros(adapter.action_dim), layout="pose")
    assert http.posts == []  # 守卫在发送之前


def test_observe_rejects_camera_count_mismatch(monkeypatch):
    """进程侧相机路数 ≠ 类常量 IMAGES 时显式报错（而不是静默少一路相机）。"""
    images = [np.full((8, 8, 3), 40, dtype=np.uint8) for _ in range(2)]  # 进程少一路

    class _Reader:
        def __init__(self, name):
            pass

        def read(self):
            return {
                "qpos": np.arange(12, dtype=np.float64),
                "gripper": np.array([0.5, 0.8]),
                "action": np.arange(12, dtype=np.float64),
                "images": images,
            }

        def close(self):
            pass

    monkeypatch.setattr("motrix_edge.adapter.http_shm_adapter.ObsShmReader", _Reader)
    with pytest.raises(ValueError):
        DualPiperAdapter().observe()


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
