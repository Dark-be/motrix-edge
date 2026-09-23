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

"""命令对象、回执与传输（传输层）。

- ``Command`` / ``CommandResult``：控制单元与回执；
- ``CommandError`` / ``UnknownCommandError``：命令错误（携带 edge 错误码 ``code``）；
- ``META_*` / ``SOURCE_*``：``cmd.meta`` 的约定键（``source`` 仅可观测性，不作授权依据）；
- ``CommandBus``：``push`` 即发即忘 / ``submit`` 同步回执 / ``__call__``（命令源契约）。
"""

import queue
import time
from dataclasses import dataclass, field
from typing import Callable

from motrix_edge.errors import ErrorCode, ServiceError

from .naming import CMD_ROBOT_ESTOP


class CommandError(ServiceError):
    """命令错误（未知命令 / 非法参数 / 超时等）：``code`` 缺省 ``invalid_argument``。

    继承 :class:`~motrix_edge.errors.ServiceError`：HTTP 面由 ``server/app.py`` 的单一处理器
    把 ``code`` 映射成 HTTP 状态码并写进响应体，本地 CLI 直接展示 ``code`` —— 同一错误、
    同一语义，两端只差渲染。
    """

    default_code = ErrorCode.INVALID_ARGUMENT


class UnknownCommandError(CommandError):
    """未知命令名 / 别名。携带建议（已知命令列表）。"""

    def __init__(self, name: str, known: list[str]):
        super().__init__(f"unknown command '{name}'", code=ErrorCode.UNKNOWN_COMMAND)
        self.name = name
        self.known = known


@dataclass
class Command:
    """控制单元 —— 命令名 + 业务参数 + 授权元数据 + 回执通道。"""

    name: str
    params: dict = field(default_factory=dict)  # 业务参数（CLI key=value / HTTP dict）
    meta: dict = field(default_factory=dict)  # 控制元数据（lease_id / requester / idempotency_key）
    reply_to: Callable[["CommandResult"], None] | None = None  # 回执回调（None = fire-and-forget）


@dataclass
class CommandResult:
    """命令回执 —— 处理器执行结果，经 reply_to 返回给 submit 调用方。

    **判成败只看 ``status``**（``ok`` / ``accepted`` 为成功），两端（HTTP / CLI）一致；
    ``code`` **不参与判断**，它是 **edge 错误码**（词表见 :class:`~motrix_edge.errors.ErrorCode`）
    —— HTTP 面自行映射成状态码并在响应体回传，CLI 直接展示。失败回执必须显式给码；
    缺省 ``None`` = 未指定（对成功回执无意义）。
    """

    status: str = "ok"  # ok / rejected / error
    data: dict = field(default_factory=dict)
    error: str | None = None
    code: str | None = None  # edge 错误码（失败回执必填；成功无意义）


def ok_result(**data) -> CommandResult:
    """构造成功回执（data 直接展开为字段）。"""
    return CommandResult(status="ok", data=data)


# 提交时的回执截止时刻（``time.monotonic()``）：由 ``CommandBus.submit`` 写入 ``cmd.meta``，
# 供「动作下发前自查调用方是否已放弃」的处理器使用（见 ``deadline_exceeded``）。
META_REPLY_DEADLINE = "reply_deadline"

# 命令来源（``cmd.meta``）：**仅用于可观测性**（日志 / 排障），**不作为授权依据** —— 放行与否
# 只看租约，而租约只约束 HTTP / RPent 入口（本地 CLI 不经该门，这也是两者**唯一差异**）；
# 租约本身已是最可靠的来源信号，本字段是显式记录，用来回答「这条命令是谁发的」。
META_SOURCE = "source"
SOURCE_CLI = "cli"  # 本地交互式 CLI
SOURCE_HTTP = "http"  # HTTP 控制面（/v1/commands 与各 REST 端点）
SOURCE_RPENT = "rpent"  # RPent 兼容的 RPC 面（POST /call）
SOURCE_INTERNAL = "internal"  # 进程内直接调用（会话内部 / 测试）

# 安全命令（**任何状态**都要立即被消费，含任务运行期间）：走总线旁路队列（见 ``CommandBus``），
# 否则会被任务线程里的长操作（如预热加载模型，几十秒～几分钟）一起挡在队列里。
CRITICAL_COMMANDS = frozenset({CMD_ROBOT_ESTOP})


def deadline_exceeded(cmd) -> bool:
    """调用方（``CommandBus.submit``）是否已放弃等回执 —— 动作下发前的自查门。

    真机安全：命令超时**不等于**取消执行（命令已被消费），若不自查就会出现「HTTP 调用方拿到
    504/超时，机器人却按这条命令动了」。故 ``infer rollout`` / ``robot execute`` 在调用
    ``adapter.rollout`` / ``adapter.execute`` **之前**先问一次；过期 → 丢弃动作并回执
    ``504``（迟到回执由 submit 的 sink 丢弃）。

    只有经 ``submit`` 提交的命令带 ``reply_deadline``：``push``（即发即忘）/ 无 deadline 的
    命令（CLI 手工构造 / 测试）一律返回 False，不改变既有语义。
    """
    meta = getattr(cmd, "meta", None) or {}
    deadline = meta.get(META_REPLY_DEADLINE)
    if deadline is None:
        return False
    try:
        return time.monotonic() > float(deadline)
    except (TypeError, ValueError):  # 非法值（手工构造 meta）：按「未设置」处理
        return False


class CommandBus:
    """命令传输 —— 单总线多生产者（CLI / HTTP / 脚本）、单消费者（EdgeNode / 会话 poll）。

    - ``push(cmd)``：即发即忘（急停等安全命令），无回执；
    - ``submit(cmd, timeout)``：同步等回执（CLI / HTTP）；内部把 ``cmd.reply_to`` 接到
      结果队列上，处理器经 ``cmd.reply_to(result)`` 返回；超时抛 ``CommandError``（504）；
    - ``__call__()``：非阻塞取下一个命令或 None（command_source 契约，命令源可替换）；
    - ``poll_critical()``：非阻塞取一条**安全命令**（``CRITICAL_COMMANDS``）：任务运行期间
      主循环不 poll 普通命令（由会话消费），但急停必须**任何状态**立即生效。

    **双队列**：安全命令走单独队列（``push`` / ``submit`` 自动分流），普通命令不走——
    否则一条几分钟级的长操作（如预热加载模型）会把急停一起挡在队列里。
    """

    def __init__(self):
        self._queue: queue.Queue[Command | None] = queue.Queue()
        self._critical: queue.Queue[Command | None] = queue.Queue()

    def push(self, cmd: Command) -> None:
        """注入一个命令（fire-and-forget，线程安全）；安全命令进旁路队列（不会被会话抢走）。"""
        target = self._critical if cmd.name in CRITICAL_COMMANDS else self._queue
        target.put(cmd)

    def submit(self, cmd: Command, timeout: float = 5.0) -> CommandResult:
        """提交命令并同步等待回执；超时抛 ``CommandError``（504）。

        **超时不等于取消**：命令可能已被消费、处理器会继续执行。故提交前在 ``cmd.meta`` 写入
        ``reply_deadline``（``time.monotonic()``）——需要「调用方已放弃就不要产生副作用」的
        处理器（``infer rollout`` / ``robot execute``：下发动作到真机）在**动作下发前**自查
        （``deadline_exceeded``），过期则丢弃动作（否则会出现「调用方看到失败、机器人却动了」）。

        迟到回执（调用方已超时）经内部 sink **丢弃**而不阻塞会话线程（``reply_to`` 仍是普通的
        ``(CommandResult) -> None``，与 push 通道同形）。
        """
        reply_q: queue.Queue = queue.Queue(maxsize=1)

        def sink(result) -> None:
            try:
                reply_q.put_nowait(result)
            except queue.Full:  # 调用方已超时放弃：丢弃迟到回执（禁止阻塞会话线程）
                pass

        cmd.reply_to = sink
        cmd.meta[META_REPLY_DEADLINE] = time.monotonic() + timeout
        self.push(cmd)
        try:
            return reply_q.get(timeout=timeout)
        except queue.Empty:
            raise CommandError(f"command timed out: {cmd.name}", code=ErrorCode.TIMEOUT) from None

    def poll_critical(self) -> Command | None:
        """非阻塞取一条安全命令（无则 None）。**任何状态都该被消费**（含任务运行期间）。"""
        try:
            return self._critical.get_nowait()
        except queue.Empty:
            return None

    def __call__(self) -> Command | None:
        """非阻塞取下一个**普通**命令；空则返回 None（与 command_source 契约一致）。"""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None
