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

"""飞书 OAuth 与 Edge 自有登录会话。

OAuth token 只活到回调处理结束；浏览器与采集 JSON 均只接触规范化用户身份。服务默认使用
进程内存储，符合 Edge 当前单进程 uvicorn 装配；多 worker 部署需换成共享 session store。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

import httpx2

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.lease import BEIJING_TZ, LeaseManager

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


class AuthError(ServiceError):
    """登录、会话或可信身份绑定失败。"""

    default_code = ErrorCode.UNAUTHENTICATED


@dataclass(frozen=True)
class FeishuUser:
    """从飞书响应归一出的最小采集员身份。"""

    open_id: str
    tenant_key: str
    display_name: str
    union_id: str | None = None
    user_id: str | None = None

    @property
    def subject(self) -> str:
        return f"feishu:{self.tenant_key}:{self.open_id}"

    def public_dict(self) -> dict:
        result = {
            "provider": "feishu",
            "subject": self.subject,
            "tenant_key": self.tenant_key,
            "open_id": self.open_id,
            "display_name": self.display_name,
        }
        if self.union_id:
            result["union_id"] = self.union_id
        if self.user_id:
            result["user_id"] = self.user_id
        return result


@dataclass(frozen=True)
class AuthConfig:
    app_id: str
    app_secret: str
    redirect_uri: str
    frontend_url: str
    allowed_tenants: frozenset[str]
    allowed_origins: tuple[str, ...] = ()
    session_ttl: float = 8 * 60 * 60
    state_ttl: float = 10 * 60
    cookie_name: str = "motrix_edge_session"
    cookie_secure: bool = False
    scopes: tuple[str, ...] = ()
    authorize_url: str = AUTHORIZE_URL
    token_url: str = TOKEN_URL
    user_info_url: str = USER_INFO_URL


@dataclass
class _LoginState:
    return_to: str
    expires_at: float


@dataclass
class _Session:
    user: FeishuUser
    authenticated_at: datetime
    expires_at: float


class FeishuOAuthClient:
    """飞书 HTTP 协议适配器；业务测试可注入假 provider 而不访问网络。"""

    def __init__(self, config: AuthConfig):
        self._config = config

    def authorization_url(self, state: str) -> str:
        params = {
            "app_id": self._config.app_id,
            "redirect_uri": self._config.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self._config.scopes:
            params["scope"] = " ".join(self._config.scopes)
        return f"{self._config.authorize_url}?{urlencode(params)}"

    def exchange_user(self, code: str) -> FeishuUser:
        try:
            with httpx2.Client(timeout=10.0) as client:
                token_response = client.post(
                    self._config.token_url,
                    json={
                        "grant_type": "authorization_code",
                        "client_id": self._config.app_id,
                        "client_secret": self._config.app_secret,
                        "code": code,
                        "redirect_uri": self._config.redirect_uri,
                    },
                )
                token_response.raise_for_status()
                token_data = self._unwrap(token_response.json(), "token")
                access_token = token_data.get("access_token")
                if not isinstance(access_token, str) or not access_token:
                    raise AuthError("Feishu token response missing access_token", code=ErrorCode.UPSTREAM_ERROR)
                user_response = client.get(
                    self._config.user_info_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user_data = self._unwrap(user_response.json(), "user info")
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError("Feishu OAuth request failed", code=ErrorCode.UPSTREAM_ERROR) from exc
        return self._parse_user(user_data)

    @staticmethod
    def _unwrap(payload, what: str) -> dict:
        if not isinstance(payload, dict):
            raise AuthError(f"invalid Feishu {what} response", code=ErrorCode.UPSTREAM_ERROR)
        code = payload.get("code")
        if code not in (None, 0):
            message = payload.get("msg") or payload.get("message") or "unknown error"
            raise AuthError(f"Feishu {what} rejected: {message}", code=ErrorCode.UPSTREAM_ERROR)
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise AuthError(f"invalid Feishu {what} data", code=ErrorCode.UPSTREAM_ERROR)
        return data

    @staticmethod
    def _parse_user(data: dict) -> FeishuUser:
        open_id = data.get("open_id")
        tenant_key = data.get("tenant_key")
        name = data.get("name") or data.get("display_name")
        if not all(isinstance(value, str) and value.strip() for value in (open_id, tenant_key, name)):
            raise AuthError("Feishu user info missing open_id, tenant_key or name", code=ErrorCode.UPSTREAM_ERROR)
        return FeishuUser(
            open_id=open_id.strip(),
            tenant_key=tenant_key.strip(),
            display_name=name.strip(),
            union_id=_optional_string(data.get("union_id")),
            user_id=_optional_string(data.get("user_id")),
        )


class AuthService:
    """一次性 OAuth state、Edge 登录会话及采集身份绑定。"""

    def __init__(self, config: AuthConfig, leases: LeaseManager, provider=None, clock=None):
        self.config = config
        self._leases = leases
        self._provider = provider or FeishuOAuthClient(config)
        self._clock = clock or time.monotonic
        self._states: dict[str, _LoginState] = {}
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()

    def begin_login(self, return_to: str | None = None) -> str:
        target = _safe_return_to(return_to)
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._states[state] = _LoginState(target, self._clock() + self.config.state_ttl)
        return self._provider.authorization_url(state)

    def complete_login(self, code: str | None, state: str | None) -> tuple[str, str]:
        if not code or not state:
            raise AuthError("Feishu callback requires code and state", code=ErrorCode.INVALID_ARGUMENT)
        with self._lock:
            self._prune()
            transaction = self._states.pop(state, None)
        if transaction is None:
            raise AuthError("invalid or expired OAuth state", code=ErrorCode.FORBIDDEN)
        user = self._provider.exchange_user(code)
        if self.config.allowed_tenants and user.tenant_key not in self.config.allowed_tenants:
            raise AuthError("Feishu tenant is not allowed", code=ErrorCode.FORBIDDEN)
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = _Session(
                user=user,
                authenticated_at=datetime.now(BEIJING_TZ),
                expires_at=self._clock() + self.config.session_ttl,
            )
        return session_id, f"{self.config.frontend_url.rstrip('/')}{transaction.return_to}"

    def me(self, session_id: str | None) -> dict:
        session = self._require_session(session_id)
        return {
            "authenticated": True,
            "user": session.user.public_dict(),
            "authenticated_at": session.authenticated_at.isoformat(),
        }

    def logout(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def bind_capture_meta(self, meta: dict, lease_id: str | None, session_id: str | None) -> dict:
        session = self._require_session(session_id)
        lease = self._leases.require(lease_id)
        if lease.holder_subject_id != session.user.subject:
            raise AuthError("authenticated user does not own the active lease", code=ErrorCode.FORBIDDEN)
        trusted = dict(meta)
        for key in ("operator", "operator_identity", "capture_context", "meta_schema_version"):
            trusted.pop(key, None)
        trusted.update(
            {
                "meta_schema_version": 2,
                "operator": session.user.display_name,
                "operator_identity": session.user.public_dict(),
                "capture_context": {
                    "source": "web_console",
                    "lease_id": lease.lease_id,
                    "authenticated_at": session.authenticated_at.isoformat(),
                    "synced_at": datetime.now(BEIJING_TZ).isoformat(),
                },
            }
        )
        return trusted

    def _require_session(self, session_id: str | None) -> _Session:
        if not session_id:
            raise AuthError("Feishu login required")
        with self._lock:
            self._prune()
            session = self._sessions.get(session_id)
        if session is None:
            raise AuthError("login session missing or expired")
        return session

    def _prune(self) -> None:
        now = self._clock()
        self._states = {key: value for key, value in self._states.items() if value.expires_at > now}
        self._sessions = {key: value for key, value in self._sessions.items() if value.expires_at > now}


def build_auth_service(base_cfg: dict, leases: LeaseManager) -> AuthService | None:
    """从 `auth.feishu` 构建可选认证服务；关闭时不改变现有 capture API。"""
    raw = (base_cfg.get("auth") or {}).get("feishu") or {}
    if not raw.get("enabled", False):
        return None
    app_id = raw.get("app_id")
    app_id_env = raw.get("app_id_env")
    if not app_id and app_id_env:
        app_id = os.environ.get(str(app_id_env))
    secret = raw.get("app_secret")
    secret_env = raw.get("app_secret_env")
    if not secret and secret_env:
        secret = os.environ.get(str(secret_env))
    required = {
        "app_id": app_id,
        "app_secret": secret,
        "redirect_uri": raw.get("redirect_uri"),
        "frontend_url": raw.get("frontend_url"),
    }
    missing = [key for key, value in required.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError(f"auth.feishu missing required configuration: {', '.join(missing)}")
    config = AuthConfig(
        app_id=required["app_id"].strip(),
        app_secret=required["app_secret"].strip(),
        redirect_uri=required["redirect_uri"].strip(),
        frontend_url=str(raw.get("frontend_url") or "").rstrip("/"),
        allowed_tenants=frozenset(str(value).strip() for value in raw.get("allowed_tenants", []) if str(value).strip()),
        allowed_origins=tuple(str(value).rstrip("/") for value in raw.get("allowed_origins", []) if str(value)),
        session_ttl=float(raw.get("session_ttl", 8 * 60 * 60)),
        state_ttl=float(raw.get("state_ttl", 10 * 60)),
        cookie_name=str(raw.get("cookie_name") or "motrix_edge_session"),
        cookie_secure=bool(raw.get("cookie_secure", False)),
        scopes=tuple(str(value) for value in raw.get("scopes", []) if str(value)),
        authorize_url=str(raw.get("authorize_url") or AUTHORIZE_URL),
        token_url=str(raw.get("token_url") or TOKEN_URL),
        user_info_url=str(raw.get("user_info_url") or USER_INFO_URL),
    )
    if config.session_ttl <= 0 or config.state_ttl <= 0:
        raise ValueError("auth.feishu session_ttl and state_ttl must be positive")
    if not config.allowed_origins or "*" in config.allowed_origins:
        raise ValueError("auth.feishu.allowed_origins must list explicit origins when authentication is enabled")
    return AuthService(config, leases)


def _safe_return_to(value: str | None) -> str:
    if not value:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        raise AuthError("return_to must be an absolute site path", code=ErrorCode.INVALID_ARGUMENT)
    return value


def _optional_string(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = ["AuthConfig", "AuthError", "AuthService", "FeishuOAuthClient", "FeishuUser", "build_auth_service"]
