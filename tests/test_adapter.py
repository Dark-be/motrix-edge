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

import numpy as np
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


# ---------------------------------------------------------------------------
# configure（RobotAdapter 基类能力）：启用臂 / 相机（home 由类常量 HOME_QPOS 定义）—— 通用、无实机
# ---------------------------------------------------------------------------


def test_configure_default_enables_all_arms():
    """缺省（不配置）：启用全部臂，action_dim = 14，动作直发。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    assert adapter.enabled_arms == ["left", "right"]
    assert adapter.action_dim == 14
    assert adapter.capabilities.action_dim == 14


def test_configure_right_arm_changes_dim_and_expands():
    """只启用右臂：action_dim=7；execute 7 维动作展开回 14 维，左臂 home 填充。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["right"])
    assert adapter.enabled_arms == ["right"]
    assert adapter.action_dim == 7
    assert adapter.capabilities.action_dim == 7

    adapter.execute(np.arange(7, dtype=np.float64))
    expected = [0.0] * 7 + list(range(7))  # 左臂 home（0），右臂动作 [7:14]
    assert adapter.executed == [expected]
    assert adapter._http.posts == [(PATH_EXECUTE, {FIELD_ACTION: expected})]
    # 维度校验：7 维之外拒绝（不发送）
    with pytest.raises(ValueError, match="execute action dim"):
        adapter.execute([0.0] * 14)
    assert len(adapter._http.posts) == 1


def test_configure_left_arm_expands_with_home():
    """只启用左臂：动作放 [0:7]，右臂用类常量 HOME_QPOS 填充。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["left"])
    adapter.execute(np.ones(7))
    # TestRobotAdapter 缺省 HOME_QPOS = 全 0
    assert adapter.executed == [[1.0] * 7 + [0.0] * 7]  # 左臂 1.0，右臂 home 0.0


def test_configure_both_arms_keeps_14_dim_passthrough():
    """全臂启用：action_dim=14，动作直发（home 不影响全启用）。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["left", "right"])
    assert adapter.action_dim == 14
    adapter.execute(np.arange(14, dtype=np.float64))
    assert adapter.executed == [list(np.arange(14, dtype=np.float64))]


def test_configure_cameras_subset():
    """只启用部分相机：capabilities / observe 只暴露启用相机。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter.configure(enabled_cameras=["cam_head", "cam_right_wrist"])
    assert adapter.images == ["cam_head", "cam_right_wrist"]
    assert adapter.capabilities.image_names == ["cam_head", "cam_right_wrist"]


def test_configure_validation_errors_atomic():
    """非法配置（未知臂 / 空臂 / 未知相机）：ValueError 且不改状态。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    with pytest.raises(ValueError, match="unknown arm"):
        adapter.configure(enabled_arms=["both"])
    with pytest.raises(ValueError, match="empty"):
        adapter.configure(enabled_arms=[])
    with pytest.raises(ValueError, match="unknown camera"):
        adapter.configure(enabled_cameras=["cam_nope"])
    # 状态未被污染：仍为缺省双臂
    assert adapter.action_dim == 14
    assert adapter.enabled_arms == ["left", "right"]


def test_configure_preserves_physical_arm_order():
    """enabled_arms 顺序无关：归一化为物理顺序（left → right）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter.configure(enabled_arms=["right", "left"])
    assert adapter.enabled_arms == ["left", "right"]
    assert adapter.action_dim == 14


def test_select_qpos_picks_enabled_arm_dims():
    """基类 _select_qpos：按启用臂物理顺序挑选 / 拼接 qpos。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    qpos = np.arange(14, dtype=np.float32)
    assert np.array_equal(adapter._select_qpos(qpos), qpos)  # 全臂 → 原样
    adapter.configure(enabled_arms=["right"])
    assert np.array_equal(adapter._select_qpos(qpos), qpos[7:14])
    adapter.configure(enabled_arms=["left"])
    assert np.array_equal(adapter._select_qpos(qpos), qpos[0:7])


def test_expand_action_uses_home_for_disabled_arm():
    """基类 _expand_action：未启用臂用类常量 HOME_QPOS 填充。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter.configure(enabled_arms=["right"])
    full = adapter._expand_action(np.ones(7), "rollout")
    # TestRobotAdapter 缺省 HOME_QPOS = 全 0
    assert np.array_equal(full, np.array([0.0] * 7 + [1.0] * 7))


# ---- node：_probe_adapter 应用运行时 adapter 配置（命令 / 前端设置，非 edge.yml）----


def test_node_probe_applies_adapter_config(monkeypatch):
    from motrix_edge.node import EdgeNode

    inner = test_adapter_mod.TestRobotAdapter(name="Test Robot")

    def fake_discover(host, port, required_capability=None):
        return inner

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)
    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    # 运行时配置（adapter config set / 前端 POST /v1/adapters/config），不读 edge.yml
    assert node._apply_adapter_config({"enabled_arms": ["right"]})
    node._last_probe = 0.0
    node._probe_adapter()
    assert node.adapter is inner  # 复用同一进程 / 同一 adapter，不新建
    assert inner.action_dim == 7
    assert inner.enabled_arms == ["right"]


def test_node_probe_invalid_config_does_not_bind(monkeypatch):
    from motrix_edge.node import EdgeNode

    inner = test_adapter_mod.TestRobotAdapter(name="Test Robot")

    def fake_discover(host, port, required_capability=None):
        return inner

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)
    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    # 无 adapter 绑定时设置非法配置（仅存状态）；discover 绑定时 configure 校验失败 → 不绑定
    assert node._apply_adapter_config({"enabled_arms": ["both"]})
    node._last_probe = 0.0
    node._probe_adapter()
    assert node.adapter is None  # 配置非法：不绑定，等待重试


def test_adapter_config_command_query_and_set():
    """adapter config / adapter config set <json>：查询与设置运行时 adapter 配置。"""
    from motrix_edge.node import EdgeNode
    from motrix_edge.utils.commands import build_command_registry

    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    registry = build_command_registry()

    # 查询（初始为空）
    replies = []
    cmd = registry.parse_argv(["adapter", "config"])
    cmd.reply_to = replies.append
    node._dispatch(cmd)
    assert replies[0].status == "ok"
    assert replies[0].data == {}
    assert node.adapter_config == {}

    # 设置（无 adapter 绑定：仅存状态）
    replies2 = []
    cmd2 = registry.parse_argv(["adapter", "config", "set", '{"enabled_arms": ["right"]}'])
    cmd2.reply_to = replies2.append
    node._dispatch(cmd2)
    assert replies2[0].status == "ok"
    assert replies2[0].data["enabled_arms"] == ["right"]
    assert node.adapter_config["enabled_arms"] == ["right"]

    # 非法 JSON → rejected（400）
    replies3 = []
    cmd3 = registry.parse_argv(["adapter", "config", "set", "not-json"])
    cmd3.reply_to = replies3.append
    node._dispatch(cmd3)
    assert replies3[0].status == "rejected"
    assert replies3[0].status_code == 400
    # 状态未被污染
    assert node.adapter_config["enabled_arms"] == ["right"]


def test_adapter_config_current_reports_effective():
    """adapter config current：返回当前绑定 adapter 实际生效的启用臂 / 相机 / 动作维度 / home。"""
    from motrix_edge.node import EdgeNode
    from motrix_edge.utils.commands import build_command_registry

    inner = test_adapter_mod.TestRobotAdapter(name="Test Robot")
    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    node.adapter = inner
    node.adapter_name = "Test Robot"
    node.adapter_type = "test_robot"
    inner.configure(enabled_arms=["right"], enabled_cameras=["cam_head"])

    registry = build_command_registry()
    replies = []
    cmd = registry.parse_argv(["adapter", "config", "current"])
    cmd.reply_to = replies.append
    node._dispatch(cmd)

    assert replies[0].status == "ok"
    data = replies[0].data
    assert data["adapter"] == {"name": "Test Robot", "type": "test_robot"}
    # 能力启用字典（configure 应用后）：只启用 right 臂 + cam_head 相机
    assert data["enabled"]["arms"].get("right") is True
    assert data["enabled"]["arms"].get("left") is False
    assert data["enabled"]["cameras"].get("cam_head") is True
    assert data["action_dim"] == 7
    assert data["home_qpos"] == [0.0] * 14  # TestRobotAdapter 缺省 HOME_QPOS 全 0


def test_adapter_config_current_without_adapter_returns_default():
    """adapter config current：未绑定 adapter → 回退包内默认 adapter 的默认配置（default=True）。"""
    from motrix_edge.node import EdgeNode
    from motrix_edge.utils.commands import build_command_registry

    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    registry = build_command_registry()
    replies = []
    cmd = registry.parse_argv(["adapter", "config", "current"])
    cmd.reply_to = replies.append
    node._dispatch(cmd)
    assert replies[0].status == "ok"
    data = replies[0].data
    assert data["default"] is True  # 未绑定 → 默认配置标记
    assert data["enabled"]["arms"]
    assert data["enabled"]["cameras"]
