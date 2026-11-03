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

"""Console → Edge 的可信身份签名与重放保护。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass

from motrix_edge.errors import ErrorCode, ServiceError
from motrix_edge.lease import BEIJING_TZ, LeaseManager

HEADER_SUBJECT = "X-Motrix-Subject"
HEADER_NAME = "X-Motrix-Name"
HEADER_TIMESTAMP = "X-Motrix-Timestamp"
HEADER_NONCE = "X-Motrix-Nonce"
HEADER_SIGNATURE = "X-Motrix-Signature"
HEADER_IDENTITY = "X-Motrix-Identity"
SIGNATURE_VERSION = "motrix-console-v1"


class ProxyAuthError(ServiceError):
    default_code = ErrorCode.UNAUTHENTICATED


@dataclass(frozen=True)
class ProxyIdentity:
    subject: str
    display_name: str
    claims: dict

    def operator_identity(self) -> dict:
        return dict(self.claims)


def signed_identity_headers(
    secret: str,
    *,
    method: str,
    target: str,
    body: bytes,
    subject: str,
    display_name: str,
    identity: dict | None = None,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """生成 Console 代理请求头；target = path + 可选 query。"""
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    nonce = nonce or secrets.token_urlsafe(18)
    _validate_text(subject, "subject")
    _validate_text(display_name, "display_name")
    claims = dict(identity or {})
    claims.update({"provider": "feishu", "subject": subject, "display_name": display_name})
    encoded_identity = _encode_header(json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    canonical = _canonical(method, target, body, subject, display_name, encoded_identity, timestamp, nonce)
    signature = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
    return {
        HEADER_SUBJECT: subject,
        HEADER_NAME: _encode_header(display_name),
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: signature,
        HEADER_IDENTITY: encoded_identity,
    }


class ProxyIdentityVerifier:
    """Edge 侧 HMAC、时间窗和 nonce 单次使用校验，并绑定租约主体。"""

    def __init__(self, secret: str, leases: LeaseManager, max_clock_skew_s: float = 30.0, clock=None):
        if not secret:
            raise ValueError("console proxy shared secret must be non-empty")
        if max_clock_skew_s <= 0:
            raise ValueError("console proxy max_clock_skew_s must be positive")
        self._secret = secret
        self._leases = leases
        self._max_clock_skew_s = float(max_clock_skew_s)
        self._clock = clock or time.time
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, *, method: str, target: str, body: bytes, headers) -> ProxyIdentity:
        subject = headers.get(HEADER_SUBJECT)
        encoded_name = headers.get(HEADER_NAME)
        raw_timestamp = headers.get(HEADER_TIMESTAMP)
        nonce = headers.get(HEADER_NONCE)
        signature = headers.get(HEADER_SIGNATURE)
        encoded_identity = headers.get(HEADER_IDENTITY)
        if not all(
            isinstance(value, str) and value
            for value in (subject, encoded_name, raw_timestamp, nonce, signature, encoded_identity)
        ):
            raise ProxyAuthError("signed Console identity required")
        name = _decode_header(encoded_name)
        _validate_text(subject, "subject")
        _validate_text(name, "display_name")
        try:
            timestamp = int(raw_timestamp)
        except ValueError as exc:
            raise ProxyAuthError("invalid Console identity timestamp", code=ErrorCode.FORBIDDEN) from exc
        now = float(self._clock())
        if abs(now - timestamp) > self._max_clock_skew_s:
            raise ProxyAuthError("Console identity signature expired", code=ErrorCode.FORBIDDEN)
        canonical = _canonical(method, target, body, subject, name, encoded_identity, timestamp, nonce)
        expected = hmac.new(self._secret.encode(), canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ProxyAuthError("invalid Console identity signature", code=ErrorCode.FORBIDDEN)
        with self._lock:
            self._prune_nonces(now)
            if nonce in self._nonces:
                raise ProxyAuthError("Console identity signature replayed", code=ErrorCode.FORBIDDEN)
            self._nonces[nonce] = now + self._max_clock_skew_s
        try:
            claims = json.loads(_decode_header(encoded_identity))
        except json.JSONDecodeError as exc:
            raise ProxyAuthError("invalid Console identity claims", code=ErrorCode.FORBIDDEN) from exc
        if not isinstance(claims, dict) or claims.get("subject") != subject or claims.get("display_name") != name:
            raise ProxyAuthError("Console identity claims mismatch", code=ErrorCode.FORBIDDEN)
        return ProxyIdentity(subject=subject, display_name=name, claims=claims)

    def bind_capture_meta(self, meta: dict, lease_id: str | None, identity: ProxyIdentity) -> dict:
        lease = self._leases.require(lease_id)
        if lease.holder_subject_id != identity.subject:
            raise ProxyAuthError("signed Console user does not own the active lease", code=ErrorCode.FORBIDDEN)
        trusted = dict(meta)
        for key in ("operator", "operator_identity", "capture_context", "meta_schema_version"):
            trusted.pop(key, None)
        from datetime import datetime

        trusted.update(
            {
                "meta_schema_version": 2,
                "operator": identity.display_name,
                "operator_identity": identity.operator_identity(),
                "capture_context": {
                    "source": "motrix_console",
                    "lease_id": lease.lease_id,
                    "synced_at": datetime.now(BEIJING_TZ).isoformat(),
                },
            }
        )
        return trusted

    def _prune_nonces(self, now: float) -> None:
        self._nonces = {nonce: expiry for nonce, expiry in self._nonces.items() if expiry > now}


def build_proxy_identity_verifier(base_cfg: dict, leases: LeaseManager) -> ProxyIdentityVerifier | None:
    raw = (base_cfg.get("auth") or {}).get("console_proxy") or {}
    if not raw.get("enabled", False):
        return None
    secret = raw.get("shared_secret")
    if not secret and raw.get("shared_secret_env"):
        secret = os.environ.get(str(raw["shared_secret_env"]))
    if not isinstance(secret, str) or not secret:
        raise ValueError("auth.console_proxy shared secret is not configured")
    return ProxyIdentityVerifier(
        secret,
        leases,
        max_clock_skew_s=float(raw.get("max_clock_skew_s", 30.0)),
    )


def _canonical(
    method: str,
    target: str,
    body: bytes,
    subject: str,
    name: str,
    encoded_identity: str,
    timestamp: int,
    nonce: str,
) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join(
        [SIGNATURE_VERSION, str(timestamp), nonce, method.upper(), target, digest, subject, name, encoded_identity]
    ).encode()


def _validate_text(value: str, what: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise ProxyAuthError(f"invalid Console identity {what}", code=ErrorCode.FORBIDDEN)


def _encode_header(value: str) -> str:
    return urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_header(value: str) -> str:
    try:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (Base64Error, ValueError, UnicodeDecodeError) as exc:
        raise ProxyAuthError("invalid Console identity display_name", code=ErrorCode.FORBIDDEN) from exc


__all__ = [
    "ProxyAuthError",
    "ProxyIdentity",
    "ProxyIdentityVerifier",
    "build_proxy_identity_verifier",
    "signed_identity_headers",
]
