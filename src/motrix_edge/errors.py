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

"""Edge 统一业务错误 —— **edge 自己的错误码**（词表唯一来源，与传输无关）。

各层只抛自己的错误类型（命令层 ``command.CommandError``、租约层 ``lease.LeaseError``、
会话层 ``session.UploadError``、服务层 ``server.PreviewError`` / ``WebRTCError`` …），且只讲
**edge 错误码**（:class:`ErrorCode`）：

- HTTP 面（``server/app.py``）**自行维护** ``code → HTTP 状态码`` 映射，并把 code 与人读原因
  一起写进响应体（``{"detail": ..., "code": ...}``）；
- 本地 CLI 直接展示 code（``[infer config] rejected (conflict): ...``）。

即：**HTTP 状态码是 HTTP 自己的事，业务层只认自己的 code**。放在顶层（而不是 ``server/``）
是为了让命令层 / 租约层 / 会话层都能继承而不产生反向依赖（``server`` 是最上层）。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Edge 错误码词表（**业务层唯一词表**，新增错误场景只在此加码）。

    ``str`` 枚举：可与字符串直接比较 / 序列化（``ErrorCode.CONFLICT == "conflict"``），
    故调用点写 ``code=ErrorCode.CONFLICT`` 与 ``code="conflict"`` 等价。
    """

    INVALID_ARGUMENT = "invalid_argument"  # 入参非法（缺参 / 类型 / 越界 / 非法键）
    UNKNOWN_COMMAND = "unknown_command"  # 未知命令词 / capability
    NOT_FOUND = "not_found"  # 目标不存在（meta key / upload 项 …）
    CONFLICT = "conflict"  # 当前状态不适用（已在会话 / 节点未就绪 / 正在录制 …）
    LEASE_REQUIRED = "lease_required"  # 需要租约但未提供
    LEASE_EXPIRED = "lease_expired"  # 租约已过期
    FORBIDDEN = "forbidden"  # 租约不匹配（异租约）
    NOT_IMPLEMENTED = "not_implemented"  # 服务未注入 / 未实现
    UNAVAILABLE = "unavailable"  # 依赖暂不可用（观测未就绪 / 机器人进程离线 …）
    UPSTREAM_ERROR = "upstream_error"  # 上游失败（策略端连接失败 …）
    TIMEOUT = "timeout"  # 命令未被消费（submit 超时）
    INTERNAL = "internal"  # 内部错误


class ServiceError(Exception):
    """Edge 业务错误：``code`` 是 edge 错误码，``message`` 是给人读的原因。

    子类用 ``default_code`` 声明缺省码；调用点可按需覆盖
    （``ServiceError("...", code=ErrorCode.NOT_FOUND)``）。
    """

    default_code = ErrorCode.INTERNAL

    def __init__(self, message: str, code: ErrorCode | str | None = None):
        super().__init__(message)
        self.code = code if code is not None else self.default_code


__all__ = ["ErrorCode", "ServiceError"]
