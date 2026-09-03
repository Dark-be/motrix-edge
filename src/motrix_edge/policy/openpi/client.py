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
from motrix_edge.policy.contract import (
    KEY_OBS_IMAGE_PREFIX,
    KEY_OBS_QPOS,
    build_observation,
    extract_action,
)
from motrix_edge.transport import WsTransport


class OpenPIClient(BasePolicyClient):
    """openpi 策略客户端：openpi 兼容格式契约 + msgpack-over-ws 传输 + 动作块逐帧消费。

    动作缓存为 **openpi 自有**（服务端按 [horizon, dim] 返回动作块，edge 本地逐帧
    消费，无通用动作缓存器——各策略自行管理其块语义）。流程与 openpi 服务端完全
    兼容（服务端无需改动）：
      connect(): websocket 连接，接收服务端 metadata（含 action_horizon）
      infer():   仅当缓存块耗尽时 msgpack(观测) → 服务端取新动作块；否则直接消耗缓存块，
                 返回单步动作（一个动作块支撑 horizon 步，期间不请求）
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
        self._chunk = None  # 缓存动作块（[horizon, dim] 或单步 [dim]）
        self._cursor = 0  # 当前块消费游标

    @property
    def connected(self) -> bool:
        return self._transport.connected

    def connect(self):
        try:
            self._transport.close()  # 幂等：断开既有连接（refresh 语义，重连安全）
            self._transport.connect()
            self.server_metadata = dict(self._transport.server_metadata or {})
            self._action_horizon = self.server_metadata.get("action_horizon") or self.policy_config.get(
                "action_horizon"
            )
            if not self._action_horizon:
                raise ValueError("action_horizon not provided by server metadata or policy config")
            self._chunk = None
            self._cursor = 0
        except Exception:
            self.server_metadata = {}
            self._action_horizon = None
            self._chunk = None
            self._cursor = 0
            self._transport.close()
            raise

    # -- 动作块缓存（openpi 自有：块逐帧切片，短块/长块按实际长度耗尽） ---------------
    @property
    def _chunk_empty(self) -> bool:
        return self._chunk is None

    def _consume_cached(self):
        """消费缓存块的当前步动作；耗尽后清空缓存。单步动作（[dim]）透传不切片。"""
        if self._chunk is None:
            return None
        if self._chunk.ndim == 1:
            action = self._chunk
            self._chunk = None
            return action
        action = self._chunk[self._cursor]
        self._cursor += 1
        if self._cursor >= self._chunk.shape[0]:
            self._chunk = None
            self._cursor = 0
        return action

    def _request_chunk(self, observation) -> None:
        """块耗尽：向推理端请求新动作块并落入缓存（不按协商值截断/越界）。"""
        qpos = observation[KEY_OBS_QPOS]
        images = {
            key[len(KEY_OBS_IMAGE_PREFIX) :]: value
            for key, value in observation.items()
            if key.startswith(KEY_OBS_IMAGE_PREFIX)
        }
        payload = build_observation(qpos, images, self.image_size, self.image_format)
        response = self._transport.request(payload)
        self._chunk = extract_action(response)
        self._cursor = 0

    def infer(self, observation):
        """单步推理：**仅当缓存块耗尽时才向推理端请求**，否则直接消耗缓存块（不请求）。

        每步由 ``infer rollout`` 驱动；一个动作块（[horizon, dim]）经本地缓存逐帧
        消费 horizon 步，期间不再访问推理端，块耗尽后才请求下一块。
        """
        if self._chunk_empty:
            self._request_chunk(observation)
        return self._consume_cached()

    def drain(self, observation=None):
        """只消费当前缓存的 action chunk（不发新推理请求）；无缓存返回 None。"""
        return self._consume_cached()

    def reset(self):
        """清空动作块缓存（推理端连接保持不变）。"""
        self._chunk = None
        self._cursor = 0

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}
