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

"""命令注册与 CLI 文本解析。

``CommandRegistry`` 是命令名的唯一注册处（``build_command_registry`` 登记全部命令）；
CLI 文本经 ``parse_argv`` 按**最长前缀匹配**命令名（支持多词），其余绑定位置参数 /
``key=value``；HTTP capability 通道也用它校验命令词（未知 → ``UnknownCommandError``）。
"""

from dataclasses import dataclass

from .core import Command, CommandError, UnknownCommandError
from .naming import (
    CMD_ADAPTER_CONFIG,
    CMD_ADAPTER_CONFIG_CURRENT,
    CMD_ADAPTER_CONFIG_SET,
    CMD_CAPTURE_EPISODE_END,
    CMD_CAPTURE_EPISODE_START,
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_CAPTURE_SYNC,
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_CONNECT,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
    CMD_INFER_PROMPT,
    CMD_INFER_ROLLOUT,
    CMD_INFER_ROLLOUT_STOP,
    CMD_INFER_RTC,
    CMD_INFER_RTC_SET,
    CMD_LEASE_REVOKE,
    CMD_NODE_RESET,
    CMD_ROBOT_ESTOP,
    CMD_ROBOT_EXECUTE,
    CMD_ROBOT_RESET,
    CMD_ROBOT_TELEOP,
    CMD_SESSION_QUIT,
    CMD_SESSION_RUN,
)


@dataclass
class CommandSpec:
    """命令定义 —— 命令名（空格分隔多词）+ 位置参数名。

    命令语义由分发方（EdgeNode / 会话）显式实现；registry 只负责解析。命令名用
    **空格**分隔多词（如 ``session run``），不再用点连接命令词；``positional`` 声明
    位置参数名（如 ``session run`` 的 ``session``，CLI 裸词按序绑定）。
    """

    name: str
    positional: tuple[str, ...] = ()


class CommandRegistry:
    """注册式命令解析 —— 命令名注册表 + CLI 文本解析。

    新增命令 = ``register(CommandSpec(...))``；``parse_argv`` 把 CLI 文本（``shlex``
    分词）解析为 ``Command``：
    - 命令名按注册表**最长前缀匹配**（支持多词，如 ``session run capture`` → 命令
      ``session run``，位置参数 ``session=capture``）；
    - 位置参数（``positional`` 声明的裸词）按序绑定进 ``params``；
    - ``key=value`` / ``--key value`` 也进 ``params``（参数合法性由处理器按需校验）。
    """

    def __init__(self):
        self._specs: dict[str, CommandSpec] = {}

    @property
    def command_names(self) -> tuple[str, ...]:
        """返回已注册的规范命令名，供 CLI 补全使用。"""
        return tuple(sorted(self._specs))

    def match_spec(self, text: str) -> CommandSpec | None:
        """返回文本开头已匹配的规范命令定义（最长前缀匹配）；未匹配返回 None。

        供 CLI 底部工具栏提示命令参数；与 ``parse_argv`` 的匹配语义保持一致。
        """
        words = text.split()
        for spec in sorted(self._specs.values(), key=lambda s: len(s.name.split()), reverse=True):
            if words[: len(spec.name.split())] == spec.name.split():
                return spec
        return None

    def register(self, spec: CommandSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"command already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> str:
        """命令名 → 规范命令名；未知抛 ``UnknownCommandError``（含已知命令）。"""
        if name not in self._specs:
            raise UnknownCommandError(name, sorted(self._specs))
        return name

    def parse_argv(self, argv: list[str]) -> Command:
        """CLI 文本解析：按最长前缀匹配命令名（多词），其余绑定位置参数 / key=value。"""
        if not argv:
            raise CommandError("empty command")
        # 最长前缀匹配命令名（如 ["session", "run", "capture"] → "session run"）
        name = ""
        matched = 0
        for spec in sorted(self._specs.values(), key=lambda s: len(s.name.split()), reverse=True):
            words = spec.name.split()
            if len(words) <= len(argv) and argv[: len(words)] == words:
                name = spec.name
                matched = len(words)
                break
        if not name:
            raise UnknownCommandError(argv[0], sorted(self._specs))
        spec = self._specs[name]

        params: dict[str, str] = {}
        pos_args = list(spec.positional)
        i = matched
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                body = tok[2:]
                if "=" in body:
                    key, val = body.split("=", 1)
                    params[key] = val
                else:
                    if i + 1 >= len(argv):
                        raise CommandError(f"missing value for --{body}")
                    params[body] = argv[i + 1]
                    i += 1
            elif "=" in tok:
                key, val = tok.split("=", 1)
                params[key] = val
            elif pos_args:  # 位置参数：裸词按序绑定（如 session run capture → session=capture）
                params[pos_args.pop(0)] = tok
            else:
                raise CommandError(f"unexpected positional argument: {tok}")
            i += 1
        return Command(name=name, params=params)


def build_command_registry() -> CommandRegistry:
    """默认命令注册表：登记全部命令名（空格分隔，不用点；无短别名）。

    命令语义由 node / session 显式实现；新增命令 = 注册 CommandSpec（含位置参数名）。
    """
    registry = CommandRegistry()
    for spec in [
        CommandSpec(name=CMD_SESSION_RUN, positional=("session",)),  # session run <type>
        CommandSpec(name=CMD_SESSION_QUIT),
        CommandSpec(name=CMD_ROBOT_RESET),
        CommandSpec(name=CMD_ROBOT_ESTOP),
        # robot execute <qpos> [joint|pose]
        CommandSpec(name=CMD_ROBOT_EXECUTE, positional=("qpos", "action_space")),
        CommandSpec(name=CMD_ROBOT_TELEOP, positional=("enabled", "mode")),  # robot teleop <true|false> [mode]
        CommandSpec(name=CMD_CAPTURE_EPISODE_START),  # capture episode start
        CommandSpec(name=CMD_CAPTURE_EPISODE_END),  # capture episode end
        CommandSpec(name=CMD_CAPTURE_SYNC, positional=("meta",)),  # capture sync --meta <json>
        CommandSpec(name=CMD_CAPTURE_META_LIST, positional=("key",)),  # capture meta list [key]
        CommandSpec(name=CMD_CAPTURE_META_ADD, positional=("key", "value")),  # capture meta add <key> <value>
        CommandSpec(name=CMD_CAPTURE_META_EDIT, positional=("key", "old", "new")),  # edit <key> <old> <new>
        CommandSpec(name=CMD_CAPTURE_META_DELETE, positional=("key", "value")),  # capture meta delete <key> <value>
        CommandSpec(name=CMD_CAPTURE_META_DELETE_KEY, positional=("key",)),  # capture meta delete-key <key>
        CommandSpec(name=CMD_ADAPTER_CONFIG),  # adapter config：查询运行时 adapter 能力配置
        CommandSpec(name=CMD_ADAPTER_CONFIG_SET, positional=("json",)),  # adapter config set <json>
        CommandSpec(name=CMD_ADAPTER_CONFIG_CURRENT),  # adapter config current：当前生效能力（启用臂 / 相机）
        CommandSpec(name=CMD_LEASE_REVOKE),  # lease revoke：撤销 Edge 当前租约（清理幽灵租约）
        CommandSpec(name=CMD_NODE_RESET),
        CommandSpec(name=CMD_INFER_ROLLOUT, positional=("mode",)),  # infer rollout [single|continuous]
        CommandSpec(name=CMD_INFER_ROLLOUT_STOP),  # infer rollout stop：停止持续推理（会话保持）
        CommandSpec(name=CMD_INFER_CONNECT),  # infer connect：连接 + 启动异步预热（下发动作之外的推理）
        CommandSpec(name=CMD_INFER_PROMPT, positional=("prompt",)),  # infer prompt <text>：运行时改文本指令
        CommandSpec(name=CMD_INFER_RTC),  # infer rtc：查询 RTC 参数 / 运行状态
        CommandSpec(name=CMD_INFER_RTC_SET, positional=("json",)),  # infer rtc set <json>：设置 RTC 参数
        CommandSpec(name=CMD_INFER_MODEL),  # infer model：查询策略模型路径（lerobot 类）
        CommandSpec(name=CMD_INFER_MODEL_SET, positional=("path",)),  # infer model set <path>
        CommandSpec(name=CMD_INFER_CONFIG),  # infer config：查询策略配置项（schema + 当前值）
        CommandSpec(name=CMD_INFER_CONFIG_SET, positional=("json",)),  # infer config set <json>
    ]:
        registry.register(spec)
    return registry
