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
    """

    def __init__(self, policy_config: dict) -> None:
        self.policy_config = policy_config or {}
        self.server_metadata: dict = {}

    def connect(self):
        """连接推理节点（初始化传输、读取服务端 metadata）。"""
        raise NotImplementedError("Subclasses should implement this method.")

    def infer(self, observation: dict):
        """输入观测，返回动作。

        动作格式由策略契约决定（如 openpi 返回单步 action 数组）；异常时返回 None 供上层跳过。
        """
        raise NotImplementedError("Subclasses should implement this method.")

    def drain(self, observation=None):
        """只消费当前缓存的 action chunk，不发新推理请求；无缓存返回 None。

        observation 可选：需要按策略映射（完整动作空间映射由 adapter 负责，见
        DualPiperAdapter.configure）时传入当前观测。动作块缓存由**各策略自有**实现
        （openpi/act 各自管理），基类无缓存消费逻辑，返回 None。
        """
        return None

    def reset(self):
        """复位策略状态（如清空 action chunk 缓存）。"""
        pass

    def disconnect(self):
        """释放连接资源。"""
        pass
