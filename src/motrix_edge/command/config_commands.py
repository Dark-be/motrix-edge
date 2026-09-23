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

"""配置级命令的实现（无状态纯函数，node 主循环与会话循环共用）。

``infer rtc(set)`` / ``infer config(set)`` / ``infer prompt`` / ``infer model(set)`` /
``capture meta *`` 都是**配置级**命令：任何状态可用、写内存态配置（不写回 yaml）、
与状态机解耦。参数非法 → 回执 ``rejected``（400）。
"""

from motrix_edge.errors import ErrorCode
from motrix_edge.policy import (
    policy_config_items,
    policy_config_keys,
    policy_config_runtime_keys,
    policy_features,
    validate_policy_type,
)
from motrix_edge.rtc import DEFAULT_RTC_CONFIG, validate_config, validate_params

from .core import CommandResult, ok_result
from .naming import (
    CMD_CAPTURE_META_ADD,
    CMD_CAPTURE_META_DELETE,
    CMD_CAPTURE_META_DELETE_KEY,
    CMD_CAPTURE_META_EDIT,
    CMD_CAPTURE_META_LIST,
    CMD_INFER_CONFIG,
    CMD_INFER_CONFIG_SET,
    CMD_INFER_MODEL,
    CMD_INFER_MODEL_SET,
    CMD_INFER_PROMPT,
    CMD_INFER_RTC_SET,
)
from .params import parse_bool, parse_meta


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
        return CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT)
    base_cfg.setdefault("policy", {})["rtc"] = merged
    return ok_result(rtc=merged)


def policy_config_status(base_cfg, policy_type=None) -> dict:
    """策略配置项状态：schema 清单 + 当前值 + 缺失必填项（供 CLI ``infer config`` / 前端表单）。

        返回 ``{"policy_type", "items": [schema 项 + value], "values": {...}, "missing": [...],
    "requires_prompt", "requires_model_path", "lerobot", "runtime_keys": [...]}``。

    ``items`` = 端点项（``host`` / ``port``，``group="endpoint"``）+ 公共项（``warmup_required``）+
    策略自身配置项，前端按同一张表单渲染；**会话内可改
    并即时生效的键**由 ``runtime_keys`` 给出（其余键改了要退出会话重进才生效）。
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
    同一写入路径（旧端点专用命令已移除），不另设一套锁定规则。

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

    每个策略有自己的独立配置项（prompt / 模型路径 / 设备 / 块长…）+ **端点项**（推理端点
    host / port，`group="endpoint"`、会话级；**仅需端点的策略**有：openpi / lerobot-act），见
    ``policy.POLICY_CONFIG_ITEMS``：
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
            return CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT)
    elif cmd.name == CMD_INFER_PROMPT:  # 快捷：文本指令（语言条件策略）
        params = {"prompt": cmd.params.get("prompt")}
    elif cmd.name == CMD_INFER_MODEL_SET:  # 快捷：模型路径（lerobot 类策略）
        params = {"pretrained_name_or_path": cmd.params.get("path")}
    else:
        return CommandResult(
            status="rejected", error=f"unsupported policy config command: {cmd.name}", code=ErrorCode.INVALID_ARGUMENT
        )
    try:
        written = set_policy_config(base_cfg, policy_type, params)
    except ValueError as exc:
        return CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT)
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
    from motrix_edge.utils.capture_meta import CaptureMetaError, CaptureMetaStore

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
    except (ValueError, CaptureMetaError) as exc:
        return CommandResult(status="rejected", error=str(exc), code=ErrorCode.INVALID_ARGUMENT)
    return CommandResult(
        status="rejected", error=f"unknown capture meta command: {cmd.name}", code=ErrorCode.INVALID_ARGUMENT
    )
