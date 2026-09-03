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


class ACTClient(BasePolicyClient):
    """ACT 策略客户端：与 lerobot 官方 AsyncInference gRPC policy_server 互通（流式动作块）。

    与 openpi（WebSocket + MsgPack 一问一答）不同，act 走 **lerobot 原生流式**
    （edge = Robot 侧 gRPC 客户端）：
      connect()  建立 gRPC channel + ``Ready`` 握手（策略指令延后到首次 infer，
                 那时才知道 state 维度 / 相机）；
      infer(obs) 动作块耗尽时上传观测（``TimedObservation`` 分块）→ ``GetActions``
                 取回动作块按 timestep 消费单步；块未耗尽直接消费缓存（不访问推理端）；
      drain()    只消费缓存动作块；无缓存返回 None；
      reset()    清空缓存动作（timestep 单调递增，不回退，避免服务端按
                 “timestep 已预测” 过滤新观测）；
      disconnect() 关闭 channel。

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
        # 运行时状态
        self._policy_sent = False
        self._next_timestep = 0  # 已执行动作步数（下一观测的 timestep）
        self._actions: dict[int, np.ndarray] = {}  # timestep -> 动作

    # -- 连接 / 断开 -----------------------------------------------------------
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
        """单步推理：动作块耗尽时请求新块，否则消费缓存块（不访问推理端）。

        与 lerobot RobotClient 语义一致：obs.timestep = 已执行步数；服务端对一个观测
        预测动作块 [timestep, timestep+K)，edge 逐帧消费；块耗尽才上传新观测
        （``must_go=True`` 强制服务端推理）。
        """
        cur = self._next_timestep
        action = self._actions.pop(cur, None)
        if action is not None:
            self._next_timestep = cur + 1
            return action

        self._ensure_policy(observation)
        raw = self._build_raw_observation(observation)
        self._send_observation(raw, cur)
        self._wait_for_timestep(cur)
        action = self._actions.pop(cur)
        self._next_timestep = cur + 1
        return action

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

    def _wait_for_timestep(self, timestep: int) -> None:
        """GetActions 轮询直到取到含该 timestep 的动作块（服务端空闲返回 Empty → 稍候重试）。"""
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import bytes_to_python_object  # noqa: PLC0415

        deadline = time.monotonic() + self._get_actions_timeout
        while time.monotonic() < deadline:
            response = self._transport.stub.GetActions(services_pb2.Empty())
            if response.data:
                for timed in bytes_to_python_object(response.data):
                    self._actions.setdefault(timed.get_timestep(), self._to_numpy(timed.get_action()))
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
