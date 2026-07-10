"""Request scope resolution for MCP tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .token_service import MCPTokenAuthError, verify_mcp_token


@dataclass(frozen=True)
class MCPRequestScope:
    username: str
    workspace_id: str
    scopes: list[str]
    source: str = "bearer_header"


def extract_bearer_token(headers: Mapping[str, str] | None) -> str:
    normalized = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    auth = str(normalized.get("authorization") or "").strip()
    if not auth:
        return ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def resolve_mcp_scope_from_token(token: str, *, required_scope: str | None = None, source: str = "bearer_header") -> MCPRequestScope:
    if not str(token or "").strip():
        raise MCPTokenAuthError("AUTH_TOKEN_MISSING", "未检测到 Bearer Token")
    payload = verify_mcp_token(token, required_scope=required_scope)
    return MCPRequestScope(
        username=payload["username"],
        workspace_id=payload["workspace_id"],
        scopes=list(payload["scopes"]),
        source=source,
    )


def auth_error_payload(exc: MCPTokenAuthError) -> dict:
    return {
        "ok": False,
        "error": {
            "type": "auth_error",
            "error_code": exc.code,
            "detail": exc.detail,
            "retriable": False,
        },
    }
