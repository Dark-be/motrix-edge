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

"""命令总线 —— Edge 控制的统一命令模型、解析与传输。

设计见 wiki/design/motrix_edge_command_bus.md。分层：

  - ``Command``：控制单元（name / params / meta / reply_to），带参数、带回执；
  - ``CommandResult``：命令回执（status / data / error / code）；
  - ``CommandSpec`` / ``CommandRegistry``：注册式命令定义与解析（命令名 / 别名，CLI 文本 → Command）；
  - ``CommandBus``：命令传输 —— ``push`` 即发即忘 / ``submit`` 同步回执 / ``__call__``
    （EdgeNode / 会话主循环 poll，命令源可替换契约保留）。

本地命令（CLI / 脚本）与 HTTP 是**同一套命令语义**（同一 ``CommandSpec``、同一消费方、同一回执），
**唯一差异是租约**：本地 CLI 在进程内直连总线（``CommandBus``），不经过 HTTP 入口的租约门；
HTTP（``server.CommandService``）与 RPent 面必须先持 Edge 级活跃租约才下发。
校验分层：状态机校验（互斥兜底）对所有来源生效 —— 它是**消费方语义**，不是入口权限。
"""

from .config_commands import (
    get_rtc_params,
    handle_capture_meta,
    handle_infer_rtc,
    handle_policy_config,
    policy_config_status,
    set_policy_config,
)
from .core import (
    CRITICAL_COMMANDS,
    META_REPLY_DEADLINE,
    META_SOURCE,
    SOURCE_CLI,
    SOURCE_HTTP,
    SOURCE_INTERNAL,
    SOURCE_RPENT,
    Command,
    CommandBus,
    CommandError,
    CommandResult,
    UnknownCommandError,
    deadline_exceeded,
    ok_result,
)
from .dispatch import DEFAULT_TIMEOUT, PUSH_COMMANDS, CommandDispatcher
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
    LEGACY_CAPABILITIES,
    CapabilityRef,
    capability_for,
    resolve_capability,
)
from .params import (
    ROLLOUT_MODE_CONTINUOUS,
    ROLLOUT_MODE_SINGLE,
    parse_bool,
    parse_meta,
    parse_qpos,
    parse_rollout_mode,
    parse_teleop_mode,
)
from .registry import CommandRegistry, CommandSpec, build_command_registry
