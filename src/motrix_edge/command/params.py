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

"""命令参数解析（CLI 文本与 HTTP 原生值都接受）。

每个解析函数只做**形状与类型**归一，缺失 / 非法一律抛 ``ValueError``（命令处理器回执
``rejected``，不崩溃）；HTTP 侧 ``params`` 是原生 JSON 值（``parse_bool`` 收 ``False`` /
``parse_meta`` 收 dict），CLI 侧是文本 —— 两端共用同一实现。
"""

import json

from motrix_edge.adapter.http_contract import TELEOP_MODES


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

    ``raw`` 为真正的 ``bool`` / 数字（``POST /v1/commands`` 的 ``params`` 是 JSON 值，
    如 ``{"enabled": false}``）时直接取其真值：**``False`` / ``0`` 是合法取值**，
    不能按 falsy 当成缺失（否则「能开不能关」）。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    text = str("" if raw is None else raw).strip().lower()
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
    """解析 JSON 对象参数（``capture sync --meta`` / ``infer rtc set`` / ``infer config set``）。

    ``raw`` 已经是 ``dict`` 时原样返回 —— HTTP 端点直接提交 JSON 对象（``POST /v1/infers/rtc``
    的 body、``POST /v1/commands`` 的 ``params``），与 ``parse_bool`` 同理：**同一条命令在
    CLI（文本）与 HTTP（原生值）下都可用**，路由层不必再做 ``json.dumps``。为字符串时按
    JSON 对象解析。``what`` 仅用于错误信息（默认 ``capture sync``）。
    缺失 / 非法 / 非对象 JSON → ``ValueError``（命令处理器回执 rejected，不崩溃）。
    """
    if isinstance(raw, dict):
        return raw
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
