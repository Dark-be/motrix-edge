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


import threading
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
from motrix_edge.transport import AsyncInferenceGrpcTransport


class LerobotActClient(BasePolicyClient):
    """ACT 策略客户端：与 lerobot 官方 AsyncInference gRPC policy_server 互通（流式动作块）。

    **非语言条件策略（``requires_prompt = False``）**：ACT 不接受文本条件，**不需要设置
    prompt**——不参与会话的 prompt 门控，也不会向服务端下发 ``task`` / prompt 字段。
    与 openpi（WebSocket + MsgPack 一问一答）不同，lerobot-act 走 **lerobot 原生流式**
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

    requires_prompt = False  # 非语言条件策略：不需要 prompt，不参与会话门控

    def __init__(self, policy_config: dict):
        super().__init__(policy_config=policy_config)
        self._connect_timeout = float(self.policy_config.get("connect_timeout", 5.0))
        self._transport = AsyncInferenceGrpcTransport(
            host=self.policy_config.get("host", "127.0.0.1"),
            port=self.policy_config.get("port"),
            connect_timeout=self._connect_timeout,
        )
        # lerobot lerobot-act 策略参数（edge.yml policy 段）
        self._actions_per_chunk = int(self.policy_config.get("actions_per_chunk", 50))
        # 块长契约：服务端按 actions_per_chunk 返回**整块**（构造时即可声明），首次推理后以实测
        # 块长覆盖（服务端仍可能少给，如临近 episode 结尾）；供 RTC 兜底校准块长上限 H。
        self.observed_chunk_len: int | None = self._actions_per_chunk
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
        # 其余 RPC 超时（服务端不响应时抛异常，避免无限阻塞；量级按各自语义给默认值）：
        #   policy_setup_timeout —— SendPolicyInstructions 触发服务端加载 ACT checkpoint，
        #                           可能耗时数十秒，给足 300s 仍保证有限；
        #   observation_timeout  —— SendObservations 单帧观测（图像已缩放）上传。
        self._policy_setup_timeout = float(self.policy_config.get("policy_setup_timeout", 300.0))
        self._observation_timeout = float(self.policy_config.get("observation_timeout", 10.0))
        # 运行时状态（绝对步号由 RTCManager 传入；本客户端不再自持 timestep 缓存）
        self._policy_sent = False  # 本连接是否已下发策略指令（连接级状态，见 _forget_policy）
        self._policy_features = None  # 已下发的 features（布局：state 维数 + 相机集）
        self._policy_lock = threading.Lock()  # 预热（命令线程）与推理（RTC 工作线程）可能并发下发

    # -- 连接 / 预热 / 断开 ---------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._transport.connected

    def prepare(self, observation=None):
        """预热：提前下发策略指令（SendPolicyInstructions），让服务端加载 ACT checkpoint。

        需要观测确定 state 维度 / 相机；已下发过则 no-op。**未连接 / 无观测 → no-op（不抛错）**
        ——与 openpi 同一契约（见 ``BasePolicyClient.prepare``）：预热失败不致命（会话记
        ``warmup_error``）；未握手的**推理**仍在 ``infer_chunk`` → ``_ensure_policy`` 里报
        ``RuntimeError``（服务端会静默忽略 Ready 之前下发的策略指令）。
        """
        if observation is None or not self.connected:
            return
        if not self._policy_sent:
            self._ensure_policy(observation)

    def connect(self):
        """建立 gRPC channel 并 ``Ready`` 握手（服务端据此**重置会话状态**）。

        ``server_metadata`` 是**本地声明**：AsyncInference 协议没有 metadata RPC（官方客户端
        也没有这个概念），这里只声明本客户端实现的协议 / 策略类型，供会话与前端展示。
        """
        self._forget_policy()  # 新连接 = 服务端新会话（Ready 清空服务端状态）：策略指令必须重发
        try:
            self._transport.connect()
            from lerobot.transport import services_pb2  # noqa: PLC0415 延迟导入

            self._transport.stub.Ready(services_pb2.Empty(), timeout=self._connect_timeout)
            self.server_metadata = {"protocol": "lerobot/async-inference", "policy_type": "lerobot-act"}
        except Exception:
            self._transport.close()
            self.server_metadata = {}
            raise

    def disconnect(self):
        self._transport.close()
        self.server_metadata = {}
        self._forget_policy()

    def _forget_policy(self) -> None:
        """忘记「已下发策略指令」（建立连接 / 断开时调用）。

        策略指令是**连接级**状态：服务端按会话加载模型（与官方 AsyncInferenceClient 一致，
        每次连接都先发 Ready + 策略指令）。不断言重发就会出现「重连后首个推理因服务端没有
        策略而失败」——重连（含超时后重试）是常见路径，不能依赖上一次连接的加载结果。

        ``chunk_seen`` 同属连接级（构造时声明的 ``actions_per_chunk`` 不是实测）：一并复位，
        否则重连后预热会误以为已取过块、跳过真正的那次 ``infer_chunk``。
        """
        with self._policy_lock:
            self._policy_sent = False
            self._policy_features = None
            self.chunk_seen = False

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
        chunk = ActionChunk(actions=actions, start_index=start)
        self._note_chunk_len(chunk)  # 实测块长：供 RTC 兜底校准块长上限 H
        return chunk

    # -- 内部 ----------------------------------------------------------------
    def _lerobot_features(self, observation) -> dict:
        """按观测生成 dataset-format 特征（state 分量名 + 相机特征），供 SendPolicyInstructions。

        与服务端 ``build_dataset_frame(features, raw_obs, "observation")`` 对齐；字段
        （``dtype`` / ``shape`` / ``names``）与官方客户端 ``hw_to_dataset_features(..., use_video=False)``
        同形——服务端还有 ``dataset_to_policy_features`` 会读 ``shape`` / ``names``，缺字段在那边会报错。
          - state:  {"dtype":"float32","shape":(N,),"names":[qpos_i]}
          - images: {"dtype":"image","shape":(H,W,3),"names":[height,width,channels]}（HWC，与下发一致）
        """
        dim = int(np.asarray(observation[KEY_OBS_QPOS]).size)
        state_names = [f"qpos_{i}" for i in range(dim)]
        features = {"observation.state": {"dtype": "float32", "shape": (dim,), "names": state_names}}
        height, width = self._image_size
        for _, dataset_cam in self._policy_cameras(observation):
            features[f"observation.images.{dataset_cam}"] = {
                "dtype": "image",
                "shape": (height, width, 3),
                "names": ["height", "width", "channels"],
            }
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
        """首次推理 / **布局变化**时下发策略指令（SendPolicyInstructions）：pickle RemotePolicyConfig。

        需要 ``pretrained_name_or_path``（服务端据此加载 ACT checkpoint）。

        布局（state 维数 + 相机集）是服务端 ``build_dataset_frame`` 的查表依据，**变了必须重发**
        （否则服务端按旧 features 查键 → KeyError）；重发会触发服务端重新加载模型，故只在布局
        真的变了才做（相机的像素值变化不算）。加锁：预热（命令线程）与推理（RTC 工作线程）
        可能并发到达，否则会重复下发。

        **必须在 connect() 之后调用**：服务端 ``Ready`` 才会把会话置为 running，在此之前
        （或连接已断）下发的策略指令会被**静默丢弃**——后续推理只会一直拿到 Empty 直到超时。
        """
        if self._transport.stub is None:  # 未握手：指令会被服务端静默忽略，这里直接报错
            raise RuntimeError(
                "lerobot-act: not connected (connect() first: server accepts policy instructions "
                "only after the Ready handshake)"
            )
        pretrained = self.policy_config.get("pretrained_name_or_path")
        if not pretrained:
            raise ValueError(
                "lerobot-act policy requires 'pretrained_name_or_path' in policy config "
                "(server loads the ACT checkpoint from it)"
            )
        from lerobot.async_inference.helpers import RemotePolicyConfig  # noqa: PLC0415
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import python_object_to_bytes  # noqa: PLC0415

        with self._policy_lock:
            features = self._lerobot_features(observation)
            if self._policy_sent and features == self._policy_features:
                return
            config = RemotePolicyConfig(
                policy_type="act",
                pretrained_name_or_path=pretrained,
                lerobot_features=features,
                actions_per_chunk=self._actions_per_chunk,
                device=self.policy_config.get("device", "cpu"),
                rename_map=dict(self.policy_config.get("rename_map") or {}),
            )
            self._transport.stub.SendPolicyInstructions(
                services_pb2.PolicySetup(data=python_object_to_bytes(config)), timeout=self._policy_setup_timeout
            )
            self._policy_sent = True
            self._policy_features = features  # 记录已下发的布局（下次比对决定是否重发）

    def _build_raw_observation(self, observation) -> dict:
        """edge 观测（qpos + jpeg/ndarray 图像）→ lerobot raw obs（state 分量标量 + uint8 图像）。

        图像解码为 uint8 RGB 后由 edge 直接 ``resize_with_pad`` 等比缩放到
        ``image_size``（默认 224×224，横向图上下留黑边），不依赖服务端缩放。
        """
        raw: dict = {}
        qpos = np.asarray(observation[KEY_OBS_QPOS]).reshape(-1)  # 兼容 (N,) / (1,N) / (N,1)（adapter 布局差异）
        for i, value in enumerate(qpos):
            raw[f"qpos_{i}"] = float(value)
        for edge, dataset_cam in self._policy_cameras(observation):
            value = observation[f"{KEY_OBS_IMAGE_PREFIX}{edge}"]
            raw[dataset_cam] = resize_with_pad(to_rgb_uint8(value), *self._image_size)
        return raw

    def _send_observation(self, raw: dict, timestep: int) -> None:
        """pickle(TimedObservation) 分块流式 SendObservations（must_go=True 强制推理）。"""
        from lerobot.async_inference.helpers import TimedObservation  # noqa: PLC0415
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import python_object_to_bytes, send_bytes_in_chunks  # noqa: PLC0415

        timed = TimedObservation(timestamp=time.time(), timestep=timestep, observation=raw, must_go=True)
        data = python_object_to_bytes(timed)
        iterator = send_bytes_in_chunks(data, services_pb2.Observation, silent=True)
        self._transport.stub.SendObservations(iterator, timeout=self._observation_timeout)

    def _receive_chunk(self, timestep: int):
        """GetActions 轮询直到取到**非空动作块**（服务端空闲 / 尚未推理完 → 稍候重试）。

        **不做步号门控**（与官方 ``RobotClient.receive_actions`` 一致）：服务端每次 GetActions
        弹出一个观测跑一次推理、返回**该观测**的整块——拿到非空块它就是最新的一块；若它整体
        过期（边界竞态 / 策略滞后），照常返回并由 RTCManager 统一丢弃（计入 ``stale_chunks``）。
        在这里按步号丢弃只会白扔一次推理、并让本步拿不到动作（旧实现即如此）。

        轮询总时长受 ``get_actions_timeout`` 约束：单次 GetActions 的 RPC 超时取**剩余时间**
        （服务端卡住时不会超出总预算）；服务端返回 Empty（尚未推理完）时稍候再轮询，避免忙等。
        “timestep” 只用于超时文案（本客户端不缺步号信息）。
        """
        from lerobot.transport import services_pb2  # noqa: PLC0415
        from lerobot.transport.utils import bytes_to_python_object  # noqa: PLC0415

        deadline = time.monotonic() + self._get_actions_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"lerobot-act: no action chunk for timestep {timestep} within {self._get_actions_timeout}s"
                )
            response = self._transport.stub.GetActions(services_pb2.Empty(), timeout=remaining)
            if response.data:
                return bytes_to_python_object(response.data)  # 首个非空块即采信（单飞保证只可能有本步的块）
            time.sleep(0.02)  # Empty：服务端尚未推理完，稍候再轮询（避免忙等）

    @staticmethod
    def _to_numpy(action) -> np.ndarray:
        """torch.Tensor / array → float32 一维动作数组（edge adapter 消费）。"""
        if hasattr(action, "detach"):
            action = action.detach().cpu()
        arr = np.asarray(action, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[0] == 1:
            arr = arr[0]
        return arr
