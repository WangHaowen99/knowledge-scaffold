"""Request scope resolution for MCP tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from workspace.workspaces import evaluate_workspace_subject_access
from .token_service import MCPTokenAuthError, verify_mcp_token


@dataclass(frozen=True)
class MCPRequestScope:
    username: str
    workspace_id: str
    scopes: list[str]
    source: str = "bearer_header"
    token: str = field(default="", repr=False, compare=False)
    traceparent: str = ""


def extract_bearer_token(headers: Mapping[str, str] | None) -> str:
    normalized = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    auth = str(normalized.get("authorization") or "").strip()
    if not auth:
        return ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def resolve_mcp_scope_from_token(
    token: str,
    *,
    required_scope: str | None = None,
    source: str = "bearer_header",
    traceparent: str = "",
) -> MCPRequestScope:
    if not str(token or "").strip():
        raise MCPTokenAuthError("AUTH_TOKEN_MISSING", "未检测到 Bearer Token")
    payload = verify_mcp_token(token, required_scope=required_scope)
    decision = evaluate_workspace_subject_access(
        payload["username"],
        payload["workspace_id"],
    )
    if not decision.allowed:
        raise _workspace_access_error(
            decision.reason,
            username=payload["username"],
            workspace_id=payload["workspace_id"],
        )
    return MCPRequestScope(
        username=payload["username"],
        workspace_id=payload["workspace_id"],
        scopes=list(payload["scopes"]),
        source=source,
        token=str(token or "").strip(),
        traceparent=str(traceparent or "").strip(),
    )


def _workspace_access_error(
    reason: str,
    *,
    username: str,
    workspace_id: str,
) -> MCPTokenAuthError:
    if reason == "workspace_not_found":
        return MCPTokenAuthError(
            "MCP_WORKSPACE_NOT_FOUND",
            "Token 绑定的工作空间不存在: {}".format(workspace_id),
        )
    if reason == "workspace_metadata_invalid":
        return MCPTokenAuthError(
            "MCP_WORKSPACE_METADATA_INVALID",
            "Token 绑定的工作空间元数据无效: {}".format(workspace_id),
        )
    if reason == "subject_invalid":
        return MCPTokenAuthError("INVALID_MCP_TOKEN", "Token subject 无效")
    return MCPTokenAuthError(
        "MCP_WORKSPACE_ACCESS_REVOKED",
        "用户 {} 当前无权访问工作空间 {}".format(username, workspace_id),
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
