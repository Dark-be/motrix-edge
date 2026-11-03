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

"""源头时间 → 标准观测 → MCAP 的逐话题时间回归。"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from test_machine_profile import _load


def test_realsense_timestamps_before_wait_and_preserves_frame_group(monkeypatch):
    module = _load("robot.sensor.realsense_sensor")
    sensor = module.RealsenseSensor("head")
    sensor.enable_depth = True
    order = []
    stamp = 1_790_000_000_123_456_789

    def now():
        order.append("clock")
        return stamp

    color = Mock(
        get_data=lambda: np.zeros((2, 3, 3), dtype=np.uint8),
        get_frame_timestamp_domain=Mock(side_effect=RuntimeError("unavailable")),
    )
    depth = Mock(
        get_data=lambda: np.ones((2, 3), dtype=np.uint16),
        get_frame_timestamp_domain=Mock(side_effect=RuntimeError("unavailable")),
    )

    def wait():
        order.append("wait")
        return Mock(get_color_frame=lambda: color, get_depth_frame=lambda: depth)

    sensor.pipeline = Mock(wait_for_frames=wait)
    monkeypatch.setattr(module.time, "time_ns", now)
    info = sensor.get_information()
    assert order == ["clock", "wait"]
    assert info["timestamp_ns"] == stamp
    assert info["color"].shape == (2, 3, 3) and info["depth"].shape == (2, 3)


@pytest.mark.parametrize("domain", ["global_time", "system_time", "hardware_clock"])
def test_realsense_device_clock_domains_and_exposure_metadata(monkeypatch, domain):
    module = _load("robot.sensor.realsense_sensor")
    monkeypatch.setattr(
        module.rs,
        "timestamp_domain",
        SimpleNamespace(global_time="global_time", system_time="system_time", hardware_clock="hardware_clock"),
        raising=False,
    )
    monkeypatch.setattr(
        module.rs,
        "frame_metadata_value",
        SimpleNamespace(
            frame_timestamp="frame_timestamp", sensor_timestamp="sensor_timestamp", actual_exposure="actual_exposure"
        ),
        raising=False,
    )
    values = dict(frame_timestamp=10000, sensor_timestamp=9500, actual_exposure=400)
    frame = Mock(
        get_frame_timestamp_domain=lambda: domain,
        get_timestamp=lambda: 1790000000123.25,
        supports_frame_metadata=lambda field: True,
        get_frame_metadata=values.__getitem__,
    )
    stamp, metadata = module.RealsenseSensor._frame_time(frame, 123)
    assert stamp == (123 if domain == "hardware_clock" else round(1790000000123.25 * 1_000_000))
    assert metadata["sensor_timestamp_us"] == 9500
    assert metadata["actual_exposure_us"] == 400
    assert metadata["timestamp_domain"] == domain


def test_realsense_reads_independent_stream_times_before_alignment(monkeypatch):
    module = _load("robot.sensor.realsense_sensor")
    sensor = module.RealsenseSensor("head")
    sensor.enable_depth = True
    color = Mock(get_data=lambda: np.zeros((2, 3, 3), dtype=np.uint8))
    depth = Mock(get_data=lambda: np.ones((2, 3), dtype=np.uint16))
    frames = Mock(get_color_frame=lambda: color, get_depth_frame=lambda: depth)
    order = []

    def frame_time(frame, fallback):
        order.append("color" if frame is color else "depth")
        return (100 if frame is color else 200), {"source": "global_time"}

    monkeypatch.setattr(sensor, "_frame_time", frame_time)
    sensor.pipeline = Mock(wait_for_frames=lambda: frames)
    sensor.align = Mock(process=lambda f: order.append("align") or frames)
    info = sensor.get_information()
    assert order == ["color", "depth", "align"]
    assert info["timestamps_ns"] == {"color": 100, "depth": 200}
    assert info["timestamp_ns"] == 100


def test_state_and_each_camera_keep_independent_sampling_times(monkeypatch):
    module = _load("robot.dual_piper_robot")
    base = _load("robot.base_robot")
    robot = module.DualPiperRobot(
        {
            "init_joint": [0.0] * 12,
            "init_gripper": [1.0, 1.0],
            "ports": {key: key for key in module.DualPiperRobot.PORT_ROLES},
            "cameras": {key: key for key in module.DualPiperRobot.IMAGE_NAMES},
        }
    )
    robot.controllers = {key: Mock(get_joint=lambda: np.zeros(6), get_gripper=lambda: 0.5) for key in robot.controllers}
    times = iter([1001, 1002, 1003])
    monkeypatch.setattr(base.time, "time_ns", lambda: next(times))
    robot.sample_state()
    state_times = robot.motion_state["timestamps_ns"].copy()
    for index, name in enumerate(robot.IMAGE_NAMES):
        robot.sensors[name] = Mock(
            pixel_format="raw",
            get_information=Mock(
                return_value={
                    "color": np.zeros((2, 3, 3), dtype=np.uint8),
                    "depth": np.ones((2, 3), dtype=np.uint16),
                    "timestamp_ns": 2001 + index,
                    "timestamps_ns": {"color": 2001 + index, "depth": 3001 + index},
                    "timestamp_metadata": {"color": {"source": "global_time"}},
                }
            ),
        )
    obs = robot.standard_observation(robot.build_observation())
    stamps = obs["timestamps_ns"]
    assert stamps["observations/qpos"] == stamps["observations/pose"] == 1001
    assert stamps["action"] == stamps["observations/pose_action"] == 1002
    for index, name in enumerate(robot.IMAGE_NAMES):
        assert stamps[f"observations/images/{name}"] == 2001 + index
        assert stamps[f"observations/depth/{name}"] == 3001 + index
        assert obs["timestamp_metadata"][f"observations/images/{name}"]["source"] == "global_time"
    assert robot.motion_state["timestamps_ns"] == state_times  # 合并不能污染控制线程缓存


def test_mcap_uses_exact_topic_nanoseconds_and_image_headers(tmp_path):
    pytest.importorskip("mcap_ros2")
    from mcap.reader import make_reader
    from mcap_ros2.decoder import DecoderFactory

    collector = _load("collector.mcap_collector").ActMcapCollector({"save_dir": str(tmp_path)})
    t = 1_790_000_000_123_456_789  # float 秒无法无损表示的纳秒尾数
    stamps = {
        "observations/qpos": t,
        "action": t + 17,
        "observations/images/head": t + 1001,
        "observations/images/wrist": t + 2002,
    }
    obs = {
        "observations/qpos": [0.1] * 14,
        "action": [0.2] * 14,
        "timestamp": 1.0,
        "timestamps_ns": stamps,
        "observations/images/head": np.zeros((2, 3, 3), dtype=np.uint8),
        "observations/images/wrist": np.zeros((2, 3, 3), dtype=np.uint8),
    }
    collector.start()
    collector.collect(obs)
    # 下一拍缓存更旧的状态：保留其时间，不按写入顺序重排 / 钳制。
    collector.collect({**obs, "timestamps_ns": {key: val - 100 for key, val in stamps.items()}})
    path = collector.finish()
    with path.open("rb") as stream:
        messages = list(
            make_reader(stream, decoder_factories=[DecoderFactory()]).iter_decoded_messages(log_time_order=False)
        )
    assert len(messages) == 8
    for _, channel, message, decoded in messages:
        expected = stamps[channel.topic] - 100 * message.sequence
        assert message.log_time == message.publish_time == expected
        if channel.topic.startswith("observations/images/"):
            assert decoded.header.stamp.sec * 1_000_000_000 + decoded.header.stamp.nanosec == expected
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["duration"] == pytest.approx(2102 / 1e9)


def test_legacy_and_partial_timestamp_fallback(monkeypatch):
    module = _load("collector.mcap_collector")
    collector = module.ActMcapCollector({})
    collector._writer = Mock()
    monkeypatch.setattr(module.time, "time_ns", lambda: 9999)
    obs = {"observations/qpos": [0], "action": [1], "timestamp": 2.5, "timestamps_ns": {"action": 1234}}
    collector.collect(obs)
    calls = collector._writer.write_message.call_args_list
    assert [call.kwargs["log_time"] for call in calls] == [2_500_000_000, 1234]
    collector._writer.reset_mock()
    collector.collect({"observations/qpos": [0], "action": [1]})
    assert all(call.kwargs["log_time"] == 9999 for call in collector._writer.write_message.call_args_list)


@pytest.mark.parametrize("bad", [-1, True, None, 1.5, float("nan"), "123", 2**64])
def test_invalid_explicit_timestamp_rejected_before_writing(bad):
    module = _load("collector.mcap_collector")
    collector = module.ActMcapCollector({})
    collector._writer = Mock()
    with pytest.raises(ValueError):
        collector.collect({"observations/qpos": [0], "action": [1], "timestamps_ns": {"action": bad}})
    collector._writer.write_message.assert_not_called()


def test_cv2_timestamps_before_read(monkeypatch):
    module = _load("robot.sensor.cv2_sensor")
    sensor = module.Cv2Sensor("wrist")
    order = []

    def now():
        order.append("clock")
        return 12345

    def read():
        order.append("read")
        return True, np.zeros((2, 3, 3), dtype=np.uint8)

    sensor.cap = Mock(isOpened=lambda: True, read=read)
    monkeypatch.setattr(module.time, "time_ns", now)
    assert sensor.get_information()["timestamp_ns"] == 12345
    assert order == ["clock", "read"]


def test_v4l2_timestamp_precedes_wait(monkeypatch):
    module = _load("robot.sensor.v4l2_sensor")
    sensor = module.V4l2Sensor("wrist")
    sensor.fd = 10
    order = []

    def now():
        order.append("clock")
        return 12345

    def select(*args):
        order.append("wait")
        return [], [], []

    monkeypatch.setattr(module.time, "time_ns", now)
    monkeypatch.setattr(module.select, "select", select)
    info = sensor.get_information()
    assert info["timestamp_ns"] == 12345 and info["color"] is None
    assert order == ["clock", "wait"]


def test_realsense_enables_global_time_for_supported_sensors(monkeypatch):
    module = _load("robot.sensor.realsense_sensor")
    supported = Mock(supports=lambda option: True)
    unsupported = Mock(supports=lambda option: False)
    device = Mock(get_info=lambda info: "serial", query_sensors=lambda: [supported, unsupported])
    fake_rs = SimpleNamespace(
        context=lambda: Mock(query_devices=lambda: [device]),
        camera_info=SimpleNamespace(serial_number="serial_number"),
        option=SimpleNamespace(global_time_enabled="global_time_enabled"),
        pipeline=Mock,
        config=Mock,
        stream=SimpleNamespace(color="color"),
        format=SimpleNamespace(bgr8="bgr8"),
    )
    monkeypatch.setattr(module, "rs", fake_rs)
    module.RealsenseSensor("head").connect("serial")
    supported.set_option.assert_called_once_with("global_time_enabled", 1)
    unsupported.set_option.assert_not_called()
