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

"""policy/lerobot_act（lerobot gRPC 流式客户端）测试 —— 进程内 fake AsyncInference 服务端，无 lerobot 官方 server。

覆盖：connect（Ready 握手 + 延迟下发 PolicyInstructions）、`infer_chunk` 按绝对步号取回整块
（策略只负责取推理结果；块缓存 / 三元切分 / 时序平滑由 motrix_edge.rtc 负责，见
tests/test_rtc.py）、must_go 语义、disconnect。
"""

import pickle
import queue
import threading
import time
from concurrent import futures
from contextlib import contextmanager

import grpc
import numpy as np
import pytest
import torch

from lerobot.async_inference.helpers import TimedAction
from lerobot.transport import services_pb2, services_pb2_grpc
from lerobot.transport.utils import python_object_to_bytes, receive_bytes_in_chunks
from motrix_edge.policy.lerobot_act.client import LerobotActClient


class _FakeAsyncInferenceServicer(services_pb2_grpc.AsyncInferenceServicer):
    """模仿 lerobot policy_server 的 AsyncInference 语义（不做真推理）：

    - SendObservations 聚合分块后解 pickle 得到 TimedObservation，把 timestep 入队；
    - GetActions 弹出一个待推理观测，返回其起始 timestep 起的 actions_per_chunk 步
      TimedAction。每块的动作为同一标量：默认 1.0；传 ``block_values`` 时按请求顺序
      取用（用于验证平滑聚合 old/new 差异）。
    """

    def __init__(self, actions_per_chunk: int = 3, dim: int = 2, block_values=None, idle_mode=None, delays=None):
        self.actions_per_chunk = actions_per_chunk
        self.dim = dim
        self.ready_calls = 0
        self.policy_calls = 0
        self.obs_calls = 0
        self.get_actions_calls = 0
        self.policy_data: bytes | None = None
        self.last_raw: dict | None = None
        self.last_obs_timestep: int | None = None
        self._obs_queue: "queue.Queue[int]" = queue.Queue()
        self._block_values = iter(block_values) if block_values is not None else None
        # idle_mode：None=正常应答；"empty"=GetActions 恒返回空块；"stale"=恒返回旧块
        # （timestep 不匹配）——用于验证客户端轮询预算与节流（不忙等）。
        self.idle_mode = idle_mode
        # delays：{RPC 名: 秒} 模拟服务端卡住，验证客户端各 RPC 的超时上限。
        self.delays = dict(delays or {})

    def _delay(self, name: str) -> None:
        if self.delays.get(name):
            time.sleep(self.delays[name])

    def Ready(self, request, context):  # noqa: N802
        self._delay("ready")
        self.ready_calls += 1
        return services_pb2.Empty()

    def SendPolicyInstructions(self, request, context):  # noqa: N802
        self._delay("policy")
        self.policy_calls += 1
        self.policy_data = request.data
        return services_pb2.Empty()

    def SendObservations(self, request_iterator, context):  # noqa: N802
        self._delay("observation")
        data = receive_bytes_in_chunks(request_iterator, None, threading.Event())
        timed = pickle.loads(data)  # noqa: S301 测试用（vendored 类）
        self.last_raw = timed.get_observation()
        self.last_obs_timestep = timed.get_timestep()
        self._obs_queue.put(timed.get_timestep())
        self.obs_calls += 1
        return services_pb2.Empty()

    def GetActions(self, request, context):  # noqa: N802
        self._delay("get_actions")
        self.get_actions_calls += 1
        if self.idle_mode == "empty":
            return services_pb2.Actions(data=b"")
        if self.idle_mode == "stale":  # 恒返回 timestep 0 起的旧块：永远不匹配请求步号
            stale = [
                TimedAction(timestamp=time.time(), timestep=i, action=torch.ones(self.dim))
                for i in range(self.actions_per_chunk)
            ]
            return services_pb2.Actions(data=python_object_to_bytes(stale))
        try:
            timestep = self._obs_queue.get(timeout=2.0)
        except queue.Empty:
            return services_pb2.Actions(data=b"")
        if self._block_values is not None:
            value = next(self._block_values, 1.0)
            action = torch.full((self.dim,), value)
        else:
            action = torch.ones(self.dim)
        chunk = [
            TimedAction(timestamp=time.time(), timestep=timestep + i, action=action)
            for i in range(self.actions_per_chunk)
        ]
        return services_pb2.Actions(data=python_object_to_bytes(chunk))


@contextmanager
def _serve(**kwargs):
    """起一个进程内 AsyncInference 假服务端（真 gRPC，不做真推理）。"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = _FakeAsyncInferenceServicer(**kwargs)
    services_pb2_grpc.add_AsyncInferenceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield port, servicer
    finally:
        server.stop(0)


@pytest.fixture()
def act_server():
    with _serve(actions_per_chunk=3, dim=2) as started:
        yield started


def _make_client(port, **overrides):
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "pretrained_name_or_path": "fake/act",
        "actions_per_chunk": 3,
    }
    cfg.update(overrides)
    return LerobotActClient(cfg)


def test_act_grpc_handshake_and_lazy_policy(act_server):
    """connect：Ready 握手成功；策略指令延后（首次 infer 才 SendPolicyInstructions）。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    assert servicer.ready_calls == 1
    assert servicer.policy_calls == 0  # 指令延后
    assert client.server_metadata["protocol"] == "lerobot/async-inference"
    client.disconnect()


def test_act_grpc_infer_chunk_returns_whole_block(act_server):
    """infer_chunk：按绝对步号上传观测一次，返回**整块**（含首步绝对步号）——不做缓存 / 切片。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

    chunk = client.infer_chunk(obs, index=0)
    assert chunk.height == 3  # 一次推理返回 actions_per_chunk 步
    assert chunk.dim == 2
    assert chunk.start_index == 0  # 首步绝对步号 = 请求的 index
    assert np.allclose(chunk.actions, 1.0)
    assert servicer.obs_calls == 1  # 每次调用真实上传一次观测
    assert servicer.policy_calls == 1  # 仅首次下发策略指令
    assert servicer.policy_data is not None
    client.disconnect()


def test_act_grpc_infer_chunk_uses_index_as_timestep(act_server):
    """infer_chunk：``index``（RTCManager 的绝对步号）作为 TimedObservation.timestep 上报。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

    chunk = client.infer_chunk(obs, index=7)
    assert servicer.last_obs_timestep == 7  # 服务端收到的观测 timestep = index
    assert chunk.start_index == 7
    assert servicer.obs_calls == 1
    client.disconnect()


def test_act_grpc_requires_pretrained_model(act_server):
    """缺 pretrained_name_or_path：首次 infer_chunk 拒绝（策略指令需要模型标识）。"""
    port, _ = act_server
    client = _make_client(port, pretrained_name_or_path=None)
    client.connect()
    with pytest.raises(ValueError, match="pretrained_name_or_path"):
        client.infer_chunk({"observations/qpos": np.zeros(2, dtype=np.float32)})
    client.disconnect()


def test_act_grpc_image_letterboxed(act_server):
    """图像上传前被 letterbox 到 224×224：等比缩放 + 上下留黑边，内容不变形。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    # 640×360 横向纯红图
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    img[:, :, 2] = 255  # R
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32), "observations/images/cam": img}
    client.infer_chunk(obs)

    got = servicer.last_raw["cam"]
    assert got.shape == (224, 224, 3)
    assert not got[:40].any()  # 上黑边
    assert not got[-40:].any()  # 下黑边
    # 中部内容行（等比缩放后内容区高 126，居中于 [49, 175)）保持纯红
    assert np.all(got[112, :, 2] == 255)
    client.disconnect()


def test_act_grpc_image_cameras_subset(act_server):
    """image_cameras：只下发策略用相机；多余相机（策略 image_features 没有的）被过滤，
    rename_cameras 把 edge 相机名改到策略训练名。"""
    port, servicer = act_server
    client = _make_client(port, image_cameras=["cam_head"], rename_cameras={"cam_head": "cam_front"})
    client.connect()
    obs = {
        "observations/qpos": np.zeros(2, dtype=np.float32),
        "observations/images/cam_head": np.zeros((64, 64, 3), dtype=np.uint8),
        "observations/images/cam_left_wrist": np.zeros((64, 64, 3), dtype=np.uint8),
    }
    client.infer_chunk(obs)
    raw = servicer.last_raw
    assert "cam_front" in raw  # 策略相机（rename 后）
    assert raw["cam_front"].shape == (224, 224, 3)
    assert "cam_left_wrist" not in raw  # 多余相机被过滤
    client.disconnect()


def test_act_grpc_observed_chunk_len_tracks_actual_block():
    """块长契约：lerobot-act 的 ``actions_per_chunk`` 是**请求值**（构造时先作声明），
    首次推理后以服务端实际返回的块步数覆盖（供 RTC 兜底校准 H）。"""
    with _serve(actions_per_chunk=3, dim=2) as (port, _servicer):
        client = _make_client(port, actions_per_chunk=5)  # 请求 5 步
        assert client.observed_chunk_len == 5  # 尚未推理：先按请求值声明
        client.connect()

        chunk = client.infer_chunk({"observations/qpos": np.zeros(2, dtype=np.float32)})
        assert chunk.height == 3  # 服务端实际只给 3 步
        assert client.observed_chunk_len == 3  # 实测覆盖声明值
        client.disconnect()


def test_act_grpc_receive_chunk_times_out_without_busy_wait():
    """服务端一直不返回动作块（GetActions 恒 Empty）：到 ``get_actions_timeout`` 抛
    TimeoutError（不挂死），且轮询有节流——单次 GetActions 超时取**剩余预算**，非忙等。"""
    with _serve(idle_mode="empty") as (port, servicer):
        client = _make_client(port, get_actions_timeout=0.3)
        client.connect()
        obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

        start = time.monotonic()
        with pytest.raises(TimeoutError, match="no action chunk for timestep"):
            client.infer_chunk(obs, index=100)
        assert time.monotonic() - start < 3.0  # 按 0.3s 预算返回，不无限等待
        assert 0 < servicer.get_actions_calls < 60  # 0.02s 节流：十几次级别，绝非忙等上千次
        client.disconnect()


def test_act_grpc_receive_chunk_accepts_first_non_empty_chunk():
    """不做步号门控（与官方 ``RobotClient.receive_actions`` 一致）：拿到非空块即采信。

    服务端可能因边界竞态 / 滞后推来**整体过期**的块（实际 ``timestep < 请求步号``）：
    这里照常返回并由 RTCManager 统一丢弃（计 ``stale_chunks``）——若在客户端丢，等于白
    扔一次推理，且本步拿不到动作。
    """
    with _serve(idle_mode="stale") as (port, servicer):
        client = _make_client(port, get_actions_timeout=0.3)
        client.connect()
        obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

        chunk = client.infer_chunk(obs, index=100)
        assert chunk is not None
        assert chunk.height == 3  # 服务端实际给的块（timestep 0..2，落后于请求的 100）
        assert chunk.start_index == 0
        assert servicer.get_actions_calls == 1  # 首个非空块即返回（不再为「步号匹配」反复轮询）
        client.disconnect()


@pytest.mark.parametrize(
    ("rpc", "config_key"),
    [
        ("ready", "connect_timeout"),
        ("policy", "policy_setup_timeout"),
        ("observation", "observation_timeout"),
    ],
)
def test_act_grpc_rpc_calls_are_time_limited(rpc, config_key):
    """各 RPC 都带时限：服务端卡住时按配置超时报错，不无限阻塞（否则 RTC 预取线程持有的
    single-flight 槽会被永久占用，后续 infer 全部拿不到动作）。"""
    with _serve(delays={rpc: 2.0}) as (port, _servicer):
        client = _make_client(port, **{config_key: 0.2})
        obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
        if rpc != "ready":
            client.connect()  # Ready 不慢，先正常握手（策略指令 / 观测上传阶段卡住）

        with pytest.raises(grpc.RpcError):
            if rpc == "ready":
                client.connect()
            else:
                client.infer_chunk(obs)
        client.disconnect()


def test_act_grpc_requires_ready_handshake_before_policy_instructions():
    """策略指令必须在 ``Ready`` 之后下发：服务端未进 running 时会**静默忽略**指令，
    后续推理只会一直拿到 Empty 直到超时——客户端直接报错，不让人误以为已就绪。"""
    with _serve() as (port, _servicer):
        client = _make_client(port)  # 未 connect（未 Ready）
        with pytest.raises(RuntimeError, match="not connected"):
            client.infer_chunk({"observations/qpos": np.zeros(2, dtype=np.float32)})


def test_act_prepare_is_noop_without_connection_or_observation():
    """``prepare`` 的契约与 openpi 一致：**未连接 / 无观测 → no-op（不抛错、不发指令）**。

    预热是优化（失败不致命，会话记 ``warmup_error``），不应因调用时机不当把调用方打挂；
    未握手的**推理**仍在 ``infer_chunk`` 里报错（见上一用例）。
    """
    with _serve() as (port, servicer):
        client = _make_client(port)  # 未 connect
        obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
        client.prepare(obs)  # 未连接 → no-op（旧实现会在此抛 RuntimeError）
        client.prepare()  # 无观测 → no-op
        assert servicer.policy_calls == 0  # 一条策略指令都没下发


def test_act_grpc_reconnect_resends_policy_instructions(act_server):
    """重连必须重发策略指令：服务端 ``Ready`` 会**重置会话状态**（清空 policy），
    不重发则重连后首个推理一直拿不到动作。"""
    port, servicer = act_server
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

    client = _make_client(port)
    assert client.chunk_seen is False  # 显式初始化（`chunk_seen` 是正式契约字段）
    client.connect()
    client.infer_chunk(obs)
    assert servicer.policy_calls == 1
    assert client.chunk_seen is True  # 本连接真正取到过块
    client.disconnect()

    client.connect()  # 重连（Ready 重置服务端会话）
    assert client.chunk_seen is False  # 连接级状态随之复位（否则预热会跳过取块）
    client.infer_chunk(obs)
    assert servicer.policy_calls == 2  # 指令重发（否则服务端没有策略）
    client.disconnect()


def test_act_grpc_repolicies_when_layout_changes(act_server):
    """布局（state 维数 / 相机集）变化后重发策略指令：服务端按 features 查表，
    旧 features 遇到新观测会 KeyError；像素值变化不算布局变化（不重发）。"""
    port, servicer = act_server
    client = _make_client(port)
    client.connect()
    one_cam = {
        "observations/qpos": np.zeros(2, dtype=np.float32),
        "observations/images/cam_head": np.zeros((32, 32, 3), dtype=np.uint8),
    }
    client.infer_chunk(one_cam)
    assert servicer.policy_calls == 1

    client.infer_chunk(one_cam)  # 同一布局：不重发（避免服务端重新加载模型）
    assert servicer.policy_calls == 1

    two_cams = {**one_cam, "observations/images/cam_right_wrist": np.zeros((32, 32, 3), dtype=np.uint8)}
    client.infer_chunk(two_cams)  # 新增相机：布局变化 → 重发
    assert servicer.policy_calls == 2
    client.disconnect()


def test_act_grpc_handshake_config_is_fixed_for_the_session(act_server):
    """握手级配置（模型路径 / 设备 / 块长 / 图像尺寸）**会话内改不生效**。

    它们随 ``SendPolicyInstructions``（Ready 之后）一次性下发，服务端据此加载 checkpoint、
    定块长；会话内改 ``policy_config`` 既不重发策略指令、已固化的块长与下发尺寸也不变。
    schema 侧对应 ``runtime: False``（见 test_policy.test_policy_config_runtime_keys_by_policy）
    —— 前端在会话内禁用这些输入框，避免「status 显示新值、推理仍用旧值」的静默陷阱；
    要生效请退出会话重进（重进会重新握手 + 重发策略指令）。
    """
    port, servicer = act_server
    client = _make_client(port)  # actions_per_chunk=3；image_size 取缺省 224
    client.connect()
    obs = {
        "observations/qpos": np.zeros(2, dtype=np.float32),
        "observations/images/cam_head": np.zeros((32, 32, 3), dtype=np.uint8),
    }
    client.infer_chunk(obs)
    assert servicer.policy_calls == 1
    assert pickle.loads(servicer.policy_data).actions_per_chunk == 3  # noqa: S301 测试用
    assert servicer.last_raw["cam_head"].shape[:2] == (224, 224)

    # 会话内改（infer config set 的写入路径）：不重发策略指令，也不改已下发的尺寸
    client.policy_config["actions_per_chunk"] = 8
    client.policy_config["image_size"] = 64
    client.infer_chunk(obs)
    assert servicer.policy_calls == 1  # 握手级配置在会话内固化
    assert servicer.last_raw["cam_head"].shape[:2] == (224, 224)
    client.disconnect()


def test_act_grpc_lerobot_features_match_official_shape(act_server):
    """下发的 features 与官方 ``hw_to_dataset_features(..., use_video=False)`` 同形：
    state 带形状 + 分量名；图像带 ``dtype=image`` + ``shape``(HWC) + ``names``
    （服务端 ``dataset_to_policy_features`` 会读这两项）。"""
    port, servicer = act_server
    client = _make_client(port, image_cameras=["cam_head"])
    client.connect()
    obs = {
        "observations/qpos": np.zeros(3, dtype=np.float32),
        "observations/images/cam_head": np.zeros((32, 32, 3), dtype=np.uint8),
        "observations/images/cam_left_wrist": np.zeros((32, 32, 3), dtype=np.uint8),
    }
    client.infer_chunk(obs)

    config = pickle.loads(servicer.policy_data)  # noqa: S301 测试用（vendored 类）
    features = config.lerobot_features
    assert features["observation.state"]["names"] == ["qpos_0", "qpos_1", "qpos_2"]
    assert features["observation.state"]["shape"] == (3,)
    assert features["observation.state"]["dtype"] == "float32"
    image = features["observation.images.cam_head"]  # 只声明下发的相机
    assert image["dtype"] == "image"
    assert image["shape"] == (224, 224, 3)  # letterbox 后的下发尺寸（HWC）
    assert image["names"] == ["height", "width", "channels"]
    assert "observation.images.cam_left_wrist" not in features  # image_cameras 之外的相机不声明
    client.disconnect()
