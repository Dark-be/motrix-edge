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
from motrix_edge.transport.grpc import AsyncInferenceGrpcTransport

# 重叠聚合函数（对齐 lerobot 官方 robot_client 的 AGGREGATE_FUNCTIONS）。
# 默认 weighted_average：重叠步动作 = 0.3*旧块 + 0.7*新块（新决策更占主导）。
_AGGREGATE_FUNCTIONS = {
    "weighted_average": lambda old, new: 0.3 * old + 0.7 * new,
    "latest_only": lambda old, new: new,
    "average": lambda old, new: 0.5 * old + 0.5 * new,
    "conservative": lambda old, new: 0.7 * old + 0.3 * new,
}


class ACTClient(BasePolicyClient):
    """ACT 策略客户端：与 lerobot 官方 AsyncInference gRPC policy_server 互通（流式动作块）。

    与 openpi（WebSocket + MsgPack 一问一答）不同，act 走 **lerobot 原生流式**
    （edge = Robot 侧 gRPC 客户端）：
      connect()  建立 gRPC channel + ``Ready`` 握手（策略指令延后到首次 infer，
                 那时才知道 state 维度 / 相机）；
      infer(obs) 消费缓存单步；缓存耗尽，或缓存剩余 ≤ ``smooth_overlap``（时序平滑
                 窗口）时**同步预取**下一块（上传观测 → ``GetActions`` 取块），与旧块
                 重叠的 timestep 做加权聚合后继续消费；
      drain()    只消费缓存动作块（含已平滑区）；无缓存返回 None；不发新推理请求；
      reset()    清空缓存动作（timestep 单调递增，不回退，避免服务端按
                 “timestep 已预测” 过滤新观测）；
      disconnect() 关闭 channel。

    动作块缓存为 act 自有（``_actions``: timestep → 动作，无通用 broker）。**时序平滑**
    = edge 同步重叠预取 + 加权聚合：在块耗尽前提前请求下一块，使相邻块在 timestep 上
    重叠 ``smooth_overlap`` 步，重叠步在 ``_store_action_chunk`` 中按 ``aggregate_fn``
    融合，块边界平滑衔接（服务端每次 ``GetActions`` 都推理、无动作缓存，语义对照
    lerobot 官方 ``robot_client``）。

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
        self._task = self.policy_config.get("task", "")
        self._rename_cameras = dict(self.policy_config.get("rename_cameras") or {})
        # 图像在 edge 侧直接 letterbox 到 (height, width)（默认 224×224，横向图上下留黑边），
        # 服务端 ACT 按 image_features(224×224) 再处理时 resize 为 no-op、不变形。
        image_size = self.policy_config.get("image_size", 224)
        if isinstance(image_size, (int, float)):
            image_size = (int(image_size), int(image_size))
        self._image_size = (int(image_size[0]), int(image_size[1]))  # (height, width)
        self._get_actions_timeout = float(self.policy_config.get("get_actions_timeout", 10.0))
        # act 时序平滑：缓存剩余 ≤ smooth_overlap 步时提前同步预取下一块并对重叠步加权
        # 聚合。smooth_overlap=0 关闭（退化为「块耗尽才推理」）；应 < actions_per_chunk。
        self._smooth_overlap = int(self.policy_config.get("smooth_overlap", 10))
        aggregate_name = self.policy_config.get("aggregate_fn", "weighted_average")
        if aggregate_name not in _AGGREGATE_FUNCTIONS:
            available = list(_AGGREGATE_FUNCTIONS)
            raise ValueError(f"Unknown aggregate_fn '{aggregate_name}'. Available: {available}")
        self._aggregate = _AGGREGATE_FUNCTIONS[aggregate_name]
        # 运行时状态
        self._policy_sent = False
        self._next_timestep = 0  # 已执行动作步数（下一观测的 timestep）
        self._actions: dict[int, np.ndarray] = {}  # timestep -> 动作

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

    def reset(self):
        """清空缓存动作块（timestep 单调递增、不回退）。"""
        self._actions = {}

    def drain(self, observation=None):
        """只消费当前缓存动作块（不发新推理请求）；无缓存返回 None。"""
        action = self._actions.pop(self._next_timestep, None)
        if action is None:
            return None
        self._next_timestep += 1
        return action

    def infer(self, observation):
        """单步推理：优先消费缓存单步；缓存耗尽或进入平滑重叠窗口时同步预取新块。

        obs.timestep = 已执行步数；服务端对一个观测预测动作块 [timestep, timestep+K)。
        - 缓存耗尽（remaining=0）：请求新块（无重叠）。
        - 缓存剩余 ≤ ``smooth_overlap``：**提前**请求下一块，与旧块重叠步加权聚合
          （时序平滑），随后继续消费。
        """
        cur = self._next_timestep
        remaining = self._cached_remaining()
        if remaining == 0 or (self._smooth_overlap > 0 and remaining <= self._smooth_overlap):
            self._request_chunk(observation, cur)
        action = self._actions.pop(cur, None)
        if action is not None:
            self._next_timestep = cur + 1
        return action

    def _cached_remaining(self) -> int:
        """缓存中从 ``_next_timestep`` 起连续未消费的步数（空为 0）。"""
        if not self._actions:
            return 0
        highest = max(self._actions)
        if highest < self._next_timestep:
            return 0
        return highest - self._next_timestep + 1

    def _request_chunk(self, observation, timestep: int) -> None:
        """同步请求一块以 ``timestep`` 起的动作块并落缓存（块耗尽 / 平滑预取共用）。

        上传当前观测（``must_go=True``）→ ``GetActions`` 等含 ``timestep`` 的块；
        新块与已缓存重叠的步在 ``_store_action_chunk`` 内加权聚合。
        """
        self._ensure_policy(observation)
        raw = self._build_raw_observation(observation)
        self._send_observation(raw, timestep)
        self._wait_for_timestep(timestep)

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
        for dataset_cam in self._camera_dataset_names(observation):
            features[f"observation.images.{dataset_cam}"] = {"dtype": "video"}
        return features

    def _camera_dataset_names(self, observation) -> list[str]:
        """观测图像键（edge 相机名）→ 策略图像特征名（重命名映射后；默认同名）。"""
        names = []
        for key in observation:
            if key.startswith(KEY_OBS_IMAGE_PREFIX):
                edge = key[len(KEY_OBS_IMAGE_PREFIX) :]
                names.append(self._rename_cameras.get(edge, edge))
        return names

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
        for key, value in observation.items():
            if key.startswith(KEY_OBS_IMAGE_PREFIX):
                edge = key[len(KEY_OBS_IMAGE_PREFIX) :]
                dataset_cam = self._rename_cameras.get(edge, edge)
                raw[dataset_cam] = resize_with_pad(to_rgb_uint8(value), *self._image_size)
        if self._task:
            raw["task"] = self._task
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

    def _store_action_chunk(self, timed_actions) -> None:
        """把动作块落入本地缓存（timestep → 动作），与已缓存重叠的步做加权聚合。

        act 动作缓存为**策略自有**（无通用 broker）。时序平滑依赖此处聚合：当平滑预取
        返回的新块与旧缓存重叠时，重叠步 = ``aggregate(old, new)``（默认
        weighted_average：0.3*旧 + 0.7*新），块边界由此平滑衔接（参照 lerobot 官方
        robot_client ``_aggregate_action_queues``）。非重叠步直接落入缓存。
        """
        for timed in timed_actions:
            timestep = timed.get_timestep()
            new = self._to_numpy(timed.get_action())
            old = self._actions.get(timestep)
            if old is None:
                self._actions[timestep] = new
            else:
                self._actions[timestep] = self._aggregate(old, new)

    def _wait_for_timestep(self, timestep: int) -> None:
        """GetActions 轮询直到取到含该 timestep 的动作块（服务端空闲返回 Empty → 稍候重试）。"""
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import bytes_to_python_object  # noqa: PLC0415

        deadline = time.monotonic() + self._get_actions_timeout
        while time.monotonic() < deadline:
            response = self._transport.stub.GetActions(services_pb2.Empty())
            if response.data:
                self._store_action_chunk(bytes_to_python_object(response.data))
                if timestep in self._actions:
                    return
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
