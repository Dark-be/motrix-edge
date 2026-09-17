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

"""llm 策略包 —— 云端 VLM 直接输出笛卡尔轨迹的无训练策略（设计见
wiki/design/motrix_edge_llm_policy.md）。

- ``trajectory``：模型输出的稀疏轨迹点 → 解析 / 校验 / 重采样成控制步动作块（纯逻辑）；
- ``client``：``LLMPolicyClient``——观测组装、调用 OpenAI 兼容端点、失败即不下发。

注册与配置项在 ``motrix_edge.policy``（``POLICY_REGISTRY["llm"]`` / ``POLICY_CONFIG_ITEMS["llm"]``）。
"""

from motrix_edge.policy.llm.client import LLMPolicyClient
from motrix_edge.policy.llm.trajectory import (
    ACTION_DIM_PER_ARM,
    POSE_DIM,
    TrajectoryError,
    TrajectoryPoint,
    parse_trajectory,
    resample_trajectory,
    trajectory_block,
)

__all__ = [
    "ACTION_DIM_PER_ARM",
    "POSE_DIM",
    "LLMPolicyClient",
    "TrajectoryError",
    "TrajectoryPoint",
    "parse_trajectory",
    "resample_trajectory",
    "trajectory_block",
]
