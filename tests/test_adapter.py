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

"""adapter 包测试 —— RobotAdapter 契约 + discover 驱动发现（不启动 SDK 进程）。

覆盖：discover_adapter（无进程 → None / 解析自描述）、get_adapter（按 discovered
实例化 / 未知类型报错 / 能力校验）、adapter_details（静态列出全部注册适配器）、
capabilities 能力声明、health / ready。机器人 SDK 进程（HTTP + 共享内存）集成不在常规
测试套件内。
"""

from types import SimpleNamespace

import pytest
from fake_robot import FakeRobotAdapter

from motrix_edge.adapter import (
    adapter_details,
    discover_adapter,
    get_adapter,
    robot_adapters,
)
from motrix_edge.adapter import test_adapter as test_adapter_mod
from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_QPOS,
    AdapterCapability,
    DiscoveredRobot,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_TELEOP_ENABLED,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_EXECUTE,
    PATH_TELEOP,
)

# 一个标准的机器人进程 discover 响应 robot 块（**只含身份** name / type）
_ROBOT_DICT = {
    "name": "Test Robot",
    "type": "test_robot",
    "running": True,  # 探活用：进程可达但未运行 → 未发现
}


# ---- discover_adapter（向固定端口发 discover）--------------------------------


def make_discovered(**overrides) -> DiscoveredRobot:
    """构造一个机器人进程身份（DiscoveredRobot，只含 name / type）。"""
    base = {
        "name": "Test Robot",
        "type": "test_robot",
    }
    base.update(overrides)
    return DiscoveredRobot(**base)


def _patch_discover_client(monkeypatch, payload=None, error=None):
    """把 discover_adapter 里的 ``httpx.Client`` 替换为桩（返回 canned 响应或抛错）。"""

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    class _Client:
        def __init__(self, *a, **k):
            self._payload = payload
            self._error = error

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            if self._error is not None:
                raise self._error
            return _Resp(self._payload)

    monkeypatch.setattr("motrix_edge.adapter.httpx", SimpleNamespace(Client=_Client))


def test_discover_adapter_returns_none_when_no_process(monkeypatch):
    """discover_adapter(host, port)：进程不可达（网络错误）→ None（节点持续重试）。"""
    _patch_discover_client(monkeypatch, error=OSError("connection refused"))
    assert discover_adapter(host="127.0.0.1", port=1) is None


def test_discover_adapter_instantiates_adapter(monkeypatch):
    """discover_adapter(host, port)：发现 + 实例化一步完成，返回实例化后的 adapter。"""
    _patch_discover_client(monkeypatch, payload={"status": "accepted", "robot": _ROBOT_DICT})
    adapter = discover_adapter(host="127.0.0.1", port=8090)
    assert adapter is not None
    assert isinstance(adapter, test_adapter_mod.TestRobotAdapter)
    assert adapter.name == "Test Robot"  # 身份来自 discover（name 展示 / type 实例化）
    assert adapter.type == "test_robot"


def test_discover_adapter_none_when_not_running(monkeypatch):
    """discover_adapter(host, port)：进程可达但未运行 → None。"""
    robot = dict(_ROBOT_DICT, running=False)
    _patch_discover_client(monkeypatch, payload={"status": "accepted", "robot": robot})
    assert discover_adapter(host="127.0.0.1", port=8090) is None


def test_discover_adapter_none_when_unknown_type(monkeypatch):
    """discover_adapter(host, port)：discover 返回未注册 type → None（持续重试）。"""
    robot = dict(_ROBOT_DICT, type="no_such_adapter")
    _patch_discover_client(monkeypatch, payload={"status": "accepted", "robot": robot})
    assert discover_adapter(host="127.0.0.1", port=8090) is None


# ---- adapter_details（静态列出全部注册适配器，与 discover 无关）---------------


def test_adapter_details_lists_all_registered():
    """adapter_details：静态列出全部注册适配器（不 discover；缺 SDK / 导入失败跳过）。

    只列 type / available / capabilities（id / name 由 discover 赋予，静态列表不列）。
    """
    details = adapter_details()
    assert len(details) >= 1
    info = next(d for d in details if d["type"] == "test_robot")
    assert info["type"] == "test_robot"
    assert "id" not in info and "name" not in info  # id / name 非静态列表项
    assert info["available"] is True
    caps = info["capabilities"]
    assert caps["action_dim"] == 14
    assert caps["image_names"] == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert caps["capabilities"]["capture"] is True
    assert caps["capabilities"]["execute"] is True


# ---- FakeRobotAdapter 契约（health / ready）----------------------------------


def test_health_reflects_availability():
    """health / ready：机器人不可用（available=False）→ 不健康、未就绪。"""
    adapter = FakeRobotAdapter(available=False)
    assert adapter.health().ok is False
    assert adapter.ready is False


# ---- get_adapter（discover 结果参数化）----------------------------------------


def test_get_adapter_parameterized_by_discovered():
    """get_adapter(discovered)：按进程 type 实例化，身份来自 discover、能力来自类常量。"""
    adapter = get_adapter(make_discovered())
    assert isinstance(adapter, test_adapter_mod.TestRobotAdapter)
    assert isinstance(adapter, RobotAdapter)
    assert adapter.name == "Test Robot"  # 身份来自 discover（name 展示 / type 实例化）
    assert adapter.type == "test_robot"
    # 能力与连接参数来自类级常量（不随 discover 传输）
    assert adapter.action_dim == 14
    assert adapter.images == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert adapter.sdk_url == "http://127.0.0.1:8090"
    assert adapter.shm_name == "test_robot_obs"


def test_get_adapter_requires_discovered():
    """get_adapter 只做实例化：须传 discovered（discover 由调用方完成）。"""
    with pytest.raises(TypeError):
        get_adapter()  # type: ignore[call-arg]


def test_get_adapter_unknown_type_raises():
    """discover 返回未注册的 type → ValueError。"""
    discovered = make_discovered(type="no_such_adapter")
    with pytest.raises(ValueError):
        get_adapter(discovered)


def test_capabilities_declares_action_dim_and_observation_keys():
    adapter = get_adapter(make_discovered())
    caps = adapter.capabilities
    assert caps.action_dim == 14
    assert caps.observation_keys == [
        KEY_QPOS,
        f"{CAMERA_PREFIX}cam_head",
        f"{CAMERA_PREFIX}cam_left_wrist",
        f"{CAMERA_PREFIX}cam_right_wrist",
    ]
    assert caps.image_names == ["cam_head", "cam_left_wrist", "cam_right_wrist"]


def test_capabilities_dict_and_supports():
    adapter = get_adapter(make_discovered())
    caps = adapter.capabilities
    assert caps.capabilities[AdapterCapability.CAPTURE] is True
    assert caps.capabilities[AdapterCapability.EXECUTE] is True
    assert caps.supports(AdapterCapability.CAPTURE) is True
    assert caps.supports(AdapterCapability.EXECUTE) is True
    # 未声明的能力 → False（supports 缺省为 False）
    assert RobotCapabilities(capabilities={}).supports(AdapterCapability.CAPTURE) is False


def test_fallback_when_no_discovered():
    """无 discover（进程内测试）→ 回退类级常量身份 / 能力。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    assert adapter.name == "test_robot"
    assert adapter.capabilities.robot_model_id == "test-robot"
    assert adapter.capabilities.robot_model_version == "0.0.0"
    assert adapter.action_dim == 14
    assert adapter.images == ["cam_head", "cam_left_wrist", "cam_right_wrist"]
    assert adapter.sdk_url == "http://127.0.0.1:8090"


def test_robot_adapters_lists_entry_points():
    names = {t for t, _, _ in robot_adapters()}
    assert names >= {"test_robot", "dual_piper"}


def test_get_adapter_with_required_capability():
    # TestRobotAdapter 同时支持采集 + 执行 → 两个能力都能通过校验
    cap = get_adapter(make_discovered(), required_capability=AdapterCapability.CAPTURE)
    assert isinstance(cap, RobotAdapter)
    exc = get_adapter(make_discovered(), required_capability=AdapterCapability.EXECUTE)
    assert isinstance(exc, RobotAdapter)


def test_robot_adapters_filters_by_capability():
    expected = {"test_robot", "dual_piper"}
    assert expected <= {t for t, _, _ in robot_adapters(AdapterCapability.CAPTURE)}
    assert expected <= {t for t, _, _ in robot_adapters(AdapterCapability.EXECUTE)}
    assert expected <= {t for t, _, _ in robot_adapters(AdapterCapability.STREAMING)}


# ---- execute（校验维度再发送）------------------------------------------------


class _FakeHttp:
    """adapter 的 HTTP 客户端桩：记录 post 调用（不真实发送）。"""

    def __init__(self):
        self.posts = []

    def post(self, url, json=None):
        self.posts.append((url, json))


def _exec_adapter():
    """构造已注入 fake HTTP 客户端的 TestRobotAdapter（execute 转发用）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter._http = _FakeHttp()
    return adapter


def test_execute_validates_dimension_before_sending():
    """execute：维度不符 → 抛 ValueError（不发送 HTTP）。"""
    adapter = _exec_adapter()
    with pytest.raises(ValueError, match="execute action dim"):
        adapter.execute([0.0] * 5)  # 5 != action_dim 14
    assert adapter._http.posts == []  # 未发送


def test_execute_sends_after_dimension_check():
    """execute：维度正确 → 本地记录 + HTTP 转发（action 为 float64 list）。"""
    adapter = _exec_adapter()
    qpos = [0.0] * adapter.action_dim
    adapter.execute(qpos)
    assert adapter.executed == [qpos]  # 本地记录（供测试断言）
    assert len(adapter._http.posts) == 1
    url, body = adapter._http.posts[0]
    assert url == PATH_EXECUTE
    assert body == {FIELD_ACTION: qpos}


# ---- teleop（遥操作开关）----------------------------------------------------


def test_set_teleop_forwards_to_sdk():
    """set_teleop：本地记录 + HTTP 转发 SDK /v1/teleop（enabled 为 bool）。"""
    adapter = _exec_adapter()
    adapter.set_teleop(True)
    assert adapter.teleop_enabled is True  # 本地回显
    assert len(adapter._http.posts) == 1
    url, body = adapter._http.posts[0]
    assert url == PATH_TELEOP
    assert body == {FIELD_TELEOP_ENABLED: True}

    adapter.set_teleop(False)
    assert adapter.teleop_enabled is False
    assert adapter._http.posts[-1] == (PATH_TELEOP, {FIELD_TELEOP_ENABLED: False})


# ---- capture episode（采集回合控制）------------------------------------------


def test_start_end_capture_forwards_to_sdk():
    """start_capture / end_capture：HTTP 转发 SDK /v1/capture/start、/v1/capture/end。"""
    adapter = _exec_adapter()
    adapter.start_capture()
    adapter.end_capture()
    assert [p[0] for p in adapter._http.posts] == [PATH_CAPTURE_START, PATH_CAPTURE_END]
