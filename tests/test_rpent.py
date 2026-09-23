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

"""server/rpent —— RPent 兼容 RPC 面（``POST /call``）：信封 / numpy 编解码 / ``env.*`` 转发。

设计见 wiki/design/motrix_edge_rpent_bridge.md。测试不依赖硬件与网络：fake adapter 记录
``rollout`` 调用，观测帧直接构造（含真实 JPEG 编解码，覆盖颜色通道回归）。
"""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from motrix_edge.adapter.base import (
    CAMERA_PREFIX,
    KEY_ACTION,
    KEY_GRIPPER,
    KEY_POSE,
    KEY_POSE_TARGET,
    KEY_QPOS,
    ActionSpace,
    HealthStatus,
    RobotAdapter,
    RobotCapabilities,
)
from motrix_edge.lease import BEIJING_TZ, Lease, LeaseManager, LeaseState
from motrix_edge.node import NodeState
from motrix_edge.server import create_app
from motrix_edge.server.rpent import (
    LAYOUT_RPENT_DUAL_FRANKA,
    RpentError,
    RpentService,
    from_wire,
    matrix_to_quat,
    matrix_to_rot6d,
    matrix_to_rpy,
    resolve_layout,
    rot6d_to_matrix,
    rpent_gripper_to_edge,
    rpy_to_matrix,
    to_wire,
)

BASE_CFG = {
    "identity": {"edge_id": "edge-test-001", "edge_name": "edge-test", "edge_version": "0.1.0"},
    "adapter": {"host": "127.0.0.1", "port": 8090},
    "server": {"host": "0.0.0.0", "port": 8000, "rpent": {"step_hz": 1000}},
}


# ---------------------------------------------------------------------------
# 假件：adapter（记录 rollout）/ node / 观测缓存 / 命令服务
# ---------------------------------------------------------------------------


class FakeAdapter(RobotAdapter):
    """双 6 维臂 + 每臂 1 夹爪（四个动作空间）+ 观测；记录 rollout 调用。

    RPent 眼里的「每臂 7 维 = 值（joint / pose 各 6）+ 夹爪」由桥接层拼出来，故本假件
    按**真实契约**声明：值维度按空间给（``ACTION_DIM_PER_ARM``），夹爪独立一维。

    ``pose_target`` = 机器人自己持有的**目标位姿**（RPent 布局）：收到 ``pose_delta`` 时叠加在
    它上面（模拟真机器人——基准是目标不是实测），并通过 ``observation_frame()`` 随观测发布。
    """

    ADAPTER_TYPE = "test_robot"
    ACTION_DIM_PER_ARM = {
        ActionSpace.JOINT.value: 6,
        ActionSpace.POSE.value: 6,
        ActionSpace.POSE_DELTA.value: 6,
        ActionSpace.GRIPPER.value: 1,
    }
    HOME = {ActionSpace.JOINT.value: [0.0] * 12, ActionSpace.GRIPPER.value: [1.0] * 2}
    POSE_FRAME = "fk"  # 位姿坐标系（仿 test robot：由同一拍 qpos 派生）
    ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.POSE, ActionSpace.POSE_DELTA, ActionSpace.GRIPPER)
    ARM_NAMES = ("left", "right")
    IMAGES = {"cam_head": (640, 480)}

    def __init__(self, *, pose: bool = True, track: bool = False, track_step: float = 0.05):
        if not pose:  # 机器人不提供位姿：不声明位姿空间（观测里也没有 observations/pose）
            self.ACTION_DIM_PER_ARM = {
                key: value
                for key, value in self.ACTION_DIM_PER_ARM.items()
                if key not in (ActionSpace.POSE.value, ActionSpace.POSE_DELTA.value)
            }
            self.ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.GRIPPER)
            self.pose_target = None
        else:
            # 目标位姿与实测位姿**同一布局**（每臂 6 维 xyz+rpy 的扁平向量，见 observations/pose）
            self.pose_target = np.asarray(default_pose(), dtype=np.float32).copy()
        super().__init__(name="Test Robot")
        self.images = ["cam_head"]
        self.rollout_calls: list[tuple[np.ndarray, ActionSpace]] = []
        self.reset_calls = 0
        self.refuse = False
        # 机器人状态（观测帧从它渲染）：实测关节 / 实测位姿 / 夹爪 + 目标位姿
        self.qpos = default_qpos()
        self.pose = default_pose()
        self.gripper = default_gripper()
        # ``track=True``：每出一帧实测位姿朝目标挪一步（模拟限速跟踪，能判到位）；
        # 缺省不动 = 走不到位（用于超时 / 停滞用例）
        self.track = track
        self.track_step = float(track_step)
        # ``apply_delta=False``：模拟机器人收到增量但目标没动（命令被丢弃）——用于 not_applied 用例
        self.apply_delta = True
        # ``target_shift``：模拟「同拍有第三方也改了目标」（遥操作接管 / CLI 直控）——用于 base_changed 用例
        self.target_shift: np.ndarray | None = None

    def observation_frame(self) -> dict:
        """当前机器人状态 → 观测帧（含 ``observations/pose_target``，与实测位姿同一拍）。"""
        if self.track and self.pose_target is not None:
            self._track_target()
        frame = make_obs(qpos=self.qpos, pose=self.pose, gripper=self.gripper)
        if self.pose_target is not None:
            frame[KEY_POSE_TARGET] = np.asarray(self.pose_target, dtype=np.float32).copy()
        return frame

    def _track_target(self) -> None:
        """限速跟踪：实测位姿每拍朝目标移一步（关节段 ``step_rad`` 的位姿等效）。"""
        current = np.asarray(self.pose, dtype=np.float64).reshape(-1).copy()
        target = np.asarray(self.pose_target, dtype=np.float64).reshape(-1)
        for index in range(len(self.ARM_NAMES)):
            start = index * self.ACTION_DIM_PER_ARM[ActionSpace.POSE.value]
            delta = target[start : start + 6] - current[start : start + 6]
            current[start : start + 6] += np.clip(delta, -self.track_step, self.track_step)
        self.pose = current.astype(np.float32)

    @property
    def capabilities(self) -> RobotCapabilities:
        keys = [KEY_QPOS, KEY_GRIPPER]
        if self.effective_pose_dim_per_arm() > 0:
            keys.append(KEY_POSE)
            keys.append(KEY_POSE_TARGET)
        return RobotCapabilities(
            action_dim=self.action_dim,
            action_dims=dict(self.action_dims),
            action_spaces=[space.value for space in self.ACTION_SPACES],
            observation_keys=keys,
            capabilities={},
        )

    def observe(self) -> dict | None:
        return None

    def health(self) -> HealthStatus:
        return HealthStatus(ok=True)

    def execute(self, action) -> None:
        pass

    def rollout(self, action, action_space=None) -> bool:
        space = self.normalize_action_space(action_space)
        vector = np.asarray(action, dtype=np.float32).reshape(-1)
        expected = self.action_dim_for(space)
        if vector.size != expected:  # 镜像真实现：维度不符 → 拒绝（不静默补齐）
            raise ValueError(f"rollout {space.value} action dim {vector.size} != {expected}")
        if space is ActionSpace.POSE_DELTA and not self.refuse and self.apply_delta:
            self._apply_pose_delta(vector)  # 机器人侧行为：增量叠在自己的目标上（不是实测）
        self.rollout_calls.append((vector.copy(), space))
        return not self.refuse  # True = 已下发；False = 遥操作中拒拍

    def _apply_pose_delta(self, values: np.ndarray) -> None:
        """增量叠加在**自己的目标位姿**上（每臂 chart 相加；基准是目标，不是实测）。"""
        if self.pose_target is None:
            return
        per_arm = self.ACTION_DIM_PER_ARM[ActionSpace.POSE.value]
        target = np.array(self.pose_target, dtype=np.float32)
        for index in range(len(self.ARM_NAMES)):
            start = index * per_arm
            target[start : start + per_arm] += values[start : start + per_arm]
        if self.target_shift is not None:  # 并发改动：基准已不是我们快照到的那个
            target = target + np.asarray(self.target_shift, dtype=np.float32)
        self.pose_target = target

    def reset(self) -> None:
        self.reset_calls += 1

    def safe_stop(self) -> None:
        pass


class FakeFrameManager:
    """观测缓存假件：直接给一帧，或**从假机器人状态渲染**（服务只调 ``latest()`` /
    读 ``image_size``）。

    ``obs`` 给定 → 固定帧（帧序列由测试自己排）；给 ``adapter`` → 每拍按适配器当前状态出帧
    （把 ``pose_delta`` 的落点反映到 ``observations/pose_target`` 上，模拟真机器人）。
    """

    image_size = (320, 240)

    def __init__(self, obs: dict | None = None, adapter: "FakeAdapter | None" = None):
        self._obs = dict(obs or {})
        self._adapter = adapter

    def latest(self) -> dict | None:
        if self._adapter is not None:
            return self._adapter.observation_frame()
        return self._obs


class FakeNode:
    def __init__(self, adapter: FakeAdapter, obs: dict | None = None):
        self.adapter = adapter
        self.adapter_name = "Test Robot"
        self.adapter_type = "test_robot"
        # 不给 obs → 帧从适配器状态渲染（默认：与 make_obs(default_qpos/pose/gripper) 同值）
        self.frame_manager = FakeFrameManager(obs, adapter if obs is None else None)
        self.state = NodeState.READY
        self.session = None
        self.adapter_health = type("Health", (), {"control_hz": 1000.0})()


class StubCommands:
    """命令服务假件：记录转发的 capability / 租约，回执可配（含 rejected）。"""

    def __init__(self, *, status: str = "ok", error: str | None = None):
        self.status = status
        self.error = error
        self.calls: list[dict] = []

    def execute(self, command_id, lease_id=None, capability=None, params=None, source=None):
        self.calls.append(
            {
                "command_id": command_id,
                "lease_id": lease_id,
                "capability": capability,
                "params": params,
                "source": source,
            }
        )
        return {"status": self.status, "executed": capability, "error": self.error, "data": {"state": "ready"}}


# ---------------------------------------------------------------------------
# 夹具 / 助手
# ---------------------------------------------------------------------------


def jpeg(rgb: np.ndarray) -> bytes:
    """RGB ndarray → JPEG bytes（与 adapter 出图同路径：RGB → BGR → imencode）。"""
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return buf.tobytes()


def make_obs(*, qpos=None, pose=None, gripper=None, action=None, pose_target=None, cameras=("cam_head",)) -> dict:
    obs: dict = {}
    if qpos is not None:
        obs[KEY_QPOS] = np.asarray(qpos, dtype=np.float32)
    if pose is not None:
        obs[KEY_POSE] = np.asarray(pose, dtype=np.float32)
    # ``action`` = **关节段目标**（机器人底层实际控制量）；不指定就等价于「没下发过指令」
    if action is not None:
        obs[KEY_ACTION] = np.asarray(action, dtype=np.float32)
    # ``pose_target`` = **目标位姿**（RPent 布局，每臂「位姿 6 + 夹爪 1」）：增量原语的基准与
    # settle 的参考目标；不指定则视为「机器人不发布该键」
    if pose_target is not None:
        obs[KEY_POSE_TARGET] = np.asarray(pose_target, dtype=np.float32)
    # 夹爪是**独立观测键**（常量：每臂 1 维，不拼在关节角里）；不指定就用默认值
    obs[KEY_GRIPPER] = default_gripper() if gripper is None else np.asarray(gripper, dtype=np.float32)
    for index, name in enumerate(cameras):
        rgb = np.zeros((4, 5, 3), dtype=np.uint8)
        rgb[..., 0] = 200 + index  # 红：彩色通道若被反转，断言会失败
        obs[f"{CAMERA_PREFIX}{name}"] = jpeg(rgb)
    return obs


def default_qpos() -> np.ndarray:
    """观测里的关节角（joint 空间：每臂 6 × 2 臂 = 12）。"""
    return np.arange(12, dtype=np.float32)


def default_gripper() -> np.ndarray:
    """观测里的夹爪（每臂 1，归一化 [0, 1]）—— ``observations/gripper``，与关节角分开。"""
    return np.asarray([0.25, 0.75], dtype=np.float32)


def rpent_state(joints=None, gripper=None) -> np.ndarray:
    """关节角 + 夹爪 → RPent 布局（每臂「值 6 + 夹爪 1」= 14）——观测 / 目标的期望值。"""
    joints = default_qpos() if joints is None else np.asarray(joints, dtype=np.float32).reshape(-1)
    values = default_gripper() if gripper is None else np.asarray(gripper, dtype=np.float32).reshape(-1)
    return np.concatenate([joints[:6], values[:1], joints[6:12], values[1:2]]).astype(np.float32)


def rpent_pose_target(pose) -> np.ndarray:
    """扁平位姿（每臂 ``xyz + rpy``）→ RPent 布局的位姿目标（每臂 ``位姿 6 + 夹爪 1``）。"""
    flat = np.asarray(pose, dtype=np.float32).reshape(-1)
    values = default_gripper()
    return np.concatenate([flat[:6], values[:1], flat[6:12], values[1:2]]).astype(np.float32)


def default_pose() -> np.ndarray:
    return np.asarray([0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 0.4, 0.5, 0.6, 0.0, 0.0, 0.0], dtype=np.float32)


def install_lease(leases: LeaseManager, lease_id: str = "ls_rpent_1") -> str:
    """直接安装租约镜像（等价 Console 签发下发；绕开 HTTP 租约路由）。"""
    from datetime import datetime, timedelta

    leases.install(
        Lease(
            lease_id=lease_id,
            edge_id="edge-test-001",
            holder_subject_id="operator-1",
            purpose="rollout",
            state=LeaseState.ACTIVE,
            expires_at=datetime.now(BEIJING_TZ) + timedelta(seconds=60),
            lease_version=1,
            ttl=60,
        )
    )
    return lease_id


@pytest.fixture
def env():
    """(service, node, adapter, leases, commands)：**已签发租约**，可直接调写方法。"""
    adapter = FakeAdapter()
    node = FakeNode(adapter)  # 观测帧从假机器人状态渲染（含 observations/pose_target）
    leases = LeaseManager()
    install_lease(leases)
    commands = StubCommands()
    service = RpentService(node, commands, leases=leases, base_cfg=BASE_CFG)
    return service, node, adapter, leases, commands


@pytest.fixture
def client(env):
    """``POST /call`` 的 HTTP 客户端（与原生面共用 app；租约已安装）。"""
    service, _node, _adapter, leases, commands = env
    return TestClient(create_app(BASE_CFG, commands=commands, lease_manager=leases, rpent=service))


# ---------------------------------------------------------------------------
# 线上编解码
# ---------------------------------------------------------------------------


def test_to_wire_tags_ndarray_and_scalar():
    """ndarray / NumPy 标量打 RPent 约定的 tag（形状 / dtype / 值可还原）。"""
    array = np.arange(6, dtype=np.float32).reshape(2, 3)
    tagged = to_wire(array)
    assert tagged["dtype"] == "float32" and tagged["shape"] == [2, 3]
    assert isinstance(tagged["__ndarray__"], str)  # base64 文本（JSON 可序列化）

    scalar = to_wire(np.float32(1.5))
    assert scalar == {"__npscalar__": 1.5, "dtype": "float32"}
    assert to_wire({"a": [np.uint8(3)], "b": "x"}) == {"a": [{"__npscalar__": 3, "dtype": "uint8"}], "b": "x"}


def test_from_wire_restores_ndarray_and_scalar():
    """解析 args：tag → ndarray（可写副本）/ NumPy 标量（保留 dtype）；未打 tag 的 dict 递归。"""
    array = np.arange(4, dtype=np.uint8)
    restored = from_wire(to_wire(array))
    assert isinstance(restored, np.ndarray) and restored.dtype == np.uint8 and np.array_equal(restored, array)
    restored[0] = 9  # 必须可写（frombuffer 的只读视图需拷贝）

    assert isinstance(from_wire(to_wire(np.int32(7))), np.int32)
    assert from_wire({"delta_xyz": to_wire(np.asarray([0.1, 0.0, 0.0], dtype=np.float32))})["delta_xyz"].shape == (3,)


# ---------------------------------------------------------------------------
# 租约规则 / 自描述
# ---------------------------------------------------------------------------


def test_healthz_and_self_description_are_lease_free():
    """握手与能力协商免租约（RPent 连接前会先轮询 healthz + get_env_meta）。"""
    adapter = FakeAdapter()
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    service = RpentService(node, StubCommands(), leases=LeaseManager(), base_cfg=BASE_CFG)

    assert service.call("healthz") == {"status": "ok"}
    meta = service.call("env.get_env_meta")
    assert meta["explicit_reset_only"] is True  # RPent dual_franka client 的硬要求
    assert meta["action_dim"] == 14 and meta["arms"] == ["left", "right"]
    assert meta["agent_observation"]["inline_cameras"] == ["cam_head"]  # 进模型上下文的相机
    assert meta["pose_convention"] == "xyz_rpy" and meta["gripper_range"] == [0.0, 1.0]
    cameras = service.call("env.get_camera_meta")
    assert cameras["cameras"] == {"cam_head": [640, 480]} and cameras["encoding"] == "uint8 HWC (RGB)"


def test_observation_and_writes_require_lease():
    """观测与写方法须持有活跃租约（与 /v1/preview、/v1/commands 同规则）。"""
    adapter = FakeAdapter()
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    service = RpentService(node, StubCommands(), leases=LeaseManager(), base_cfg=BASE_CFG)

    for method, kwargs in [("env.get_observation", {}), ("env.reset", {}), ("env.set_gripper", {"open": True})]:
        with pytest.raises(RpentError) as excinfo:
            service.call(method, kwargs=kwargs)
        assert excinfo.value.kind == "lease" and "lease" in str(excinfo.value)


def test_unknown_method_is_rejected():
    service = RpentService(FakeNode(FakeAdapter(), {}), None, leases=LeaseManager(), base_cfg=BASE_CFG)
    with pytest.raises(RpentError) as excinfo:
        service.call("env.nonexistent")
    assert excinfo.value.kind == "unknown_method" and "unknown RPC method" in str(excinfo.value)


def test_session_rpc_is_accepted_noop():
    """RPent 的 session RPC：edge 的隔离载体是 Edge 级租约，故只确认收到。"""
    service = RpentService(FakeNode(FakeAdapter(), {}), None, leases=LeaseManager(), base_cfg=BASE_CFG)
    assert service.call("session.register") == {"ok": True}
    assert service.call("session.close") == {"ok": True}


# ---------------------------------------------------------------------------
# 观测（含图像）
# ---------------------------------------------------------------------------


def test_observation_returns_states_and_ndarray_frames(env):
    """观测：``states`` = 同帧 qpos；相机帧解码为 uint8 HWC **RGB** ndarray（非 JPEG bytes）。"""
    service, _node, _adapter, _leases, _commands = env
    obs = service.call("env.get_observation")

    assert np.allclose(obs["states"], rpent_state())  # RPent 的 wrapped_state_vector（每臂 7）
    assert np.allclose(obs["qpos"], default_qpos()) and obs["arms"] == ["left", "right"]
    frames = obs["raw_camera_frames"]
    assert obs["images"] == ["cam_head"] and set(frames) == {"cam_head"}
    frame = frames["cam_head"]
    assert isinstance(frame, np.ndarray) and frame.dtype == np.uint8 and frame.shape == (4, 5, 3)
    assert frame[..., 0].mean() > 150 and frame[..., 2].mean() < 60  # RGB 未被 BGR 反转


def test_robot_state_reports_grippers_and_pose(env):
    service, _node, _adapter, _leases, _commands = env
    state = service.call("env.get_robot_state")
    assert np.allclose(state["wrapped_state_vector"], rpent_state())
    assert np.allclose(state["pose"], default_pose())
    assert state["gripper"] == {"left": 0.25, "right": 0.75}  # 独立夹爪键（每臂 1 维）


def test_task_language_reads_infer_session_prompt(env):
    service, node, _adapter, _leases, _commands = env
    assert service.call("env.get_task_language") is None
    node.session = type("Session", (), {"prompt": "把零件放好"})()
    assert service.call("env.get_task_language") == "把零件放好"


# ---------------------------------------------------------------------------
# 控制：转发到既有路径
# ---------------------------------------------------------------------------


def test_reset_goes_through_command_service(env):
    """``env.reset`` 走命令通道 ``robot/reset``（同一条路，含租约透传）。"""
    service, _node, _adapter, _leases, commands = env
    result = service.call("env.reset")
    assert result["ok"] is True and np.allclose(result["states"], rpent_state())
    assert commands.calls == [
        {
            "command_id": commands.calls[0]["command_id"],
            "lease_id": "ls_rpent_1",
            "capability": "robot/reset",
            "params": {},
            "source": "rpent",  # 来源标记（仅日志 / 排障）
        }
    ]
    assert commands.calls[0]["command_id"].startswith("rpent-")


def test_reset_reports_rejected_receipt_as_error():
    adapter = FakeAdapter()
    node = FakeNode(adapter, make_obs(qpos=default_qpos()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, StubCommands(status="rejected", error="robot busy"), leases=leases, base_cfg=BASE_CFG)
    with pytest.raises(RpentError) as excinfo:
        service.call("env.reset")
    assert "robot busy" in str(excinfo.value) and excinfo.value.kind == "state"


def test_move_delta_sends_delta_and_reports_robot_target(env):
    """``env.move_delta``：只下发**增量**（``pose_delta``），目标由机器人在自己的目标上叠加。

    旧实现把「当前**观测**位姿 + 增量」算成绝对目标下发：底层 MIT 有稳态误差，实测恒落后目标，
    于是每一条增量都把当前误差写进新目标（逐条累积）。现在基准只存在于机器人侧（同一拍读 / 叠 /
    解），回执里的 ``target`` 是**机器人解算出来的**目标位姿（取自 ``observations/pose_target``）。
    """
    service, _node, adapter, _leases, _commands = env
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.01, 0.0, 0.0]})

    # 下发：一条 ``pose_delta``（每臂 6 维增量），**不碰夹爪**（旧实现会顺手把实测夹爪当绝对目标重发）
    assert len(adapter.rollout_calls) == 1
    values, space = adapter.rollout_calls[-1]
    assert space is ActionSpace.POSE_DELTA and values.shape == (12,)
    assert values == pytest.approx([0.01, 0.0, 0.0, 0.0, 0.0, 0.0] * 2)

    # 回执：机器人侧的目标位姿（RPent 布局：每臂「位姿 6 + 夹爪 1」）
    target = np.asarray(result["target"])
    assert target.shape == (14,)
    assert result["action_space"] == "pose_delta" and result["target_source"] == "pose_target"
    assert target[:3] == pytest.approx([0.11, 0.2, 0.3])
    assert target[7:10] == pytest.approx([0.41, 0.5, 0.6])  # 未指定 arm → 两臂同加
    assert target[6] == pytest.approx(0.25) and target[13] == pytest.approx(0.75)  # 夹爪保持


def test_move_delta_single_arm_keeps_the_other(env):
    """指定 ``arm`` → 只改该臂，另一臂增量恒为 0（``rotate_delta`` 同形）。"""
    service, _node, adapter, _leases, _commands = env
    result = service.call("env.rotate_delta", kwargs={"arm": "right", "delta_rpy": [0.0, 0.1, 0.0]})

    values, space = adapter.rollout_calls[-1]
    assert space is ActionSpace.POSE_DELTA
    assert values[:6] == pytest.approx([0.0] * 6)  # 左臂增量全 0
    assert values[6:12] == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.1, 0.0])  # 右臂只加 ry
    target = np.asarray(result["target"])
    assert target[:6] == pytest.approx(list(default_pose()[:6]))  # 左臂目标不变
    assert target[11] == pytest.approx(0.1)  # 右臂段第 5 位 = ry（原为 0.0）+ 0.1


def test_move_delta_without_pose_is_rejected():
    """机器人不提供位姿（不声明 ``pose`` / ``pose_delta`` 空间）→ 拒绝：不猜语义下发。"""
    adapter = FakeAdapter(pose=False)
    node = FakeNode(adapter)
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, StubCommands(), leases=leases, base_cfg=BASE_CFG)
    with pytest.raises(RpentError) as excinfo:
        service.call("env.move_delta", kwargs={"delta_xyz": [0.01, 0.0, 0.0]})
    assert excinfo.value.kind == "unsupported" and "does not support pose_delta actions" in str(excinfo.value)
    assert adapter.rollout_calls == []


def test_move_delta_never_falls_back_to_observed_pose_base():
    """机器人只声明 ``pose``（旧版，未声明 ``pose_delta``）→ **拒绝**，不静默回退旧基准。

    旧基准（实测位姿 + 增量）在 MIT 下会累积误差；与其潦草兼容，不如响亮报错让人升级机器人进程。
    """
    adapter = FakeAdapter()
    adapter.ACTION_DIM_PER_ARM = {
        key: value for key, value in adapter.ACTION_DIM_PER_ARM.items() if key != ActionSpace.POSE_DELTA.value
    }
    adapter.ACTION_SPACES = (ActionSpace.JOINT, ActionSpace.POSE, ActionSpace.GRIPPER)
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(FakeNode(adapter), StubCommands(), leases=leases, base_cfg=BASE_CFG)

    with pytest.raises(RpentError) as excinfo:
        service.call("env.move_delta", kwargs={"delta_xyz": [0.01, 0.0, 0.0]})

    assert excinfo.value.kind == "unsupported"
    assert adapter.rollout_calls == []  # 没有下发任何动作


def test_set_gripper_only_changes_gripper(env):
    """``env.set_gripper``：关节段原样重发（保持当前姿态），夹爪单独走 gripper 空间。"""
    service, _node, adapter, _leases, _commands = env
    service.call("env.set_gripper", kwargs={"arm": "right", "open": False})
    joints, joints_space = adapter.rollout_calls[-2]
    grippers, gripper_space = adapter.rollout_calls[-1]
    assert joints_space is ActionSpace.JOINT and gripper_space is ActionSpace.GRIPPER
    assert joints == pytest.approx(list(default_qpos()))  # 关节目标不动
    assert list(grippers) == pytest.approx([0.25, 0.0])  # 只改右夹爪（闭合）

    service.call("env.set_gripper", kwargs={"open": True})
    grippers, _space = adapter.rollout_calls[-1]
    assert list(grippers) == pytest.approx([1.0, 1.0])


def test_set_gripper_bases_on_joint_target_not_measured_joints():
    """``env.set_gripper`` 的基座是 ``action``（**关节段目标**），不是实测 qpos。

    底层是 MIT 力矩控制，实测关节恒落后目标一个稳态误差（``τ_gravity / kp``）：拿实测当基座，
    连「只改夹爪」都会把这次的稳态误差写进新目标，并在手臂还在执行上一条目标时把它拽回实测值
    ——逐个原语累积成显著偏差。
    """
    measured = default_qpos()  # 实测关节（落后目标）
    commanded = default_qpos() + 0.2  # 上一条指令目标（手臂仍在往这里走）
    frames = [make_obs(qpos=measured, action=commanded, pose=default_pose())]
    service, adapter = settle_service(frames=frames)
    result = service.call("env.set_gripper", kwargs={"arm": "right", "open": False})
    joints, joints_space = adapter.rollout_calls[-2]
    assert joints_space is ActionSpace.JOINT
    assert list(joints) == pytest.approx(list(commanded))  # 目标原样保留，没被拉回实测
    assert result["base"] == "target"


def test_set_gripper_falls_back_to_qpos_base_without_target():
    """机器人没发布 ``action``（从未下发过指令）→ 退实测 qpos，回执 ``base`` 标注。"""
    frames = [make_obs(qpos=default_qpos(), pose=default_pose())]
    service, adapter = settle_service(frames=frames)
    result = service.call("env.set_gripper", kwargs={"open": True})
    joints, _space = adapter.rollout_calls[-2]
    assert list(joints) == pytest.approx(list(default_qpos()))
    assert result["base"] == "qpos"


def test_set_gripper_unknown_arm_is_rejected(env):
    service, _node, adapter, _leases, _commands = env
    with pytest.raises(RpentError) as excinfo:
        service.call("env.set_gripper", kwargs={"arm": "middle", "open": True})
    assert excinfo.value.kind == "argument" and adapter.rollout_calls == []


def test_step_returns_gym_tuple_and_rejects_refusal(env):
    service, _node, adapter, _leases, _commands = env
    action = rpent_state()  # RPent 布局：每臂「关节 6 + 夹爪 1」
    result = service.call("env.step", args=(action.tolist(),))
    assert isinstance(result, list) and len(result) == 5
    assert result[1:] == [0.0, False, False, {"action_space": "joint"}]
    assert np.allclose(result[0]["states"], action)

    adapter.refuse = True  # 遥操作（人工接管）中：机器人拒拍
    with pytest.raises(RpentError) as excinfo:
        service.call("env.step", args=(action.tolist(),))
    assert excinfo.value.kind == "state" and "refused" in str(excinfo.value)


def test_step_rejects_wrong_action_dim(env):
    """维度不符 → 拒绝（不静默补齐；校验归 adapter）。"""
    service, _node, _adapter, _leases, _commands = env
    with pytest.raises(RpentError) as excinfo:
        service.call("env.step", args=([0.1, 0.2],))
    assert excinfo.value.kind == "argument"


def test_chunk_step_counts_frames_and_reports_refusals(env):
    service, _node, adapter, _leases, _commands = env
    actions = np.tile(rpent_state(), (3, 1))
    result = service.call("env.chunk_step", args=(actions,), kwargs={"return_all_frames": True})
    assert result["sent"] == 3 and result["requested"] == 3 and result["refused"] == 0
    assert isinstance(result["observation"], list) and len(result["observation"]) == 3
    assert result["terminated"] is False and result["truncated"] is False
    assert np.allclose(result["states"], rpent_state())

    adapter.refuse = True
    with pytest.raises(RpentError) as excinfo:
        service.call("env.chunk_step", args=(actions[:2],))
    assert excinfo.value.kind == "state" and "'refused every action'" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# HTTP 信封（POST /call）
# ---------------------------------------------------------------------------


def test_http_healthz_and_unknown_method_envelope(client):
    """``POST /call`` **始终 200**：失败在 body 里用 ``ok=false`` 表达（RPent 只读 body）。"""
    ok = client.post("/call", json={"method": "healthz", "args": [], "kwargs": {}, "session_id": None})
    assert ok.status_code == 200 and ok.json() == {"ok": True, "result": {"status": "ok"}}

    bad = client.post("/call", json={"method": "env.nope", "args": [], "kwargs": {}})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False and bad.json()["kind"] == "unknown_method"


def test_http_envelope_decodes_tagged_args_and_encodes_numpy_result(client):
    """入参 ``__ndarray__`` 还原（RPent 发动作块）+ 出参 ndarray 打 tag（否则 JSON 会炸）。"""
    body = {
        "method": "env.step",
        "args": [to_wire(rpent_state())],
        "kwargs": {},
        "session_id": None,
    }
    r = client.post("/call", json=body)
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    states = payload["result"][0]["states"]
    assert states["dtype"] == "float32" and states["shape"] == [14]
    assert np.allclose(from_wire(states), rpent_state())


def test_http_observation_serializes_images_as_base64_tag(client):
    """相机帧经 HTTP 传输：``__ndarray__`` base64（RPent 侧解码后写 PNG）。"""
    payload = client.post("/call", json={"method": "env.get_observation", "args": [], "kwargs": {}}).json()
    assert payload["ok"] is True
    frame = from_wire(payload["result"]["raw_camera_frames"]["cam_head"])
    assert isinstance(frame, np.ndarray) and frame.shape == (4, 5, 3) and frame.dtype == np.uint8


def test_http_lease_failure_is_envelope_not_4xx(env):
    """未签发租约：仍 200，body ``ok=false`` + ``kind=lease``（agent 能读到原因）。"""
    _service, node, _adapter, _leases, commands = env
    bare = RpentService(node, commands, leases=LeaseManager(), base_cfg=BASE_CFG)
    client = TestClient(create_app(BASE_CFG, commands=commands, lease_manager=LeaseManager(), rpent=bare))
    r = client.post("/call", json={"method": "env.get_observation", "args": [], "kwargs": {}})
    assert r.status_code == 200 and r.json()["kind"] == "lease"


def test_http_call_without_service_returns_envelope():
    """未注入 RpentService：回 ``ok=false``（不是 501——RPent 只读 body 的 error）。"""
    client = TestClient(create_app(BASE_CFG))
    r = client.post("/call", json={"method": "healthz"})
    assert r.status_code == 200 and r.json() == {
        "ok": False,
        "error": "rpent service not enabled",
        "kind": "unavailable",
    }


def test_rpent_introspection_endpoint(client, env):
    """``GET /v1/rpent``：方法清单 + 租约解析状态（免租约）。"""
    info = client.get("/v1/rpent").json()
    assert info["enabled"] is True and info["endpoint"] == "/call"
    assert "env.get_env_meta" in info["methods"] and "env.chunk_step" in info["methods"]
    lease = info["lease"]
    assert lease["lease_id"] == "ls_rpent_1" and lease["source"] == "active" and lease["satisfied"] is True
    # 启动自检字段：RPent 连上就 env.reset，必须能提前看到租约还能撑多久
    assert lease["required"] is True and lease["expires_at"] is not None and lease["expires_in_s"] > 0

    assert TestClient(create_app(BASE_CFG)).get("/v1/rpent").status_code == 501


# ---------------------------------------------------------------------------
# 外部动作块布局（rpent/dual_franka → edge 笛卡尔目标）
# ---------------------------------------------------------------------------


def rpent_block(xyz, rpy, grip: float) -> np.ndarray:
    """一个 RPent 每臂块：``xyz(3) + rot6d(6) + 夹爪(±1)``。"""
    return np.asarray(
        [*np.asarray(xyz, dtype=np.float32), *matrix_to_rot6d(rpy_to_matrix(rpy)), grip], dtype=np.float32
    )


def delta_service(*, track: bool = False, track_step: float = 0.05, settle: dict | None = None):
    """位姿增量原语的 service（帧从**有状态**假机器人渲染：``pose_delta`` 真的改它的目标）。

    ``track=True`` → 实测位姿每拍朝目标挪一步（能判到位）；缺省不动（超时 / 停滞用例）。
    返回 ``(service, adapter)``；容差配置与 ``settle_service`` 同一套（快节奏）。
    """
    adapter = FakeAdapter(track=track, track_step=track_step)
    node = FakeNode(adapter)
    leases = LeaseManager()
    install_lease(leases)
    cfg = {
        "settle": {
            "enabled": True,
            "pos_tol": 0.005,
            "rot_tol": 0.05,
            "timeout_s": 0.05,
            "stall_s": 0.02,
            "poll_s": 0.001,
            **(settle or {}),
        }
    }
    return RpentService(node, StubCommands(), leases=leases, base_cfg={**BASE_CFG, "server": {"rpent": cfg}}), adapter


def layout_service(*, dry_run: bool = True, pose_dim: int = 6, enabled_arms=None, qpos=None, pose=None):
    """配了 ``action_layout`` 的 service（已签发租约）；返回 (service, adapter)。

    ``pose_dim``：机器人是否提供位姿（0 = 不提供 → 假件不声明 ``pose`` 空间）。
    """
    adapter = FakeAdapter(pose=bool(pose_dim))
    if enabled_arms is not None:
        adapter.configure(enabled_arms=list(enabled_arms))
    node = FakeNode(
        adapter,
        make_obs(
            qpos=default_qpos() if qpos is None else qpos,
            pose=pose,
            pose_target=pose,  # 与 observations/pose 同一布局（每臂 6 维扁平）
        ),
    )
    leases = LeaseManager()
    install_lease(leases)
    base_cfg = {
        **BASE_CFG,
        "server": {**BASE_CFG["server"], "rpent": {"action_layout": LAYOUT_RPENT_DUAL_FRANKA, "dry_run": dry_run}},
    }
    return RpentService(node, StubCommands(), leases=leases, base_cfg=base_cfg), adapter


def test_rot6d_and_rpy_are_mutually_inverse():
    """rot6d ↔ rpy：随机姿态往返一致；带噪 rot6d 也能正交化成合法旋转。"""
    for rpy in ([0.0, 0.0, 0.0], [0.3, -0.7, 1.2], [-1.0, 0.4, -2.5]):
        matrix = rpy_to_matrix(rpy)
        recovered = rot6d_to_matrix(matrix_to_rot6d(matrix))
        assert np.allclose(recovered, matrix, atol=1e-9)
        assert np.allclose(rpy_to_matrix(matrix_to_rpy(matrix)), matrix, atol=1e-9)

    noisy = matrix_to_rot6d(rpy_to_matrix([0.2, 0.5, -0.3])) + 1e-3
    matrix = rot6d_to_matrix(noisy)
    assert np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-9)  # 正交化后仍是合法旋转
    assert np.isclose(np.linalg.det(matrix), 1.0)


def test_gripper_domain_mapping_is_clamped():
    """对方 ``+1 = 张开 / -1 = 闭合`` → edge ``[0, 1]``；超界先限幅。"""
    assert rpent_gripper_to_edge(1.0) == 1.0
    assert rpent_gripper_to_edge(-1.0) == 0.0
    assert rpent_gripper_to_edge(0.0) == 0.5
    assert rpent_gripper_to_edge(5.0) == 1.0 and rpent_gripper_to_edge(-5.0) == 0.0


def test_layout_step_converts_absolute_pose_and_gripper():
    """布局转换：20 维块 → 每臂 ``[xyz, rpy, gripper]`` 绝对目标（**不下发**，dry_run）。"""
    service, adapter = layout_service()
    block = np.concatenate(
        [rpent_block([0.1, 0.2, 0.3], [0.0, 0.0, 0.5], 1.0), rpent_block([0.4, 0.5, 0.6], [0.1, 0.0, 0.0], -1.0)]
    )
    result = service.call("env.step", args=(block,))
    info = result[4]
    assert info["dry_run"] is True and info["action_space"] == "pose"
    assert info["action_layout"] == LAYOUT_RPENT_DUAL_FRANKA
    assert adapter.rollout_calls == []  # dry_run：不动机器人
    target = info["converted"]
    assert target.shape == (14,)
    assert target[:3] == pytest.approx([0.1, 0.2, 0.3])
    assert target[3:6] == pytest.approx([0.0, 0.0, 0.5])  # rpy 由 rot6d 还原
    assert target[6] == pytest.approx(1.0)  # +1 → 张开
    assert target[7:10] == pytest.approx([0.4, 0.5, 0.6])
    assert target[10:13] == pytest.approx([0.1, 0.0, 0.0])
    assert target[13] == pytest.approx(0.0)  # -1 → 闭合


def test_layout_chunk_step_dry_run_reports_without_sending():
    """``dry_run`` 的 chunk：回转换结果 + 零下发（真机联调先对数值）。"""
    service, adapter = layout_service(dry_run=True)
    chunk = np.tile(np.concatenate([rpent_block([0.11, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0)] * 2), (2, 1))
    result = service.call("env.chunk_step", args=(chunk,))
    assert result["dry_run"] is True and result["sent"] == 0 and result["requested"] == 2
    assert result["converted"].shape == (2, 14)
    assert adapter.rollout_calls == []


def test_layout_chunk_step_sends_converted_pose_targets():
    """非 dry_run：逐帧按位姿目标下发，action_space 由布局决定（忽略调用方传的 joint）。"""
    service, adapter = layout_service(dry_run=False)  # 绝对目标 → 不需位姿观测
    chunk = np.stack(
        [
            np.concatenate(
                [
                    rpent_block([0.12, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0),
                    rpent_block([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], -1.0),
                ]
            ),
            np.concatenate(
                [
                    rpent_block([0.13, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0),
                    rpent_block([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], -1.0),
                ]
            ),
        ]
    )
    result = service.call("env.chunk_step", args=(chunk,), kwargs={"action_space": "joint"})
    assert result["sent"] == 2 and result["action_space"] == "pose"
    # 逐帧拆两条下发：位姿值（该布局空间）+ 夹爪（独立空间）
    assert [space for _target, space in adapter.rollout_calls] == [
        ActionSpace.POSE,
        ActionSpace.GRIPPER,
    ] * 2
    poses = [target for target, space in adapter.rollout_calls if space is ActionSpace.POSE]
    assert poses[0][0] == pytest.approx(0.12)
    assert poses[1][0] == pytest.approx(0.13)


def test_layout_ignores_arms_not_enabled_on_edge():
    """布局里有而 edge 未启用的臂 → 忽略该臂块（单臂配置也能吃双 20 维块）。"""
    service, adapter = layout_service(dry_run=True, enabled_arms=["right"])
    block = np.concatenate(
        [rpent_block([9.9, 9.9, 9.9], [0.0, 0.0, 0.0], 1.0), rpent_block([0.4, 0.5, 0.6], [0.0, 0.0, 0.0], -1.0)]
    )
    target = service.call("env.step", args=(block,))[4]["converted"]
    assert target.shape == (7,)
    assert target[:3] == pytest.approx([0.4, 0.5, 0.6])  # 只有 right 块被采纳
    assert target[6] == pytest.approx(0.0)


def test_layout_backfills_uncovered_arm_from_current_pose(monkeypatch):
    """edge 启用但布局未覆盖的臂 → 用当前观测位姿回填（保持不动的臂）。"""
    from motrix_edge.server.rpent import layout as rpent_layout

    monkeypatch.setitem(rpent_layout._RPENT_LAYOUTS, "rpent/left_only", ("left",))  # 单臂布局
    adapter = FakeAdapter()
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(
        node,
        StubCommands(),
        leases=leases,
        base_cfg={**BASE_CFG, "server": {"rpent": {"action_layout": "rpent/left_only", "dry_run": True}}},
    )
    target = service.call("env.step", args=(rpent_block([0.2, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0),))[4]["converted"]
    assert target.shape == (14,)
    assert target[:3] == pytest.approx([0.2, 0.0, 0.0])  # left：来自块
    assert target[7:10] == pytest.approx(list(default_pose()[6:9]))  # right：当前位姿回填
    assert target[13] == pytest.approx(0.75)  # right 夹爪 = 当前值（原样保持）


def test_layout_wrong_dim_is_rejected():
    """块维度与布局不符 → 拒绝（不静默补齐）。"""
    service, adapter = layout_service()
    with pytest.raises(RpentError) as excinfo:
        service.call("env.step", args=(np.zeros(7, dtype=np.float32),))
    assert excinfo.value.kind == "argument" and adapter.rollout_calls == []


def test_layout_unknown_name_is_rejected():
    """未知布局名（配置笔误）→ 拒绝且给出可用取值。"""
    with pytest.raises(RpentError) as excinfo:
        resolve_layout("rpent/nope")
    assert excinfo.value.kind == "unsupported" and LAYOUT_RPENT_DUAL_FRANKA in str(excinfo.value)


def test_layout_without_matching_arm_names_is_rejected():
    """布局臂名与 adapter 臂名不相交 → 拒绝（不静默下发）。"""
    adapter = FakeAdapter()
    adapter.enabled_arms = ["arm1"]
    adapter.action_dim = 7
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(
        node,
        StubCommands(),
        leases=leases,
        base_cfg={**BASE_CFG, "server": {"rpent": {"action_layout": LAYOUT_RPENT_DUAL_FRANKA}}},
    )
    with pytest.raises(RpentError) as excinfo:
        service.call("env.step", args=(np.zeros(20, dtype=np.float32),))
    assert excinfo.value.kind == "unsupported" and adapter.rollout_calls == []


# ---------------------------------------------------------------------------
# RPent 侧 review 的四条（P0-1 / P0-2 / P1-3 / P1-4）
# ---------------------------------------------------------------------------


class SteppingFrameManager:
    """每次 ``latest()`` 前进一步（模拟机器人逐拍靠近目标）；帧用完就停在最后一帧。"""

    image_size = (320, 240)

    def __init__(self, frames: list[dict]):
        self._frames = list(frames) or [{}]
        self._index = 0

    def latest(self) -> dict:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


def settle_service(*, frames, pose_dim: int = 6, settle: dict | None = None, commands=None):
    """配了 ``settle``（快节奏）的 service；观测帧序列由 ``frames`` 控制。"""
    adapter = FakeAdapter(pose=bool(pose_dim))
    node = FakeNode(adapter, {})
    node.frame_manager = SteppingFrameManager(frames)
    leases = LeaseManager()
    install_lease(leases)
    cfg = {
        "settle": {
            "enabled": True,
            "pos_tol": 0.005,
            "rot_tol": 0.05,
            "timeout_s": 0.05,
            "stall_s": 0.02,
            "poll_s": 0.001,
            **(settle or {}),
        }
    }
    return RpentService(
        node, commands or StubCommands(), leases=leases, base_cfg={**BASE_CFG, "server": {"rpent": cfg}}
    ), adapter


def test_camera_meta_includes_agent_observation():
    """P0-1 回归：RPent 的 ``dump_state`` 只读 ``get_camera_meta`` 决定内联哪几路图 ——
    缺 ``agent_observation`` 会回落到 ``["d455"]``，而我们的相机叫 cam_head… → 模型盲跑。"""
    adapter = FakeAdapter()
    service = RpentService(FakeNode(adapter, {}), None, leases=LeaseManager(), base_cfg=BASE_CFG)
    cameras = service.call("env.get_camera_meta")

    assert cameras["enabled_cameras"] == ["cam_head"]
    assert cameras["agent_observation"] == {"inline_cameras": ["cam_head"], "auxiliary_cameras": []}
    # 与 env_meta 的相机段逐字段一致（同一 helper 派生，防两处漂移）
    meta = service.call("env.get_env_meta")
    for key in ("cameras", "enabled_cameras", "image_size", "agent_observation", "observation_camera_map"):
        assert cameras[key] == meta[key]


def test_env_meta_exposes_lease_and_settle_contract():
    """P1-3：启动阶段就能知道“写操作会不会被拒”“原语会不会阻塞到到位”。"""
    adapter = FakeAdapter()
    node = FakeNode(adapter, {})
    leases = LeaseManager()
    service = RpentService(node, StubCommands(), leases=leases, base_cfg=BASE_CFG)
    meta = service.call("env.get_env_meta")
    assert meta["lease"] == {
        "lease_id": None,
        "source": None,
        "satisfied": False,
        "required": True,
        "state": None,
        "expires_at": None,
        "expires_in_s": None,
        "reason": "no Edge lease installed (no active lease)",
    }
    assert meta["settle"] == {
        "enabled": True,
        "pos_tol_m": 0.01,  # 按 MIT 实际稳态误差标定（只有 P/D、无重力前馈）
        "rot_tol_rad": 0.05,
        "timeout_s": 5.0,
        "max_timeout_s": 90.0,  # 逐次覆盖的上限（客户端 HTTP 超时 120s）
        "target_wait_s": 1.0,  # 增量下发后等「命令落地」的上限（pose_target 跃迁）
        "image_source": "native",
        "dry_run": False,  # 启动自检：dry-run 的节点会回 sent: false / reached: null
        "action_layout": None,
    }
    assert meta["cartesian_dim_per_arm"] == 7
    assert meta["pose_frame"] == "fk"  # 读（位姿观测）与写（笛卡尔动作）必须同系

    install_lease(leases, "ls_meta")
    assert service.call("env.get_env_meta")["lease"]["satisfied"] is True


def test_lease_satisfied_means_usable_not_merely_resolved():
    """``satisfied`` = 现在调写方法能过，不只是“解析出了 id”。

    pinned ``lease_id`` 但租约未安装时：旧语义会给 ``satisfied: true``（但调用时照样 ``kind=lease``）
    ——这个洞会被 RPent 的启动自检当成“可以跑”，故改成反映“可用”并给 ``reason``。
    """
    adapter = FakeAdapter()
    leases = LeaseManager()
    cfg = {**BASE_CFG, "server": {**BASE_CFG["server"], "rpent": {"lease_id": "ls_pinned"}}}
    service = RpentService(FakeNode(adapter, {}), None, leases=leases, base_cfg=cfg)

    lease = service.call("env.get_env_meta")["lease"]
    assert lease["lease_id"] == "ls_pinned" and lease["source"] == "pinned"
    assert lease["satisfied"] is False and "not installed" in lease["reason"]
    with pytest.raises(RpentError) as excinfo:  # 自检与实际行为一致
        service.call("env.get_robot_state")
    assert excinfo.value.kind == "lease"

    install_lease(leases, "ls_pinned")
    lease = service.call("env.get_env_meta")["lease"]
    assert lease["satisfied"] is True and lease["state"] == "active" and lease["reason"] is None


def test_dry_run_never_sends_write_primitives_or_reset():
    """安全回归：``dry_run`` 下四条写原语与 ``env.reset`` 都**不下发**（真机不动）。

    旧实现：写原语直接下发、``env.reset`` 照样打命令通道，而回执又声称 ``reason: dry_run``
    ——“以为不会动但会动”。
    """
    service, adapter = layout_service(dry_run=True, pose_dim=6, pose=default_pose())
    commands = service._commands

    move = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})
    rotate = service.call("env.rotate_delta", kwargs={"delta_rpy": [0.0, 0.0, 0.1]})
    gripper = service.call("env.set_gripper", kwargs={"open": False})
    recover = service.call("env.recover_joint_posture", kwargs={"reason": "slip"})
    reset = service.call("env.reset")

    for receipt in (move, rotate, gripper, recover, reset):
        assert receipt["dry_run"] is True and receipt["sent"] is False
        assert receipt["reached"] is None and receipt["reason"] == "dry_run"
    assert move["target"].shape == (14,) and gripper["action"].shape == (14,)  # 数值仍可核对
    assert adapter.rollout_calls == [] and adapter.reset_calls == 0  # 一帧都没下发
    assert commands.calls == []  # env.reset 也没打命令通道


def test_dry_run_guard_blocks_any_missed_push():
    """兜底：若将来有路径绕过早返回，``_push_action`` 响亮报错而不是默默把机器人动了。"""
    service, adapter = layout_service(dry_run=True)
    with pytest.raises(RpentError) as excinfo:
        service._push_action(np.zeros(14, dtype=np.float32), ActionSpace.JOINT)
    assert excinfo.value.kind == "state" and "dry_run" in str(excinfo.value)
    assert adapter.rollout_calls == []


def test_settle_status_exposes_dry_run_for_startup_checks():
    """自描述能看出节点处于 dry-run（否则 agent 只会在 ``reached: null`` 里打转）。"""
    service, _adapter = layout_service(dry_run=True)
    assert service.settle_status()["dry_run"] is True
    assert service.settle_status()["action_layout"] == LAYOUT_RPENT_DUAL_FRANKA


def test_settle_override_timeout_is_capped_at_max():
    """逐次覆盖也封顶：超了会把结构化回执变成客户端 HTTP 超时异常。"""
    service, _adapter = settle_service(frames=[make_obs(qpos=default_qpos(), pose=default_pose())])
    assert service._settle_config.with_override({"timeout_s": 500.0}).timeout_s == 90.0
    assert service._settle_config.timeout_s == 0.05  # 部署配置本身不受影响


def test_settle_receipt_carries_effective_tolerances():
    """回执带生效容差：agent 自己区分“还差一点”（final_err 接近 tol）与“受阻”（stalled）。"""
    service, _adapter = delta_service(settle={"stall_s": 0.005, "timeout_s": 5.0})
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})
    assert result["stalled"] is True
    assert result["settle_pos_tol"] == 0.005 and result["settle_rot_tol"] == 0.05
    assert result["final_err"] > result["settle_pos_tol"]  # 模型可据此判“还没到 / 撞住了”


def test_settle_judges_position_and_rotation_separately():
    """位置与姿态**分别**比容差（不把米和弧度混进一个阈值）。

    回归：旧实现拿 ``max(位置, 姿态)`` 去比 ``pos_tol``（米），姿态容差被压成 0.005 rad，
    ``rot_tol`` 形同虚设 → 姿态差 0.03 rad（本应在 0.05 内）也会永远判不到。
    """
    service, _adapter = settle_service(frames=[make_obs(qpos=default_qpos(), pose=default_pose())] * 20)
    target = rpent_pose_target(default_pose())  # 每臂 [xyz(3), rpy(3), 夹爪(1)]
    target[0] += 0.002  # 位置差 2mm ≤ pos_tol 5mm
    target[3] += 0.03  # 姿态差 0.03 rad ≤ rot_tol 0.05

    receipt = service._settle_action(target, [0, 1], ActionSpace.POSE)
    assert receipt["reached"] is True
    assert receipt["final_err_m"] == pytest.approx(0.002, abs=1e-5)
    assert receipt["final_err_rad"] == pytest.approx(0.03, abs=1e-5)
    assert receipt["final_err"] == pytest.approx(0.03, abs=1e-5)  # 主误差 = 两者较大者


def test_settle_receipt_reports_per_unit_errors_for_joint_space():
    """关节空间只回 ``final_err_rad``（``final_err_m`` 为 None）：不拿位置量纲去比关节。"""
    service, _adapter = settle_service(frames=[make_obs(qpos=default_qpos(), pose=default_pose())] * 20)
    receipt = service._settle_action(rpent_state(default_qpos() + 0.001), [0, 1], ActionSpace.JOINT)

    assert receipt["final_err_m"] is None
    assert receipt["final_err_rad"] == pytest.approx(0.001, abs=1e-5)


def test_default_tolerances_absorb_mit_steady_state_error():
    """包内**兜底**容差（位置 1cm / 姿态 0.05rad）能吸收 MIT 的静态误差：0.03 rad 算到位。

    部署值在 ``edge.yml``（当前 5cm / 0.4rad，有意更宽以盖住静态误差）；本用例没给
    ``server.rpent.settle``，走的就是 ``SettleConfig`` 的兜底值。

    底层只有 P/D、无重力前馈，“设定什么关节就是什么关节”不成立——容差小于稳态误差时
    ``reached`` 永远不成立，每个写原语都会走满 ``stall_s`` / ``timeout_s``（agent 看到「全 stalled」）。
    """
    adapter = FakeAdapter()
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    service = RpentService(node, StubCommands(), leases=LeaseManager(), base_cfg=BASE_CFG)

    receipt = service._settle_action(rpent_state(default_qpos() + 0.03), [0, 1], ActionSpace.JOINT)
    assert receipt["reached"] is True and "stalled" not in receipt
    status = service.settle_status()
    assert status["pos_tol_m"] == 0.01 and status["rot_tol_rad"] == 0.05  # 自描述能看出当前标定值


def test_native_image_source_returns_full_resolution_frames():
    """默认 ``image_source: native``：直读 adapter 原图 + 同一拍状态（RPent 原样落盘并内联）。"""
    adapter = FakeAdapter()
    native = np.zeros((480, 640, 3), dtype=np.uint8)
    native[..., 0] = 7
    adapter.observe = lambda: {
        KEY_QPOS: default_qpos(),
        KEY_POSE: default_pose(),
        f"{CAMERA_PREFIX}cam_head": jpeg(native),
    }
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))  # 缓存是 4x5
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, None, leases=leases, base_cfg=BASE_CFG)

    obs = service.call("env.get_observation")
    assert obs["image_source"] == "native"
    assert obs["raw_camera_frames"]["cam_head"].shape == (480, 640, 3)
    assert obs["states"].shape == (14,)


def test_observation_falls_back_to_preview_when_adapter_has_no_frame():
    """读不到原图（瞬态无帧 / 机器人忙）→ 回落缓存，不把 RPC 搞挂。"""
    adapter = FakeAdapter()  # observe() → None
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, None, leases=leases, base_cfg=BASE_CFG)

    obs = service.call("env.get_observation")
    assert obs["image_source"] == "preview"
    assert obs["raw_camera_frames"]["cam_head"].shape == (4, 5, 3)
    assert obs["states"].shape == (14,)


def test_preview_image_source_skips_native_read():
    """``image_source: preview`` → 不碰 adapter（省带宽 / 省机器人一次 HTTP）。"""
    adapter = FakeAdapter()
    calls = {"observe": 0}

    def _observe():
        calls["observe"] += 1
        return {f"{CAMERA_PREFIX}cam_head": jpeg(np.zeros((480, 640, 3), dtype=np.uint8))}

    adapter.observe = _observe
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    cfg = {"server": {"rpent": {"image_source": "preview"}}}
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, None, leases=leases, base_cfg=cfg)

    obs = service.call("env.get_observation")
    assert obs["image_source"] == "preview" and calls["observe"] == 0
    assert obs["raw_camera_frames"]["cam_head"].shape == (4, 5, 3)


def test_chunk_step_frames_stay_on_preview_cache():
    """动作块逐帧观测面向 VLA（不喂 LLM）→ 走缓存，不逐帧拉原图。"""
    adapter = FakeAdapter()
    calls = {"observe": 0}

    def _observe():
        calls["observe"] += 1
        return {f"{CAMERA_PREFIX}cam_head": jpeg(np.zeros((480, 640, 3), dtype=np.uint8))}

    adapter.observe = _observe
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, None, leases=leases, base_cfg=BASE_CFG)

    result = service.call(
        "env.chunk_step", args=(np.stack([rpent_state(), rpent_state()]),), kwargs={"return_all_frames": True}
    )
    assert len(result["observation"]) == 2
    assert all(frame["image_source"] == "preview" for frame in result["observation"])
    assert calls["observe"] == 0


def test_camera_meta_reports_native_size_and_artifact_safe_names():
    """相机名必须是合法 artifact 基名（无 ``.`` / ``/``），且原生分辨率单独声明。"""
    adapter = FakeAdapter()
    service = RpentService(FakeNode(adapter, {}), None, leases=LeaseManager(), base_cfg=BASE_CFG)
    cameras = service.call("env.get_camera_meta")

    assert cameras["cameras"] == {"cam_head": [640, 480]}
    assert cameras["observation_image_size"] == [640, 480]  # 观测图给原图
    assert cameras["image_size"] == [320, 240]  # WebRTC / 预览缓存仍是降采样
    for name in cameras["agent_observation"]["inline_cameras"]:
        assert "." not in name and "/" not in name


def test_robot_state_has_per_arm_blocks_with_quat_tcp_pose():
    """P1-4：``env.get_robot_state`` 除扁平字段外再给每臂块（RPent 机器人包的期望形态）。"""
    service, _adapter = settle_service(frames=[make_obs(qpos=default_qpos(), pose=default_pose())])
    state = service.call("env.get_robot_state")

    left = state["left_arm"]
    assert np.allclose(left["arm_joint_position"], rpent_state()[:7])  # 每臂「关节 6 + 夹爪」
    assert left["gripper_open"] is False  # 左夹爪 0.25 < 0.5
    assert state["right_arm"]["gripper_open"] is True  # 右夹爪 0.75 ≥ 0.5
    # tcp_pose = [x, y, z, qx, qy, qz, qw]（与 RPent 的 Rotation.from_quat 同约定）
    tcp = left["tcp_pose"]
    assert tcp.shape == (7,) and np.allclose(tcp[:3], default_pose()[:3])
    assert np.isclose(np.linalg.norm(tcp[3:]), 1.0)
    assert np.allclose(tcp[3:], matrix_to_quat(rpy_to_matrix(default_pose()[3:6])), atol=1e-9)


def test_robot_state_omits_tcp_pose_without_pose_observation():
    """没有位姿观测时不给假的 ``tcp_pose``（宁可缺字段，不给错数据）。"""
    service, _adapter = settle_service(frames=[make_obs(qpos=default_qpos())], pose_dim=0)
    state = service.call("env.get_robot_state")
    assert "tcp_pose" not in state["left_arm"] and state["left_arm"]["gripper_open"] is False


def test_move_delta_blocks_until_reached():
    """P0-2：下发后阻塞到到位（RPent 紧接着 dump_state，不能读到未动的那一帧）。

    帧从**有状态**假机器人渲染：``pose_delta`` 真的改它的目标，实测位姿按限速跟踪——所以这里同时
    验证了「下发 → 目标落地 → 等到位」全链路（含 ``observations/pose_target`` 当参考）。
    """
    service, adapter = delta_service(track=True, track_step=0.005)

    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})

    assert adapter.rollout_calls  # 确实下发了
    assert result["reached"] is True
    assert result["final_err"] <= 0.005  # 位置容差
    assert 0.0 <= result["elapsed_s"] < 1.0
    assert "stalled" not in result and "timeout" not in result


def test_move_delta_reports_not_applied_when_target_never_moves():
    """机器人目标位姿一直不动（命令没落地 / 目标区不可用）→ ``reached: null`` + ``not_applied``。

    绝不能用「下发前拿到的旧目标」当参考——那会把「实测本就在旧目标附近」误判成到位。
    """
    adapter = FakeAdapter()
    adapter.apply_delta = False  # 机器人收到增量但目标没动（命令被丢弃 / 进程忙）
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(
        FakeNode(adapter),
        StubCommands(),
        leases=leases,
        base_cfg={**BASE_CFG, "server": {"rpent": {"settle": {"target_wait_s": 0.01}}}},
    )

    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})

    assert result["reached"] is None and result["not_applied"] is True
    assert result["target_source"] == "predicted"  # 只回预测值，不谎报这是机器人的目标


def test_move_delta_flags_base_changed_when_target_moves_elsewhere():
    """目标位姿被第三方改动（遥操作接管 / CLI 直控）→ 回执标 ``base_changed``，不当成自己的增量结果。"""
    adapter = FakeAdapter()
    # 并发改动：本层下发的增量落到目标上时，目标已另外被推了 +5cm（y 方向，远超容差）
    adapter.target_shift = np.array([0.0, 0.05, 0.0, 0.0, 0.0, 0.0] * 2)
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(FakeNode(adapter), StubCommands(), leases=leases, base_cfg=BASE_CFG)

    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.01, 0.0, 0.0]})

    assert result["base_changed"] is True
    # 回执里的目标仍是**机器人实际的目标**（第三方改动后的），不是我们预测的那个
    target = np.asarray(result["target"])
    assert target[1] == pytest.approx(0.25)  # 左臂 y：0.2 + 第三方 0.05
    assert target[8] == pytest.approx(0.55)  # 右臂 y：0.5 + 第三方 0.05
    assert target[7] == pytest.approx(0.41)  # 右臂 x：0.4 + 自己的 0.01


def test_move_delta_reports_timeout_when_never_reached():
    """目标已落地但机械臂一直不动 → 超时回 ``reached: false`` + ``timeout``（不谎报到位）。"""
    service, _adapter = delta_service(settle={"stall_s": 10.0})  # 不触发停滞，先撞超时
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})
    assert result["reached"] is False and result["timeout"] is True
    assert result["final_err"] > 0.005 and result["settle_timeout_s"] == 0.05


def test_move_delta_reports_stall_early():
    """误差在 ``stall_s`` 内没改善 → 提前回 ``stalled``（撞到东西 / 力不足）。"""
    service, _adapter = delta_service(settle={"stall_s": 0.005, "timeout_s": 5.0})
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})
    assert result["reached"] is False and result["stalled"] is True
    assert result["elapsed_s"] < 1.0  # 不是等满 timeout


def test_settle_reports_null_when_robot_has_no_pose():
    """机器人不提供位姿（``pose_dim = 0``）→ ``reached: null`` + 原因（不谎报）。"""
    service, _adapter = settle_service(
        frames=[make_obs(qpos=default_qpos())] * 20, pose_dim=0, settle={"enabled": True}
    )
    target = np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    receipt = service._settle_action(target, [0, 1], ActionSpace.POSE)
    assert receipt["reached"] is None and "observations/pose" in receipt["reason"]


def test_settle_disabled_reports_null():
    """``settle.enabled: false`` → 不阻塞，``reached: null``（调用方自己轮询）。"""
    service, _adapter = delta_service(settle={"enabled": False})
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0]})
    assert result["reached"] is None and result["reason"] == "settle disabled"
    assert result["target_source"] == "pose_target"  # 不等到位也照样回机器人侧的目标


def test_settle_per_call_override_can_disable_waiting():
    """逐次 ``settle=False`` → 不阻塞、``reached: null``（长程脚本想自己轮询时用）。"""
    service, _adapter = delta_service()
    result = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0], "settle": False})
    assert result["reached"] is None and result["reason"] == "settle disabled"


def test_settle_per_call_override_shortens_timeout_and_tolerance():
    """逐次覆盖单项：``timeout_s`` 更短先撞超时；``pos_tol`` 更宽则立刻算到位。"""
    service, _adapter = delta_service(settle={"stall_s": 10.0, "timeout_s": 5.0})
    hard = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0], "settle": {"timeout_s": 0.001}})
    assert hard["reached"] is False and hard["timeout"] is True
    assert hard["settle_timeout_s"] == pytest.approx(0.001) and hard["elapsed_s"] < 1.0

    service, _adapter = delta_service()
    loose = service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0], "settle": {"pos_tol": 1.0}})
    assert loose["reached"] is True and loose["settle_timeout_s"] == 0.05  # 未覆盖的仍取部署配置


def test_settle_per_call_override_rejects_bad_type():
    """``settle`` 传非法类型 → ``kind=invalid_params``（不静默当默认值）。"""
    service, _adapter = delta_service()
    with pytest.raises(RpentError) as excinfo:
        service.call("env.move_delta", kwargs={"delta_xyz": [0.02, 0.0, 0.0], "settle": "fast"})
    assert excinfo.value.kind == "invalid_params" and "settle" in str(excinfo.value)


def test_set_gripper_waits_for_gripper_to_close():
    """``set_gripper`` 也等到位（夹爪是独立键）：``observations/gripper`` 走到目标值。"""
    open_frames = [make_obs(qpos=default_qpos(), gripper=[1.0, 1.0], pose=default_pose())]
    closed_frames = [make_obs(qpos=default_qpos(), gripper=[0.0, 0.0], pose=default_pose()) for _ in range(20)]

    service, _adapter = settle_service(frames=open_frames + closed_frames)
    result = service.call("env.set_gripper", kwargs={"open": False})
    assert result["reached"] is True and result["final_err"] <= 0.05
    assert result["action"][6] == pytest.approx(0.0) and result["action"][13] == pytest.approx(0.0)


def test_recover_joint_posture_keeps_grippers():
    """P1-4：``recover_joint_posture`` = 关节回 home + **保持各夹爪当前开合**（不再整体回 home）。"""
    held = np.array([0.0, 1.0], dtype=np.float32)  # 左夹爪夹住、右夹爪张开
    frames = [make_obs(qpos=default_qpos(), gripper=held, pose=default_pose())]
    frames += [make_obs(qpos=np.zeros(12), gripper=held, pose=default_pose()) for _ in range(20)]

    service, adapter = settle_service(frames=frames)
    result = service.call("env.recover_joint_posture", kwargs={"reason": "slip"})

    target = np.asarray(result["target"])  # RPent 布局：每臂「关节 6 + 夹爪 1」
    assert adapter.reset_calls == 0  # 不再调 adapter.reset()
    assert np.allclose(target[:6], 0.0) and np.allclose(target[7:13], 0.0)  # 关节回 home
    assert target[6] == pytest.approx(0.0) and target[13] == pytest.approx(1.0)  # 夹爪保持
    joints, joint_space = adapter.rollout_calls[-2]
    grippers, gripper_space = adapter.rollout_calls[-1]
    assert joint_space is ActionSpace.JOINT and np.allclose(joints, 0.0)
    assert gripper_space is ActionSpace.GRIPPER and list(grippers) == pytest.approx([0.0, 1.0])
    assert result["gripper_preserved"] is True and result["reached"] is True


def test_recover_falls_back_to_adapter_reset_without_home():
    """adapter 没有 ``HOME['joint']`` 声明 → 退回 ``adapter.reset()`` 并在回执里标注（不静默换语义）。"""
    adapter = FakeAdapter()
    adapter.HOME = {}
    node = FakeNode(adapter, make_obs(qpos=default_qpos(), pose=default_pose()))
    leases = LeaseManager()
    install_lease(leases)
    service = RpentService(node, StubCommands(), leases=leases, base_cfg=BASE_CFG)
    result = service.call("env.recover_joint_posture")
    assert adapter.reset_calls == 1 and result["fallback"] == "adapter.reset()"
    assert result["gripper_preserved"] is False
