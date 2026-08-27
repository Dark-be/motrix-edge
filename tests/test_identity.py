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

"""identity 子包单元测试 —— 身份配置加载与预留发送接口，无硬件可跑。"""

from motrix_edge.identity import (
    Identity,
    load_identity,
    new_correlation_id,
    new_idempotency_key,
)


def test_load_identity_from_config():
    base_cfg = {"identity": {"edge_id": "edge-1", "edge_name": "edge-one", "edge_version": "0.2.0"}}
    idn = load_identity(base_cfg)
    assert idn.edge_id == "edge-1"
    assert idn.edge_name == "edge-one"
    assert idn.edge_version == "0.2.0"


def test_load_identity_defaults():
    idn = load_identity({})
    assert idn.edge_id == "edge-unknown"
    assert idn.edge_name == "edge-unknown-name"
    assert idn.edge_version == "0.0.0"


def test_identity_headers_preallocated_send_interface():
    idn = Identity(edge_id="edge-x", edge_name="edge-x-name", edge_version="1.0.0")
    headers = idn.headers()  # 预留发送接口：HTTP 层用它上报身份
    assert headers["X-Edge-Id"] == "edge-x"
    assert headers["X-Edge-Name"] == "edge-x-name"
    assert headers["X-Edge-Version"] == "1.0.0"
    assert set(headers) == {"X-Edge-Id", "X-Edge-Name", "X-Edge-Version"}


def test_correlation_id_unique():
    assert new_correlation_id().startswith("corr_")
    assert new_correlation_id() != new_correlation_id()


def test_idempotency_key_unique():
    assert new_idempotency_key().startswith("idem_")
    assert new_idempotency_key() != new_idempotency_key()
