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

"""飞书登录与可信采集员元信息测试（provider 全部为本地桩，不访问网络）。"""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi.testclient import TestClient

from motrix_edge.lease import BEIJING_TZ, Lease, LeaseManager, LeaseState
from motrix_edge.server import create_app
from motrix_edge.server.auth import AuthConfig, AuthService, FeishuUser, build_auth_service


class _Provider:
    def __init__(self, user: FeishuUser):
        self.user = user
        self.codes = []

    def authorization_url(self, state: str) -> str:
        return f"https://accounts.example/authorize?{urlencode({'state': state})}"

    def exchange_user(self, code: str) -> FeishuUser:
        self.codes.append(code)
        return self.user


class _Commands:
    def __init__(self):
        self.calls = []

    def submit(self, name, params=None, *, lease_id=None, **kwargs):
        self.calls.append({"name": name, "params": params, "lease_id": lease_id})
        return {"meta": params["meta"]}


def _auth(leases: LeaseManager, *, allowed_tenants=frozenset({"tenant-1"})) -> AuthService:
    config = AuthConfig(
        app_id="cli_test",
        app_secret="secret",
        redirect_uri="http://edge.test/v1/auth/feishu/callback",
        frontend_url="http://console.test",
        allowed_tenants=allowed_tenants,
    )
    user = FeishuUser(
        open_id="ou_operator",
        tenant_key="tenant-1",
        display_name="张三",
        union_id="on_operator",
        user_id="u_operator",
    )
    return AuthService(config, leases, provider=_Provider(user))


def _login(client: TestClient) -> dict:
    response = client.get("/v1/auth/feishu/login", params={"return_to": "/capture"}, follow_redirects=False)
    assert response.status_code == 307
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    callback = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "oauth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "http://console.test/capture"
    assert "HttpOnly" in callback.headers["set-cookie"]
    return client.get("/v1/auth/me").json()


def _install_lease(leases: LeaseManager, holder: str) -> None:
    leases.install(
        Lease(
            lease_id="lease-1",
            edge_id="edge-1",
            holder_subject_id=holder,
            purpose="capture",
            state=LeaseState.ACTIVE,
            expires_at=datetime.now(BEIJING_TZ) + timedelta(minutes=5),
        )
    )


def test_login_callback_creates_private_session_and_current_user():
    leases = LeaseManager()
    auth = _auth(leases)
    client = TestClient(create_app({}, lease_manager=leases, auth=auth))

    me = _login(client)

    assert me["authenticated"] is True
    assert me["user"] == {
        "provider": "feishu",
        "subject": "feishu:tenant-1:ou_operator",
        "tenant_key": "tenant-1",
        "open_id": "ou_operator",
        "display_name": "张三",
        "union_id": "on_operator",
        "user_id": "u_operator",
    }
    assert auth._provider.codes == ["oauth-code"]


def test_oauth_state_is_single_use_and_return_to_rejects_external_url():
    leases = LeaseManager()
    auth = _auth(leases)
    client = TestClient(create_app({}, lease_manager=leases, auth=auth))
    login = client.get("/v1/auth/feishu/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]

    assert (
        client.get(
            "/v1/auth/feishu/callback", params={"code": "one", "state": state}, follow_redirects=False
        ).status_code
        == 303
    )
    replay = client.get("/v1/auth/feishu/callback", params={"code": "two", "state": state}, follow_redirects=False)
    assert replay.status_code == 403
    assert (
        client.get(
            "/v1/auth/feishu/login", params={"return_to": "https://evil.example"}, follow_redirects=False
        ).status_code
        == 400
    )


def test_capture_sync_injects_trusted_user_and_overwrites_spoofed_fields():
    leases = LeaseManager()
    _install_lease(leases, "feishu:tenant-1:ou_operator")
    auth = _auth(leases)
    commands = _Commands()
    client = TestClient(create_app({}, commands=commands, lease_manager=leases, auth=auth))
    _login(client)

    response = client.post(
        "/v1/captures/sync",
        headers={"X-Lease-Id": "lease-1"},
        json={
            "meta": {
                "operator": "伪造用户",
                "operator_identity": {"subject": "fake"},
                "capture_context": {"source": "fake"},
                "task_name": "桌面前移",
            }
        },
    )

    assert response.status_code == 200
    synced = commands.calls[-1]["params"]["meta"]
    assert synced["operator"] == "张三"
    assert synced["operator_identity"]["subject"] == "feishu:tenant-1:ou_operator"
    assert synced["capture_context"]["lease_id"] == "lease-1"
    assert synced["task_name"] == "桌面前移"
    assert synced["meta_schema_version"] == 2


def test_capture_sync_requires_login_and_matching_lease_subject():
    leases = LeaseManager()
    _install_lease(leases, "feishu:tenant-1:someone-else")
    auth = _auth(leases)
    commands = _Commands()
    client = TestClient(create_app({}, commands=commands, lease_manager=leases, auth=auth))

    missing = client.post("/v1/captures/sync", headers={"X-Lease-Id": "lease-1"}, json={"meta": {"task_name": "task"}})
    assert missing.status_code == 401
    _login(client)
    mismatch = client.post("/v1/captures/sync", headers={"X-Lease-Id": "lease-1"}, json={"meta": {"task_name": "task"}})
    assert mismatch.status_code == 403
    assert commands.calls == []


def test_auth_disabled_preserves_existing_capture_sync_contract():
    assert TestClient(create_app({})).get("/v1/auth").json() == {"enabled": False, "provider": None}


def test_auth_builder_reads_app_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("FEISHU_TEST_APP_ID", "cli_from_env")
    monkeypatch.setenv("FEISHU_TEST_SECRET", "secret-from-env")
    service = build_auth_service(
        {
            "auth": {
                "feishu": {
                    "enabled": True,
                    "app_id_env": "FEISHU_TEST_APP_ID",
                    "app_secret_env": "FEISHU_TEST_SECRET",
                    "redirect_uri": "http://edge.test/v1/auth/feishu/callback",
                    "frontend_url": "http://console.test",
                    "allowed_origins": ["http://console.test"],
                }
            }
        },
        LeaseManager(),
    )

    assert service is not None
    assert service.config.app_id == "cli_from_env"
    assert service.config.app_secret == "secret-from-env"
