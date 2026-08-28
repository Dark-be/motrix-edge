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

import numpy as np

from motrix_edge.policy.base import BasePolicyClient
from motrix_edge.policy.broker import ActionChunkBroker
from motrix_edge.policy.contract import (
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    build_observation,
    extract_action,
)
from motrix_edge.policy.transport import MsgpackTransport


class ACTClient(BasePolicyClient):
    """ACT 策略客户端。

    ACT 服务端使用与当前推理传输层相同的 WebSocket + MsgPack 通道，但图像输入
    默认按 640x480 编码。除图像尺寸外，观测和动作消息沿用统一契约：连接后接收
    metadata，推理请求发送 ``observations/*``，响应返回 ``{"action": ...}``。
    """

    DEFAULT_IMAGE_SIZE = (480, 640)

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self.image_size = tuple(self.policy_config.get("image_size", self.DEFAULT_IMAGE_SIZE))
        self.image_format = self.policy_config.get("image_format", "jpeg")
        self._transport = MsgpackTransport(
            host=self.policy_config.get("host", "0.0.0.0"),
            port=self.policy_config.get("port"),
            api_key=self.policy_config.get("api_key"),
        )
        self._action_horizon = None
        self._broker = None

    def connect(self):
        try:
            self._transport.connect()
            self.server_metadata = dict(self._transport.server_metadata or {})
            self._action_horizon = self.server_metadata.get("action_horizon") or self.policy_config.get(
                "action_horizon"
            )
            if not self._action_horizon:
                raise ValueError("action_horizon not provided by server metadata or policy config")
            self._broker = ActionChunkBroker(self._action_horizon)
        except Exception:
            self.server_metadata = {}
            self._action_horizon = None
            self._broker = None
            self._transport.close()
            raise

    def infer(self, observation):
        """单步推理：动作块耗尽时请求新块，否则从缓存中逐帧消费。"""
        if self._broker.empty:
            qpos = observation[KEY_OBS_QPOS]
            images = {
                key[len(KEY_OBS_IMAGE_PREFIX) :]: value
                for key, value in observation.items()
                if key.startswith(KEY_OBS_IMAGE_PREFIX)
            }
            payload = build_observation(qpos, images, self.image_size, self.image_format)
            response = self._transport.request(payload)
            self._broker.feed(extract_action(response))
        return self._broker.step()

    def reset(self):
        if self._broker is not None:
            self._broker.reset()

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}


class ACT7DofClient(ACTClient):
    """ACT 策略客户端（固定 qpos 维度 + 从已有观测键挑选）。

    在通用 ``ACTClient`` 基础上增加五项**可配置约束**：
      - ``qpos_dim``（默认 7）：推理前校验发送 qpos 的维度，不匹配抛错；
      - ``qpos_indices``（默认空）：从观测 ``observations/qpos`` 中按索引挑选 / 重排
        维度（如从 14 维双臂观测取单臂 ``[0..6]``）；空则用全量；
      - ``cameras``（默认空）：从 adapter 已有相机观测键中挑选要发送的相机名列表，
        空则发送全部 ``observations/images/*``（回退通用行为）；
      - ``action_indices``（默认空）：模型输出 action 映射回机器人**完整动作空间**的
        下标；空则透传模型输出。
      - ``action_fill``（默认空）：未覆盖维度（如另一臂）的填充值 —— **home 位姿**，
        标量广播或按未覆盖下标顺序的 list；缺省 0，**不跟随当前 qpos**。
    图像尺寸 / 格式、``action_horizon`` 约定与父类一致。
    """

    DEFAULT_QPOS_DIM = 7

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self.qpos_dim = self.policy_config.get("qpos_dim", self.DEFAULT_QPOS_DIM)
        self.qpos_indices = tuple(self.policy_config.get("qpos_indices") or ())
        self.cameras = tuple(self.policy_config.get("cameras") or ())
        self.action_indices = tuple(self.policy_config.get("action_indices") or ())
        self.action_fill = self.policy_config.get("action_fill")

    def _select_qpos(self, qpos) -> np.ndarray:
        """按 ``qpos_indices`` 从观测 qpos 中挑选 / 重排维度；未配置则用全量。"""
        qpos = np.asarray(qpos)
        if self.qpos_indices:
            return qpos[list(self.qpos_indices)]
        return qpos

    def _expand_action(self, action, observation) -> np.ndarray:
        """把模型输出 action 映射回机器人完整动作空间。

        ``action_indices`` 为空则透传模型输出；否则校验模型输出维度与 ``action_indices``
        一致，并填充到完整动作数组的对应下标。未覆盖维度（如另一臂）用 ``action_fill``
        配置的 home 位姿填充（标量广播或按未覆盖下标顺序的 list；缺省 0），不跟随当前
        qpos。
        """
        if not self.action_indices:
            return action
        model_action = np.asarray(action, dtype=np.float64)
        if model_action.shape[0] != len(self.action_indices):
            raise ValueError(f"expected model action dim {len(self.action_indices)}, got {model_action.shape[0]}")
        full_dim = np.asarray(observation[KEY_OBS_QPOS]).shape[0]
        full = np.zeros(full_dim, dtype=np.float64)
        if self.action_fill is not None:
            fill = np.asarray(self.action_fill, dtype=np.float64)
            uncovered = [i for i in range(full_dim) if i not in self.action_indices]
            if fill.ndim == 0:
                full[uncovered] = fill
            else:
                if fill.shape[0] != len(uncovered):
                    raise ValueError(f"expected action_fill dim {len(uncovered)}, got {fill.shape[0]}")
                full[uncovered] = fill
        full[list(self.action_indices)] = model_action
        return full

    def infer(self, observation):
        """单步推理：块耗尽时组装观测取新块；qpos / 相机 / action 由配置映射。"""
        if self._broker.empty:
            qpos = self._select_qpos(observation[KEY_OBS_QPOS])
            if qpos.shape[0] != self.qpos_dim:
                raise ValueError(f"expected qpos dim {self.qpos_dim}, got {qpos.shape[0]}")
            if self.cameras:
                images = {name: observation[f"{KEY_OBS_IMAGE_PREFIX}{name}"] for name in self.cameras}
            else:
                images = {
                    key[len(KEY_OBS_IMAGE_PREFIX) :]: value
                    for key, value in observation.items()
                    if key.startswith(KEY_OBS_IMAGE_PREFIX)
                }
            payload = build_observation(qpos, images, self.image_size, self.image_format)
            response = self._transport.request(payload)
            self._broker.feed(extract_action(response))
        return self._expand_action(self._broker.step(), observation)
