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

import threading

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from motrix_edge.command import CommandBus, CommandResult, build_command_registry
from motrix_edge.errors import ErrorCode
from motrix_edge.utils import cli as cli_module
from motrix_edge.utils.cli import CliSession, CommandCompleter


def test_command_completer_uses_registered_commands():
    completer = CommandCompleter(build_command_registry())
    completions = list(completer.get_completions(Document("session r"), CompleteEvent()))

    assert [item.text for item in completions] == ["session run"]


def test_command_completer_skips_argument_position():
    completer = CommandCompleter(build_command_registry())
    # 已输入完整命令词 + 空格：进入参数位置，不补全
    assert list(completer.get_completions(Document("robot execute "), CompleteEvent())) == []


def test_registry_match_spec_returns_positional_hint():
    registry = build_command_registry()
    # 已输入完整命令 + 参数：命中规范命令，位置参数用于工具栏提示
    spec = registry.match_spec("robot execute 0,0,0")
    assert spec is not None
    assert spec.name == "robot execute"
    assert spec.positional == ("qpos",)
    # 未匹配命令返回 None
    assert registry.match_spec("unknown foo") is None


def test_execute_line_submits_command_and_formats_result():
    cli = CliSession(build_command_registry())
    bus = CommandBus()

    def reply_worker():
        command = None
        while command is None:
            command = bus()
        assert command.reply_to is not None
        command.reply_to(CommandResult(status="ok", data={"node_state": "IDLE"}))

    threading.Thread(target=reply_worker, daemon=True).start()
    result = cli.execute_line("infer config", bus)

    assert result == "[infer config] ok {'node_state': 'IDLE'}"


def test_execute_line_reports_parse_error():
    cli = CliSession(build_command_registry())
    result = cli.execute_line("unknown command", CommandBus())

    assert result is not None
    assert "unknown command" in result


def test_execute_line_estop_is_fire_and_forget():
    cli = CliSession(build_command_registry())
    bus = CommandBus()

    result = cli.execute_line("robot estop", bus)
    command = bus.poll_critical()  # 急停走旁路队列（任务运行期间也即时生效）

    assert result == "[robot estop] accepted"
    assert command is not None
    assert command.name == "robot estop"


def test_execute_line_reports_rejection_with_error_code():
    """命令被拒与 HTTP 同一错误类型 / 同一错误码：CLI 也带 ``code`` 打印。"""
    cli = CliSession(build_command_registry())
    bus = CommandBus()

    def reply_worker():
        command = None
        while command is None:
            command = bus()
        assert command.reply_to is not None
        command.reply_to(CommandResult(status="rejected", error="not in this state", code=ErrorCode.CONFLICT))

    threading.Thread(target=reply_worker, daemon=True).start()
    result = cli.execute_line("infer config", bus)

    assert result == "[infer config] rejected (conflict): not in this state"


def test_format_result_omits_absent_error_code():
    """失败回执未带错误码时也能渲染：判成败只看 ``status``，不依赖「200 = OK」。"""
    line = CliSession.format_result("infer config", CommandResult(status="rejected", error="boom"))

    assert line == "[infer config] rejected: boom"


def test_execute_line_reports_timeout_as_warning(monkeypatch):
    """等不到回执（派发超时）→ 一行 WARNING，不抛异常、不阻塞调用方。"""
    monkeypatch.setattr(cli_module, "_CLI_TIMEOUT", 0.05)  # 免得测试真等 10s
    cli = CliSession(build_command_registry())

    result = cli.execute_line("infer config", CommandBus())  # 无人消费 → 超时

    assert result.startswith("WARNING:")
