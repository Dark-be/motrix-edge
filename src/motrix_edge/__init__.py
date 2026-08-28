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

"""motrix_edge —— 机器人边缘节点（任务运行时层）。

对现有机器人的二次抽象：边缘节点作为任务运行时，负责收集 observation 并**主动请求**
推理节点（Endpoint）返回 action，本地校验后限速执行；任务生命周期由控制面（Console）驱动。

核心入口：
    EdgeNode      节点生命周期控制器（唯一节点入口/启动对象）
    get_adapter   机器人适配器工厂（硬件抽象层，entry point 发现）
    get_session   会话工厂（capture / infer）
"""

from motrix_edge.adapter import get_adapter
from motrix_edge.node import EdgeNode, NodeLifecycle, NodeState
from motrix_edge.session import get_session

__all__ = ["EdgeNode", "NodeLifecycle", "NodeState", "get_adapter", "get_session"]
