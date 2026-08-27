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

"""identity 子包 —— Edge 本地设备身份声明与请求元数据。

提供：
  Identity                 身份 dataclass（``headers()`` 为预留发送接口）
  load_identity()          从 ``base_cfg["identity"]`` 加载
  new_correlation_id()     跨产品请求链路 correlation_id 生成器
  new_idempotency_key()    幂等键生成器（创建 / 上传 / 命令 / 状态推进请求必带）
"""

import uuid

from .base import Identity


def load_identity(base_cfg: dict) -> Identity:
    """从 ``base_cfg["identity"]`` 加载身份声明（缺省给占位值，便于无配置联调）。"""
    cfg = base_cfg.get("identity", {})
    return Identity(
        edge_id=cfg.get("edge_id", "edge-unknown"),
        edge_name=cfg.get("edge_name", "edge-unknown-name"),
        edge_version=cfg.get("edge_version", "0.0.0"),
    )


def new_correlation_id() -> str:
    """生成贯穿请求链路的 correlation_id（浏览器 → API → Edge → manifest）。"""
    return f"corr_{uuid.uuid4().hex}"


def new_idempotency_key() -> str:
    """生成幂等键：所有创建 / 上传完成 / 命令 / 状态推进请求必带。"""
    return f"idem_{uuid.uuid4().hex}"


__all__ = ["Identity", "load_identity", "new_correlation_id", "new_idempotency_key"]
