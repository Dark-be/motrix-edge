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

"""命令派发 —— 注册校验 + push / submit 分流 + 同步等回执（**传输无关**）。

本地 CLI（``utils/cli.py``）与 HTTP（``server.CommandService``）共用本类，因此两端没有
任何语义差异：同一条命令在两端都走同一注册表、同一分流规则、同一超时与同一回执类型
（:class:`~motrix_edge.command.core.CommandResult`）；失败也抛同一错误类型
（:class:`~motrix_edge.command.core.CommandError`，带 edge 错误码 ``code``）。

**租约不在本层**：它是 HTTP 入口的权限门（见 ``server.CommandService``），也是 CLI 与
HTTP 唯一的差异。本层不认识 HTTP / 租约 / capability。
"""

from .core import Command, CommandBus, CommandResult
from .naming import CMD_NODE_RESET, CMD_ROBOT_ESTOP
from .registry import CommandRegistry, build_command_registry

# push 型命令（即发即忘，只入总线不等回执）：「不能等 / 不该等」的路径 —— 全局急停与节点复位。
PUSH_COMMANDS = (CMD_ROBOT_ESTOP, CMD_NODE_RESET)

# 同步等回执的缺省超时（秒）；调用方（CLI 由人在终端等）可按需放宽。
DEFAULT_TIMEOUT = 5.0


class CommandDispatcher:
    """命令派发：注册校验 → push / submit 分流 → 等回执。"""

    def __init__(self, bus: CommandBus, registry: CommandRegistry | None = None):
        self._bus = bus
        self.registry = registry if registry is not None else build_command_registry()

    def dispatch(
        self,
        name: str,
        params: dict | None = None,
        *,
        meta: dict | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """派发一条命令并返回回执。

        - 未注册的命令 → ``UnknownCommandError``（404）；
        - :data:`PUSH_COMMANDS` → 入总线后立即返回 ``status="accepted"``（无回执通道）；
        - 其余 → 同步等回执（缺省 :data:`DEFAULT_TIMEOUT`）；命令未被消费 → ``CommandError``（504）。

        ``meta`` 是调用方注入的控制元数据（``lease_id`` / ``source``）：本层只透传，
        **不解释** ``lease_id``（租约校验属 HTTP 入口）。
        """
        self.registry.get(name)
        cmd = Command(name, params=params or {}, meta=dict(meta or {}))
        if name in PUSH_COMMANDS:
            self._bus.push(cmd)
            return CommandResult(status="accepted")
        return self._bus.submit(cmd, timeout=DEFAULT_TIMEOUT if timeout is None else timeout)


__all__ = ["DEFAULT_TIMEOUT", "PUSH_COMMANDS", "CommandDispatcher"]
