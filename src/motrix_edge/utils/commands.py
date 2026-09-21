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
import time
from dataclasses import dataclass, field
from typing import Callable

from motrix_edge.adapter.http_contract import TELEOP_MODES
from motrix_edge.policy import (
    policy_config_items,
    policy_config_keys,
    policy_config_runtime_keys,
    policy_features,
    validate_policy_type,
)
from motrix_edge.rtc import DEFAULT_RTC_CONFIG, validate_config, validate_params

# ===== 命令名（空格分隔，单点定义）===========================================
# 命令词统一**空格分隔、不用点**。分层：
#   session run <type>  启动会话（选择 + 启动一步完成；type = capture / infer，参数）
#   session quit        退出当前会话（无参）
#   robot reset/estop   机器人复位 / 急停（仅 adapter 可用；estop 全局安全命令）
#   node reset          节点复位 / ERROR 恢复 → IDLE
#   infer rollout       单步推理闭环（会话内消费）
#   infer connect       连接 + 启动异步预热（会话内消费；立即回执，可被急停/退出中断）
#   infer rtc           查询 / 设置 RTC 参数（实时动作块管理器；配置级，任何状态可用）
#   infer model         查询 / 设置策略模型路径（lerobot 类策略；运行时提供，配置级）
#   infer config        查询 / 设置策略配置项（每个策略有自己的独立配置项；配置级）
#   capture sync        同步采集元信息到机器人进程（会话内消费）
CMD_SESSION_RUN = "session run"  # 启动会话（参数 session = capture / infer；一步完成）
CMD_SESSION_QUIT = "session quit"  # 退出当前会话
CMD_ROBOT_RESET = "robot reset"  # 复位机器人（仅 adapter 可用时）
CMD_ROBOT_ESTOP = "robot estop"  # 急停（安全停止 + 转 ERROR；全局安全命令）
CMD_ROBOT_EXECUTE = "robot execute"  # 直接下发 raw 动作（位置参数 qpos，逗号分隔数字）
CMD_ROBOT_TELEOP = "robot teleop"  # 设置遥操作（位置参数 enabled = true/false；可选 mode = absolute|delta 人工接管）
CMD_CAPTURE_EPISODE_START = "capture episode start"  # 开始一轮采集（episode 开始）
CMD_CAPTURE_EPISODE_END = "capture episode end"  # 结束一轮采集（episode 结束）
CMD_CAPTURE_SYNC = "capture sync"  # 同步采集元信息（位置参数 meta，JSON；采集会话内消费）
CMD_CAPTURE_META_LIST = "capture meta list"  # 列出采集元信息选项（位置参数 key 可选）
CMD_CAPTURE_META_ADD = "capture meta add"  # 新增采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_EDIT = "capture meta edit"  # 编辑采集元信息选项（位置参数 key, old, new）
CMD_CAPTURE_META_DELETE = "capture meta delete"  # 删除采集元信息选项（位置参数 key, value）
CMD_CAPTURE_META_DELETE_KEY = "capture meta delete-key"  # 删除采集元信息分类（位置参数 key）
CMD_ADAPTER_CONFIG = "adapter config"  # 查询运行时 adapter 能力配置（enabled_arms / cameras / home）
CMD_ADAPTER_CONFIG_SET = "adapter config set"  # 设置运行时 adapter 能力配置（位置参数 json，JSON 对象）
CMD_ADAPTER_CONFIG_CURRENT = "adapter config current"  # 获取当前绑定 adapter 实际生效的能力配置（启用臂 / 相机）
CMD_LEASE_REVOKE = "lease revoke"  # 撤销 Edge 当前租约（管理员清理幽灵租约，释放可签发槽位）
CMD_NODE_RESET = "node reset"  # 节点复位 / ERROR 恢复 → IDLE
CMD_INFER_ROLLOUT = "infer rollout"  # 推理闭环（无参=单步；continuous=持续；多步/drain 已取消）
CMD_INFER_ROLLOUT_STOP = "infer rollout stop"  # 停止持续推理（回到会话 READY，不退会话也不断策略连接）
CMD_INFER_CONNECT = "infer connect"  # 连接 + 启动异步预热（推理会话内消费；立即回执，重复调用幂等）

CMD_INFER_PROMPT = "infer prompt"  # 设置推理文本指令（位置参数 prompt；会话内运行时可改）
CMD_INFER_RTC = "infer rtc"  # 查询 RTC 参数与运行状态（内存态 policy.rtc）
CMD_INFER_RTC_SET = "infer rtc set"  # 设置 RTC 参数（位置参数 json，JSON 对象；内存态 policy.rtc）
CMD_INFER_MODEL = "infer model"  # 查询 lerobot 类策略的模型路径（内存态 policy.pretrained_name_or_path）
CMD_INFER_MODEL_SET = "infer model set"  # 设置模型路径（位置参数 path；运行时提供，不写回 yaml）
CMD_INFER_CONFIG = "infer config"  # 查询当前策略的配置项（schema 清单 + 当前值 + 缺失必填项）
CMD_INFER_CONFIG_SET = "infer config set"  # 设置策略配置项（位置参数 json，按当前策略 schema 校验）


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


# 提交时的回执截止时刻（``time.monotonic()``）：由 ``CommandBus.submit`` 写入 ``cmd.meta``，
# 供「动作下发前自查调用方是否已放弃」的处理器使用（见 ``deadline_exceeded``）。
META_REPLY_DEADLINE = "reply_deadline"

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


def parse_teleop_mode(raw) -> str | None:
    """解析 ``robot teleop`` 的可选模式参数 → ``absolute`` / ``delta`` / ``None``。

    - 缺失 / 空 → ``None``：不指定模式（进程侧缺省 ``absolute``，与旧调用方等价）；
    - ``absolute`` → 主臂绝对位姿直连从臂 target（示教采集）；
    - ``delta`` → **人工接管**：以接管瞬间的主 / 从位姿为锚点、只叠加主臂增量（从臂不突变）；
    - 非法 → ``ValueError``（命令处理器回执 rejected，不崩溃）。

    取值单点定义在 ``motrix_edge.adapter.http_contract``（``TELEOP_MODES``，与 /v1/teleop 契约同源）。
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text not in TELEOP_MODES:
        raise ValueError(f"invalid teleop mode: {raw!r} (expect {'|'.join(TELEOP_MODES)})")
    return text


ROLLOUT_MODE_SINGLE = "single"  # 单步推理（infer rollout）
ROLLOUT_MODE_CONTINUOUS = "continuous"  # 持续推理（直到 infer rollout stop / session quit / estop）


def parse_rollout_mode(raw) -> str:
    """解析 ``infer rollout`` 参数 → 模式（``single`` 单步 / ``continuous`` 持续）。

    - 空 / ``"1"`` → ``single``：单步推理（一次 观测 → 推理 → 动作 闭环）；
    - ``"continuous"`` → ``continuous``：持续推理（启动即回执，直到 ``infer rollout
      stop`` / session quit / estop）；
    - 数字 ``>1`` → ``ValueError``（**多步推理已取消**：改用单步 / 持续 + ``capture
      episode start/end`` 录制 rollout 回合，见 wiki/design/motrix_edge_session.md）；
    - ``"drain"`` → ``ValueError``（**缓存推理已取消**：动作块只作策略内部缓存，
      不再提供「只消费缓存块」的命令模式）。
    非法 → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip().lower()
    if text in ("", "1"):
        return ROLLOUT_MODE_SINGLE
    if text == ROLLOUT_MODE_CONTINUOUS:
        return ROLLOUT_MODE_CONTINUOUS
    if text == "drain":
        raise ValueError("rollout drain mode removed: use capture episode start/end to record a rollout")
    if text.isdigit():
        count = int(text)
        if count > 1:
            raise ValueError(f"multi-step rollout removed: use single-step or continuous (got {raw!r})")
        raise ValueError(f"invalid rollout: {raw!r}")
    raise ValueError(f"invalid rollout: {raw!r}")


def parse_meta(raw, what: str = "capture sync") -> dict:
    """解析 JSON 对象参数（``capture sync --meta`` / ``infer rtc set`` / ``adapter config set``）。

    ``what`` 仅用于错误信息（默认 ``capture sync``）。缺失 / 非法 / 非对象 JSON →
    ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{what} requires a JSON object")
    try:
        meta = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError(f"invalid meta json: {raw!r}") from None
    if not isinstance(meta, dict):
        raise ValueError(f"meta must be a JSON object: {raw!r}")
    return meta


def get_rtc_params(base_cfg) -> dict:
    """读取 RTC 参数（``base_cfg["policy"]["rtc"]`` 覆盖代码缺省）。"""
    policy = base_cfg.get("policy", {})
    return {**DEFAULT_RTC_CONFIG, **(policy.get("rtc") or {})}


def handle_infer_rtc(base_cfg, cmd) -> CommandResult:
    """处理 RTC 配置命令（``infer rtc`` / ``infer rtc set <json>``）。

    写内存态 ``base_cfg["policy"]["rtc"]``（不写回 yaml），下次 ``session run infer``
    实例化 RTCManager 时生效；会话内由 InferSession 额外应用到**正在运行的** manager
    （下一块起生效）。节点主循环（非任务态）与会话循环（任务态）**共用**本函数，保证
    配置命令「任何状态可用」（与 ``infer config`` 同款）。参数非法 → rejected（400）。
    """
    if cmd.name != CMD_INFER_RTC_SET:
        return ok_result(rtc=get_rtc_params(base_cfg))
    try:
        patch = validate_params(parse_meta(cmd.params.get("json"), what="infer rtc set"))
        merged = {**get_rtc_params(base_cfg), **patch}
        validate_config(merged)  # 交叉约束（P + S < H、E > P）：配置错误在设置时就拦住
    except ValueError as exc:
        return CommandResult(status="rejected", error=str(exc), status_code=400)
    base_cfg.setdefault("policy", {})["rtc"] = merged
    return ok_result(rtc=merged)


def policy_config_status(base_cfg, policy_type=None) -> dict:
    """策略配置项状态：schema 清单 + 当前值 + 缺失必填项（供 CLI ``infer config`` / 前端表单）。

        返回 ``{"policy_type", "items": [schema 项 + value], "values": {...}, "missing": [...],
    "requires_prompt", "requires_model_path", "lerobot", "runtime_keys": [...]}``。

    ``items`` = 公共项（推理端点 ``host`` / ``port``，``group="endpoint"``）+ 策略自身配置项，
    前端按同一张表单渲染；**会话内可改并即时生效的键**由 ``runtime_keys`` 给出（其余键改了要
    退出会话重进才生效）。
    """
    policy_cfg = base_cfg.get("policy", {})
    policy_type = validate_policy_type(policy_type or policy_cfg.get("type", "openpi"))
    items: list[dict] = []
    values: dict = {}
    missing: list[str] = []
    for item in policy_config_items(policy_type):
        value = policy_cfg.get(item["key"], item.get("default"))
        values[item["key"]] = value
        if item.get("required") and (value is None or str(value).strip() == ""):
            missing.append(item["key"])
        items.append({**item, "value": value})
    return {
        "policy_type": policy_type,
        "items": items,
        "values": values,
        "missing": missing,
        "runtime_keys": sorted(policy_config_runtime_keys(policy_type)),
        **policy_features(policy_type),
    }


def _coerce_policy_config_value(key: str, item: dict, raw):
    """校验 / 归一化**单个**策略配置项取值（**只校验不落地**，由调用方统一写入）。

    - 空值（``None`` / 空串）：必填项 → ``ValueError``；非必填 → 返回 ``None``（调用方删键回缺省）；
    - ``int`` 项只接受整数（``bool`` / 带小数的浮点 → ``ValueError``，端口等参数不静默截断），
      ``float`` 项接受数字（如 timeout / max_pose_step），两者都校验
      schema 的 ``min`` / ``max``；
    - ``bool`` / 文本项按声明类型归一化（文本只去空白，不做格式校验：``host`` 可为裸 host 或
      完整 ``ws://host:port``）。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if item.get("required"):
            raise ValueError(f"{key} is required (non-empty)")
        return None
    kind = item.get("type", "text")
    if kind in ("int", "float"):
        if kind == "int" and (isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer())):
            raise ValueError(f"{key} must be an integer, got {raw!r}")
        try:
            value = int(raw) if kind == "int" else float(raw)
        except (TypeError, ValueError):
            expect = "an integer" if kind == "int" else "a number"
            raise ValueError(f"{key} must be {expect}, got {raw!r}") from None
        low, high = item.get("min"), item.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(f"{key} must be within [{low}, {high}], got {value}")
        return value
    if kind == "bool":
        return parse_bool(raw)
    return str(raw).strip()


def set_policy_config(base_cfg, policy_type: str, params: dict) -> dict:
    """按策略 schema 校验并写入内存态 ``base_cfg["policy"]``（不写回 yaml）；返回写入项。

        未知键（不在该策略的配置项清单内）/ 类型不符 / 越界（``min`` / ``max``）/ 必填为空
    → ``ValueError``。**端点 ``host`` / ``port`` 就是普通配置项**：同一白名单、同一校验、
    同一写入路径（没有专用命令）。

    **先全量校验、通过后才写入**：一批里任一项非法 → 整批 400，不留部分写入（调用方不必回滚）。
    **空值（``null`` / 空串）= 清除该项**（删键回到代码缺省，``written`` 记 ``None``）：必填项
    空值 → 400（不允许把必填项清空）。
    """
    allowed = policy_config_keys(policy_type)
    unknown = [key for key in params if key not in allowed]
    if unknown:
        raise ValueError(f"unknown {policy_type} config key(s): {unknown} (allowed: {sorted(allowed)})")
    items = {item["key"]: item for item in policy_config_items(policy_type)}
    resolved = {key: _coerce_policy_config_value(key, items[key], raw) for key, raw in params.items()}
    policy_cfg = base_cfg.setdefault("policy", {})
    written: dict = {}
    for key, value in resolved.items():
        if value is None:  # 空值 = 清除该项（删键回缺省）
            policy_cfg.pop(key, None)
        else:
            policy_cfg[key] = value
        written[key] = value
    return written


def handle_policy_config(base_cfg, cmd, policy_type=None) -> CommandResult:
    """策略配置命令族：``infer config`` / ``infer config set <json>`` / ``infer prompt`` /
    ``infer model`` / ``infer model set <path>``。

    每个策略有自己的独立配置项（prompt / 模型路径 / 设备 / 块长…）+ **公共项**（推理端点
    host / port，`group="endpoint"`、会话级），见 ``policy.POLICY_CONFIG_ITEMS``：
    ``infer config`` 返回清单 + 当前值 + 缺失必填项；``infer config set <json>`` 按当前策略 schema
    校验并写入（可部分）；``infer prompt`` / ``infer model(set)`` 是 ``prompt`` /
    ``pretrained_name_or_path`` 两个内置项的**快捷命令**（同一校验与写入路径）。

    配置写入内存态 ``base_cfg["policy"]``（**不写回 yaml**），下次 ``session run infer`` 生效；
    会话内由 InferSession 额外写入运行中的策略客户端（下一请求生效）。节点主循环（非任务态）
    与会话循环（任务态）**共用**本函数，保证「任何状态可用」（与 ``infer config`` 同款）。
    参数缺失 / 非法键 / 类型不符 → rejected（400，不崩溃）。
    """
    policy_type = validate_policy_type(policy_type or base_cfg.get("policy", {}).get("type", "openpi"))
    if cmd.name == CMD_INFER_CONFIG:  # 查询：配置项清单 + 当前值 + 缺失必填项
        return ok_result(policy_config=policy_config_status(base_cfg, policy_type))
    if cmd.name == CMD_INFER_MODEL:  # 查询模型路径（lerobot 类策略）
        return ok_result(
            policy_type=policy_type,
            pretrained_name_or_path=base_cfg.get("policy", {}).get("pretrained_name_or_path"),
        )
    if cmd.name == CMD_INFER_CONFIG_SET:
        try:
            params = parse_meta(cmd.params.get("json"), what="infer config set")
        except ValueError as exc:
            return CommandResult(status="rejected", error=str(exc), status_code=400)
    elif cmd.name == CMD_INFER_PROMPT:  # 快捷：文本指令（语言条件策略）
        params = {"prompt": cmd.params.get("prompt")}
    elif cmd.name == CMD_INFER_MODEL_SET:  # 快捷：模型路径（lerobot 类策略）
        params = {"pretrained_name_or_path": cmd.params.get("path")}
    else:
        return CommandResult(status="rejected", error=f"unsupported policy config command: {cmd.name}", status_code=400)
    try:
        written = set_policy_config(base_cfg, policy_type, params)
    except ValueError as exc:
        return CommandResult(status="rejected", error=str(exc), status_code=400)
    return ok_result(
        policy_type=policy_type,
        written=written,
        policy_config=policy_config_status(base_cfg, policy_type),
    )


def handle_capture_meta(cmd, store=None) -> CommandResult:
    """处理 ``capture meta`` 命令族（list / add / edit / delete / delete-key）。

    读写 ``config/capture.yml`` 的 ``meta`` 段（``CaptureMetaStore``，可拓展任意分类 →
    选项数组）；配置级命令「任何状态可用」（与 ``infer config`` 一致），节点主循环与会话
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
            raise CommandError(f"command timed out: {cmd.name}", status_code=504) from None

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
