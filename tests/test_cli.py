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

from motrix_edge.utils.cli import CliSession, CommandCompleter
from motrix_edge.utils.commands import CommandBus, CommandResult, build_command_registry


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
    result = cli.execute_line("infer ip", bus)

    assert result == "[infer ip] ok {'node_state': 'IDLE'}"


def test_execute_line_reports_parse_error():
    cli = CliSession(build_command_registry())
    result = cli.execute_line("unknown command", CommandBus())

    assert result is not None
    assert "unknown command" in result


def test_execute_line_estop_is_fire_and_forget():
    cli = CliSession(build_command_registry())
    bus = CommandBus()

    result = cli.execute_line("robot estop", bus)
    command = bus()

    assert result == "[robot estop] accepted"
    assert command is not None
    assert command.name == "robot estop"
