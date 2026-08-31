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
from motrix_edge.session import UploadSession
from motrix_edge.session.base import RunResult, SessionState
from motrix_edge.utils.commands import (
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_INFER_CONNECT,
    CMD_INFER_ROLLOUT,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
    CommandBus,
    ok_result,
    parse_rollout_mode,
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
    assert [p["type"] for p in body["adapters"]["policies"]] == ["openpi", "act", "act7dof"]


def test_health_without_node_returns_empty_robot():
    """未注入 node 时 /v1/health 不实时 discover，robot / robots 返回空。"""
    client = TestClient(create_app(BASE_CFG))
    body = client.get("/v1/health").json()
    assert body["robot"] == {"name": None, "type": None}
    assert body["adapters"]["robots"] == []
    assert [p["type"] for p in body["adapters"]["policies"]] == ["openpi", "act", "act7dof"]


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

    def __init__(self, command_source=None):
        self.state = SessionState.READY
        self.command_source = command_source
        self.pulled = []
        self.adapter = FakeRobot()
        self.policy = SimpleNamespace(name="fake-policy", server_metadata={})
        self.connected = False  # 策略服务器连接状态（infer connect 成功后为 True）

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
            elif name == CMD_INFER_ROLLOUT:  # 推理闭环：按 count 参数解析模式回执
                mode, count = parse_rollout_mode((cmd.params or {}).get("count"))
                if mode == "continuous":
                    self._reply(cmd, ok_result(state="continuous", started=True, count=0, actions=[]))
                elif mode == "drain":
                    self._reply(cmd, ok_result(state="ready", count=1, action=[1.0, 2.0], actions=[[1.0, 2.0]]))
                else:
                    self._reply(
                        cmd,
                        ok_result(
                            state="ready",
                            count=count,
                            action=[1.0, 2.0],
                            actions=[[1.0, 2.0]] * count,
                        ),
                    )
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
                        self.session = FakeInferSession(command_source=self.command_source)
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
    """绑定「正在运行的 fake node」+ 共享 CommandBus + 共享 LeaseManager。"""
    bus = CommandBus()
    node.command_source = bus
    leases = LeaseManager()
    service = CaptureService(node, bus, leases=leases)
    threading.Thread(target=node.run, name="fake-node", daemon=True).start()
    return service, TestClient(create_app(BASE_CFG, captures=service, lease_manager=leases))


def test_preview_requires_lease():
    """GET /v1/preview：受控操作，须持有有效租约（未持有 → 409）。"""
    node = FakeNode()
    service, client = make_captures_client(node)
    assert client.get("/v1/preview").status_code == 409  # 无活跃租约


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
    # 查询镜像：GET /v1/leases/{id} → 200（返回 lease 信息）；不存在 → 404
    info = client.get(f"/v1/leases/{lease}").json()
    assert info["lease_id"] == lease
    assert info["edge_id"] == "edge-test-001"
    assert client.get("/v1/leases/ls_none").status_code == 404
    # 续约：POST /v1/leases/{id}:renew（lease_version 递增，原地延长）
    future = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    r1 = client.post(f"/v1/leases/{lease}:renew", json={"lease_version": 2, "expires_at": future})
    assert r1.status_code == 200
    body = r1.json()
    assert body["lease_id"] == lease
    assert body["lease_version"] == 2
    assert body["state"] == "active"
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
                "save_dir": str(tmp_path),  # 运行时行为参数（适配器特有）；能力由适配器返回
            }
        ],
        "capture": {"obs_freq": 30},
    }
    from motrix_edge.node import EdgeNode, NodeState

    bus = CommandBus()
    node = EdgeNode(base_cfg=cfg, command_source=bus, alive_check_interval=0.2)
    # 注入进程内 FakeRobotAdapter 并置 READY（不依赖 SDK 进程 / 探测绑定）；
    # 先 discover 标记就绪，避免 READY 后 _tick 失联检查将其转 ERROR
    node.adapter = FakeRobotAdapter(config={"save_dir": str(tmp_path)})
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


def test_infers_rollout_modes():
    """推理闭环模式：body count / mode=continuous / mode=drain → 命令层解析并回执。"""
    node = FakeNode()
    service, client = make_infers_client(node)
    lease = install_lease(client)
    assert client.post("/v1/infers", headers={"X-Lease-Id": lease}).status_code == 200
    # count 模式：body count=3 → infer rollout 3
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"count": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert len(body["actions"]) == 3
    # continuous 模式：mode=continuous → 启动即回执 started
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"mode": "continuous"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "continuous"
    # drain 模式：mode=drain → 只消耗缓存动作块
    r = client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"mode": "drain"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["action"] == [1.0, 2.0]
    # 非法 count → 400（pydantic 校验）
    assert client.post("/v1/infers/rollout", headers={"X-Lease-Id": lease}, json={"count": 0}).status_code == 422
    # 清理退出
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
