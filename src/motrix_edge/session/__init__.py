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

"""session 包 —— 会话（任务执行器）注册式工厂 + EdgeNode 节点生命周期。

通过 SESSION_REGISTRY 注册会话类，由 get_session() 依据上层命令（Command.name，
见 command/ 包）或配置 session.type 选择性实例化。会话按「**基座 + 功能 + 引擎**」三层
装配（见 wiki/design/motrix_edge_session.md）：只有 ``infer`` / ``rl`` 跑步进循环，
``capture`` 是基座装配（跑基座命令循环，不 observe）。

EdgeNode（node.py）在自身生命周期中，根据上层下发的命令（session run <type>）
选择并实例化会话（选择 + 启动合并为一个流程）；会话只是被节点启停的任务执行器。

**本包只有任务会话**：上传（``UploadService``）不是会话（无 ``run`` 生命周期、不经命令
总线、不占机器人互斥），由服务层直接持有，单独放在 ``motrix_edge/upload.py``。
"""

from .base import BaseSession, CaptureSession, RunResult
from .infer import InferSession

# 注册表：会话类型名 -> 会话类
SESSION_REGISTRY = {
    "capture": CaptureSession,
    "infer": InferSession,
}


def get_session(
    base_cfg,
    session_type=None,
    command_source=None,
    frame_manager=None,
    adapter=None,
    policy_type=None,
    capture_meta_store=None,
):
    """工厂：从注册表实例化会话。

    参数:
      base_cfg:      基础配置 dict
      session_type:  会话类型名（capture / infer），
                     缺省时读取 base_cfg["session"]["type"]，再缺省为 "capture"
      command_source: 无参可调用，非阻塞返回命令或 None；缺省为 CLI 按键。
      frame_manager: 共享 ``FrameManager``（Edge 级观测帧缓存；None = 会话自建）。
      adapter:       节点注入的 active adapter（单 adapter 包：采集 / 推理复用同一
                     adapter，生命周期归节点；会话只引用，不持有 / 不释放，必传）。
      policy_type:   推理策略类型（仅 infer 使用）；缺省用配置 policy.type。
      capture_meta_store: 采集元信息选项存储（``CaptureMetaStore``）；节点注入同一实例，
                     使「会话内 CLI 命令」与节点命令共用一份数据与一把锁（缺省 None = 会话
                     自行按需创建；不注入时不传该实参，兼容自定义会话）。
    """
    if session_type is None:
        session_type = base_cfg.get("session", {}).get("type", "capture")

    if session_type not in SESSION_REGISTRY:
        available = list(SESSION_REGISTRY.keys())
        raise ValueError(f"Unknown session type '{session_type}'. Available: {available}")

    cls = SESSION_REGISTRY[session_type]
    kwargs: dict = {
        "base_cfg": base_cfg,
        "command_source": command_source,
        "frame_manager": frame_manager,
        "adapter": adapter,
    }
    if session_type == "infer":
        kwargs["policy_type"] = policy_type  # 仅推理会话消费策略类型
    if capture_meta_store is not None:  # 进程内单实例：节点注入，避免多把锁各管一段
        kwargs["capture_meta_store"] = capture_meta_store
    return cls(**kwargs)


__all__ = [
    "BaseSession",
    "RunResult",
    "CaptureSession",
    "InferSession",
    "SESSION_REGISTRY",
    "get_session",
]
