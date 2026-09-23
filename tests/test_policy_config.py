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

"""策略配置项 schema 测试（端点项优先 / 白名单校验 / 长文本项）。

覆盖：`policy_config_items` 的组成与顺序、`policy_config_status` 的值回显与缺失必填项、
`set_policy_config` 的白名单与类型校验。
"""

import pytest

from motrix_edge import policy as policy_pkg
from motrix_edge.command import policy_config_status, set_policy_config
from motrix_edge.policy import (
    policy_config_items,
    policy_config_runtime_keys,
)

_TYPE = "test-schema"  # 临时策略类型（仅测试注入）
_ITEMS = [
    {"key": "model", "label": "模型名 model", "type": "text", "required": False, "runtime": True},
    {"key": "device", "label": "设备 device", "type": "text", "required": True, "runtime": False},
]


@pytest.fixture
def schema_policy(monkeypatch):
    """注入一个临时策略（清单 + 注册表都要有，才能过类型校验）。"""
    monkeypatch.setitem(policy_pkg.POLICY_CONFIG_ITEMS, _TYPE, [dict(item) for item in _ITEMS])
    monkeypatch.setitem(policy_pkg.POLICY_REGISTRY, _TYPE, ("motrix_edge.policy.openpi.client", "OpenPIClient"))
    return _TYPE


def _cfg(**policy):
    return {"policy": {"type": _TYPE, **policy}}


def test_items_are_endpoint_first_then_common_then_policy(schema_policy):
    """清单顺序 = 端点项（host / port）+ 公共项（warmup_required）+ 策略自身项。"""
    keys = [item["key"] for item in policy_config_items(schema_policy)]
    assert keys == ["host", "port", "warmup_required", "model", "device"]


def test_endpoint_items_lead_every_policy_schema():
    """端点项（host / port）排在**每个**注册策略清单的最前两项（策略都连 TCP 端点）。"""
    for policy_type in policy_pkg.POLICY_REGISTRY:
        assert [item["key"] for item in policy_config_items(policy_type)][:2] == ["host", "port"]


def test_runtime_keys_only_include_hot_reloadable(schema_policy):
    """``runtime=True`` 的键可在会话内热改；``False`` 的（设备等）要退出重进。"""
    assert policy_config_runtime_keys(schema_policy) == {"model"}


def test_prompt_item_is_multiline():
    """长文本项（openpi 的 prompt）标 ``multiline``：前端渲染多行 textarea。"""
    openpi = {item["key"]: item for item in policy_config_items("openpi")}
    assert openpi["prompt"].get("multiline") is True


def test_status_reports_values_and_missing_required(schema_policy):
    """状态回显：值原样给出；缺失的必填项进 ``missing``（前端据此提示）。"""
    status = policy_config_status(_cfg(model="m"), policy_type=schema_policy)
    assert status["values"]["model"] == "m"
    assert status["values"]["device"] is None
    assert status["missing"] == ["device"]


def test_unknown_config_key_is_rejected():
    """白名单：不在该策略清单内的键 → ``ValueError``（不会静默写入无用配置）。"""
    with pytest.raises(ValueError, match="unknown openpi config key"):
        set_policy_config({"policy": {}}, "openpi", {"bogus": 1})


def test_empty_value_clears_key_but_required_rejects():
    """空值 = 清除该项；必填项清空 → 拒绝（不允许把必填项置空）。"""
    cfg = {"policy": {"image_size": 128}}
    assert set_policy_config(cfg, "openpi", {"image_size": None}) == {"image_size": None}
    assert "image_size" not in cfg["policy"]
    with pytest.raises(ValueError, match="prompt is required"):
        set_policy_config(cfg, "openpi", {"prompt": ""})
