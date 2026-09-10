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

"""server HTTP API 单元测试 —— FastAPI TestClient，无硬件、无网络可跑。"""

import copy
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fake_robot import FakeRobotAdapter
from fastapi.testclient import TestClient

from motrix_edge.frame import FrameManager
from motrix_edge.lease import Lease, LeaseManager, LeaseState
from motrix_edge.node import EdgeNode, NodeState
from motrix_edge.server import create_app
from motrix_edge.server.capture import CaptureService
from motrix_edge.server.command import CommandService
from motrix_edge.server.infer import InferService
from motrix_edge.server.preview import PreviewService
from motrix_edge.session import UploadSession
from motrix_edge.session.base import RunResult, SessionState
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_CONNECT,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
    CMD_INFER_PROMPT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_RTC,
    CMD_INFER_RTC_SET,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    ROLLOUT_MODE_CONTINUOUS,
    CommandBus,
    handle_policy_config,
    ok_result,
    parse_rollout_mode,
    policy_config_status,
)

BASE_CFG = {
    "identity": {
        "edge_id": "edge-test-001",
        "edge_name": "edge-test",
        "edge_version": "0.1.0",
    },
    "discover": {"host": "127.0.0.1", "port": 8090},
}


@pytest.fixture(autouse=True)
def _no_discover(monkeypatch):
    """server 测试不依赖真实 SDK 进程 / 网络：discover_adapter 视为无进程（返回 None）。

    server 测试一律注入 node（已绑定 adapter 或手工置 READY），不真正走 discover；
    此 fixture 兜底防止任何隐式 discover 触发真实网络请求。
    """

    def fake_discover(host, port, required_capability=None):
        return None

    monkeypatch.setattr("motrix_edge.adapter.discover_adapter", fake_discover)


@pytest.fixture(autouse=True)
def _isolate_base_cfg():
    """隔离共享 BASE_CFG：推理端点 / 策略配置项等内存态写入不串测（用例按序变动）。"""
    original = copy.deepcopy(BASE_CFG)
    yield
    BASE_CFG.clear()
    BASE_CFG.update(original)


def test_health_returns_identity_and_version():
    """/v1/health：版本 / identity / node 已绑定适配器 / 磁盘 / 时钟（不实时 discover）。"""
    node = FakeNode()  # 已绑定 adapter（adapter_id/type=test_robot, name=Test Robot）
    client = TestClient(create_app(BASE_CFG, node=node))
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["identity"]["X-Edge-Id"] == "edge-test-001"
    assert body["identity"]["X-Edge-Name"] == "edge-test"
    assert body["identity"]["X-Edge-Version"] == "0.1.0"
    assert body["robot"]["type"] == "test_robot"
    assert "version" in body and "time" in body
    assert "adapters" in body and "robots" in body["adapters"]
    # robots = node 当前绑定（单 adapter 包）；policies = 当前配置选中的策略
    assert body["adapters"]["robots"] == [{"name": "Test Robot", "type": "test_robot"}]
    assert [p["type"] for p in body["adapters"]["policies"]] == ["openpi", "act"]
    # 每个策略携带自己的配置项 schema（公共项 = 推理端点 host/port + 策略自身项）
    by_type = {p["type"]: p for p in body["adapters"]["policies"]}
    assert [item["key"] for item in by_type["openpi"]["config_items"]] == ["host", "port", "prompt"]
    assert [item["key"] for item in by_type["act"]["config_items"]] == [
        "host",
        "port",
        "pretrained_name_or_path",
        "device",
        "actions_per_chunk",
    ]


def test_health_without_node_returns_empty_robot():
    """未注入 node 时 /v1/health 不实时 discover，robot / robots 返回空。"""
    client = TestClient(create_app(BASE_CFG))
    body = client.get("/v1/health").json()
    assert body["robot"] == {"name": None, "type": None}
    assert body["adapters"]["robots"] == []
    assert [p["type"] for p in body["adapters"]["policies"]] == ["openpi", "act"]


def test_adapters_info_returns_capabilities():
    """GET /v1/adapters：静态列出已注册且已实现的 adapter，与 discover 无关。

    只列 type / available / capabilities（id / name 由 discover 赋予，静态列表不列）。
    """
    client = TestClient(create_app(BASE_CFG))
    body = client.get("/v1/adapters").json()
    assert {a["type"] for a in body["adapters"]} >= {"test_robot"}
    for adapter_type in ("test_robot",):
        info = next(a for a in body["adapters"] if a["type"] == adapter_type)
        assert "id" not in info and "name" not in info
        assert info["available"] is True
        caps = info["capabilities"]
        assert caps["action_dim"] == 14
        assert "image_names" in caps and "capabilities" in caps


def test_adapters_config_get_set():
    """GET/POST /v1/adapters/config：运行时 adapter 能力配置（命令 / 前端设置，受控操作）。"""
    from motrix_edge.node import EdgeNode

    node = EdgeNode(BASE_CFG)
    client = TestClient(create_app(BASE_CFG, node=node))
    # GET 初始为空
    assert client.get("/v1/adapters/config").json() == {}
    # POST 未持租约 → 409
    assert client.post("/v1/adapters/config", json={"enabled_arms": ["right"]}).status_code == 409
    # 签发租约后设置（无 adapter 绑定：存运行时状态）
    lease = install_lease(client)
    r = client.post("/v1/adapters/config", json={"enabled_arms": ["right"]}, headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    assert r.json()["enabled_arms"] == ["right"]
    # GET 反映
    assert client.get("/v1/adapters/config").json()["enabled_arms"] == ["right"]


def test_adapters_current_returns_effective():
    """GET /v1/adapters/current：未绑定回退默认配置；绑定后返回实际生效能力。"""
    from motrix_edge.node import EdgeNode

    node = EdgeNode(BASE_CFG)
    client = TestClient(create_app(BASE_CFG, node=node))
    # 未绑定 adapter → 回退包内默认 adapter 的默认配置（default=True，前端刷新可见勾选）
    r = client.get("/v1/adapters/current")
    assert r.status_code == 200
    body = r.json()
    assert body["default"] is True
    assert body["enabled"]["arms"]  # 默认启用字典非空
    assert body["enabled"]["cameras"]
    # 绑定 fake adapter（实际生效能力字典：双臂 + 三相机）
    node.adapter = SimpleNamespace(
        action_dim=14,
        _home_qpos=[0.0] * 14,
        enabled_map=lambda: {
            "arms": {"left": True, "right": True},
            "cameras": {"cam_head": True, "cam_left_wrist": True, "cam_right_wrist": True},
        },
    )
    node.adapter_name = "Test Robot"
    node.adapter_type = "test_robot"
    r = client.get("/v1/adapters/current")
    assert r.status_code == 200
    body = r.json()
    assert body["adapter"] == {"name": "Test Robot", "type": "test_robot"}
    assert body["enabled"] == {
        "arms": {"left": True, "right": True},
        "cameras": {"cam_head": True, "cam_left_wrist": True, "cam_right_wrist": True},
    }
    assert body["action_dim"] == 14
    assert body["home_qpos"] == [0.0] * 14
    assert "default" not in body  # 已绑定 → 无 default 标记


def test_health_returns_correlation_header():
    client = TestClient(create_app(BASE_CFG))
    resp = client.get("/v1/health")
    assert resp.headers.get("X-Correlation-Id")


def test_upload_endpoints_scan_and_select(tmp_path):
    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_0.json").write_text('{"collector": "operator-1"}', encoding="utf-8")
    upload = UploadSession()
    client = TestClient(create_app(BASE_CFG, uploads=upload))

    response = client.post("/v1/uploads", json={"folder_path": str(tmp_path)})
    assert response.status_code == 200
    assert response.json()["episode_count"] == 1

    response = client.post("/v1/uploads/select", json={"episode_ids": ["episode_0"]})
    assert response.status_code == 200
    assert response.json()["selected_episode_ids"] == ["episode_0"]

    assert client.post("/v1/uploads/upload").status_code == 501
    assert client.get("/v1/uploads").json()["episodes"][0]["selected"] is True


def test_upload_scan_without_path_returns_bad_request():
    client = TestClient(create_app(BASE_CFG, uploads=UploadSession()))
    response = client.post("/v1/uploads")
    assert response.status_code == 400


def test_upload_scan_falls_back_to_adapter_data_dir(tmp_path):
    """POST /v1/uploads 缺省目录回退链：adapter 数据目录（node.data_status.data_dir）优先。"""
    from types import SimpleNamespace

    (tmp_path / "episode_0.mcap").write_bytes(b"mcap")
    (tmp_path / "episode_0.json").write_text("{}", encoding="utf-8")
    node = SimpleNamespace(data_status=SimpleNamespace(data_dir=str(tmp_path)))
    client = TestClient(create_app(BASE_CFG, node=node, uploads=UploadSession()))

    response = client.post("/v1/uploads")  # 无 folder_path → 回退 adapter 数据目录
    assert response.status_code == 200
    body = response.json()
    assert body["episode_count"] == 1
    assert body["folder_path"] == str(tmp_path.resolve())
    assert body["episodes"][0]["meta"]["robot_name"] is None  # 空 {} json → 字段补 None


def test_command_accepted_with_metadata():
    client = TestClient(create_app(BASE_CFG))
    resp = client.post(
        "/v1/commands",
        json={
            "command_id": "cmd-1",
            "lease_id": "lease-1",
            "capability": "move",
            "params": {"joint": [0.1, 0.2]},
            "idempotency_key": "idem-1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["command_id"] == "cmd-1"
    assert body["idempotency_key"] == "idem-1"
    assert body["correlation_id"]
    assert resp.headers.get("X-Correlation-Id") == body["correlation_id"]


# ---------------------------------------------------------------------------
# /v1/captures/*（web 是 node 的独立线程：绑定正在运行的 fake node + CommandBus）—— 无硬件可跑
# ---------------------------------------------------------------------------


class FakeRobot:
    def __init__(self):
        self.ready = True
        self.name = "Test Robot"  # 适配器展示名（preview / enter 回显）

    def data_status(self):
        """采集数据状态：未启用 → None（适配器决定保存路径 / 数据列表）。"""
        return None

    def safe_stop(self):
        pass


class FakeCaptureSession:
    """仿 CaptureSession：观测会话，run 循环消费命令直到 session quit 退出。"""

    def __init__(self, command_source=None):
        self.state = SessionState.READY
        self.command_source = command_source
        self.pulled = []
        self.adapter = FakeRobot()

    def run(self):
        while True:
            cmd = self.command_source()
            if cmd is None:
                time.sleep(0.005)
                continue
            self.pulled.append(cmd)
            name = getattr(cmd, "name", None)
            if name == CMD_SESSION_QUIT:  # 退出会话
                self.state = SessionState.FINISHED
                self._reply(cmd, ok_result(node_state="finished"))
                return RunResult.FINISHED

    def _reply(self, cmd, result):
        if cmd is not None and cmd.reply_to is not None:
            cmd.reply_to(result)


class FakeInferSession:
    """仿 InferSession：推理无回合概念，run 循环消费命令直到 session quit 退出。"""

    def __init__(self, command_source=None, policy_type=None):
        self.state = SessionState.READY
        self.command_source = command_source
        self.pulled = []
        self.adapter = FakeRobot()
        self.policy = SimpleNamespace(name="fake-policy", server_metadata={}, prompt=None)
        self.connected = False  # 策略服务器连接状态（infer connect 成功后为 True）
        self.prompt = None  # 当前推理文本指令（prompt；推理/录制前必须非空）
        self.recording = False  # 推理会话是否开启 rollout 录制（capture episode start/end）
        self.prompt_required = True  # 是否语言条件策略（openpi=True 门控；act=False 不门控）
        # 会话使用的策略类型（决定配置项 schema）；缺省取配置 policy.type
        self.policy_config_type = policy_type or BASE_CFG.get("policy", {}).get("type", "openpi")
        self._rtc_params = {"enabled": True, "suffix_len": 10}  # RTC 参数（infer rtc set 可改）

    def policy_config_status(self):
        """仿 InferSession.policy_config_status（server /v1/infers 的 policy_config 字段）。"""
        return policy_config_status(BASE_CFG, self.policy_config_type)

    def rtc_status(self):
        """仿 RTCManager.status（server /v1/infers 的 rtc 字段）。"""
        return {
            "enabled": bool(self._rtc_params.get("enabled", True)),
            "params": dict(self._rtc_params),
            "index": 0,
            "remaining": 0,
            "fetches": 0,
            "last_chunk": None,
        }

    def run(self):
        while True:
            cmd = self.command_source()
            if cmd is None:
                time.sleep(0.005)
                continue
            self.pulled.append(cmd)
            name = getattr(cmd, "name", None)
            if name == CMD_INFER_CONNECT:  # 单次尝试连接推理节点：回执含 metadata
                self.connected = True
                self.policy.server_metadata = {"action_horizon": 16}
                self._reply(cmd, ok_result(state="ready", connected=True, metadata={"action_horizon": 16}))
            elif name == CMD_INFER_ROLLOUT:  # 推理闭环：单步（缺省）/ continuous 持续
                mode = parse_rollout_mode((cmd.params or {}).get("mode"))
                if mode == ROLLOUT_MODE_CONTINUOUS:
                    self._reply(cmd, ok_result(state="continuous", started=True, count=0, actions=[]))
                else:
                    self._reply(
                        cmd,
                        ok_result(state="ready", count=1, action=[1.0, 2.0], actions=[[1.0, 2.0]]),
                    )
            elif name in (  # 策略配置项：infer config(set) / infer model(set)（按策略 schema 校验）
                CMD_INFER_CONFIG,
                CMD_INFER_CONFIG_SET,
                CMD_INFER_MODEL,
                CMD_INFER_MODEL_SET,
            ):
                self._reply(cmd, handle_policy_config(BASE_CFG, cmd, policy_type=self.policy_config_type))
            elif name == CMD_INFER_PROMPT:  # 会话内预置文本指令（prompt；经策略配置校验 + 写入内存态）
                result = handle_policy_config(BASE_CFG, cmd, policy_type=self.policy_config_type)
                if result.status == "ok":
                    self.prompt = (cmd.params or {}).get("prompt")
                    self.policy.prompt = self.prompt
                    result = ok_result(state="ready", prompt=self.prompt)
                self._reply(cmd, result)
            elif name == CMD_CAPTURE_EPISODE_START:  # 推理时 rollout 录制开始
                self.recording = True
                self._reply(cmd, ok_result(state="recording", episode="start", recording=True))
            elif name == CMD_CAPTURE_EPISODE_END:  # 推理时 rollout 录制结束
                self.recording = False
                self._reply(cmd, ok_result(state="ready", episode="end", recording=False))
            elif name == CMD_CAPTURE_SYNC:  # 推理录制同步采集元信息（operator/task_name 等）
                meta = json.loads((cmd.params or {}).get("meta") or "{}")
                self._reply(cmd, ok_result(state="ready", meta=meta))
            elif name in (CMD_INFER_RTC, CMD_INFER_RTC_SET):  # RTC 参数：查询 / 设置
                if name == CMD_INFER_RTC_SET:
                    self._rtc_params.update(json.loads((cmd.params or {}).get("json") or "{}"))
                self._reply(cmd, ok_result(state="ready", rtc=self.rtc_status()))
            elif name == CMD_SESSION_QUIT:  # 退出推理会话
                self._reply(cmd, ok_result(node_state="finished"))
                return RunResult.FINISHED

    def _reply(self, cmd, result):
        if cmd is not None and cmd.reply_to is not None:
            cmd.reply_to(result)


class FakeNode:
    """仿 EdgeNode：镜像节点生命周期（初始 READY：adapter 已绑定 → session run <type>
    选择 + 启动一步完成 → session quit 回 READY）。任务期主循环不 poll（会话命令由任务线程
    内的 FakeCaptureSession 消费），线程结束收尾回 READY。"""

    def __init__(self):
        self.base_cfg = BASE_CFG
        self.command_source = None
        self.lifecycle = SimpleNamespace(state=NodeState.READY)
        self.session = None
        self.pulled = []
        self.pending_adapter = None
        self.frame_manager = FrameManager()  # Edge 级观测帧缓存（preview / WebRTC 读取）
        self.adapter_name = "Test Robot"  # 节点绑定的唯一 adapter 名称（单 adapter 包）
        self.adapter_type = "test_robot"
        self.adapter = FakeRobot()
        self._task_thread = None  # 任务线程（session.run 后台线程，镜像真实 EdgeNode）
        self._task_result = None

    def set_pending_adapter(self, adapter_id):
        """记录 HTTP 预留的待选适配器 id（真实 EdgeNode 为带锁槽位，此处仅记录）。"""
        self.pending_adapter = adapter_id

    @property
    def state(self):
        return self.lifecycle.state

    def run(self):
        while True:
            self._finish_task_thread()
            if self._task_thread is None:
                cmd = self.command_source()
                if cmd is None:
                    time.sleep(0.005)
                    continue
                self.pulled.append(cmd)
                name = getattr(cmd, "name", None)
                if name == CMD_SESSION_RUN:  # session run <type>：选择 + 启动一步完成
                    session_type = (cmd.params or {}).get("session")
                    if session_type == "capture":
                        self.session = FakeCaptureSession(command_source=self.command_source)
                    elif session_type == "infer":
                        self.session = FakeInferSession(
                            command_source=self.command_source, policy_type=(cmd.params or {}).get("policy_type")
                        )
                    else:
                        self._reply(cmd, ok_result(status="rejected", error=f"unknown session: {session_type}"))
                        continue
                    self.lifecycle.state = NodeState.ACTIVE
                    self._reply(cmd, self._start_task())
                elif name == CMD_SESSION_QUIT:  # 节点级 session quit（no-op，会话内由任务线程消费）
                    self._reply(cmd, ok_result(node_state=self.state))
                elif name == CMD_ROBOT_EXECUTE:  # robot execute：qpos 直接作为参数（回执 ok）
                    self._reply(cmd, ok_result(action=(cmd.params or {}).get("qpos")))
            else:
                time.sleep(0.005)

    def _start_task(self):
        """启动任务后台线程（镜像真实 EdgeNode：立即回执「已启动」）。"""
        self._task_result = None
        self._task_thread = threading.Thread(target=self._task_entry, daemon=True)
        self._task_thread.start()
        return ok_result(node_state=self.state)

    def _task_entry(self):
        if self.session is not None:
            self.session.run()
        self._task_result = RunResult.FINISHED

    def _finish_task_thread(self):
        """任务线程结束 → 释放会话回 READY（adapter 保留）。"""
        thread = self._task_thread
        if thread is None:
            return
        if thread.is_alive():
            return
        self._task_thread = None
        self.lifecycle.state = NodeState.READY
        self.session = None

    def _reply(self, cmd, result):
        if cmd is not None and cmd.reply_to is not None:
            cmd.reply_to(result)


def wait_session_state(node, state, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = node.session
        if session is not None and session.state == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"session state did not reach {state!r}, got {getattr(node.session, 'state', None)!r}")


def make_captures_client(node):
    """绑定「正在运行的 fake node」+ 共享 CommandBus + 共享 LeaseManager + 独立 PreviewService。"""
    bus = CommandBus()
    node.command_source = bus
    leases = LeaseManager()
    service = CaptureService(node, bus, leases=leases)
    preview_svc = PreviewService(node, leases=leases)
    threading.Thread(target=node.run, name="fake-node", daemon=True).start()
    return service, TestClient(create_app(BASE_CFG, captures=service, lease_manager=leases, preview=preview_svc))


def test_preview_requires_lease():
    """GET /v1/preview：受控操作，须持有有效租约（未持有 → 409）。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    assert client.get("/v1/preview").status_code == 409  # 无活跃租约


def test_preview_without_session():
    """GET /v1/preview：只须持有租约，**不要求会话**（无会话也返回观测缓存，随时可开）。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = install_lease(client)
    # 注入观测（模拟 observe 缓存）；无会话也应 200
    node.frame_manager.update(
        {
            "observations/qpos": np.array([0.3, 0.4]),
            "observations/images/cam_head": np.full((8, 8, 3), 64, dtype=np.uint8),
        }
    )
    r = client.get("/v1/preview", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == SessionState.INIT  # 无会话：state=INIT
    obs = body["observation"]
    assert obs["qpos"] == [0.3, 0.4]
    assert obs["images"] == ["cam_head"]


def test_preview_returns_latest_observation():
    """GET /v1/preview：返回 session state / adapter / observation（qpos / action + 摄像头名列表）。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = enter_captures(client, node)
    # 注入最新观测（模拟 observe 循环写入 FrameManager）：qpos + 一路摄像头帧
    node.frame_manager.update(
        {
            "observations/qpos": np.array([0.1, 0.2]),  # float64：float 转换精确
            "observations/images/cam_head": np.full((8, 8, 3), 128, dtype=np.uint8),
        }
    )
    r = client.get("/v1/preview", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == SessionState.READY
    assert body["adapter"]["name"] == "Test Robot"
    obs = body["observation"]
    assert obs["qpos"] == [0.1, 0.2]
    # 图像不内联（HTTP JSON 不承载二进制）：只返回摄像头名列表，图像由 WebRTC 推流
    assert obs["images"] == ["cam_head"]
    # 异租约：preview → 403
    assert client.get("/v1/preview", headers={"X-Lease-Id": "other"}).status_code == 403
    # 清理会话
    assert client.delete("/v1/captures", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def wait_node_state(node, state, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if node.state == state:
            return
        time.sleep(0.01)
    raise AssertionError(f"node state did not reach {state!r}, got {node.state!r}")


_LEASE_COUNTER = 0


def new_test_lease_id() -> str:
    """生成测试租约 id（全局递增，保证单活跃约束下每次签发唯一）。"""
    global _LEASE_COUNTER
    _LEASE_COUNTER += 1
    return f"ls_test_{_LEASE_COUNTER}"


def lease_payload(
    lease_id: str | None = None,
    holder_subject_id: str = "operator-1",
    purpose: str = "capture",
    ttl: float = 30,
    state: str = "active",
    expires_at: str | None = None,
) -> dict:
    """构造 Console 签发的租约镜像请求体（POST /v1/leases）。"""
    return {
        "lease_id": lease_id or new_test_lease_id(),
        "edge_id": "edge-test-001",
        "holder_subject_id": holder_subject_id,
        "purpose": purpose,
        "state": state,
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
        "lease_version": 1,
        "ttl": ttl,
    }


def install_lease(client, **overrides) -> str:
    """Console 签发租约镜像（POST /v1/leases），返回 lease_id。"""
    payload = lease_payload(**overrides)
    r = client.post("/v1/leases", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["lease_id"]


def enter_captures(client, node, adapter_id=None, lease_id=None):
    """先持有租约（缺省自动签发），再创建采集会话（POST /v1/captures，带 X-Lease-Id）。"""
    lease = lease_id or install_lease(client)
    payload = {"adapter_id": adapter_id} if adapter_id else {}
    r = client.post("/v1/captures", json=payload, headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    wait_session_state(node, SessionState.READY)
    return lease


# ---------------------------------------------------------------------------
# /v1/leases/*（Edge 级租约，独立于机器人 / 任务；受控操作须持有）—— 无硬件可跑
# ---------------------------------------------------------------------------


def test_leases_install_renew_revoke():
    """Console 权威：POST /v1/leases 签发镜像 → renew 续约（版本递增）→ revoke 撤销。"""
    client = TestClient(create_app(BASE_CFG))
    # 初始无租约：leasable
    snap = client.get("/v1/leases").json()
    assert snap["lease_id"] is None
    assert snap["leasable"] is True
    assert snap["renew_interval"] == 60  # 默认建议续租间隔（DEFAULT_RENEW_INTERVAL）
    # 签发镜像：POST /v1/leases（Console 生成 lease，Edge 保存镜像）
    lease = install_lease(client, holder_subject_id="operator-1", purpose="capture", ttl=30)
    # 单活跃：已有活跃租约再签发 → 409
    assert client.post("/v1/leases", json=lease_payload(lease_id="ls_second")).status_code == 409
    # 状态反映镜像字段
    snap = client.get("/v1/leases").json()
    assert snap["lease_id"] == lease
    assert snap["holder_subject_id"] == "operator-1"
    assert snap["purpose"] == "capture"
    assert snap["state"] == "active"
    assert snap["lease_version"] == 1
    assert snap["leasable"] is False
    assert snap["expires_at"].endswith("+08:00")  # 统一北京时区序列化
    # 查询镜像：GET /v1/leases/{id} → 200（返回 lease 信息）；不存在 → 404
    info = client.get(f"/v1/leases/{lease}").json()
    assert info["lease_id"] == lease
    assert info["edge_id"] == "edge-test-001"
    assert client.get("/v1/leases/ls_none").status_code == 404
    # 续约：POST /v1/leases/{id}:renew（lease_version 递增；Console 传新 expires_at）
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    r1 = client.post(f"/v1/leases/{lease}:renew", json={"lease_version": 2, "expires_at": future})
    assert r1.status_code == 200
    body = r1.json()
    assert body["lease_id"] == lease
    assert body["lease_version"] == 2
    assert body["state"] == "active"
    assert body["expires_at"].endswith("+08:00")
    # 版本回退 → 409
    assert client.post(f"/v1/leases/{lease}:renew", json={"lease_version": 1, "expires_at": future}).status_code == 409
    # 续约后镜像版本更新
    assert client.get(f"/v1/leases/{lease}").json()["lease_version"] == 2
    # 撤销：POST /v1/leases/{id}:revoke → Revoked 失效；镜像查询保留
    rv = client.post(f"/v1/leases/{lease}:revoke")
    assert rv.status_code == 200
    assert rv.json()["state"] == "revoked"
    assert client.get(f"/v1/leases/{lease}").json()["state"] == "revoked"
    assert client.get("/v1/leases").json()["state"] == "revoked"
    # 撤销后重新签发（撤销不占用单活跃名额）
    lease2 = install_lease(client, holder_subject_id="operator-2", purpose="rollout", ttl=30)
    assert lease2 != lease


def test_leases_expired_rejected_410():
    """租约超期未续约 → 失效：受控操作拒绝（410），需重新签发。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = install_lease(client, ttl=1)
    time.sleep(1.2)  # ttl=1s 到期
    # GET 保留过期状态：expired 且 leasable（可重新签发）
    snap = client.get("/v1/leases").json()
    assert snap["lease_id"] == lease
    assert snap["state"] == "expired"
    assert snap["leasable"] is True
    # 过期租约的受控操作（进入采集）→ 410（需重新签发）
    assert client.post("/v1/captures", headers={"X-Lease-Id": lease}).status_code == 410
    # 过期后可重新签发（覆盖）
    lease2 = install_lease(client, ttl=30)
    assert lease2 != lease


def test_leases_trusts_console_expiry():
    """过期时间由 Console 决定：Edge 信任镜像 expires_at，传「过去」则状态为过期（不重算）。"""
    client = TestClient(create_app(BASE_CFG))
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    lease = install_lease(client, ttl=60, expires_at=past)
    snap = client.get("/v1/leases").json()
    assert snap["lease_id"] == lease
    assert snap["state"] == "expired"  # 过去的 expires_at 被保留（未按 ttl 重算）
    exp = datetime.fromisoformat(snap["expires_at"])
    assert exp < datetime.now(timezone.utc)  # 仍是「过去」时刻
    assert snap["expires_at"].endswith("+08:00")


# ---------------------------------------------------------------------------
# /v1 控制面防缓存：实时状态一律 Cache-Control: no-store（防浏览器回放旧 410 / 状态）
# ---------------------------------------------------------------------------


def test_v1_responses_are_no_store():
    """/v1/* 响应统一 no-store：preview / 租约等轮询 GET 不得被浏览器缓存。"""
    client = TestClient(create_app(BASE_CFG))
    for path in ("/v1/health", "/v1/leases", "/v1/adapters", "/v1/captures", "/v1/infers", "/v1/preview"):
        r = client.get(path)
        assert r.status_code in (200, 501), f"{path} -> {r.status_code}"  # 未注入服务也可能 501
        assert r.headers.get("cache-control") == "no-store", path


# ---------------------------------------------------------------------------
# /v1/commands（受控命令：须持有租约；capability=estop → 全局急停）—— 无硬件可跑
# ---------------------------------------------------------------------------


def make_commands_client(node):
    """绑定 fake node + CommandBus + 共享 LeaseManager + CommandService。"""
    bus = CommandBus()
    node.command_source = bus
    leases = LeaseManager()
    commands = CommandService(node, bus, leases=leases)
    threading.Thread(target=node.run, name="fake-node", daemon=True).start()
    return TestClient(create_app(BASE_CFG, commands=commands, lease_manager=leases))


def test_commands_require_lease_and_estop():
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：命令被拒（无活跃租约 → 409）
    r = client.post("/v1/commands", json={"command_id": "c1", "capability": "estop"})
    assert r.status_code == 409
    # 签发租约后：estop 放行并 push SIG_ROBOT_ESTOP
    lease = install_lease(client)
    r = client.post("/v1/commands", json={"command_id": "c1", "lease_id": lease, "capability": "estop"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    time.sleep(0.1)  # 等 fake node 后台线程消费总线命令
    assert CMD_ROBOT_ESTOP in [getattr(c, "name", None) for c in node.pulled]
    # 异租约：estop 被拒（403）
    assert (
        client.post("/v1/commands", json={"command_id": "c2", "lease_id": "other", "capability": "estop"}).status_code
        == 403
    )


def test_commands_reset_recovers_node():
    """capability=reset → push node.reset（ERROR 恢复：释放 adapter 回 IDLE 重新探测）。"""
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：reset 被拒（409）
    assert client.post("/v1/commands", json={"command_id": "c1", "capability": "reset"}).status_code == 409
    lease = install_lease(client)
    r = client.post("/v1/commands", json={"command_id": "c1", "lease_id": lease, "capability": "reset"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    time.sleep(0.1)
    assert CMD_NODE_RESET in [getattr(c, "name", None) for c in node.pulled]


def test_commands_robot_reset_pushes_robot_reset():
    """capability=robot_reset → push robot reset（adapter.reset，非节点复位）。"""
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：robot_reset 被拒（409）
    assert client.post("/v1/commands", json={"command_id": "c1", "capability": "robot_reset"}).status_code == 409
    lease = install_lease(client)
    r = client.post("/v1/commands", json={"command_id": "c1", "lease_id": lease, "capability": "robot_reset"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    time.sleep(0.1)
    assert CMD_ROBOT_RESET in [getattr(c, "name", None) for c in node.pulled]


def test_commands_robot_execute_pushes_qpos():
    """capability=robot_execute → submit robot execute（qpos 直接作为参数），回执透传。"""
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：robot_execute 被拒（409）
    assert client.post("/v1/commands", json={"command_id": "c1", "capability": "robot_execute"}).status_code == 409
    lease = install_lease(client)
    qpos = "1,2,3,4,5,6,7,8,9,10,11,12,13,14"
    r = client.post(
        "/v1/commands",
        json={"command_id": "c1", "lease_id": lease, "capability": "robot_execute", "params": {"qpos": qpos}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["executed"] == "robot_execute"
    assert body["data"]["action"] == qpos  # qpos 直接作为参数
    # 命令已进入总线（submit 同步等回执）
    assert CMD_ROBOT_EXECUTE in [getattr(c, "name", None) for c in node.pulled]


def test_commands_robot_teleop_pushes_command():
    """capability=robot_teleop → push robot teleop（enabled 直接作为参数）。"""
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：robot_teleop 被拒（409）
    assert client.post("/v1/commands", json={"command_id": "c1", "capability": "robot_teleop"}).status_code == 409
    lease = install_lease(client)
    r = client.post(
        "/v1/commands",
        json={"command_id": "c1", "lease_id": lease, "capability": "robot_teleop", "params": {"enabled": "true"}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    time.sleep(0.1)
    cmd = next(c for c in node.pulled if getattr(c, "name", None) == CMD_ROBOT_TELEOP)
    assert cmd.params.get("enabled") == "true"  # enabled 直接作为参数


def test_commands_capture_episode_start_end_pushes_command():
    """capability=capture_episode_start/end → push capture episode start/end 命令。"""
    node = FakeNode()
    client = make_commands_client(node)
    # 未持有租约：被拒（409）
    assert (
        client.post("/v1/commands", json={"command_id": "c1", "capability": "capture_episode_start"}).status_code == 409
    )
    lease = install_lease(client)
    r = client.post("/v1/commands", json={"command_id": "c1", "lease_id": lease, "capability": "capture_episode_start"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    r = client.post("/v1/commands", json={"command_id": "c2", "lease_id": lease, "capability": "capture_episode_end"})
    assert r.status_code == 200
    time.sleep(0.1)
    names = [getattr(c, "name", None) for c in node.pulled]
    assert CMD_CAPTURE_EPISODE_START in names
    assert CMD_CAPTURE_EPISODE_END in names


# ---------------------------------------------------------------------------
# 推理端点配置（infer ip / infer port get/set）：capability 经命令总线 submit，
# 走真实 EdgeNode._dispatch（配置级命令任何状态可用）—— 无硬件可跑
# ---------------------------------------------------------------------------


def make_endpoint_client():
    """真实 EdgeNode（IDLE，无 adapter）+ CommandBus + Command/Infer 服务。

    推理端点配置命令（infer ip / infer port）经 CommandService submit 到总线 → 真实
    EdgeNode._dispatch 消费并回执，验证 HTTP capability 到命令总线的完整链路。
    """
    bus = CommandBus()
    node = EdgeNode({"policy": {"host": "0.0.0.0", "port": 8765}}, command_source=bus)
    leases = LeaseManager()
    commands = CommandService(node, bus, leases=leases)
    infers = InferService(node, bus, leases=leases)
    threading.Thread(target=node.run, name="fake-node", daemon=True).start()
    return TestClient(create_app(BASE_CFG, node=node, commands=commands, infers=infers, lease_manager=leases))


def test_commands_infer_ip_and_port_get_set():
    """capability=infer_ip / infer_ip_set / infer_port / infer_port_set → 查询 / 设置推理端点。

    经同一命令总线 submit 同步回执（与本地 CLI 行为一致）；配置写入内存态 policy 段。
    """
    client = make_endpoint_client()
    # 未持有租约：一律 409
    for cap in ("infer_ip", "infer_port", "infer_ip_set", "infer_port_set"):
        assert client.post("/v1/commands", json={"command_id": "c1", "capability": cap}).status_code == 409
    lease = install_lease(client)

    # 查询当前端点（host / port）
    r = client.post("/v1/commands", json={"command_id": "c1", "lease_id": lease, "capability": "infer_ip"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["executed"] == "infer_ip"
    assert r.json()["data"] == {"host": "0.0.0.0", "port": 8765}

    # 设置 IP：回执带回更新后端点
    r = client.post(
        "/v1/commands",
        json={"command_id": "c2", "lease_id": lease, "capability": "infer_ip_set", "params": {"ip": "10.0.0.5"}},
    )
    assert r.status_code == 200
    assert r.json()["executed"] == "infer_ip_set"
    assert r.json()["data"] == {"host": "10.0.0.5", "port": 8765}

    # 设置端口
    r = client.post(
        "/v1/commands",
        json={"command_id": "c3", "lease_id": lease, "capability": "infer_port_set", "params": {"port": "9000"}},
    )
    assert r.status_code == 200
    assert r.json()["data"] == {"host": "10.0.0.5", "port": 9000}

    # 再查询确认已更新
    r = client.post("/v1/commands", json={"command_id": "c4", "lease_id": lease, "capability": "infer_port"})
    assert r.json()["data"] == {"host": "10.0.0.5", "port": 9000}


def test_commands_infer_port_set_rejects_invalid():
    """capability=infer_port_set：非法端口 → 回执 error（400 透传，配置不变）。"""
    client = make_endpoint_client()
    lease = install_lease(client)
    r = client.post(
        "/v1/commands",
        json={"command_id": "c1", "lease_id": lease, "capability": "infer_port_set", "params": {"port": "abc"}},
    )
    assert r.status_code == 200  # 命令已消费，回执透传 rejected
    body = r.json()
    assert body["status"] == "error"
    assert body["executed"] == "infer_port_set"
    assert body["error"] is not None
    # 端口未更新（仍是初始 8765）
    r = client.post("/v1/commands", json={"command_id": "c2", "lease_id": lease, "capability": "infer_port"})
    assert r.json()["data"]["port"] == 8765


def test_infers_status_exposes_endpoint():
    """GET /v1/infers status：endpoint 字段回读当前配置的推理端点（前端推理卡片显示）。"""
    client = make_endpoint_client()
    snap = client.get("/v1/infers").json()
    assert snap["endpoint"] == {"host": "0.0.0.0", "port": 8765}


def test_captures_501_when_not_enabled():
    client = TestClient(create_app(BASE_CFG))
    assert client.get("/v1/captures").status_code == 501
    assert client.get("/v1/captures/precheck").status_code == 501
    assert client.post("/v1/captures", json={}).status_code == 501
    assert client.delete("/v1/captures").status_code == 501


def test_captures_meta_returns_options(tmp_path):
    """GET /v1/captures/meta：返回 config/capture.yml 的元信息选项（前端选择列表，免租约）。"""
    from motrix_edge.utils.capture_meta import CaptureMetaStore

    store = CaptureMetaStore(tmp_path / "capture.yml")
    store.add("operator", "张三")
    store.add("task_name", "桌面前移")
    bus = CommandBus()
    node = FakeNode()
    node.command_source = bus
    service = CaptureService(node, bus, capture_meta_store=store)
    client = TestClient(create_app(BASE_CFG, captures=service))
    resp = client.get("/v1/captures/meta")
    assert resp.status_code == 200
    assert resp.json() == {"meta": {"operator": ["张三"], "task_name": ["桌面前移"]}}
    # 未注入 captures 服务 → 501
    assert TestClient(create_app(BASE_CFG)).get("/v1/captures/meta").status_code == 501


def test_captures_enter_exit_lifecycle():
    node = FakeNode()
    service, client = make_captures_client(node)
    # 未持有租约：enter / exit(DELETE) 一律 409（无活跃租约）
    assert client.post("/v1/captures", json={}).status_code == 409
    assert client.delete("/v1/captures").status_code == 409
    # 先激活租约再进入环境：IDLE → ACTIVE（env READY）
    lease = enter_captures(client, node)
    assert isinstance(lease, str) and lease
    assert node.state == NodeState.ACTIVE
    pulled_names = [getattr(c, "name", None) for c in node.pulled]
    assert CMD_SESSION_RUN in pulled_names  # session run capture（选择 + 启动一步完成）
    # 已在环境中：再次 POST /v1/captures → 409（带正确租约）
    assert client.post("/v1/captures", json={}, headers={"X-Lease-Id": lease}).status_code == 409
    # 异租约 / 缺失租约：DELETE 拒绝
    assert client.delete("/v1/captures", params={"lease_id": "other"}).status_code == 403
    assert client.delete("/v1/captures").status_code == 403
    # 正确租约退出任务：ACTIVE → READY；租约独立，不随退出释放
    assert client.delete("/v1/captures", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)
    assert node.session is None
    assert service.status()["lease_id"] == lease  # 租约仍活跃（独立于任务）
    # 撤销租约后 status 无租约
    assert client.post(f"/v1/leases/{lease}:revoke").status_code == 200
    assert service.status()["lease_id"] is None
    # 可重新签发并进入任务（新租约）
    lease2 = enter_captures(client, node)
    assert lease2 != lease


def test_captures_enter_returns_bound_adapter():
    """单 adapter 包：enter 无 adapter 选择，响应回显节点绑定的唯一 adapter + 租约。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = install_lease(client)
    r = client.post("/v1/captures", json={}, headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    # 响应回显节点绑定的唯一 adapter 身份 + 当前活跃租约
    assert body["adapter"] == {"name": "Test Robot", "type": "test_robot"}
    assert body["lease_id"] == lease
    wait_session_state(node, SessionState.READY)
    assert client.delete("/v1/captures", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_captures_rejects_other_lease():
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = enter_captures(client, node)
    # 异租约退出 → 403
    assert client.delete("/v1/captures", params={"lease_id": "other-lease"}).status_code == 403
    # 缺失租约 → 403（已有会话）
    assert client.delete("/v1/captures").status_code == 403
    # 正确租约 → 放行退出
    assert client.delete("/v1/captures", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_captures_status_and_precheck():
    node = FakeNode()
    service, client = make_captures_client(node)
    # 会话未激活：status 反映节点 idle / env 未建；precheck 报未激活 + 可租
    body = client.get("/v1/captures").json()
    assert body["node_state"] == NodeState.READY  # 无会话但 adapter 已就绪
    assert body["state"] == SessionState.INIT
    assert body["lease_id"] is None
    pre = client.get("/v1/captures/precheck").json()
    assert pre["ok"] is False
    assert "collect session not active" in pre["errors"]
    assert pre["lease_id"] is None
    assert pre["leasable"] is True  # 未持租约、节点正常 → 可租
    # 进入环境后：status 带租约、precheck 通过且不可再租
    lease = enter_captures(client, node)
    assert client.get("/v1/captures").json()["lease_id"] == lease
    pre = client.get("/v1/captures/precheck").json()
    assert pre["ok"] is True
    assert pre["robot_ready"] is True
    assert pre["node_state"] == NodeState.ACTIVE
    assert pre["lease_id"] == lease
    assert pre["leasable"] is False  # 已持租约 → 不可再租


def test_captures_invalid_transition_returns_409():
    node = FakeNode()
    service, client = make_captures_client(node)
    # 未进入环境：exit(DELETE) 非法（无会话）
    assert client.delete("/v1/captures").status_code == 409
    # 已在环境中：再次创建会话非法（带正确租约 → 409；无租约 → 403）
    lease = enter_captures(client, node)
    assert client.post("/v1/captures", json={}, headers={"X-Lease-Id": lease}).status_code == 409
    assert client.post("/v1/captures", json={}).status_code == 403


def test_captures_exit_finishes_session():
    node = FakeNode()
    service, client = make_captures_client(node)
    lease = enter_captures(client, node)
    assert client.delete("/v1/captures", params={"lease_id": lease}).status_code == 200
    # capture.finish 结束任务：节点回 READY、释放 session；租约独立不随退出释放
    wait_node_state(node, NodeState.READY)
    assert node.session is None
    assert client.get("/v1/leases").json()["lease_id"] == lease
    # 撤销旧租约 → 签发新租约 → 重新进入（新会话）
    assert client.post(f"/v1/leases/{lease}:revoke").status_code == 200
    lease2 = enter_captures(client, node)
    assert lease2 != lease


@pytest.mark.skip(
    reason="临时跳过：真实节点观测会遗留原生帧线程，进程退出时偶发 SIGABRT "
    "(`terminate called without an active exception`，exit 134)，本地/CI 间歇失败。"
    "待实现线程安全 teardown 后再启用。"
)
def test_captures_real_node_observes_until_exit(tmp_path):
    """端到端：web（CaptureService）驱动真实 EdgeNode + FakeRobotAdapter，enter → 持续观测 → exit。"""
    cfg = {
        **BASE_CFG,
        "adapter": [
            {
                "name": "Test Robot",
                "type": "test_robot",
                "data_dir": str(tmp_path),  # 运行时行为参数（适配器特有）；能力由适配器返回
            }
        ],
        "capture": {"obs_freq": 30},
    }
    from motrix_edge.node import EdgeNode, NodeState

    bus = CommandBus()
    node = EdgeNode(base_cfg=cfg, command_source=bus, alive_check_interval=0.2)
    # 注入进程内 FakeRobotAdapter 并置 READY（不依赖 SDK 进程 / 探测绑定）；
    # 先 discover 标记就绪，避免 READY 后 _tick 失联检查将其转 ERROR
    node.adapter = FakeRobotAdapter(config={"data_dir": str(tmp_path)})
    node.adapter_name = "Test Robot"
    node.adapter_type = "test_robot"
    node.initialize()  # INIT → IDLE（构造后默认 INIT，先完成初始化再置 READY）
    node.lifecycle.transition(NodeState.READY)
    leases = LeaseManager()
    service = CaptureService(node, bus, leases=leases)
    threading.Thread(target=node.run, name="node", daemon=True).start()
    try:
        # 先部署 Console 签发的租约镜像（独立于任务）再进入采集
        lease = leases.install(
            Lease(
                lease_id="ls_real_node",
                edge_id="edge-test-001",
                holder_subject_id="operator-1",
                purpose="capture",
                state=LeaseState.ACTIVE,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
                lease_version=1,
                ttl=300,
            )
        ).lease_id
        service.enter(lease_id=lease)
        assert service.status()["lease_id"] == lease
        wait_capture_state(service, SessionState.READY, timeout=5)
        time.sleep(0.2)  # 持续观测几帧
        assert node.frame_manager.latest()  # FrameManager 已有观测帧（供 preview / WebRTC）
        assert service.exit(lease_id=lease)["status"] == "accepted"
        assert service.status()["lease_id"] == lease  # 租约独立，不随退出释放
        leases.revoke(lease)
        assert service.status()["lease_id"] is None
    finally:
        # 兜底：若仍在会话中则退出（幂等）；并撤销活跃租约
        if node.session is not None:
            try:
                service.exit(lease_id=leases.status()["lease_id"])
            except Exception:  # noqa: BLE001
                pass
        try:
            active = leases.status()["lease_id"]
            if active is not None:
                leases.revoke(active)
        except Exception:  # noqa: BLE001
            pass


def wait_capture_state(service, state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service.status()["state"] == state:
            return
        time.sleep(0.02)
    raise AssertionError(f"capture state did not reach {state!r}")


# ---------------------------------------------------------------------------
# /v1/infers/*（推理会话：无回合概念，enter → 持续推理 → exit）—— 无硬件可跑
# ---------------------------------------------------------------------------


def make_infers_client(node):
    """绑定 fake node + CommandBus + 共享 LeaseManager + InferService。"""
    bus = CommandBus()
    node.command_source = bus
    leases = LeaseManager()
    service = InferService(node, bus, leases=leases)
    threading.Thread(target=node.run, name="fake-node", daemon=True).start()
    return service, TestClient(create_app(BASE_CFG, infers=service, lease_manager=leases))


def test_infers_501_when_not_enabled():
    client = TestClient(create_app(BASE_CFG))
    assert client.get("/v1/infers").status_code == 501
    assert client.post("/v1/infers").status_code == 501
    assert client.delete("/v1/infers").status_code == 501


def test_infers_enter_exit_lifecycle():
    node = FakeNode()
    service, client = make_infers_client(node)
    # 未持有租约：enter / exit 一律 409
    assert client.post("/v1/infers").status_code == 409
    assert client.delete("/v1/infers").status_code == 409
    # 签发租约后进入推理会话：infer.start + session.run
    lease = install_lease(client)
    r = client.post("/v1/infers", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["lease_id"] == lease
    assert body["adapter"] == {"name": "Test Robot", "type": "test_robot"}
    pulled_names = [getattr(c, "name", None) for c in node.pulled]
    assert CMD_SESSION_RUN in pulled_names  # session run infer（选择 + 启动一步完成）
    # 已在推理会话中：再次 enter → 409
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 409
    # status：node active + 绑定 adapter + policy
    snap = client.get("/v1/infers").json()
    assert snap["node_state"] == NodeState.ACTIVE
    assert snap["adapter"]["name"] == "Test Robot"
    assert snap["policy"] == "fake-policy"
    # 正确租约退出：ACTIVE → READY
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)
    assert node.session is None
    assert client.get("/v1/infers").json()["node_state"] == NodeState.READY


def test_infers_rollout_steps_inference():
    """单步推理闭环：POST /v1/infers/rollout → 会话执行一次 rollout 并回执动作。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    # 未进入推理会话：rollout → 409
    assert client.post("/v1/infers/rollout").status_code == 409
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 单步推理：rollout → 回执动作
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["action"] == [1.0, 2.0]
    assert CMD_INFER_ROLLOUT in [getattr(c, "name", None) for c in node.session.pulled]
    # 清理退出
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_rollout_single_and_continuous():
    """推理闭环模式：单步（缺省）/ continuous 持续；多步与 drain 已取消 → 拒绝。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 单步（缺省 body）→ 回执 count=1 / action / actions
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["count"] == 1
    assert body["action"] == [1.0, 2.0]
    # continuous 模式：mode=continuous → 启动即回执 started
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"mode": "continuous"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "continuous"
    # 多步（count=3）已取消：pydantic 只允许 count=1 → 422
    assert client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"count": 3}).status_code == 422
    # drain（缓存推理）已取消：mode 只允许 single/continuous → 422
    assert client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"mode": "drain"}).status_code == 422
    # 非法 count=0 → 422（pydantic 校验）
    assert client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"count": 0}).status_code == 422
    # 清理退出
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_status_exposes_prompt_and_recording_defaults():
    """status 携带 prompt / recording / capture_meta（operator=policy、task_name=prompt）。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    snap = client.get("/v1/infers").json()
    assert snap["prompt"] is None
    assert snap["recording"] is False
    assert snap["capture_meta"] == {"operator": "policy", "task_name": None}
    # 会话内设置 prompt → status.prompt / capture_meta.task_name 同步
    assert (
        client.post("/v1/infers/prompt", headers={"X-Lease-Id": lease}, json={"prompt": "把零件放好"}).status_code
        == 200
    )
    snap = client.get("/v1/infers").json()
    assert snap["prompt"] == "把零件放好"
    assert snap["capture_meta"] == {"operator": "policy", "task_name": "把零件放好"}
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_status_exposes_policy_config():
    """status 携带 policy_config（每个策略独立配置项的 schema + 当前值 + 缺失必填项）。

    前端据此动态渲染表单，并在 ``missing`` 非空时门控推理 / 录制按钮。
    """
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    snap = client.get("/v1/infers").json()
    cfg = snap["policy_config"]
    assert cfg["policy_type"] == "openpi"
    assert cfg["requires_prompt"] is True  # 语言条件策略：prompt 必填
    assert cfg["missing"] == ["prompt"]  # 尚未预置 → 缺失
    assert [item["key"] for item in cfg["items"]] == ["host", "port", "prompt"]  # 端点公共项在前
    assert cfg["runtime_keys"] == ["host", "port", "prompt"]  # 端点与其它项同级（会话内可改）
    assert cfg["connect_locked_keys"] == ["host", "port"]  # 但连接策略后锁定
    assert cfg["items"][2]["type"] == "text"
    # 会话内设置 prompt（走 infer prompt 快捷命令）→ 写入内存态 → 缺失清空
    assert (
        client.post("/v1/infers/prompt", headers={"X-Lease-Id": lease}, json={"prompt": "把零件放好"}).status_code
        == 200
    )
    assert client.get("/v1/infers").json()["policy_config"]["missing"] == []
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_config_sets_policy_config():
    """POST /v1/infers/config（infer config set）：按当前策略 schema 校验并写入配置项。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    # 未进入会话：受控操作 → 409
    assert (
        client.post("/v1/infers/config", headers={"X-Lease-Id": lease}, json={"config": {"prompt": "x"}}).status_code
        == 409
    )
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    r = client.post("/v1/infers/config", headers={"X-Lease-Id": lease}, json={"config": {"prompt": "把零件放好"}})
    assert r.status_code == 200
    assert r.json()["policy_config"]["missing"] == []
    assert r.json()["policy_config"]["values"]["prompt"] == "把零件放好"
    # 非本策略的配置项（act 的模型路径）→ 400（不静默写入）
    bad = client.post(
        "/v1/infers/config",
        headers={"X-Lease-Id": lease},
        json={"config": {"pretrained_name_or_path": "/tmp/x"}},
    )
    assert bad.status_code == 400
    assert "unknown openpi config key" in bad.json()["detail"]
    # 缺租约 → 403
    assert client.post("/v1/infers/config", json={"config": {"prompt": "y"}}).status_code == 403
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_enter_config_applies_endpoint():
    """POST /v1/infers 的 config 含公共项 host / port → 进入会话前写入推理端点（随会话锁定）。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    r = client.post(
        "/v1/infers",
        headers={"X-Lease-Id": lease},
        json={"policy_type": "openpi", "config": {"host": "10.0.0.7", "port": 9000, "prompt": "把零件放好"}},
    )
    assert r.status_code == 200
    assert BASE_CFG["policy"]["host"] == "10.0.0.7"
    assert BASE_CFG["policy"]["port"] == 9000
    assert BASE_CFG["policy"]["prompt"] == "把零件放好"
    assert r.json()["policy_config"]["values"]["host"] == "10.0.0.7"  # 回执含端点当前值
    # 非法端口 → 400（且不进入会话）
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)
    bad = client.post("/v1/infers", headers={"X-Lease-Id": lease}, json={"config": {"port": 70000}})
    assert bad.status_code == 400
    assert node.session is None


def test_infers_enter_applies_policy_config():
    """POST /v1/infers 携带 config：进入会话前写入策略配置项（act 的模型路径运行时给定）。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    r = client.post(
        "/v1/infers",
        headers={"X-Lease-Id": lease},
        json={"policy_type": "act", "config": {"pretrained_name_or_path": "/tmp/pretrained_model"}},
    )
    assert r.status_code == 200
    assert BASE_CFG["policy"]["pretrained_name_or_path"] == "/tmp/pretrained_model"
    assert node.session.policy_config_type == "act"  # 会话按所选策略渲染配置项
    # 会话内设置非本策略的配置项（act 无 prompt）→ 400
    bad_key = client.post("/v1/infers/config", headers={"X-Lease-Id": lease}, json={"config": {"prompt": "x"}})
    assert bad_key.status_code == 400
    assert "unknown act config key" in bad_key.json()["detail"]
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)
    # 进入会话前下发非法键 → 400（且不进入会话）
    bad = client.post(
        "/v1/infers", headers={"X-Lease-Id": lease}, json={"policy_type": "act", "config": {"prompt": "x"}}
    )
    assert bad.status_code == 400
    assert "unknown act config key" in bad.json()["detail"]
    assert node.session is None  # 校验失败不进入会话


def test_infers_config_available_before_session():
    """未进入会话时 status.policy_config 由 base_cfg 计算（前端 enter 前即可渲染表单）。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    assert client.get("/v1/infers").json()["policy_config"]["policy_type"] == "openpi"
    BASE_CFG["policy"] = {"type": "act", "pretrained_name_or_path": "/tmp/m", "device": "cuda"}
    cfg = client.get("/v1/infers").json()["policy_config"]
    assert cfg["policy_type"] == "act"
    assert cfg["requires_model_path"] is True
    assert cfg["missing"] == []


def test_infers_episode_recording_start_end():
    """推理时 rollout 录制：episode start/end → capture episode 命令 → recording 状态翻转。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    # 未进入推理会话：episode → 409
    assert client.post("/v1/infers/episode/start", headers={"X-Lease-Id": lease}).status_code == 409
    assert client.post("/v1/infers/episode/end", headers={"X-Lease-Id": lease}).status_code == 409
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 开始录制
    r = client.post("/v1/infers/episode/start", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["episode"] == "start"
    assert body["recording"] is True
    assert node.session.recording is True
    assert client.get("/v1/infers").json()["recording"] is True
    # 结束录制
    r = client.post("/v1/infers/episode/end", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    assert r.json()["episode"] == "end"
    assert r.json()["recording"] is False
    assert node.session.recording is False
    assert CMD_CAPTURE_EPISODE_START in [getattr(c, "name", None) for c in node.session.pulled]
    assert CMD_CAPTURE_EPISODE_END in [getattr(c, "name", None) for c in node.session.pulled]
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_sync_syncs_capture_meta():
    """推理录制同步采集元信息：POST /v1/infers/sync → capture sync 命令 → 回执 meta。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    r = client.post(
        "/v1/infers/sync",
        headers={"X-Lease-Id": lease},
        json={"meta": {"operator": "policy", "task_name": "把零件放好"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["meta"] == {"operator": "policy", "task_name": "把零件放好"}
    assert CMD_CAPTURE_SYNC in [getattr(c, "name", None) for c in node.session.pulled]
    # 缺租约头（已有活跃租约）→ 403
    assert client.post("/v1/infers/sync", json={"meta": {}}).status_code == 403
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_rtc_configure_and_status():
    """RTC 参数：POST /v1/infers/rtc → infer rtc set → 应用到会话并反映在 status。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    # 未进入推理会话：rtc → 409
    assert client.post("/v1/infers/rtc", headers={"X-Lease-Id": lease}, json={"suffix_len": 5}).status_code == 409
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 设置 RTC 参数（部分更新）：回执生效后的状态
    r = client.post(
        "/v1/infers/rtc",
        headers={"X-Lease-Id": lease},
        json={"suffix_len": 5, "aggregate_fn": "latest_only"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["rtc"]["params"]["suffix_len"] == 5
    assert body["rtc"]["params"]["aggregate_fn"] == "latest_only"
    # status 同步暴露 rtc（enabled / params / index / remaining / last_chunk）
    snap = client.get("/v1/infers").json()
    assert snap["rtc"]["params"]["suffix_len"] == 5
    assert snap["rtc"]["enabled"] is True
    assert CMD_INFER_RTC_SET in [getattr(c, "name", None) for c in node.session.pulled]
    # 非法参数（负数）→ pydantic 422
    assert client.post("/v1/infers/rtc", headers={"X-Lease-Id": lease}, json={"suffix_len": -1}).status_code == 422
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_connect_exposes_status():
    """POST /v1/infers/connect：单次尝试连接推理节点，成功回执含 metadata；status 反映 connected。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    # 未进入会话：connect → 409
    assert client.post("/v1/infers/connect", headers={"X-Lease-Id": lease}).status_code == 409
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 初始未连接：status connected=False / metadata=None
    snap = client.get("/v1/infers").json()
    assert snap["connected"] is False
    assert snap["metadata"] is None
    # 连接成功：回执 connected=True + metadata；status 同步暴露
    r = client.post("/v1/infers/connect", headers={"X-Lease-Id": lease})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["connected"] is True
    assert body["metadata"] == {"action_horizon": 16}
    snap = client.get("/v1/infers").json()
    assert snap["connected"] is True
    assert snap["metadata"] == {"action_horizon": 16}
    assert CMD_INFER_CONNECT in [getattr(c, "name", None) for c in node.session.pulled]
    # 清理退出
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_prompt_updates_runtime_prompt():
    """运行时改文本指令：POST /v1/infers/prompt → 会话内 infer prompt 命令 → 回执。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    # 未进入推理会话：prompt → 409
    assert client.post("/v1/infers/prompt", headers={"X-Lease-Id": lease}, json={"prompt": "x"}).status_code == 409
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # 运行时设置 prompt：会话内命令被执行，回执回显
    r = client.post("/v1/infers/prompt", headers={"X-Lease-Id": lease}, json={"prompt": "把零件放好"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["prompt"] == "把零件放好"
    assert CMD_INFER_PROMPT in [getattr(c, "name", None) for c in node.session.pulled]
    assert node.session.policy.prompt == "把零件放好"
    # 缺 prompt → pydantic 422
    assert client.post("/v1/infers/prompt", headers={"X-Lease-Id": lease}, json={}).status_code == 422
    # 清理退出
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)


def test_infers_enter_applies_host_port():
    """进入会话前可把推理端点（host / port）随 enter 传入：写 base_cfg，创建会话即生效。"""
    import copy

    node = FakeNode()
    node.base_cfg = copy.deepcopy(BASE_CFG)  # 独立配置副本，避免污染全局 BASE_CFG
    service, client = make_infers_client(node)
    lease = install_lease(client)
    r = client.post("/v1/infers", headers={"X-Lease-Id": lease}, json={"host": "10.0.0.9", "port": 8765})
    assert r.status_code == 200
    assert node.base_cfg["policy"]["host"] == "10.0.0.9"
    assert node.base_cfg["policy"]["port"] == 8765
    # 非法端口 → 400（pydantic 校验）
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}, json={"port": 0}).status_code == 422
    # 清理退出
    assert client.delete("/v1/infers", params={"lease_id": lease}).status_code == 200
    wait_node_state(node, NodeState.READY)
