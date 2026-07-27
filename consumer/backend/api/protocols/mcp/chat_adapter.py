"""MCP chat client for the canonical backend workflow bridge."""

from __future__ import annotations

from typing import Any, Mapping

import requests

from integration.mcp.config import MCPRuntimeConfig, load_mcp_runtime_config
from integration.mcp.scope import MCPRequestScope

from .paths import MCP_INTERNAL_CHAT_PATH
from .schemas import error_result


def execute_chat_tool(
    args: Mapping[str, Any] | None,
    *,
    scope: MCPRequestScope,
    config: MCPRuntimeConfig | None = None,
) -> dict[str, Any]:
    payload = dict(args or {})
    query = str(payload.get("query") or payload.get("request") or "").strip()
    if not query:
        return error_result(
            error_type="tool_input_error",
            error_code="INVALID_TOOL_ARGUMENTS",
            detail="query 不能为空",
            hint="请传入 chat(query)。",
            retriable=True,
        )
    if not scope.token:
        return error_result(
            error_type="auth_error",
            error_code="AUTH_TOKEN_MISSING",
            detail="MCP chat bridge 缺少原始 Bearer Token",
            retriable=False,
        )

    cfg = config or load_mcp_runtime_config()
    request_payload = {
        "request": query,
        "session_id": str(payload.get("session_id") or "").strip(),
        "context": dict(payload.get("context") or {}),
        "arguments": payload.get("arguments") if isinstance(payload.get("arguments"), dict) else None,
        "include_trace": payload.get("include_trace") is True,
    }
    model_profile_id = str(payload.get("model_profile_id") or "").strip()
    if model_profile_id:
        request_payload["context"]["model_profile_id"] = model_profile_id

    headers = {
        "Authorization": "Bearer {}".format(scope.token),
        "Content-Type": "application/json",
        "X-Workspace-Id": scope.workspace_id,
    }
    if scope.traceparent:
        headers["traceparent"] = scope.traceparent

    try:
        response = requests.post(
            "{}{}".format(
                cfg.agentic_base_url.rstrip("/"),
                MCP_INTERNAL_CHAT_PATH,
            ),
            headers=headers,
            json=request_payload,
            timeout=max(1, int(cfg.http_timeout_seconds)),
        )
    except requests.RequestException as exc:
        detail = str(exc).replace(scope.token, "***")
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_UNAVAILABLE",
            detail=detail,
            retriable=True,
        )

    try:
        body = response.json()
    except ValueError:
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_INVALID_RESPONSE",
            detail="Backend workflow bridge 返回非 JSON 响应",
            retriable=response.status_code >= 500,
        )
    if not isinstance(body, dict):
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_INVALID_RESPONSE",
            detail="Backend workflow bridge 响应必须是对象",
            retriable=response.status_code >= 500,
        )
    if response.status_code >= 400:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        return error_result(
            error_type=str(error.get("type") or "backend_error"),
            error_code=str(error.get("error_code") or "MCP_CHAT_BRIDGE_ERROR"),
            detail=str(error.get("detail") or body.get("detail") or "Backend workflow bridge 调用失败"),
            retriable=bool(error.get("retriable", response.status_code >= 500)),
        )
    return body
