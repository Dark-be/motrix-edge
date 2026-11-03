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

"""lerobot remote-inference 策略客户端（zenoh v1 协议：`lerobot-policy-server`）。

与旧的 ``lerobot-act``（gRPC ``async_inference``）**协议完全不同**：新版是 zenoh 上的
「discovery → admission → 观测/动作 一问一答」，服务端只负责推理，块缓存 / 三元切分 /
时序平滑 / 预取时机仍由 ``motrix_edge.rtc`` 统一负责（本客户端只实现 ``infer_chunk``）。

职责边界：
  - 连接 / 会话：``RemoteClient.connect``（发现唯一就绪实例）+ ``admit``（校验相机、state、
    action、语义与频率契约，占用独占会话）；
  - 取块：按服务端声明的特征名组装 ``ObservationSnapshot`` → ``ChunkRequest(mode=chunk)``
    → ``RemoteClient.infer`` → 原始 ``ActionChunk``（首步绝对步号 = 请求步号）。

与旧协议的关键差异（配置 / 语义层面）：
  - 没有 ``actions_per_chunk`` / ``image_size`` / ``pretrained_name_or_path``：模型与块长在
    **服务端**的 deployment YAML 里，端上只按 ``capabilities`` 对齐；图像尺寸取服务端声明的
    特征形状（端侧 letterbox 到该尺寸，与训练分辨率一致）；
  - 频率必须与服务端 ``action_interval`` 一致（端上直接采用服务端声明值完成 admission）；
  - 相机名必须**逐字**等于服务端特征名（如 ``observation.images.piper_0_cam_head``）：
    默认按相机短名自动匹配（``cam_head`` → ``..._cam_head``），可用 yaml 的
    ``camera_map``（端上相机名 → 服务端特征名）显式覆盖。
"""

from __future__ import annotations

import threading
import time
from uuid import uuid4

import numpy as np

from lerobot.inference import (
    ChunkRequest,
    ExecutionMode,
    ObservationSnapshot,
    RemoteInferenceConfig,
)
from lerobot.policies import QueueSnapshot
from lerobot.remote_inference import PROTOCOL_VERSION, RemoteClient
from motrix_edge.policy.base import BasePolicyClient
from motrix_edge.policy.contract import (
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    KEY_OBS_QPOS_ERR,
    resize_with_pad,
    to_rgb_uint8,
)
from motrix_edge.rtc import ActionChunk

OBSERVATION_LAYOUT_QPOS = "qpos"
OBSERVATION_LAYOUT_QPOS_ERR = "qpos+qpos_err"
OBSERVATION_LAYOUTS = {OBSERVATION_LAYOUT_QPOS, OBSERVATION_LAYOUT_QPOS_ERR}

DEFAULT_ENDPOINT = "tcp/127.0.0.1:7447"


def _normalize_observation_layout(value) -> str:
    """规范化 state 布局；缺省纯 qpos（与旧 checkpoint 输入一致）。"""
    layout = OBSERVATION_LAYOUT_QPOS if value in (None, "") else str(value).strip().lower()
    if layout not in OBSERVATION_LAYOUTS:
        raise ValueError(f"invalid observation_layout {value!r} (expect one of {sorted(OBSERVATION_LAYOUTS)})")
    return layout


def _short_name(key: str) -> str:
    """``observation.images.cam_head`` → ``cam_head``。"""
    return key.rsplit(".", 1)[-1]


class LerobotRemoteClient(BasePolicyClient):
    """zenoh 远程推理客户端（服务端 = ``lerobot-policy-server``）。

    **非语言条件策略**：``requires_prompt = False``（ACT 不接受文本条件）；``task`` 仍会随观测
    上报（空串或 yaml 的 policy.task），供支持文本的服务端使用。
    """

    requires_prompt = False

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self._config = self._build_config()
        self._client: RemoteClient | None = None
        self._admitted = False
        self._adapter_cameras: list[str] | None = None
        self._camera_map: dict[str, str] = {}
        self._lock = threading.Lock()
        self._task = str(self.policy_config.get("task") or self.policy_config.get("prompt") or "")
        # 显式相机映射（yaml 的 policy.camera_map：端上相机名 → 服务端特征名）
        raw_map = self.policy_config.get("camera_map") or {}
        if not isinstance(raw_map, dict):
            raise ValueError("policy.camera_map must be a mapping of edge camera name -> server feature name")
        self._explicit_camera_map = {str(key).strip(): str(value).strip() for key, value in raw_map.items()}
        self._observation_layout = _normalize_observation_layout(self.policy_config.get("observation_layout"))

    # -- 配置 ---------------------------------------------------------------
    def _build_config(self) -> RemoteInferenceConfig:
        """从 edge.yml 的 ``policy`` 段构造远程推理配置（会话级，构造即固化）。"""
        endpoint = self.policy_config.get("endpoint")
        if not endpoint:
            host = self.policy_config.get("host") or "127.0.0.1"
            port = self.policy_config.get("port") or 7447
            endpoint = f"tcp/{host}:{port}"
        return RemoteInferenceConfig(
            endpoint=str(endpoint),
            deployment=str(self.policy_config.get("deployment") or ""),
            semantics=str(self.policy_config.get("semantics") or ""),
            # edge 自己负责块缓存/切分/预取，服务端只返回整块 → append 语义最直接
            mode=str(self.policy_config.get("mode") or "chunk"),
            chunk_merge=str(self.policy_config.get("chunk_merge") or "append"),
            instance=self.policy_config.get("instance") or None,
            expected_artifact=self.policy_config.get("expected_artifact") or None,
            encoding=str(self.policy_config.get("encoding") or "raw"),
            jpeg_quality=int(self.policy_config.get("jpeg_quality") or 90),
            action_timeout_s=float(self.policy_config.get("action_timeout_s") or 5.0),
            handshake_timeout_s=float(self.policy_config.get("handshake_timeout_s") or 10.0),
            zenoh_mode=str(self.policy_config.get("zenoh_mode") or "peer"),
            zenoh_config_path=self.policy_config.get("zenoh_config_path") or None,
        )

    # -- 生命周期 -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self):
        """连接并发现唯一就绪的 deployment（``DESCRIBE`` 查询 = 新协议的握手）。"""
        self.disconnect()
        client = RemoteClient.connect(self._config)
        self._client = client
        self._admitted = False
        self.chunk_seen = False
        self.observed_chunk_len = None
        self.server_metadata = {
            "protocol": f"lerobot/remote-inference/v{PROTOCOL_VERSION}",
            "deployment": self._config.deployment,
            "instance": client.instance_id,
            "artifact_identity": client.artifact_identity,
        }

    def disconnect(self):
        client, self._client = self._client, None
        self._admitted = False
        self.server_metadata = {}
        if client is not None:
            client.close()

    def bind_adapter(self, action_dim=None, camera_names=None):
        """绑定 adapter 启用的相机名（相机名单一事实来源 = adapter 配置）。"""
        self._adapter_cameras = [str(c).strip() for c in camera_names] if camera_names else None

    def prepare(self, observation=None):
        """预热：提前完成 admission（占会话 + 校验契约），让首个 rollout 不再等握手。

        **未连接 / 无观测 → no-op**（与其它策略客户端同一契约）；admission 失败会**抛错**，
        因为它不是延迟收益而是契约不一致（相机 / state / 频率对不上），早失败早定位。
        """
        if observation is None or not self.connected:
            return
        self._ensure_admitted(observation)

    # -- 推理 ---------------------------------------------------------------
    def infer_chunk(self, observation, index: int | None = None) -> ActionChunk | None:
        """按绝对步号请求一块动作，返回**原始动作块**（首步 = 该步号）。"""
        if self._client is None:
            raise RuntimeError("lerobot-remote: not connected (connect() first)")
        self._ensure_admitted(observation)
        step = int(index) if index is not None else 0

        snapshot = ObservationSnapshot(
            features=self._observation_features(observation),
            capture_time=time.monotonic(),
            task=self._task,
            observation_id=uuid4().hex,
        )
        request = ChunkRequest(
            observation=snapshot,
            continuation=QueueSnapshot(
                generation=self._generation,
                cursor=step,
                index=step,
                model_actions=None,
                canonical_actions=None,
                provenance=(),
            ),
            request_id=uuid4().hex,
            generation=self._generation,
            mode=ExecutionMode(self._config.mode),
            delay=0,
            submitted_at=time.monotonic(),
        )
        result = self._client.infer(request)
        actions = result.canonical_actions.detach().cpu().numpy().astype(np.float32, copy=False)
        chunk = ActionChunk(actions=actions, start_index=step)
        self._note_chunk_len(chunk)
        return chunk

    # -- 内部 ---------------------------------------------------------------
    _generation = 0

    def _ensure_admitted(self, observation) -> None:
        """首次推理 / 布局变化时完成 admission（校验并占用独占会话）。"""
        with self._lock:
            if self._admitted:
                return
            caps = self._client.capabilities
            self._camera_map = self._resolve_cameras(caps, observation)
            self._client.admit(
                features=caps.features,
                action_feature=caps.action_feature,
                semantics=self._config.semantics,
                # 直接采用服务端声明的动作周期：端上频率必须与之一致，否则 admission 会被拒
                action_interval=caps.action_interval,
                mode=self._config.mode,
            )
            self._admitted = True
            self.observed_chunk_len = int(caps.execution_steps)

    def _resolve_cameras(self, caps, observation) -> dict[str, str]:
        """服务端图像特征名 → 端上相机名（显式 camera_map 优先，其次短名后缀匹配）。"""
        candidates = list(self._adapter_cameras or [])
        if not candidates:
            candidates = [
                key[len(KEY_OBS_IMAGE_PREFIX) :] for key in observation if key.startswith(KEY_OBS_IMAGE_PREFIX)
            ]
        mapping: dict[str, str] = {}
        for spec in caps.features:
            if spec.kind != "rgb":
                continue
            explicit = next(
                (edge for edge, feature_name in self._explicit_camera_map.items() if feature_name == spec.name),
                None,
            )
            if explicit is not None:
                mapping[spec.name] = explicit
                continue
            short = _short_name(spec.name)
            matches = [c for c in candidates if c == short or short.endswith(c) or c.endswith(short)]
            if len(matches) != 1:
                raise ValueError(
                    f"lerobot-remote: cannot map server camera {spec.name!r} to an edge camera "
                    f"({matches!r} out of {candidates!r}); set policy.camera_map "
                    "{edge_camera: server_feature_name}"
                )
            mapping[spec.name] = matches[0]
        missing = [edge for edge in mapping.values() if f"{KEY_OBS_IMAGE_PREFIX}{edge}" not in observation]
        if missing:
            raise ValueError(
                f"lerobot-remote: observation is missing camera(s) {missing!r}; "
                f"available: {[k for k in observation if k.startswith(KEY_OBS_IMAGE_PREFIX)]!r}"
            )
        return mapping

    def _state_values(self, observation) -> np.ndarray:
        """按 ``observation_layout`` 组装 state 向量。"""
        qpos = np.asarray(observation[KEY_OBS_QPOS], dtype=np.float32).reshape(-1)
        if self._observation_layout != OBSERVATION_LAYOUT_QPOS_ERR:
            return qpos
        if KEY_OBS_QPOS_ERR not in observation:
            raise KeyError(f"{KEY_OBS_QPOS_ERR!r} is required by observation_layout={self._observation_layout!r}")
        qpos_err = np.asarray(observation[KEY_OBS_QPOS_ERR], dtype=np.float32).reshape(-1)
        if qpos_err.shape != qpos.shape:
            raise ValueError(f"qpos_err shape {qpos_err.shape} != qpos shape {qpos.shape}")
        return np.concatenate((qpos, qpos_err))

    def _observation_features(self, observation) -> dict[str, np.ndarray]:
        """按服务端声明的特征名组装观测（state 向量 + 端侧 letterbox 到声明尺寸的 RGB）。"""
        caps = self._client.capabilities
        state = self._state_values(observation)
        features: dict[str, np.ndarray] = {}
        for spec in caps.features:
            if spec.kind == "rgb":
                height, width = int(spec.shape[0]), int(spec.shape[1])
                image = observation[f"{KEY_OBS_IMAGE_PREFIX}{self._camera_map[spec.name]}"]
                features[spec.name] = resize_with_pad(to_rgb_uint8(image), height, width)
            else:
                expected = int(np.prod(spec.shape)) if spec.shape else state.size
                if state.size != expected:
                    raise ValueError(
                        f"lerobot-remote: state size {state.size} does not match server feature "
                        f"{spec.name!r} shape {spec.shape} "
                        f"(observation_layout={self._observation_layout!r})"
                    )
                features[spec.name] = state.astype(np.float32, copy=False)
        return features


__all__ = ["LerobotRemoteClient", "OBSERVATION_LAYOUT_QPOS", "OBSERVATION_LAYOUT_QPOS_ERR"]
