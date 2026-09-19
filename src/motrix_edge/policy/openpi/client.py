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
from motrix_edge.policy.contract import KEY_OBS_IMAGE_PREFIX, KEY_OBS_QPOS
from motrix_edge.policy.openpi.contract import (
    build_openpi_observation,
    extract_action_response,
    prepare_openpi_image,
)
from motrix_edge.rtc import ActionChunk
from motrix_edge.transport import WsTransport
from motrix_edge.utils.data_handler import debug_print


class OpenPIClient(BasePolicyClient):
    """openpi 策略客户端：**官方 openpi WebSocket 契约** + msgpack-over-ws 传输。

    语言条件策略（``requires_prompt = True``）：推理前必须已 ``infer prompt <text>`` 预置
    非空文本指令，openpi 每次请求动态携带（服务端每帧重新 tokenize）。

    与官方 ``WebsocketPolicyServer``（openpi 仓 ``serve_policy.py``）互通，服务端零改动：
      connect(): websocket 连接，读取服务端首条 metadata（相机清单以它为准；块长不依赖它）
      infer_chunk(): 组装官方 flat 观测 → 请求一次 → 返回**原始动作块**（``ActionChunk``）
      prepare(): 预热（发一帧观测触发服务端模型加载，丢弃结果）
      disconnect(): 关闭连接

    **策略只负责取推理结果**：动作块缓存 / 三元切分 / 时序平滑 / 预取时机由
    ``motrix_edge.rtc``（RTCManager）统一负责（见 wiki/design/motrix_edge_rtc.md）。
    """

    requires_prompt = True  # 语言条件策略：推理前必须已 infer prompt 预置非空文本

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        # 下发相机集（过滤）：服务端 metadata 声明了相机清单就以它为准，否则用 adapter 启用集
        # （见 ``_image_cameras``）；两者都无 → 透传观测内全部相机。
        self._adapter_cameras: list[str] | None = None  # adapter 实际启用的相机（bind_adapter）
        self._server_cameras: list[str] | None = None  # 服务端 metadata 声明的相机清单
        # edge 相机名 → 下发相机名重命名（默认同名）：本仓对接的 openpi 分支里，服务端
        # ``PiperInputs`` 收的是**源**相机名（cam_head / cam_left_wrist / cam_right_wrist，
        # 再由它映射到模型键 base_left_0_rgb 等），与 edge 侧名一致 → 一般留空。
        self._rename_cameras = dict(self.policy_config.get("rename_cameras") or {})
        # 文本指令（prompt）：openpi 官方「运行时动态传入」，每次 infer 请求可换。
        # 配置值作缺省；推理会话可运行时更新（infer prompt <text> / POST /v1/infers/prompt）。
        # None = 不发（服务端用 default_prompt 兜底）。
        self.prompt = self.policy_config.get("prompt")
        self._transport = WsTransport(
            host=self.policy_config.get("host", "0.0.0.0"),
            port=self.policy_config.get("port"),
            api_key=self.policy_config.get("api_key"),
            connect_timeout=float(self.policy_config.get("connect_timeout", 5.0)),
            # 单次推理请求限时：服务端不响应时抛异常断开，避免无限阻塞（RTC 预取线程持槽，
            # 挂死会连带拖垮后续 infer）。
            request_timeout=float(self.policy_config.get("request_timeout", 60.0)),
        )

    def bind_adapter(self, action_dim=None, camera_names=None):
        """绑定机器人适配器运行时启用的布局（adapter config set 的 enabled_cameras）。

        相机名单一事实来源 = adapter 配置：推理会话（InferSession）进入时把 adapter 启用的
        相机名传入本方法，openpi 据此过滤要下发的相机（只下发列表内），**无需另读 edge.yml
        的相机名**。``camera_names=None``（未绑定）→ 透传观测内全部相机（adapter.observe
        本就只含启用相机，等效）。``action_dim``（启用臂 qpos 维数）仅信息性：state 维度由
        观测 qpos 实际长度决定，随 adapter 启用臂自动子集。

        服务端 metadata **声明了**相机清单时以服务端为准（见 ``_image_cameras``）：声明的是
        服务端要的相机，比 adapter 启用集更准（多送的相机白白占带宽）。
        """
        self._adapter_cameras = [str(c).strip() for c in camera_names] if camera_names else None
        self._warn_missing_server_cameras()

    @property
    def image_size(self) -> tuple[int, int]:
        """下发图像尺寸 ``(height, width)``（默认 224×224）。

        **每次请求现读** ``policy_config``：openpi 无握手状态、每次请求独立变换，故会话内
        ``infer config set image_size`` 立即生效（改尺寸只影响带宽 / 端侧开销；服务端自己的
        输入变换会再缩到它的尺寸）。
        """
        size = self.policy_config.get("image_size", 224)
        if isinstance(size, (int, float)):
            return (int(size), int(size))
        return (int(size[0]), int(size[1]))

    @property
    def _image_cameras(self) -> list[str] | None:
        """要下发的相机名单：服务端声明 > adapter 启用集 > None（透传观测内全部）。

        用属性而不在 bind_adapter / connect 里写字段：两件事（会话绑定 adapter、连接拿 metadata）
        的先后顺序不属于契约，取谁都必须得同一个结果。
        """
        return list(self._server_cameras) if self._server_cameras else self._adapter_cameras

    def _warn_missing_server_cameras(self) -> None:
        """告警「服务端要的相机 adapter 没启用」：不告警的话，服务端只会报缺键 / 一直拿不到动作。"""
        if not self._server_cameras or self._adapter_cameras is None:
            return
        missing = [c for c in self._server_cameras if c not in self._adapter_cameras]
        if missing:
            debug_print(
                "policy",
                f"server declares camera(s) {missing} that the adapter does not provide; inference will fail",
                "WARNING",
            )

    @property
    def connected(self) -> bool:
        return self._transport.connected

    def connect(self):
        """连接并读取服务端首条 metadata（官方 websocket 契约：连接后先发一帧 metadata）。

        metadata 的丰富度因服务端而异：本仓对接的 openpi piper 分支会额外声明 ``action_horizon``
        / ``cameras`` / ``action_dim`` 等，原生 openpi 只有 ``reset_pose`` 之类。只有 ``cameras``
        被采纳（服务端按这些名字取图，多送无用、少送报错）；``action_horizon`` 不参与块长决策
        ——块长以实测为准（``observed_chunk_len`` → RTC ``calibrate``）。
        """
        try:
            self._transport.close()  # 幂等：断开既有连接（refresh 语义，重连安全）
            self._transport.connect()
            self.server_metadata = dict(self._transport.server_metadata or {})
            self._apply_server_contract()
        except Exception:
            self.server_metadata = {}
            self._transport.close()
            raise

    def _apply_server_contract(self) -> None:
        """采纳服务端 metadata 声明的**相机清单**（声明了就以下发为准；未声明则用 adapter 启用集）。

        本仓对接的 openpi piper 分支在 ``policy_metadata`` 里声明 ``cameras``（服务端输入变换
        按这些名字取图，多送无用、少送报错）；官方 openpi 不声明任何布局字段 → 不做任何改动。
        """
        declared = self.server_metadata.get("camera_names") or self.server_metadata.get("cameras")
        self._server_cameras = [str(c).strip() for c in declared] if declared else None
        self._warn_missing_server_cameras()

    def prepare(self, observation=None):
        """预热：openpi 官方首帧推理很慢（模型加载/编译），官方建议先发观测 warm up。

        用当前观测向服务端发一次推理并**丢弃结果**（服务端无动作缓存，丢弃安全），提前
        触发模型加载，避免首次 rollout 卡在推理。**未连接 / 无观测 → no-op（不抛错）**
        ——所有策略客户端的 ``prepare`` 同一契约（见 ``BasePolicyClient.prepare``）。

        副产品：这一块同时**实测出真实块长**（``observed_chunk_len``）——官方 metadata
        不声明 action_horizon，预热是拿到真实块长最早的时机（供 RTC 兜底校准 H）。
        """
        if observation is None or not self.connected:
            return
        self.infer_chunk(observation)  # 丢弃预热块

    def infer_chunk(self, observation, index: int | None = None) -> ActionChunk:
        """请求一次推理并返回**原始动作块**（``[H, dim]`` 或单步 ``[dim]``）。

        ``index``（绝对步号）对 openpi 无意义（服务端每次完整返回一块、由块长决定覆盖范围），
        仅用于回填 ``ActionChunk.start_index``（RTCManager 据此对齐块起始步号）。

        按 **openpi 官方 flat 契约** 组装观测：qpos → ``state``（原样上传，服务端归一化）；
        图像解码 → 等比补零缩放 ``image_size``（与官方服务端 / 训练管线同样的 letterbox，
        服务端再缩时为 no-op）→ **uint8 数组**（不再 jpeg），经 ``image_cameras`` 过滤 +
        ``rename_cameras`` 改名到服务端期望的**源**相机名；``prompt`` 每次请求动态携带
        （服务端每帧重新 tokenize，可换）。
        块缓存 / 切分 / 平滑由 RTCManager 负责，本方法不做任何缓存。
        """
        qpos = observation[KEY_OBS_QPOS]
        cameras = self._image_cameras  # 现读（bind_adapter / metadata 都可能改它）
        image_size = self.image_size  # 现读：会话内 infer config set image_size 立即生效
        prepared = {}
        for key, value in observation.items():
            if not key.startswith(KEY_OBS_IMAGE_PREFIX):
                continue
            edge_name = key[len(KEY_OBS_IMAGE_PREFIX) :]
            if cameras is not None and edge_name not in cameras:
                continue  # 只下发 image_cameras 子集（双相机），其余过滤
            model_key = self._rename_cameras.get(edge_name, edge_name)
            prepared[model_key] = prepare_openpi_image(value, image_size)
        payload = build_openpi_observation(qpos, prepared, prompt=self.prompt)
        response = self._transport.request(payload)
        chunk = ActionChunk(actions=extract_action_response(response), start_index=int(index or 0))
        self._note_chunk_len(chunk)  # 实测块长：供 RTC 兜底校准块长上限 H
        return chunk

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}
