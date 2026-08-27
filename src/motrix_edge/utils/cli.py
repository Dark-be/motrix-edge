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

"""基于 prompt_toolkit 的交互式 CLI。"""

from __future__ import annotations

import shlex

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout

from motrix_edge.utils.commands import CMD_ROBOT_ESTOP, CommandBus, CommandError, CommandRegistry


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

    def execute_line(self, line: str, bus: CommandBus) -> str | None:
        """解析并执行一行命令，返回回执文本。"""
        try:
            cmd = self.registry.parse_argv(shlex.split(line))
        except (ValueError, CommandError) as exc:
            return f"WARNING: {exc}"

        if cmd.name == CMD_ROBOT_ESTOP:  # 急停：即发即忘，不阻塞输入线程
            bus.push(cmd)
            return f"[{cmd.name}] accepted"

        try:
            result = bus.submit(cmd, timeout=10.0)
        except CommandError as exc:
            return f"WARNING: {exc}"
        return self.format_result(cmd, result)

    @staticmethod
    def format_result(cmd, result) -> str:
        """格式化命令回执。"""
        line = f"[{cmd.name}] {result.status}"
        if result.status == "ok":
            if result.data:
                line += f" {result.data}"
        else:
            line += f" ({result.status_code}): {result.error or 'no error'}"
        return line

    def run(self, bus: CommandBus) -> None:
        """阻塞读取命令，EOF / Ctrl-C 退出输入线程。"""
        with patch_stdout(raw=True):
            while True:
                try:
                    line = self._session.prompt(self.prompt)
                except (EOFError, KeyboardInterrupt):
                    print("CLI 输入结束。")
                    return
                if not line.strip():
                    continue
                message = self.execute_line(line, bus)
                if message:
                    print(message)


__all__ = ["CliSession", "CommandCompleter"]
