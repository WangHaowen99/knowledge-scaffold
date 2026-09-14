# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Explicit, versioned provider connection contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ConnectionEnvelopeError

AuthType = Literal["none", "bearer", "mindsdb_password"]
_AUTH_TYPES = frozenset({"none", "bearer", "mindsdb_password"})


@dataclass(frozen=True)
class AuthSpec:
    type: AuthType
    token: str = ""
    username: str = ""
    password: str = ""

    @classmethod
    def from_dict(cls, payload: object) -> "AuthSpec":
        if not isinstance(payload, dict):
            raise ConnectionEnvelopeError("auth must be an object")
        auth_type = payload.get("type")
        if auth_type not in _AUTH_TYPES:
            raise ConnectionEnvelopeError("unsupported auth type")
        return cls(
            type=auth_type,
            token=str(payload.get("token") or ""),
            username=str(payload.get("username") or ""),
            password=str(payload.get("password") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {"type": self.type}
        if self.type == "bearer":
            payload["token"] = self.token
        elif self.type == "mindsdb_password":
            payload["username"] = self.username
            payload["password"] = self.password
        return payload


@dataclass(frozen=True)
class EffectiveConnection:
    version: int
    provider: str
    endpoint: str
    auth: AuthSpec
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: object) -> "EffectiveConnection":
        if not isinstance(payload, dict):
            raise ConnectionEnvelopeError("connection envelope must be an object")
        if payload.get("version") != 1 or isinstance(payload.get("version"), bool):
            raise ConnectionEnvelopeError("connection envelope version must be 1")
        provider = str(payload.get("provider") or "").strip().lower()
        endpoint = str(payload.get("endpoint") or "").strip().rstrip("/")
        options = payload.get("options", {})
        if not provider:
            raise ConnectionEnvelopeError("provider is required")
        if not endpoint:
            raise ConnectionEnvelopeError("endpoint is required")
        if not isinstance(options, dict):
            raise ConnectionEnvelopeError("options must be an object")
        return cls(
            version=1,
            provider=provider,
            endpoint=endpoint,
            auth=AuthSpec.from_dict(payload.get("auth")),
            options=dict(options),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "auth": self.auth.to_dict(),
            "options": dict(self.options),
        }
