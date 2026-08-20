"""Request scope resolution for MCP tool calls."""

from __future__ import annotations

import hmac
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from workspace.workspaces import (
    WorkspaceMCPEndpointError,
    WorkspaceNotFoundError,
    evaluate_workspace_subject_access,
    get_workspace_mcp_endpoint_id,
)
from integration.observability.trace_context import (
    TraceParentValidationError,
    normalize_traceparent,
)
from .token_service import MCPTokenAuthError, verify_mcp_token


_mcp_endpoint_id_var: ContextVar[str] = ContextVar("mcp_endpoint_id", default="")


@dataclass(frozen=True)
class MCPRequestScope:
    username: str
    workspace_id: str
    scopes: list[str]
    source: str = "bearer_header"
    token: str = field(default="", repr=False, compare=False)
    traceparent: str = ""
    ontology_id: str = ""


@contextmanager
def bind_mcp_endpoint_id(endpoint_id: str) -> Iterator[str]:
    normalized = str(endpoint_id or "").strip()
    token = _mcp_endpoint_id_var.set(normalized)
    try:
        yield normalized
    finally:
        _mcp_endpoint_id_var.reset(token)


def get_bound_mcp_endpoint_id() -> str:
    return str(_mcp_endpoint_id_var.get() or "").strip()


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
    mcp_endpoint_id: str = "",
) -> MCPRequestScope:
    if not str(token or "").strip():
        raise MCPTokenAuthError("AUTH_TOKEN_MISSING", "未检测到 Bearer Token")
    try:
        normalized_traceparent = normalize_traceparent(traceparent)
    except TraceParentValidationError as exc:
        raise MCPTokenAuthError("INVALID_TRACEPARENT", str(exc)) from exc
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
    requested_endpoint_id = str(mcp_endpoint_id or "").strip()
    if requested_endpoint_id:
        try:
            expected_endpoint_id = get_workspace_mcp_endpoint_id(
                payload["workspace_id"],
            )
        except WorkspaceNotFoundError as exc:
            raise MCPTokenAuthError(
                "MCP_WORKSPACE_NOT_FOUND",
                "Token 绑定的工作空间不存在",
            ) from exc
        except WorkspaceMCPEndpointError as exc:
            raise MCPTokenAuthError(
                "MCP_WORKSPACE_ENDPOINT_INVALID",
                "Token 绑定工作空间的 MCP Endpoint 元数据无效",
            ) from exc
        if not hmac.compare_digest(requested_endpoint_id, expected_endpoint_id):
            raise MCPTokenAuthError(
                "MCP_WORKSPACE_ENDPOINT_MISMATCH",
                "MCP Endpoint 与 Token 工作空间不匹配",
            )
    return MCPRequestScope(
        username=payload["username"],
        workspace_id=payload["workspace_id"],
        scopes=list(payload["scopes"]),
        source=source,
        token=str(token or "").strip(),
        traceparent=normalized_traceparent,
        ontology_id=str(payload.get("ontology_id") or "").strip(),
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
