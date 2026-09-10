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


import time

import numpy as np

from motrix_edge.policy.base import BasePolicyClient
from motrix_edge.policy.contract import (
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    resize_with_pad,
    to_rgb_uint8,
)
from motrix_edge.rtc import ActionChunk
from motrix_edge.transport.grpc import AsyncInferenceGrpcTransport


class ACTClient(BasePolicyClient):
    """ACT 策略客户端：与 lerobot 官方 AsyncInference gRPC policy_server 互通（流式动作块）。

    与 openpi（WebSocket + MsgPack 一问一答）不同，act 走 **lerobot 原生流式**
    （edge = Robot 侧 gRPC 客户端）：
      connect()  建立 gRPC channel + ``Ready`` 握手（策略指令延后到首次 infer，
                 那时才知道 state 维度 / 相机）；
      infer_chunk(obs, index)  按绝对步号上传观测（``must_go=True``）→ ``GetActions``
                 取回整块 → 返回**原始动作块**（``ActionChunk``，含首步绝对步号）；
      disconnect() 关闭 channel。

    **策略只负责取推理结果**：块缓存 / 三元切分 / 时序平滑（重叠聚合）/ 预取时机全部由
    ``motrix_edge.rtc``（RTCManager）负责（见 wiki/design/motrix_edge_rtc.md）——本客户端
    不再维护 timestep→动作 缓存、不做重叠加权。

    wire 层已从 policy 解耦：
      - 连接（channel/stub）: ``motrix_edge.transport.grpc.AsyncInferenceGrpcTransport``
      - proto / 分块 / pickle 工具 与观测/动作 pickle 数据类（RemotePolicyConfig /
        TimedObservation / TimedAction）: vendored ``lerobot``（``src/lerobot``）
    """

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self._transport = AsyncInferenceGrpcTransport(
            host=self.policy_config.get("host", "127.0.0.1"),
            port=self.policy_config.get("port"),
            connect_timeout=self.policy_config.get("connect_timeout", 5.0),
        )
        # lerobot act 策略参数（edge.yml policy 段）
        self._actions_per_chunk = int(self.policy_config.get("actions_per_chunk", 50))
        self._fps = int(self.policy_config.get("fps", 30))
        # 文本指令（prompt）：与 openpi 统一概念——``infer prompt <text>`` 会话内设置，
        # 推理前必须非空；作为策略指令下发（raw observation 的 ``task``）。
        # 配置键：``prompt`` 优先，旧 ``task`` 键向后兼容（缺省 None）。
        self.prompt = self.policy_config.get("prompt") or self.policy_config.get("task") or None
        self._rename_cameras = dict(self.policy_config.get("rename_cameras") or {})
        # 策略输入相机子集（edge 观测图像名；None = 全部）：只下发这些相机，其余过滤——
        # 避免把策略 image_features 里没有的相机（如 cam_left_wrist）发给服务端导致
        # KeyError；顺序保持观测键序（rename_cameras 再改名到策略训练名）。
        raw_cameras = self.policy_config.get("image_cameras")
        self._image_cameras = [str(c).strip() for c in raw_cameras] if raw_cameras else None
        # 图像在 edge 侧直接 letterbox 到 (height, width)（默认 224×224，横向图上下留黑边），
        # 服务端 ACT 按 image_features(224×224) 再处理时 resize 为 no-op、不变形。
        image_size = self.policy_config.get("image_size", 224)
        if isinstance(image_size, (int, float)):
            image_size = (int(image_size), int(image_size))
        self._image_size = (int(image_size[0]), int(image_size[1]))  # (height, width)
        self._get_actions_timeout = float(self.policy_config.get("get_actions_timeout", 10.0))
        # 运行时状态（绝对步号由 RTCManager 传入；本客户端不再自持 timestep 缓存）
        self._policy_sent = False

    # -- 连接 / 预热 / 断开 ---------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._transport.connected

    def prepare(self, observation=None):
        """预热：显式 connect 后提前下发策略指令（SendPolicyInstructions，服务端加载模型）。

        需要观测确定 state 维度 / 相机；已下发过则 no-op。openpi 无此阶段（connect 即
        就绪）——act 的「真正就绪 = 服务端加载模型」，可提前触发避免首次 infer 阻塞。
        """
        if not self._policy_sent and observation is not None:
            self._ensure_policy(observation)

    def connect(self):
        """建立 gRPC channel 并 ``Ready`` 握手；读取 protocol 概要为 server_metadata。"""
        try:
            self._transport.connect()
            from lerobot.transport import services_pb2  # noqa: PLC0415 延迟导入

            self._transport.stub.Ready(services_pb2.Empty())
            self.server_metadata = {"protocol": "lerobot/async-inference", "policy_type": "act"}
        except Exception:
            self._transport.close()
            self.server_metadata = {}
            raise

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}

    def infer_chunk(self, observation, index: int | None = None) -> ActionChunk | None:
        """按绝对步号请求一次推理，返回**原始动作块**（``ActionChunk``，含首步绝对步号）。

        ``index`` = RTCManager 的当前绝对步号，作为 ``TimedObservation.timestep`` 上报
        （服务端据此对该时刻起预测一块）；缺省 0。块缓存 / 三元切分 / 时序平滑 / 预取时机
        由 RTCManager 负责，本方法每次调用都真实请求一次推理、不做任何缓存。

        返回 ``None`` 表示服务端未返回动作（空块）。
        """
        timestep = int(index) if index is not None else 0
        self._ensure_policy(observation)
        raw = self._build_raw_observation(observation)
        self._send_observation(raw, timestep)
        timed_actions = self._receive_chunk(timestep)
        if not timed_actions:
            return None
        actions = np.stack([self._to_numpy(timed.get_action()) for timed in timed_actions])
        start = min(int(timed.get_timestep()) for timed in timed_actions)
        return ActionChunk(actions=actions, start_index=start)

    # -- 内部 ----------------------------------------------------------------
    def _lerobot_features(self, observation) -> dict:
        """按观测生成 dataset-format 特征（state 分量名 + 相机名），供 SendPolicyInstructions。

        与服务端 ``build_dataset_frame(features, raw_obs, "observation")`` 对齐：
          - state:  {"dtype":"float32","shape":(N,),"names":[qpos_i]}
          - images: {"dtype":"video"}（实际取值走 raw_obs[相机名]）
        """
        dim = int(np.asarray(observation[KEY_OBS_QPOS]).size)
        state_names = [f"qpos_{i}" for i in range(dim)]
        features = {"observation.state": {"dtype": "float32", "shape": (dim,), "names": state_names}}
        for _, dataset_cam in self._policy_cameras(observation):
            features[f"observation.images.{dataset_cam}"] = {"dtype": "video"}
        return features

    def _policy_cameras(self, observation) -> list[tuple[str, str]]:
        """策略输入相机（edge 名, 下发名）列表，保持观测键序。

        经 ``image_cameras`` 过滤（未列出的相机不下发，避免服务端按策略 image_features
        查键 KeyError）与 ``rename_cameras`` 重命名（edge 名 → 策略训练相机名）。
        """
        cams: list[tuple[str, str]] = []
        for key in observation:
            if key.startswith(KEY_OBS_IMAGE_PREFIX):
                edge = key[len(KEY_OBS_IMAGE_PREFIX) :]
                if self._image_cameras is not None and edge not in self._image_cameras:
                    continue
                cams.append((edge, self._rename_cameras.get(edge, edge)))
        return cams

    def _ensure_policy(self, observation) -> None:
        """首次 infer 时下发策略指令（SendPolicyInstructions）：pickle RemotePolicyConfig。

        需要 ``pretrained_name_or_path``（服务端据此加载 ACT checkpoint）。
        """
        if self._policy_sent:
            return
        pretrained = self.policy_config.get("pretrained_name_or_path")
        if not pretrained:
            raise ValueError(
                "act/lerobot policy requires 'pretrained_name_or_path' in policy config "
                "(server loads the ACT checkpoint from it)"
            )
        from lerobot.async_inference.helpers import RemotePolicyConfig  # noqa: PLC0415
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import python_object_to_bytes  # noqa: PLC0415

        config = RemotePolicyConfig(
            policy_type="act",
            pretrained_name_or_path=pretrained,
            lerobot_features=self._lerobot_features(observation),
            actions_per_chunk=self._actions_per_chunk,
            device=self.policy_config.get("device", "cpu"),
            rename_map=dict(self.policy_config.get("rename_map") or {}),
        )
        self._transport.stub.SendPolicyInstructions(services_pb2.PolicySetup(data=python_object_to_bytes(config)))
        self._policy_sent = True

    def _build_raw_observation(self, observation) -> dict:
        """edge 观测（qpos + jpeg/ndarray 图像）→ lerobot raw obs（state 分量标量 + uint8 图像）。

        图像解码为 uint8 RGB 后由 edge 直接 ``resize_with_pad`` 等比缩放到
        ``image_size``（默认 224×224，横向图上下留黑边），不依赖服务端缩放。
        """
        raw: dict = {}
        qpos = np.asarray(observation[KEY_OBS_QPOS])
        for i, value in enumerate(qpos):
            raw[f"qpos_{i}"] = float(value)
        for edge, dataset_cam in self._policy_cameras(observation):
            value = observation[f"{KEY_OBS_IMAGE_PREFIX}{edge}"]
            raw[dataset_cam] = resize_with_pad(to_rgb_uint8(value), *self._image_size)
        if self.prompt:
            raw["task"] = self.prompt
        return raw

    def _send_observation(self, raw: dict, timestep: int) -> None:
        """pickle(TimedObservation) 分块流式 SendObservations（must_go=True 强制推理）。"""
        from lerobot.async_inference.helpers import TimedObservation  # noqa: PLC0415
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import python_object_to_bytes, send_bytes_in_chunks  # noqa: PLC0415

        timed = TimedObservation(timestamp=time.time(), timestep=timestep, observation=raw, must_go=True)
        data = python_object_to_bytes(timed)
        iterator = send_bytes_in_chunks(data, services_pb2.Observation, silent=True)
        self._transport.stub.SendObservations(iterator)

    def _receive_chunk(self, timestep: int):
        """GetActions 轮询直到取到**含该 timestep** 的动作块（服务端空闲返回 Empty → 稍候重试）。

        取回整块（``list[TimedAction]``，含各自 timestep）后原样返回，由 ``infer_chunk``
        转成 ``ActionChunk``；不再落本地缓存（缓存 / 平滑由 RTCManager 负责）。
        """
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import bytes_to_python_object  # noqa: PLC0415

        deadline = time.monotonic() + self._get_actions_timeout
        while time.monotonic() < deadline:
            response = self._transport.stub.GetActions(services_pb2.Empty())
            if response.data:
                timed_actions = bytes_to_python_object(response.data)
                if any(int(timed.get_timestep()) >= timestep for timed in timed_actions):
                    return timed_actions
            else:
                time.sleep(0.02)
        raise TimeoutError(f"act/lerobot: no action for timestep {timestep} within {self._get_actions_timeout}s")

    @staticmethod
    def _to_numpy(action) -> np.ndarray:
        """torch.Tensor / array → float32 一维动作数组（edge adapter 消费）。"""
        if hasattr(action, "detach"):
            action = action.detach().cpu()
        arr = np.asarray(action, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[0] == 1:
            arr = arr[0]
        return arr
