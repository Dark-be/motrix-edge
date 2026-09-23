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

"""基于 prompt_toolkit 的交互式 CLI（``motrix-edge run`` 的键盘输入线程）。

本模块只管**终端交互**：行编辑 / 历史 / 补全 / 并发输出保护，以及把一行文本经
``CommandRegistry`` 解析后提交给共享命令通道。命令的注册校验 / push 分流 / 回执语义与
HTTP **共用** ``CommandDispatcher``（错误也是同一个 ``CommandError``，带 edge 错误码 ``code``）；
唯一差异是 CLI 在进程内直连总线、**不经 HTTP 入口的租约门**。入口、子命令与运行装配在
``__main__.py``；见 ``wiki/design/motrix_edge_config.md`` 与
``wiki/design/motrix_edge_command_bus.md``。
"""

from __future__ import annotations

import shlex

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout

from motrix_edge.command import (
    META_SOURCE,
    SOURCE_CLI,
    CommandBus,
    CommandDispatcher,
    CommandError,
    CommandRegistry,
    CommandResult,
)

# CLI 回执超时（秒）：人在终端等，比派发缺省（5s）宽松
_CLI_TIMEOUT = 10.0


class CommandCompleter(Completer):
    """按命令注册表补全命令名（支持多词命令，如 ``session run``）。"""

    def __init__(self, registry: CommandRegistry):
        self._commands = tuple(sorted(registry.command_names))

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text or text.endswith(" "):  # 参数位置不补全
            return
        for command in self._commands:
            if command.startswith(text):
                yield Completion(command, start_position=-len(text))


class CliSession:
    """交互终端会话：prompt_toolkit 提供行编辑、历史、补全与并发输出保护。

    ``patch_stdout`` 使 node / web 线程的 ``print`` 输出（含 ``debug_print``）在
    终端内联且不打断当前输入行；命令仍由 ``CommandRegistry`` 解析，CLI 与 HTTP
    共享同一命令契约。
    """

    def __init__(self, registry: CommandRegistry, *, prompt: str = "motrix-edge> "):
        self.registry = registry
        self.prompt = prompt
        self._session = PromptSession(
            history=InMemoryHistory(),
            completer=CommandCompleter(registry),
            complete_while_typing=True,
            bottom_toolbar=self._toolbar,
        )

    def _toolbar(self):
        """底部工具栏：提示当前已匹配命令及其位置参数名（未匹配时为空）。"""
        spec = self.registry.match_spec(get_app().current_buffer.text)
        if spec is None:
            return ""
        hint = f"命令: {spec.name}"
        if spec.positional:
            hint += f"  参数: {' '.join(spec.positional)}"
        return HTML(f"<b>{hint}</b>")

    def execute_line(self, line: str, bus: CommandBus) -> str:
        """解析并执行一行命令，返回可打印的回执文本（解析失败 / 超时也返回文案，不抛）。

        派发与 HTTP 共用 ``CommandDispatcher``（同一注册校验 / push 分流 / 回执语义），错误也
        是同一个 ``CommandError``（带 edge 错误码 ``code``）；唯一差异是 CLI 在进程内直连
        总线、**不经过 HTTP 入口的租约门**。
        """
        try:
            cmd = self.registry.parse_argv(shlex.split(line))
        except (ValueError, CommandError) as exc:  # 解析失败 / 未注册命令
            return f"WARNING: {exc}"

        # CLI = 进程内本地通道：直连 CommandBus，**不需要租约**（租约只约束 HTTP / RPent 入口）。
        # 除租约外与 HTTP 完全同语义：同一注册表、同一消费方、同一状态校验与回执形状。
        dispatcher = CommandDispatcher(bus, self.registry)
        try:
            result = dispatcher.dispatch(cmd.name, cmd.params, meta={META_SOURCE: SOURCE_CLI}, timeout=_CLI_TIMEOUT)
        except CommandError as exc:  # 与 HTTP 同一错误类型：同一个 edge 错误码
            return f"WARNING: [{exc.code}] {exc}"
        return self.format_result(cmd.name, result)

    @staticmethod
    def format_result(name: str, result: CommandResult) -> str:
        """格式化回执（只依赖命令名，与命令对象解耦；回执类型与 HTTP 同源）。"""
        line = f"[{name}] {result.status}"
        if result.status in ("ok", "accepted"):
            if result.data:
                line += f" {result.data}"
        else:
            # 失败：code 是 edge 错误码（缺省 None = 未指定，不显示括号）
            code = f" ({result.code})" if result.code else ""
            line += f"{code}: {result.error or 'no error'}"
        return line

    def run(self, bus: CommandBus) -> None:
        """阻塞读取命令；EOF / Ctrl-C 只退出输入线程（node 主循环与生命周期清理不受影响）。"""
        with patch_stdout(raw=True):
            while True:
                try:
                    line = self._session.prompt(self.prompt)
                except (EOFError, KeyboardInterrupt):
                    print("CLI 输入结束（EOF / Ctrl-C），节点继续运行。")
                    return
                if line.strip():
                    print(self.execute_line(line, bus))


__all__ = ["CliSession", "CommandCompleter"]
