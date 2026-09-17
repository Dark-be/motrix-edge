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

"""策略配置项 schema / 敏感项脱敏测试（密钥只存内存态、不回显）。

覆盖：`policy_secret_keys` / `mask_policy_secrets`、`policy_config_status` 对 secret 项的
脱敏与 `configured` 标记、命令回执 `written` 的脱敏（HTTP / 日志不出现原值）。
"""

import json

from motrix_edge.policy import (
    mask_policy_secrets,
    policy_config_items,
    policy_secret_keys,
)
from motrix_edge.utils.commands import CommandResult, mask_command_secrets, policy_config_status

_SECRET = "sk-super-secret-value"


def _cfg(**policy):
    return {"policy": {"type": "llm", **policy}}


def test_llm_declares_api_key_as_secret():
    assert policy_secret_keys("llm") == {"api_key"}
    assert policy_secret_keys("act") == set()  # 其它策略没有敏感项


def test_llm_schema_has_api_key_but_no_env_item():
    """密钥只做表单项：``api_key_env`` 不在 schema（面板 / infer config 不再出现），

    它只作为 ``edge.yml`` 的兜底配置被客户端直读（默认 ``OPENAI_API_KEY``）。
    """
    keys = {item["key"] for item in policy_config_items("llm")}
    assert "api_key" in keys
    assert "api_key_env" not in keys


def test_mask_policy_secrets_replaces_only_non_empty():
    masked = mask_policy_secrets("llm", {"api_key": _SECRET, "model": "gpt-4o"})
    assert masked["api_key"] == "***"
    assert masked["model"] == "gpt-4o"  # 非敏感项原样
    assert mask_policy_secrets("llm", {"api_key": ""})["api_key"] == ""  # 空值不假装已设置


def test_policy_config_status_hides_secret_value_but_reports_configured():
    status = policy_config_status(_cfg(api_key=_SECRET, model="m"), policy_type="llm")
    item = next(item for item in status["items"] if item["key"] == "api_key")
    assert item["value"] == ""
    assert item["configured"] is True
    assert item["secret"] is True
    assert status["values"]["api_key"] == ""
    assert _SECRET not in json.dumps(status)  # 状态回显里绝不出现密钥原值


def test_policy_config_status_marks_secret_unset():
    status = policy_config_status(_cfg(model="m"), policy_type="llm")
    item = next(item for item in status["items"] if item["key"] == "api_key")
    assert item["configured"] is False


def test_mask_command_secrets_redacts_written_only():
    result = CommandResult(status="ok", data={"policy_type": "llm", "written": {"api_key": _SECRET, "model": "m"}})
    masked = mask_command_secrets(result, "llm")
    assert masked.data["written"]["api_key"] == "***"
    assert masked.data["written"]["model"] == "m"  # 非敏感项原样
    assert _SECRET not in json.dumps(masked.data)
    assert result.data["written"]["api_key"] == _SECRET  # 原回执不被改动（会话据此写内存态）


def test_mask_command_secrets_passthrough_without_written():
    result = CommandResult(status="rejected", error="bad", status_code=400)
    assert mask_command_secrets(result, "llm") is result
