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
    KEY_ACTION,
    KEY_GRIPPER,
    KEY_POSE,
    KEY_POSE_TARGET,
    KEY_QPOS,
    AdapterCapability,
    DiscoveredRobot,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.adapter.http_contract import (
    FIELD_ACTION,
    FIELD_TELEOP_ENABLED,
    FIELD_TELEOP_MODE,
    PATH_CAPTURE_END,
    PATH_CAPTURE_START,
    PATH_EXECUTE,
    PATH_TELEOP,
)
from motrix_edge.errors import ErrorCode

# 一个标准的机器人进程 discover 响应 robot 块（身份 name / type + 自报连接参数）
_ROBOT_DICT = {
    "name": "Test Robot",
    "type": "test_robot",
    "running": True,  # 探活用：进程可达但未运行 → 未发现
}


# ---- discover_adapter（向固定端口发 discover）--------------------------------


def make_discovered(**overrides) -> DiscoveredRobot:
    """构造一个机器人进程 discover 结果（name / type + 可选自报连接参数）。"""
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


def test_discover_adapter_uses_reported_connection_params(monkeypatch):
    """进程自报的 endpoint / shm_name 进 adapter 构造（类常量退化为缺省值）。

    回归：曾只取 name / type，导致换端口后「discover 成功、指令仍发往写死的 8090」。
    """
    robot = dict(_ROBOT_DICT, endpoint="http://127.0.0.1:8091", shm_name="reported_obs")
    _patch_discover_client(monkeypatch, payload={"status": "accepted", "robot": robot})
    adapter = discover_adapter(host="127.0.0.1", port=8091)
    assert adapter is not None
    assert adapter.sdk_url == "http://127.0.0.1:8091"  # 不是类常量缺省 8090
    assert adapter.shm_name == "reported_obs"


def test_adapter_falls_back_to_class_constants():
    """无 discover 上报（如进程内测试 / 老进程）：连接参数回退 adapter 类常量。"""
    adapter = get_adapter(make_discovered())
    assert adapter.sdk_url == test_adapter_mod.TestRobotAdapter.SDK_URL.rstrip("/")
    assert adapter.shm_name == test_adapter_mod.TestRobotAdapter.SHM_NAME


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
    assert caps["action_dim"] == 12  # 兼容字段 = 关节空间维度（双臂 6 × 2）
    assert caps["action_dims"] == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
    assert caps["action_spaces"] == ["joint", "pose", "pose_delta", "gripper"]  # 控制台据此开放四空间直控
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
    assert adapter.action_dim == 12
    assert adapter.action_dims == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
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
    assert caps.action_dim == 12
    assert caps.action_dims == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
    assert caps.action_spaces == ["joint", "pose", "pose_delta", "gripper"]  # 关节 + 位姿（绝对/增量）+ 夹爪
    assert caps.observation_keys == [
        KEY_QPOS,
        KEY_ACTION,
        KEY_GRIPPER,
        KEY_POSE,
        KEY_POSE_TARGET,
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
    assert adapter.action_dim == 12
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
    """adapter 的 HTTP 客户端桩：记录 post 调用（不真实发送），统一返回 ``status_code``。"""

    def __init__(self, status_code=200):
        self.posts = []
        self.status_code = status_code  # 409 = SDK 拒绝（遥操作中推理让位）

    def post(self, url, json=None):
        self.posts.append((url, json))
        return SimpleNamespace(status_code=self.status_code)


def _exec_adapter():
    """构造已注入 fake HTTP 客户端的 TestRobotAdapter（execute 转发用）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter._http = _FakeHttp()
    return adapter


def test_execute_validates_dimension_before_sending():
    """execute：维度不符 → 抛 ValueError（不发送 HTTP）。"""
    adapter = _exec_adapter()
    with pytest.raises(ValueError, match="execute joint action dim"):
        adapter.execute([0.0] * 5)  # 5 != joint 空间维度 12
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


# ---- teleop（遥操作 / 人工接管）----------------------------------------------


def test_set_teleop_forwards_to_sdk():
    """set_teleop：本地记录 + HTTP 转发 SDK /v1/teleop（enabled 为 bool，mode 仅在开启时透传）。"""
    adapter = _exec_adapter()
    adapter.set_teleop(True)
    assert adapter.teleop_enabled is True  # 本地回显
    assert adapter.teleop_mode is None  # 未指定模式 → 不发 mode 字段（进程侧缺省 absolute）
    assert len(adapter._http.posts) == 1
    url, body = adapter._http.posts[0]
    assert url == PATH_TELEOP
    assert body == {FIELD_TELEOP_ENABLED: True}

    adapter.set_teleop(True, "delta")  # 人工接管（增量）
    assert adapter.teleop_mode == "delta"
    assert adapter._http.posts[-1] == (PATH_TELEOP, {FIELD_TELEOP_ENABLED: True, FIELD_TELEOP_MODE: "delta"})

    adapter.set_teleop(False)
    assert adapter.teleop_enabled is False
    assert adapter.teleop_mode is None  # 关闭后不留模式
    assert adapter._http.posts[-1] == (PATH_TELEOP, {FIELD_TELEOP_ENABLED: False})


# ---- rollout（推理闭环：遥操作中 SDK 拒绝）------------------------------------


def test_rollout_returns_true_on_accepted():
    """rollout：SDK 接受（200）→ True（本拍已下发）。"""
    adapter = _exec_adapter()
    assert adapter.rollout([0.0] * 12) is True
    assert adapter.rollout_calls == 1
    assert adapter.rollout_refused_calls == 0


def test_rollout_returns_false_when_sdk_refuses_teleop():
    """rollout：SDK 返回 409（遥操作 / 人工接管中）→ False，计数与日志限流位翻转。"""
    adapter = _exec_adapter()
    adapter._http = _FakeHttp(status_code=409)  # SDK 拒绝（遥操作中推理让位）
    assert adapter.rollout([0.0] * 12) is False
    assert adapter.rollout([0.0] * 12) is False
    assert adapter.rollout_calls == 2
    assert adapter.rollout_refused_calls == 2
    assert adapter._rollout_refused_logged is True  # 日志只在进入拒绝时记一条

    adapter._http = _FakeHttp(status_code=None)  # 遥操作结束：恢复下发
    assert adapter.rollout([0.0] * 12) is True
    assert adapter._rollout_refused_logged is False


# ---- capture episode（采集回合控制）------------------------------------------


def test_start_end_capture_forwards_to_sdk():
    """start_capture / end_capture：HTTP 转发 SDK /v1/capture/start、/v1/capture/end。"""
    adapter = _exec_adapter()
    adapter.start_capture()
    adapter.end_capture()
    assert [p[0] for p in adapter._http.posts] == [PATH_CAPTURE_START, PATH_CAPTURE_END]


# ---------------------------------------------------------------------------
# configure（RobotAdapter 基类能力）：启用臂 / 相机（home 由类常量 HOME 按空间定义）—— 通用、无实机
# ---------------------------------------------------------------------------


def test_configure_default_enables_all_arms():
    """缺省（不配置）：启用全部臂，action_dims = {joint:12, pose:12, gripper:2}，动作直发。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    assert adapter.enabled_arms == ["left", "right"]
    assert adapter.action_dim == 12
    assert adapter.action_dims == {"joint": 12, "pose": 12, "pose_delta": 12, "gripper": 2}
    assert adapter.capabilities.action_dim == 12


def test_configure_right_arm_changes_dim_and_expands():
    """只启用右臂：关节维度 6；execute 6 维动作展开回 12 维，左臂 home 填充。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["right"])
    assert adapter.enabled_arms == ["right"]
    assert adapter.action_dim == 6
    assert adapter.action_dims == {"joint": 6, "pose": 6, "pose_delta": 6, "gripper": 1}
    assert adapter.capabilities.action_dim == 6

    adapter.execute(np.arange(6, dtype=np.float64))
    expected = [0.0] * 6 + list(np.arange(6, dtype=np.float64))  # 左臂 home（0），右臂动作
    assert adapter.executed == [expected]
    assert adapter._http.posts == [(PATH_EXECUTE, {FIELD_ACTION: expected})]
    # 维度校验：6 维之外拒绝（不发送）
    with pytest.raises(ValueError, match="execute joint action dim"):
        adapter.execute([0.0] * 12)
    assert len(adapter._http.posts) == 1


def test_configure_left_arm_expands_with_home():
    """只启用左臂：动作放 [0:6]，右臂用类常量 HOME['joint'] 填充。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["left"])
    adapter.execute(np.ones(6))
    # TestRobotAdapter 缺省 HOME["joint"] = 全 0
    assert adapter.executed == [[1.0] * 6 + [0.0] * 6]


def test_configure_both_arms_keeps_full_dim_passthrough():
    """全臂启用：动作直发（home 不影响全启用）。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["left", "right"])
    assert adapter.action_dim == 12
    adapter.execute(np.arange(12, dtype=np.float64))
    assert adapter.executed == [list(np.arange(12, dtype=np.float64))]


def test_gripper_space_expands_with_gripper_home():
    """夹爪空间同样按臂裁剪：未启用臂用**同空间** home（张开）填充，不会拿关节值充数。"""
    adapter = _exec_adapter()
    adapter.configure(enabled_arms=["right"])
    assert adapter.action_dim_for("gripper") == 1
    adapter.execute(np.array([0.3]), "gripper")
    assert adapter.executed == [[1.0, 0.3]]  # 左臂 home = 1（张开），右臂动作


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
    assert adapter.action_dim == 12
    assert adapter.enabled_arms == ["left", "right"]


def test_configure_preserves_physical_arm_order():
    """enabled_arms 顺序无关：归一化为物理顺序（left → right）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter.configure(enabled_arms=["right", "left"])
    assert adapter.enabled_arms == ["left", "right"]
    assert adapter.action_dim == 12


def test_select_arm_segments_picks_enabled_arm_dims():
    """基类 _select_arm_segments：按启用臂物理顺序挑选 / 拼接等长分段（三空间共用）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    qpos = np.arange(12, dtype=np.float32)
    assert np.array_equal(adapter._select_arm_segments(qpos, 6), qpos)  # 全臂 → 原样
    adapter.configure(enabled_arms=["right"])
    assert np.array_equal(adapter._select_arm_segments(qpos, 6), qpos[6:12])
    adapter.configure(enabled_arms=["left"])
    assert np.array_equal(adapter._select_arm_segments(qpos, 6), qpos[0:6])


def test_expand_action_uses_home_for_disabled_arm():
    """基类 _expand_action：未启用臂用类常量 HOME[space] 填充（同空间）。"""
    adapter = test_adapter_mod.TestRobotAdapter()
    adapter.configure(enabled_arms=["right"])
    full = adapter._expand_action(np.ones(6), "rollout")
    # TestRobotAdapter 缺省 HOME["joint"] = 全 0
    assert np.array_equal(full, np.array([0.0] * 6 + [1.0] * 6))


# ---- node：_probe_adapter 应用运行时 adapter 配置（命令 / 前端设置，非 edge.yml）----


def test_node_probe_applies_adapter_config(monkeypatch):
    from motrix_edge.node import EdgeNode

    inner = test_adapter_mod.TestRobotAdapter(name="Test Robot")

    def fake_discover(host, port, required_capability=None):
        return inner

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)
    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    # 运行时配置（adapter config set / 前端 POST /v1/adapters/config），不读 edge.yml
    assert node.apply_adapter_config({"enabled_arms": ["right"]})
    node._last_probe = 0.0
    node._probe_adapter()
    assert node.adapter is inner  # 复用同一进程 / 同一 adapter，不新建
    assert inner.action_dim == 6
    assert inner.enabled_arms == ["right"]


def test_node_probe_invalid_config_does_not_bind(monkeypatch):
    from motrix_edge.node import EdgeNode

    inner = test_adapter_mod.TestRobotAdapter(name="Test Robot")

    def fake_discover(host, port, required_capability=None):
        return inner

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)
    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    # 防御：绕过入口（apply_adapter_config 已静态校验）直接写坏内存态 → discover 绑定时
    # configure 校验失败 → 不绑定
    node.adapter_config = {"enabled_arms": ["both"]}
    node._last_probe = 0.0
    node._probe_adapter()
    assert node.adapter is None  # 配置非法：不绑定，等待重试
    # 身份与实例同生命周期：不留陈旧机型（否则 adapter_ref 会报出已释放的 adapter）
    assert node.adapter_ref == {"name": None, "type": None}


def test_node_rejects_invalid_config_before_binding():
    """未绑定 adapter 时也按**类常量**静态校验：非法配置停在 set 时刻，不进状态。"""
    from motrix_edge.node import EdgeNode

    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    assert node.adapter is None
    assert not node.apply_adapter_config({"enabled_arms": ["both"]})  # 未知臂
    assert not node.apply_adapter_config({"enabled_cameras": ["cam_nope"]})  # 未知相机
    assert node.adapter_config == {}  # 非法配置不更新状态
    assert node.apply_adapter_config({"enabled_arms": ["right"]})  # 合法 → 通过
    assert node.adapter_config == {"enabled_arms": ["right"]}


def test_adapter_config_command_query_and_set():
    """adapter config / adapter config set <json>：查询与设置运行时 adapter 配置。"""
    from motrix_edge.command import build_command_registry
    from motrix_edge.node import EdgeNode

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
    assert replies3[0].code == ErrorCode.INVALID_ARGUMENT
    # 状态未被污染
    assert node.adapter_config["enabled_arms"] == ["right"]


def test_adapter_config_current_reports_effective():
    """adapter config current：返回当前绑定 adapter 实际生效的启用臂 / 相机 / 动作维度 / home。"""
    from motrix_edge.command import build_command_registry
    from motrix_edge.node import EdgeNode

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
    assert data["action_dim"] == 6
    assert data["action_dims"] == {"joint": 6, "pose": 6, "pose_delta": 6, "gripper": 1}
    assert data["home"] == {"joint": [0.0] * 12, "gripper": [1.0] * 2}  # 类常量 HOME（全臂）


def test_adapter_config_current_without_adapter_is_rejected():
    """adapter config current：未绑定 adapter → rejected（无机型信息，不猜默认）。"""
    from motrix_edge.command import build_command_registry
    from motrix_edge.node import EdgeNode

    node = EdgeNode({"adapter": {"host": "127.0.0.1", "port": 8090}})
    registry = build_command_registry()
    replies = []
    cmd = registry.parse_argv(["adapter", "config", "current"])
    cmd.reply_to = replies.append
    node._dispatch(cmd)
    assert replies[0].status == "rejected"
    assert replies[0].code == ErrorCode.CONFLICT
    assert "not bound" in replies[0].error
