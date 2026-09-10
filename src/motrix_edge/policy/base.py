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


class BasePolicyClient:
    """推理策略客户端基类（策略侧最小接口）。

    与 RobotAdapter 基类一致：以 NotImplementedError 定义抽象接口，
    生命周期由 InferSession（会话）驱动：session_start 时 connect，session_finish 时 disconnect。

    ``requires_prompt``：该策略是否**需要文本指令（prompt）**——语言条件策略（openpi）为
    ``True``，推理前必须已 ``infer prompt <text>`` 预置非空文本（会话据此门控）；
    非语言条件策略（act：ACT 不接受文本条件）为 ``False``，不参与 prompt 门控。
    """

    requires_prompt: bool = False  # 是否需要 prompt（语言条件策略子类覆盖为 True）

    def __init__(self, policy_config: dict) -> None:
        self.policy_config = policy_config or {}
        self.server_metadata: dict = {}
        # 文本指令（prompt）：**仅语言条件策略（``requires_prompt=True``，如 openpi）使用**——
        # 推理前必须非空（InferSession 门控），录制 rollout 时作 episode 的 task_name。
        # 非语言条件策略（act）不使用 prompt：保持 None，不参与门控、不下发。
        # 运行时经 ``infer prompt <text>`` 更新。
        self.prompt = None

    @property
    def connected(self) -> bool:
        """是否已建立到推理节点的连接。子类实现（如基于 ``transport.connected``）。"""
        return False

    def connect(self):
        """建立连接（可安全重复 / 重连）：初始化传输、读取服务端 metadata。"""
        raise NotImplementedError("Subclasses should implement this method.")

    def ensure_connected(self):
        """惰性连接：未连接则 ``connect()``，已连 no-op。供会话 rollout 自动触发。"""
        if not self.connected:
            self.connect()

    def prepare(self, observation=None):
        """可选预热：下发策略指令 / 触发服务端模型加载（act 覆盖）；其余策略 no-op。

        需要观测以确定 state 维度 / 相机；显式 ``infer connect`` 或首次 rollout 前调用，
        把「真正就绪」提前，避免首次 infer 阻塞模型加载。
        """
        pass

    def bind_adapter(self, action_dim=None, camera_names=None):
        """绑定推理时机器人适配器启用的布局（启用臂 qpos 维数 + 启用相机名）。

        由推理会话（InferSession）在进入会话时调用：把 adapter 运行时配置（``adapter
        config set`` 的 enabled_arms / enabled_cameras）传给策略客户端，使布局的单一
        事实来源 = adapter（策略**不另读** edge.yml 的相机名）。默认 no-op；需要按启用
        相机过滤 / 按启用臂切分的策略（如 openpi）覆盖。
        """
        pass

    def infer_chunk(self, observation: dict, index: int | None = None):
        """输入观测，返回**一次推理的原始动作块**（策略唯一职责：取推理结果）。

        返回 ``ActionChunk``（``[H, dim]`` + 首步绝对步号）或裸 ndarray（``[H, dim]`` /
        单步 ``[dim]``），空 / 失败返回 ``None`` 供上层跳过。

        ``index`` = 当前**绝对步号**（``RTCManager`` 传入）：需要按步号组织请求的流式策略
        （act 的 ``TimedObservation.timestep``）使用；其余策略（openpi）忽略。

        **块缓存 / 三元切分 / 时序平滑 / 预取时机由 ``motrix_edge.rtc`` 统一负责**，策略
        实现里不再有游标、timestep 缓存与重叠聚合（见 wiki/design/motrix_edge_rtc.md）。
        """
        raise NotImplementedError("Subclasses should implement this method.")

    def reset(self):
        """复位策略状态（如服务端会话状态）。动作块缓存由 RTCManager 负责，不在此。"""
        pass

    def disconnect(self):
        """释放连接资源。"""
        pass
