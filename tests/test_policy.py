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

"""policy 包单元测试 —— 格式契约 / 动作块 / 工厂，无硬件、无网络可跑。"""

import cv2
import numpy as np
import pytest

from motrix_edge.policy import POLICY_REGISTRY, get_policy
from motrix_edge.policy.contract import (
    resize_with_pad,
    to_rgb_uint8,
)


@pytest.fixture
def qpos():
    return np.zeros(14, dtype=np.float32)


def test_resize_with_pad_center_pads():
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    out = resize_with_pad(img, 224, 224)
    assert out.shape == (224, 224, 3)
    assert out.dtype == np.uint8


def test_jpeg_roundtrip_preserves_rgb_channels():
    # 已知 RGB 纯色块：验证 cv2 解码/编码路径没有把 R/B 通道对调（openpi 模型要求 RGB）
    rgb = np.zeros((16, 16, 3), dtype=np.uint8)
    rgb[..., 0] = 200  # R 强
    rgb[..., 1] = 100
    rgb[..., 2] = 50  # B 弱
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok
    jpeg = buf.tobytes()
    decoded = to_rgb_uint8(jpeg)
    # jpeg 有损，允许容差；若 R/B 对调则 R 均值≈50、B≈200，必然失败
    assert abs(decoded[..., 0].mean() - 200) < 20
    assert abs(decoded[..., 2].mean() - 50) < 20


class _FakeTransport:
    """计数 transport：支持 connect/close，记录最近 payload，返回固定动作块 [horizon, dim]。"""

    def __init__(self, horizon=2, dim=2, metadata=None):
        self.calls = 0
        self.close_calls = 0
        self.horizon = horizon
        self.dim = dim
        self.server_metadata = metadata
        self.last_payload = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self):
        self._connected = True

    def close(self):
        self.close_calls += 1
        self._connected = False

    def request(self, payload):
        self.calls += 1
        self.last_payload = payload
        return {"actions": np.ones((self.horizon, self.dim), dtype=np.float32)}


def test_openpi_connect_without_action_horizon_metadata():
    """官方服务端 metadata 不提供 action_horizon（原生 openpi 常态）也能连上，不再报错。

    块长不由 metadata 协商（以实测 ``observed_chunk_len`` 为准）；连接失败清理
    （refresh 语义：close 既有 / 半开连接）仍须保持。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({})
    transport = _FakeTransport(metadata={"model": "piper", "action_dim": 14})
    client._transport = transport
    client.connect()
    assert client.server_metadata == {"model": "piper", "action_dim": 14}
    assert client.observed_chunk_len is None  # 块长只由实测回填，connect 阶段不猜
    client.disconnect()
    assert client.server_metadata == {}


def test_openpi_exposes_server_metadata():
    from motrix_edge.policy.openpi.client import OpenPIClient

    metadata = {"model": "test-policy", "action_horizon": 16, "action_dim": 14}
    client = OpenPIClient({})
    client._transport = _FakeTransport(metadata=metadata)
    client.connect()
    assert client.server_metadata == metadata
    assert client.observed_chunk_len is None  # metadata 不作为块长来源
    client.disconnect()
    assert client.server_metadata == {}


def test_openpi_adopts_server_declared_cameras():
    """服务端 metadata 声明相机清单（openpi piper 分支的 ``policy_metadata.cameras``）时以它为准：
    服务端按这些名字取图（多送没用、少送报错），所以下发过滤要跟它一致——且与「先 connect 还是
    先 bind_adapter」无关（取属性的方式算，不靠写入顺序）。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient
    from motrix_edge.policy.openpi.contract import OPENPI_KEY_IMAGES

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    obs = {
        "observations/qpos": np.zeros(14, dtype=np.float32),
        "observations/images/cam_head": rgb,
        "observations/images/cam_right_wrist": rgb,
    }
    declared = ["cam_head", "cam_left_wrist", "cam_right_wrist"]  # piper 分支声明的齐全清单

    # 先 connect 再 bind_adapter（会话实际顺序相反）——两种顺序结果必须一致
    client = OpenPIClient({})
    client._transport = _FakeTransport(horizon=2, dim=2, metadata={"cameras": declared})
    client.connect()
    client.bind_adapter(camera_names=["cam_head", "cam_right_wrist"])  # adapter 只有两个相机
    client.infer_chunk(obs)
    assert set(client._transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head", "cam_right_wrist"}
    client.disconnect()

    client2 = OpenPIClient({})
    client2.bind_adapter(camera_names=["cam_head", "cam_right_wrist"])
    client2._transport = _FakeTransport(metadata={"cameras": declared})
    client2.connect()
    client2.infer_chunk(obs)
    assert set(client2._transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head", "cam_right_wrist"}
    client2.disconnect()

    # 官方 openpi 不声明任何布局字段 → 用 adapter 启用集（多送的相机不会出现）
    client3 = OpenPIClient({})
    client3._transport = _FakeTransport(metadata={"reset_pose": [0, 0, 0, 0, 0, 0]}, horizon=2, dim=2)
    client3.connect()
    client3.bind_adapter(camera_names=["cam_head"])
    client3.infer_chunk(obs)
    assert set(client3._transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head"}
    assert client3._server_cameras is None
    client3.disconnect()


def test_openpi_image_size_is_read_live():
    """image_size **每次请求现读**：会话内 ``infer config set image_size`` 下一块立即按新尺寸下发。

    openpi 无握手状态（每请求独立变换），故 schema 标 ``runtime=True``；lerobot-act 的图像尺寸
    是握手级配置（随策略指令下发），会话内改不生效，见 test_lerobot_act_client。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"image_size": 64})
    client._transport = _FakeTransport(horizon=2, dim=2)
    client.connect()
    obs = {
        "observations/qpos": np.zeros(2, dtype=np.float32),
        "observations/images/cam_head": np.zeros((32, 48, 3), dtype=np.uint8),
    }
    client.infer_chunk(obs)
    assert client._transport.last_payload["images"]["cam_head"].shape[:2] == (64, 64)

    client.policy_config["image_size"] = 96  # 运行期改（infer config set 的写入路径）
    client.infer_chunk(obs)
    assert client._transport.last_payload["images"]["cam_head"].shape[:2] == (96, 96)
    client.disconnect()


def test_policy_config_runtime_keys_by_policy():
    """``runtime`` 口径：**会话内改立即生效**的键才标 True。

    - openpi：``prompt`` / ``image_size`` 每请求现读 → 会话内可改；
    - 公共项 ``host`` / ``port``（推理端点）：**会话级**（进入会话时构造传输层并固化）→
      ``runtime: False``，与其它会话级键同一语义（没有专门命令，也没有「连接后锁定」轴）；
    - lerobot-act：模型路径 / 设备 / 块长 / 图像尺寸都是**握手级**（只在 Ready +
      SendPolicyInstructions 时下发，服务端据此加载 checkpoint / 定块长），会话内改要退出重进
      → 全部标 ``runtime: False``。
    """
    from motrix_edge.policy import policy_config_items, policy_config_runtime_keys

    assert policy_config_runtime_keys("openpi") == {"prompt", "image_size"}
    assert policy_config_runtime_keys("lerobot-act") == set()
    # 端点是必需项（没配好连不上推理节点），且属会话级
    endpoint = {item["key"]: item for item in policy_config_items("openpi") if item.get("group") == "endpoint"}
    assert set(endpoint) == {"host", "port"}
    assert all(item["runtime"] is False for item in endpoint.values())  # 会话级（进入会话时固化）
    assert all(item["required"] is False for item in endpoint.values())  # 后端不硬性要求（前端按 group=endpoint 门控）
    assert (endpoint["port"]["min"], endpoint["port"]["max"]) == (1, 65535)
    # 预热门控（公共项）：缺省要求先 infer connect（连接 + prepare + 取一块丢弃，不下发动作）
    warmup = next(item for item in policy_config_items("openpi") if item["key"] == "warmup_required")
    assert warmup["type"] == "bool" and warmup["default"] is True and warmup["runtime"] is False


def test_set_policy_config_clears_empty_and_rejects_non_integer():
    """配置写入的取值口径（所有配置项一视同仁，含 host / port）：

    - 空值（``None`` / 空串）= **清除该项**（删键回到代码缺省，``written`` 记 ``None``）——
      不把 ``str(None)`` 当文本写成字面量 ``"None"``，也不写空串（空串 host 会拼出 `ws://`
      在连接时才报错）；
    - 必填项（prompt / 模型路径）空值 → ``ValueError``：脏值会清空 ``missing``、绕开推理门控；
    - ``int`` 项只接受整数：``bool`` / 带小数的浮点 / 越界 → ``ValueError``（端口不静默截断）；
    - **全量校验通过才写入**：一批里任一项非法 → 整批拒绝，不留部分写入。
    """
    from motrix_edge.command import set_policy_config

    cfg = {"policy": {"type": "openpi", "host": "127.0.0.1", "port": 8000}}
    assert set_policy_config(cfg, "openpi", {"host": None, "port": 9000}) == {"host": None, "port": 9000}
    assert "host" not in cfg["policy"]
    assert cfg["policy"]["port"] == 9000
    assert set_policy_config(cfg, "openpi", {"prompt": "把零件放好"}) == {"prompt": "把零件放好"}
    for bad in ({"port": True}, {"port": 9000.5}, {"port": "abc"}, {"port": 65536}):
        with pytest.raises(ValueError):
            set_policy_config(cfg, "openpi", bad)
    for empty_required in ({"prompt": None}, {"prompt": "   "}):
        with pytest.raises(ValueError):
            set_policy_config(cfg, "openpi", empty_required)
    assert cfg["policy"]["port"] == 9000  # 失败项不落地
    assert cfg["policy"]["prompt"] == "把零件放好"
    # 一批里先合法后非法 → 整批不落地（合法项也不写入，调用方无需回滚）
    assert set_policy_config(cfg, "openpi", {"host": "10.0.0.9"}) == {"host": "10.0.0.9"}
    with pytest.raises(ValueError):
        set_policy_config(cfg, "openpi", {"host": None, "prompt": ""})
    assert cfg["policy"]["host"] == "10.0.0.9"
    assert cfg["policy"]["prompt"] == "把零件放好"
    # 未知键也一样：整批拒绝（不只看“本次是否包含非法值”）
    with pytest.raises(ValueError):
        set_policy_config(cfg, "openpi", {"port": 9001, "rename_cameras": {}})
    assert cfg["policy"]["port"] == 9000


def test_openpi_prepare_is_noop_without_connection_or_observation():
    """``prepare`` 契约（两个策略客户端一致）：未连接 / 无观测 → no-op，不抛错也不发请求。"""
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({})
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
    client.prepare(obs)  # 未连接 → no-op
    client.prepare()  # 无观测 → no-op
    assert transport.calls == 0


def test_openpi_observed_chunk_len_from_first_chunk():
    """块长以**实测**为准（服务端不一定声明）：预热那一块测出 16 步 → ``observed_chunk_len=16``
    （RTC 据此兜底校准块长上限 H）；配置不管，实测多长就多长。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}

    client = OpenPIClient({})
    client._transport = _FakeTransport(horizon=16, dim=2)
    client.connect()
    assert client.observed_chunk_len is None  # 尚未推理 → 未观测
    client.prepare(obs)  # 预热：发一帧触发模型加载，同时实测块长
    assert client.observed_chunk_len == 16
    client.disconnect()

    client2 = OpenPIClient({"action_horizon": 8})  # 配置不影响实测口径
    client2._transport = _FakeTransport(horizon=16, dim=2)
    client2.connect()
    client2.infer_chunk(obs)
    assert client2.observed_chunk_len == 16
    client2.disconnect()


def test_openpi_infer_chunk_returns_raw_chunk():
    """OpenPIClient.infer_chunk：每次调用真实请求一次，返回**原始动作块**（不做缓存 / 切片）。

    块缓存 / 三元切分 / 时序平滑由 RTCManager 负责（见 tests/test_rtc.py）——策略只取结果。
    """
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 2})
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    client.connect()

    obs = {"observations/qpos": np.zeros(2, dtype=np.float32)}
    chunk = client.infer_chunk(obs)
    assert chunk.height == 2  # 整块返回（[horizon, dim]）
    assert chunk.dim == 2
    assert chunk.start_index == 0  # 未传 index → 0
    assert np.allclose(chunk.actions, 1.0)
    assert transport.calls == 1
    # 再次调用仍真实请求（不再「块内不请求」——缓存归 RTCManager）
    chunk2 = client.infer_chunk(obs, index=5)
    assert chunk2.start_index == 5  # 回填 RTCManager 的绝对步号
    assert transport.calls == 2


def _jpeg_bytes(rgb):
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    assert ok
    return buf.tobytes()


def test_openpi_sends_official_flat_observation():
    """OpenPIClient 按官方 flat 契约组装观测：state=qpos、图像为 uint8 数组（非 jpeg bytes）、
    相机集来自 bind_adapter（adapter 启用的相机名，非 edge.yml）、rename_cameras 改名、
    prompt 动态携带。"""
    from motrix_edge.policy.openpi.client import OpenPIClient
    from motrix_edge.policy.openpi.contract import OPENPI_KEY_IMAGES, OPENPI_KEY_PROMPT, OPENPI_KEY_STATE

    client = OpenPIClient(
        {
            "action_horizon": 2,
            "image_size": 16,
            "rename_cameras": {"cam_head": "base_0_rgb"},
            "prompt": "open the box",
        }
    )
    transport = _FakeTransport(horizon=2, dim=2)
    client._transport = transport
    # 布局来自 adapter 运行时配置（InferSession 进入时 bind_adapter）：只启用 cam_head + cam_right_wrist
    client.bind_adapter(action_dim=7, camera_names=["cam_head", "cam_right_wrist"])
    client.connect()

    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    rgb[..., 0] = 200
    obs = {
        "observations/qpos": np.arange(2, dtype=np.float32),
        "observations/images/cam_head": _jpeg_bytes(rgb),  # jpeg bytes → 改名 base_0_rgb
        "observations/images/cam_left_wrist": _jpeg_bytes(rgb),  # 未启用（不在 bind 相机集）→ 过滤
        "observations/images/cam_right_wrist": rgb,  # 数组直传
    }
    assert client.infer_chunk(obs) is not None

    payload = transport.last_payload
    assert np.array_equal(payload[OPENPI_KEY_STATE], obs["observations/qpos"])
    assert payload[OPENPI_KEY_PROMPT] == "open the box"
    images = payload[OPENPI_KEY_IMAGES]
    assert set(images) == {"base_0_rgb", "cam_right_wrist"}  # bind 相机集过滤 + 改名
    for img in images.values():
        assert isinstance(img, np.ndarray)
        assert img.dtype == np.uint8
        assert img.shape == (16, 16, 3)  # uint8 HWC（非 jpeg bytes）


def test_openpi_camera_layout_comes_from_bind_adapter_not_config():
    """openpi **不读 edge.yml 的相机名**：仅 bind_adapter 决定要下发的相机（未绑定 = 透传全部）。"""
    from motrix_edge.policy.openpi.client import OpenPIClient
    from motrix_edge.policy.openpi.contract import OPENPI_KEY_IMAGES

    client = OpenPIClient({"action_horizon": 1, "image_size": 8})  # policy_config 不带任何相机列表
    transport = _FakeTransport(horizon=1, dim=1)
    client._transport = transport
    client.connect()
    obs = {
        "observations/qpos": np.zeros(1, dtype=np.float32),
        "observations/images/cam_head": np.zeros((4, 4, 3), dtype=np.uint8),
        "observations/images/cam_left_wrist": np.zeros((4, 4, 3), dtype=np.uint8),
    }
    client.infer_chunk(obs)  # 未绑定 → 透传观测内全部相机
    assert set(transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head", "cam_left_wrist"}

    client.bind_adapter(action_dim=7, camera_names=["cam_head"])  # adapter 只启用 cam_head（双相机→单相机）
    client.infer_chunk(obs)  # 上一块（horizon=1）已耗尽 → 再请求
    assert set(transport.last_payload[OPENPI_KEY_IMAGES]) == {"cam_head"}


def test_openpi_prompt_dynamic_per_request():
    """prompt 运行时动态可换（会话侧 set policy.prompt 后，下一请求携带新文本）。"""
    from motrix_edge.policy.openpi.client import OpenPIClient

    client = OpenPIClient({"action_horizon": 1, "image_size": 8})
    transport = _FakeTransport(horizon=1, dim=1)
    client._transport = transport
    client.connect()
    obs = {"observations/qpos": np.zeros(1, dtype=np.float32)}

    client.infer_chunk(obs)  # 块为空 → 请求
    assert "prompt" not in transport.last_payload  # 缺省 prompt=None → 不发（服务端 default_prompt 兜底）

    client.prompt = "switch tasks now"
    client.infer_chunk(obs)  # 块已耗尽（horizon=1）→ 再请求
    assert transport.last_payload["prompt"] == "switch tasks now"


# lerobot-act 策略已改为 lerobot gRPC 流式客户端（见 tests/test_act_grpc_client.py），
# 不再使用 ws / ActionChunkBroker / image_size；相关旧断言已移除。


def test_policy_registry_has_openpi_and_act():
    assert "openpi" in POLICY_REGISTRY
    assert "lerobot-act" in POLICY_REGISTRY


def test_validate_policy_type():
    from motrix_edge.policy import validate_policy_type

    assert validate_policy_type("lerobot-act") == "lerobot-act"
    with pytest.raises(ValueError, match="nonexistent"):
        validate_policy_type("nonexistent")


def test_get_policy_unknown_type():
    with pytest.raises(ValueError):
        get_policy({"policy": {"type": "nonexistent"}})


def test_get_policy_default_openpi():
    policy = get_policy({})
    assert policy.__class__.__name__ == "OpenPIClient"


def test_get_policy_act():
    policy = get_policy({"policy": {"type": "lerobot-act"}})
    assert policy.__class__.__name__ == "LerobotActClient"
    # lerobot-act 走 lerobot gRPC：不再有 ws 的 image_size / image_format
    assert not hasattr(policy, "image_size")


def test_get_policy_uses_only_shared_endpoint_config():
    policy = get_policy({"policy": {"host": "127.0.0.1", "port": 8765}}, policy_type="lerobot-act")
    assert policy.policy_config["host"] == "127.0.0.1"
    assert policy.policy_config["port"] == 8765
    assert policy._transport.endpoint == {"host": "127.0.0.1", "port": 8765}
