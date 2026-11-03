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

"""Node Console 与 Edge HMAC 身份契约测试。"""

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from motrix_edge.lease import BEIJING_TZ, Lease, LeaseManager, LeaseState
from motrix_edge.server import create_app
from motrix_edge.server.proxy_auth import ProxyAuthError, ProxyIdentityVerifier, signed_identity_headers


def test_node_console_server_contract():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        ["node", "--test", "console/server/index.test.mjs"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_signature_rejects_tamper_expiry_and_replay():
    now = 1000
    verifier = ProxyIdentityVerifier("shared", LeaseManager(), max_clock_skew_s=30, clock=lambda: now)
    headers = signed_identity_headers(
        "shared",
        method="POST",
        target="/v1/captures/sync",
        body=b"{}",
        subject="feishu:t:ou",
        display_name="张三",
        timestamp=now,
        nonce="one",
    )
    identity = verifier.verify(method="POST", target="/v1/captures/sync", body=b"{}", headers=headers)
    assert identity.display_name == "张三"
    assert identity.claims["provider"] == "feishu"
    with pytest.raises(ProxyAuthError, match="replayed"):
        verifier.verify(method="POST", target="/v1/captures/sync", body=b"{}", headers=headers)

    tampered = signed_identity_headers(
        "shared",
        method="POST",
        target="/v1/captures/sync",
        body=b"{}",
        subject="feishu:t:ou",
        display_name="张三",
        timestamp=now,
        nonce="two",
    )
    with pytest.raises(ProxyAuthError, match="invalid Console identity signature"):
        verifier.verify(method="POST", target="/v1/captures/sync", body=b'{"changed":true}', headers=tampered)

    expired = signed_identity_headers(
        "shared",
        method="POST",
        target="/v1/captures/sync",
        body=b"{}",
        subject="feishu:t:ou",
        display_name="张三",
        timestamp=now - 31,
    )
    with pytest.raises(ProxyAuthError, match="expired"):
        verifier.verify(method="POST", target="/v1/captures/sync", body=b"{}", headers=expired)


def test_python_signature_matches_node_contract():
    headers = signed_identity_headers(
        "secret",
        method="POST",
        target="/v1/captures/sync",
        body=b"{}",
        subject="feishu:t:ou",
        display_name="张三",
        identity={"provider": "feishu", "tenant_key": "t", "open_id": "ou"},
        timestamp=1000,
        nonce="nonce",
    )
    assert headers["X-Motrix-Signature"] == "15e2e0647af95ad1176170bbc5f2af069fe1af3d0dcfc253927c0729d2646df8"


class _Commands:
    def __init__(self):
        self.calls = []

    def submit(self, name, params=None, *, lease_id=None, **kwargs):
        self.calls.append(SimpleNamespace(name=name, params=params, lease_id=lease_id))
        return {"meta": params["meta"]}


def test_edge_capture_sync_requires_signature_and_binds_lease_owner():
    leases = LeaseManager()
    subject = "feishu:tenant-1:ou_console"
    leases.install(
        Lease(
            lease_id="lease-1",
            edge_id="edge-a",
            holder_subject_id=subject,
            purpose="capture",
            state=LeaseState.ACTIVE,
            expires_at=datetime.now(BEIJING_TZ) + timedelta(minutes=5),
        )
    )
    commands = _Commands()
    verifier = ProxyIdentityVerifier("edge-secret", leases)
    client = TestClient(create_app({}, commands=commands, lease_manager=leases, proxy_auth=verifier))
    body = json.dumps({"meta": {"operator": "伪造", "task_name": "桌面前移"}}, separators=(",", ":")).encode()
    headers = signed_identity_headers(
        "edge-secret",
        method="POST",
        target="/v1/captures/sync",
        body=body,
        subject=subject,
        display_name="张三",
        identity={"tenant_key": "tenant-1", "open_id": "ou_console"},
    )
    headers.update({"Content-Type": "application/json", "X-Lease-Id": "lease-1"})

    assert client.post("/v1/captures/sync", content=body, headers={"X-Lease-Id": "lease-1"}).status_code == 401
    response = client.post("/v1/captures/sync", content=body, headers=headers)

    assert response.status_code == 200
    meta = commands.calls[-1].params["meta"]
    assert meta["operator"] == "张三"
    assert meta["operator_identity"]["subject"] == subject
    assert meta["operator_identity"]["open_id"] == "ou_console"
    assert meta["task_name"] == "桌面前移"
