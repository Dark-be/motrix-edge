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

"""capability 命名（``<scope>/<verb>``）与旧拼写别名。

单一事实来源 = ``utils/commands.py`` 的命令词：capability 由命令词的空格 → 斜杠派生，
HTTP 面因此不需要第二张表。见 wiki/design/motrix_edge_rpent_bridge.md「命名约定」。
"""

from motrix_edge.command import (
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONNECT,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_TAKEOVER,
    CMD_ROBOT_TEACH,
    CMD_ROBOT_TELEOP,
    LEGACY_CAPABILITIES,
    build_command_registry,
    capability_for,
    resolve_capability,
)


def test_capability_is_command_words_joined_by_slashes():
    assert capability_for(CMD_ROBOT_EXECUTE) == "robot/execute"
    assert capability_for(CMD_ROBOT_TELEOP) == "robot/teleop"
    # 遥操作语义别名：模式由命令名决定（示教 / 接管），capability 各一
    assert capability_for(CMD_ROBOT_TEACH) == "robot/teach"
    assert capability_for(CMD_ROBOT_TAKEOVER) == "robot/takeover"
    assert capability_for(CMD_ROBOT_ESTOP) == "robot/estop"
    assert capability_for(CMD_NODE_RESET) == "node/reset"
    assert capability_for(CMD_INFER_CONNECT) == "infer/connect"
    assert capability_for(CMD_CAPTURE_SYNC) == "capture/sync"
    # 多级命令：capability 保留全部层级（scope 始终是首词）
    assert capability_for("infer rollout stop") == "infer/rollout/stop"
    assert capability_for("capture meta delete-key") == "capture/meta/delete-key"


def test_every_registered_command_round_trips():
    """命令词 → capability → 命令词：往返稳定，且 scope 是首词、无空格残留。"""
    names = build_command_registry().command_names
    assert names  # 注册表非空（防遍历空集合的假绿）
    for name in names:
        capability = capability_for(name)
        assert " " not in capability
        assert capability.startswith(f"{name.split()[0]}/")
        ref = resolve_capability(capability)
        assert ref is not None
        assert (ref.command, ref.capability, ref.deprecated) == (name, capability, False)


def test_space_separated_capability_is_accepted():
    """空格写法（``robot execute``）与斜杠写法解析结果一致。"""
    ref = resolve_capability("robot execute")
    assert ref is not None
    assert (ref.command, ref.capability, ref.deprecated) == (CMD_ROBOT_EXECUTE, "robot/execute", False)


def test_legacy_capability_maps_to_canonical_and_is_flagged():
    """旧拼写（``robot_execute`` 一类）保留一版：解析为规范 capability + deprecated。"""
    assert LEGACY_CAPABILITIES  # 别名表非空
    registered = set(build_command_registry().command_names)
    for legacy, canonical in LEGACY_CAPABILITIES.items():
        ref = resolve_capability(legacy)
        assert ref is not None
        assert ref.capability == canonical
        assert ref.deprecated is True  # 提示调用方迁移
        assert ref.command in registered  # 别名指向真实命令词（不指向幽灵命令）
        assert capability_for(ref.command) == canonical  # 别名值本身也是派生结果


def test_legacy_and_canonical_resolve_to_same_command():
    assert resolve_capability("estop").command == resolve_capability("robot/estop").command == CMD_ROBOT_ESTOP
    assert resolve_capability("reset").command == resolve_capability("node/reset").command == CMD_NODE_RESET


def test_blank_capability_resolves_to_none():
    """空 capability → None（调用方走骨架分支；不当作命令词）。"""
    assert resolve_capability(None) is None
    assert resolve_capability("") is None
    assert resolve_capability("   ") is None


def test_unknown_capability_passes_through_as_command_word():
    """未知拼写不在这里报错：原样转成命令词，由各处理器判定（未消费 → 骨架 accepted）。"""
    ref = resolve_capability("robot/nonexistent")
    assert ref is not None
    assert (ref.command, ref.capability, ref.deprecated) == ("robot nonexistent", "robot/nonexistent", False)
