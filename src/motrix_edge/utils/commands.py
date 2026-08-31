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
  - ``CommandResult``：命令回执（status / data / error / status_code）；
  - ``CommandSpec`` / ``CommandRegistry``：注册式命令定义与解析（命令名 / 别名，CLI 文本 → Command）；
  - ``CommandBus``：命令传输 —— ``push`` 即发即忘 / ``submit`` 同步回执 / ``__call__``
    （EdgeNode / 会话主循环 poll，命令源可替换契约保留）。

本地命令（CLI / 脚本）与 HTTP 统一为命令来源：本地无需租约、不走 HTTP（进程内总线）；
HTTP 命令须持有租约（``meta.lease_id``）。校验分层：状态机校验对所有来源生效（互斥兜底）；
租约校验仅对携带 ``meta.lease_id`` 的来源生效。
"""

import json
import queue
from dataclasses import dataclass, field
from typing import Callable

# ===== 命令名（空格分隔，单点定义）===========================================
# 命令词统一**空格分隔、不用点**。分层：
#   session run <type>  启动会话（选择 + 启动一步完成；type = capture / infer，参数）
#   session quit        退出当前会话（无参）
#   robot reset/estop   机器人复位 / 急停（仅 adapter 可用；estop 全局安全命令）
#   node reset          节点复位 / ERROR 恢复 → IDLE
#   infer rollout       单步推理闭环（会话内消费）
#   infer connect       显式连接推理节点（会话内消费）
#   capture sync        同步采集元信息到机器人进程（会话内消费）
CMD_SESSION_RUN = "session run"  # 启动会话（参数 session = capture / infer；一步完成）
CMD_SESSION_QUIT = "session quit"  # 退出当前会话
CMD_ROBOT_RESET = "robot reset"  # 复位机器人（仅 adapter 可用时）
CMD_ROBOT_ESTOP = "robot estop"  # 急停（安全停止 + 转 ERROR；全局安全命令）
CMD_ROBOT_EXECUTE = "robot execute"  # 直接下发 raw 动作（位置参数 qpos，逗号分隔数字）
CMD_ROBOT_TELEOP = "robot teleop"  # 设置遥操作开关（位置参数 enabled = true / false）
CMD_CAPTURE_EPISODE_START = "capture episode start"  # 开始一轮采集（episode 开始）
CMD_CAPTURE_EPISODE_END = "capture episode end"  # 结束一轮采集（episode 结束）
CMD_CAPTURE_SYNC = "capture sync"  # 同步采集元信息（位置参数 meta，JSON；采集会话内消费）
CMD_CAPTURE_META_LIST = "capture meta list"  # 列出采集元信息选项（位置参数 key 可选）
CMD_CAPTURE_META_ADD = "capture meta add"  # 新增采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_EDIT = "capture meta edit"  # 编辑采集元信息选项（位置参数 key, old, new）
CMD_CAPTURE_META_DELETE = "capture meta delete"  # 删除采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_DELETE_KEY = "capture meta delete-key"  # 删除采集元信息分类（位置参数 key）
CMD_NODE_RESET = "node reset"  # 节点复位 / ERROR 恢复 → IDLE
CMD_INFER_ROLLOUT = "infer rollout"  # 推理闭环（参数 count，连续执行多步）
CMD_INFER_CONNECT = "infer connect"  # 单次尝试连接推理节点（推理会话内消费）
CMD_INFER_IP = "infer ip"  # 查询推理节点 IP（内存态 policy.host）
CMD_INFER_IP_SET = "infer ip set"  # 设置推理节点 IP（位置参数 ip；下次 session run infer 生效）
CMD_INFER_PORT = "infer port"  # 查询推理节点端口（内存态 policy.port）
CMD_INFER_PORT_SET = "infer port set"  # 设置推理节点端口（位置参数 port；下次 session run infer 生效）


class CommandError(Exception):
    """命令错误（未知命令 / 非法参数 / 超时等）。携带 HTTP status_code。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class UnknownCommandError(CommandError):
    """未知命令名 / 别名。携带建议（已知命令列表）。"""

    def __init__(self, name: str, known: list[str]):
        super().__init__(f"unknown command '{name}'", status_code=404)
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
    """命令回执 —— 处理器执行结果，经 reply_to 返回给 submit 调用方。"""

    status: str = "ok"  # ok / rejected / error
    data: dict = field(default_factory=dict)
    error: str | None = None
    status_code: int = 200


def ok_result(**data) -> CommandResult:
    """构造成功回执（data 直接展开为字段）。"""
    return CommandResult(status="ok", data=data)


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


def parse_qpos(raw) -> list[float]:
    """解析 ``robot execute`` 的 qpos 位置参数：数字列表 → ``list[float]``。

    兼容方括号 / 空白 / **中英文逗号** / 分号 / 顿号分隔（如 ``[0, 0, 0]``、``0,0,0``、
    ``1，1，1``（全角逗号，中文输入法）、``0 0 0``、``0;0;0``）；缺失 / 非法 → ``ValueError``
    （命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip().strip("[]()").strip()
    if not text:
        raise ValueError("robot execute requires qpos (comma-separated numbers)")
    # 统一分隔符：全角逗号 / 分号 / 顿号 / 竖线 → 空格（容忍中文输入法）
    for sep in (",", "，", ";", "；", "、", "|"):
        text = text.replace(sep, " ")
    tokens = text.split()
    try:
        return [float(tok) for tok in tokens]
    except ValueError:
        raise ValueError(f"invalid qpos: {raw!r}") from None


def parse_bool(raw) -> bool:
    """解析布尔参数（``true/false``、``1/0``、``yes/no``、``on/off``）→ ``bool``。

    缺失 / 非法 → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ValueError(f"invalid boolean: {raw!r}")


def parse_rollout_count(raw, default: int = 1, maximum: int = 100) -> int:
    """解析 ``infer rollout`` 连续执行次数；缺省 1，合法范围 ``1..maximum``。"""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        count = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid rollout count: {raw!r}") from None
    if not 1 <= count <= maximum:
        raise ValueError(f"rollout count must be between 1 and {maximum}")
    return count


ROLLOUT_MODE_COUNT = "count"  # 推理 N 次（infer rollout <N>）
ROLLOUT_MODE_CONTINUOUS = "continuous"  # 持续推理（直到 session quit / estop）
ROLLOUT_MODE_DRAIN = "drain"  # 只消耗当前缓存动作块（不发新推理请求）


def parse_rollout_mode(raw, default: int = 1, maximum: int = 100) -> tuple[str, int]:
    """解析 ``infer rollout`` 参数 → ``(mode, count)``。

    - 空 / 数字 → ``("count", N)``：推理 N 次（缺省 1，范围 1..maximum）；
    - ``"continuous"`` → 持续推理（启动即回执，直到 session quit / estop）；
    - ``"drain"`` → 只消费当前已缓存的 action chunk（不发新推理请求）。
    非法 → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip().lower()
    if text in (ROLLOUT_MODE_CONTINUOUS, ROLLOUT_MODE_DRAIN):
        return (text, 0)
    return (ROLLOUT_MODE_COUNT, parse_rollout_count(raw, default=default, maximum=maximum))


def parse_meta(raw) -> dict:
    """解析 ``capture sync`` 的 meta 参数（JSON 字符串）→ ``dict``。

    缺失 / 非法 / 非对象 JSON → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("capture sync requires meta (JSON object)")
    try:
        meta = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError(f"invalid meta json: {raw!r}") from None
    if not isinstance(meta, dict):
        raise ValueError(f"meta must be a JSON object: {raw!r}")
    return meta


def get_policy_endpoint(base_cfg) -> dict:
    """读取推理节点端点配置（``base_cfg["policy"]`` 的 host / port）。

    返回 ``{"host": <str|None>, "port": <int|None>}``；未配置时对应字段为 None
    （端口合法值 1..65535，配置错误时返回原始值供上层判定）。
    """
    policy = base_cfg.get("policy", {})
    return {"host": policy.get("host"), "port": policy.get("port")}


def set_policy_endpoint(base_cfg, host=None, port=None) -> dict:
    """设置推理节点端点（写入 ``base_cfg["policy"]``，**内存态**，不写回 yaml）。

    返回更新后的端点 ``{"host", "port"}``；host / port 至少提供一个。非法端口 /
    空 host → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    policy = base_cfg.setdefault("policy", {})
    if host is not None:
        host = str(host).strip()
        if not host:
            raise ValueError("infer ip requires a non-empty host")
        policy["host"] = host
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError(f"invalid port: {port!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid port: {port!r}")
        policy["port"] = port
    return {"host": policy.get("host"), "port": policy.get("port")}


def handle_infer_endpoint(base_cfg, cmd):
    """处理推理端点配置命令（``infer ip`` / ``infer ip set`` / ``infer port`` / ``infer port set``）。

    配置写入 ``base_cfg["policy"]``（**内存态**，不写回 yaml），下次 ``session run
    infer`` 实例化策略客户端时生效。节点主循环（非任务态）与会话循环（任务态）**共用**
    本函数，保证配置命令「任何状态可用」——ACTIVE 会话运行期间由会话循环响应，不再被拒。
    参数缺失 / 非法 → rejected。
    """
    if cmd.name == CMD_INFER_IP_SET:
        host = cmd.params.get("ip")
        if host is None or not str(host).strip():
            return CommandResult(status="rejected", error="infer ip set requires <ip>", status_code=400)
        try:
            endpoint = set_policy_endpoint(base_cfg, host=host)
        except ValueError as exc:
            return CommandResult(status="rejected", error=str(exc), status_code=400)
        return ok_result(**endpoint)
    if cmd.name == CMD_INFER_PORT_SET:
        port = cmd.params.get("port")
        if port is None or not str(port).strip():
            return CommandResult(status="rejected", error="infer port set requires <port>", status_code=400)
        try:
            endpoint = set_policy_endpoint(base_cfg, port=port)
        except ValueError as exc:
            return CommandResult(status="rejected", error=str(exc), status_code=400)
        return ok_result(**endpoint)
    # infer ip / infer port：查询当前配置端点
    return ok_result(**get_policy_endpoint(base_cfg))


def handle_capture_meta(cmd, store=None) -> CommandResult:
    """处理 ``capture meta`` 命令族（list / add / edit / delete / delete-key）。

    读写 ``config/capture.yml`` 的 ``meta`` 段（``CaptureMetaStore``，可拓展任意分类 →
    选项数组）；配置级命令「任何状态可用」（与 ``infer ip`` 一致），节点主循环与会话
    循环共用本函数。``store`` 缺省用默认路径，测试可注入临时 store。
    参数缺失 / 重复 / 不存在 → rejected（不崩溃）。
    """
    from motrix_edge.utils.capture_meta import CaptureMetaStore

    store = store if store is not None else CaptureMetaStore()
    try:
        if cmd.name == CMD_CAPTURE_META_LIST:
            return ok_result(meta=store.list_meta(cmd.params.get("key")))
        if cmd.name == CMD_CAPTURE_META_ADD:
            return ok_result(meta=store.add(cmd.params.get("key"), cmd.params.get("value")))
        if cmd.name == CMD_CAPTURE_META_EDIT:
            return ok_result(meta=store.edit(cmd.params.get("key"), cmd.params.get("old"), cmd.params.get("new")))
        if cmd.name == CMD_CAPTURE_META_DELETE:
            return ok_result(meta=store.delete(cmd.params.get("key"), cmd.params.get("value")))
        if cmd.name == CMD_CAPTURE_META_DELETE_KEY:
            return ok_result(meta=store.delete_key(cmd.params.get("key")))
    except ValueError as exc:
        return CommandResult(status="rejected", error=str(exc), status_code=400)
    return CommandResult(status="rejected", error=f"unknown capture meta command: {cmd.name}", status_code=400)


class CommandBus:
    """命令传输 —— 单总线多生产者（CLI / HTTP / 脚本）、单消费者（EdgeNode / 会话 poll）。

    - ``push(cmd)``：即发即忘（急停等安全命令），无回执；
    - ``submit(cmd, timeout)``：同步等回执（CLI / HTTP）；内部把 ``cmd.reply_to`` 接到
      结果队列上，处理器经 ``cmd.reply_to(result)`` 返回；超时抛 ``CommandError``（504）；
    - ``__call__()``：非阻塞取下一个命令或 None（command_source 契约，命令源可替换）。
    """

    def __init__(self):
        self._queue: queue.Queue[Command | None] = queue.Queue()

    def push(self, cmd: Command) -> None:
        """注入一个命令（fire-and-forget，线程安全）。"""
        self._queue.put(cmd)

    def submit(self, cmd: Command, timeout: float = 5.0) -> CommandResult:
        """提交命令并同步等待回执；超时抛 ``CommandError``（504）。"""
        reply_q: queue.Queue = queue.Queue(maxsize=1)
        cmd.reply_to = reply_q.put  # 处理器调用 cmd.reply_to(result) → 结果入队
        self.push(cmd)
        try:
            return reply_q.get(timeout=timeout)
        except queue.Empty:
            raise CommandError(f"command timed out: {cmd.name}", status_code=504) from None

    def __call__(self) -> Command | None:
        """非阻塞取下一个命令；空则返回 None（与 command_source 契约一致）。"""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


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
        CommandSpec(name=CMD_ROBOT_EXECUTE, positional=("qpos",)),  # robot execute <qpos>
        CommandSpec(name=CMD_ROBOT_TELEOP, positional=("enabled",)),  # robot teleop <true|false>
        CommandSpec(name=CMD_CAPTURE_EPISODE_START),  # capture episode start
        CommandSpec(name=CMD_CAPTURE_EPISODE_END),  # capture episode end
        CommandSpec(name=CMD_CAPTURE_SYNC, positional=("meta",)),  # capture sync --meta <json>
        CommandSpec(name=CMD_CAPTURE_META_LIST, positional=("key",)),  # capture meta list [key]
        CommandSpec(name=CMD_CAPTURE_META_ADD, positional=("key", "value")),  # capture meta add <key> <value>
        CommandSpec(name=CMD_CAPTURE_META_EDIT, positional=("key", "old", "new")),  # edit <key> <old> <new>
        CommandSpec(name=CMD_CAPTURE_META_DELETE, positional=("key", "value")),  # capture meta delete <key> <value>
        CommandSpec(name=CMD_CAPTURE_META_DELETE_KEY, positional=("key",)),  # capture meta delete-key <key>
        CommandSpec(name=CMD_NODE_RESET),
        CommandSpec(name=CMD_INFER_ROLLOUT, positional=("count",)),  # infer rollout [count]
        CommandSpec(name=CMD_INFER_CONNECT),  # infer connect：单次尝试连接推理节点
        CommandSpec(name=CMD_INFER_IP),  # infer ip：查询推理节点 IP（无参）
        CommandSpec(name=CMD_INFER_IP_SET, positional=("ip",)),  # infer ip set <ip>
        CommandSpec(name=CMD_INFER_PORT),  # infer port：查询推理节点端口（无参）
        CommandSpec(name=CMD_INFER_PORT_SET, positional=("port",)),  # infer port set <port>
    ]:
        registry.register(spec)
    return registry
