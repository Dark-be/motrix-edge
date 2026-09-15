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
    build_openpi_observation,
    extract_action_response,
    prepare_openpi_image,
)
from motrix_edge.rtc import ActionChunk
from motrix_edge.transport import WsTransport


class OpenPIClient(BasePolicyClient):
    """openpi 策略客户端：**官方 openpi WebSocket 契约** + msgpack-over-ws 传输。

    语言条件策略（``requires_prompt = True``）：推理前必须已 ``infer prompt <text>`` 预置
    非空文本指令，openpi 每次请求动态携带（服务端每帧重新 tokenize）。

    与官方 ``WebsocketPolicyServer``（openpi 仓 ``serve_policy.py``）互通，服务端零改动：
      connect(): websocket 连接，接收服务端 metadata（官方**不含** action_horizon，
                 action_horizon 以 policy_config 为准，缺省 50）
      infer_chunk(): 组装官方 flat 观测 → 请求一次 → 返回**原始动作块**（``ActionChunk``）
      prepare(): 预热（发一帧观测触发服务端模型加载，丢弃结果）
      disconnect(): 关闭连接

    **策略只负责取推理结果**：动作块缓存 / 三元切分 / 时序平滑 / 预取时机由
    ``motrix_edge.rtc``（RTCManager）统一负责（见 wiki/design/motrix_edge_rtc.md）。
    """

    requires_prompt = True  # 语言条件策略：推理前必须已 infer prompt 预置非空文本

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        # openpi 布局/契约配置（edge.yml policy 段；host/port 为共享端点键）
        image_size = self.policy_config.get("image_size", 224)
        if isinstance(image_size, (int, float)):
            image_size = (int(image_size), int(image_size))
        self.image_size = (int(image_size[0]), int(image_size[1]))  # (height, width)，默认 224×224
        # 下发相机集：由推理会话从 adapter 运行时配置（enabled_cameras）经 bind_adapter 绑定，
        # **不读 edge.yml 的相机名**；None = 透传观测内全部相机（adapter.observe 已只含启用相机）。
        self._image_cameras = None
        # edge 相机名 → 模型相机名重命名（默认同名；Piper 等模型相机名与 edge 一致可留空）
        self._rename_cameras = dict(self.policy_config.get("rename_cameras") or {})
        # 文本指令（prompt）：openpi 官方「运行时动态传入」，每次 infer 请求可换。
        # 配置值作缺省；推理会话可运行时更新（infer rollout prompt=...）。None = 不发
        # （服务端用 default_prompt 兜底）。
        self.prompt = self.policy_config.get("prompt")
        self._transport = WsTransport(
            host=self.policy_config.get("host", "0.0.0.0"),
            port=self.policy_config.get("port"),
            api_key=self.policy_config.get("api_key"),
        )
        self._action_horizon = None  # 动作块长（信息性：metadata 或 policy_config，缺省 50）

    def bind_adapter(self, action_dim=None, camera_names=None):
        """绑定机器人适配器运行时启用的布局（adapter config set 的 enabled_cameras）。

        相机名单一事实来源 = adapter 配置：推理会话（InferSession）进入时把 adapter 启用的
        相机名传入本方法，openpi 据此过滤要下发的相机（只下发列表内），**无需另读 edge.yml
        的相机名**。``camera_names=None``（未绑定）→ 透传观测内全部相机（adapter.observe
        本就只含启用相机，等效）。``action_dim``（启用臂 qpos 维数）仅信息性：state 维度由
        观测 qpos 实际长度决定，随 adapter 启用臂自动子集。
        """
        self._image_cameras = [str(c).strip() for c in camera_names] if camera_names else None

    @property
    def connected(self) -> bool:
        return self._transport.connected

    def connect(self):
        try:
            self._transport.close()  # 幂等：断开既有连接（refresh 语义，重连安全）
            self._transport.connect()
            self.server_metadata = dict(self._transport.server_metadata or {})
            # 动作块长：官方服务端 metadata **不提供** action_horizon（如仅 reset_pose），
            # 故以 policy_config 为准（缺省 50），metadata 有则优先。该值仅信息性——块缓存
            # 按服务端实际返回块长（[horizon, dim]）逐帧耗尽，不依赖本协商值。
            self._action_horizon = int(
                self.server_metadata.get("action_horizon") or self.policy_config.get("action_horizon", 50)
            )
        except Exception:
            self.server_metadata = {}
            self._action_horizon = None
            self._transport.close()
            raise

    def prepare(self, observation=None):
        """预热：openpi 官方首帧推理很慢（模型加载/编译），官方建议先发观测 warm up。

        用当前观测向服务端发一次推理并**丢弃结果**（服务端无动作缓存，丢弃安全），提前
        触发模型加载，避免首次 rollout 卡在推理。无观测 / 未连接 → no-op；失败不致命
        （会话捕获后首个 rollout 会自动重试）。
        """
        if observation is None or not self.connected:
            return
        self.infer_chunk(observation)  # 丢弃预热块

    def infer_chunk(self, observation, index: int | None = None) -> ActionChunk:
        """请求一次推理并返回**原始动作块**（``[H, dim]`` 或单步 ``[dim]``）。

        ``index``（绝对步号）对 openpi 无意义（服务端每次完整返回一块、由块长决定覆盖范围），
        仅用于回填 ``ActionChunk.start_index``（RTCManager 据此对齐块起始步号）。

        按 **openpi 官方 flat 契约** 组装观测：qpos → ``state``（原样上传，服务端归一化）；
        图像解码 → 缩放 ``image_size``（省带宽）→ **uint8 数组**（不再 jpeg），经
        ``image_cameras`` 过滤 + ``rename_cameras`` 改名到模型相机名；``prompt`` 每次请求
        动态携带（服务端每帧重新 tokenize，可换）。
        块缓存 / 切分 / 平滑由 RTCManager 负责，本方法不做任何缓存。
        """
        qpos = observation[KEY_OBS_QPOS]
        prepared = {}
        for key, value in observation.items():
            if not key.startswith(KEY_OBS_IMAGE_PREFIX):
                continue
            edge_name = key[len(KEY_OBS_IMAGE_PREFIX) :]
            if self._image_cameras is not None and edge_name not in self._image_cameras:
                continue  # 只下发 image_cameras 子集（双相机），其余过滤
            model_key = self._rename_cameras.get(edge_name, edge_name)
            prepared[model_key] = prepare_openpi_image(value, self.image_size)
        payload = build_openpi_observation(qpos, prepared, prompt=self.prompt)
        response = self._transport.request(payload)
        return ActionChunk(actions=extract_action_response(response), start_index=int(index or 0))

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}
