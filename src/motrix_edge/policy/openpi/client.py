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

from motrix_edge.policy.base import BasePolicyClient
from motrix_edge.policy.broker import ActionChunkBroker
from motrix_edge.policy.contract import (
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    build_observation,
    extract_action,
)
from motrix_edge.transport import WsTransport


class OpenPIClient(BasePolicyClient):
    """openpi 策略客户端：openpi 兼容格式契约 + 通用 msgpack 传输 + 动作块逐帧下发。

    流程与 openpi 服务端完全兼容（服务端无需改动）：
      connect(): websocket 连接，接收服务端 metadata（含 action_horizon）
      infer():   仅当动作块耗尽时 msgpack(观测) → 服务端取新动作块；否则直接消耗缓存块，
                 按 action_horizon 切片返回单步动作（一个动作块支撑 horizon 步，期间不请求）
      reset():   清空动作块缓存
    """

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self.image_size = tuple(self.policy_config.get("image_size", [224, 224]))
        self.image_format = self.policy_config.get("image_format", "jpeg")
        self._transport = WsTransport(
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
        """单步推理：**仅当动作块耗尽时才向推理端请求**，否则直接消耗缓存块（不请求）。

        每步由 ``infer rollout`` 驱动；一个动作块（[horizon, dim]）经 ``ActionChunkBroker``
        逐帧消耗 horizon 步，期间不再访问推理端，块耗尽后才请求下一块。
        """
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

    def drain(self, observation=None):
        """只消费当前缓存的 action chunk（不发新推理请求）；无缓存返回 None。"""
        if self._broker is None or self._broker.empty:
            return None
        return self._broker.step()

    def reset(self):
        if self._broker is not None:
            self._broker.reset()

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}
