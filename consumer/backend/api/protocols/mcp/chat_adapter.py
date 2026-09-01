"""MCP chat client for the canonical backend workflow bridge."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import logging
from typing import Any, Mapping

import httpx
import requests

from integration.mcp.config import MCPRuntimeConfig, load_mcp_runtime_config
from integration.mcp.scope import MCPRequestScope
from integration.mcp.ontology_binding import (
    MCPOntologyBindingError,
    bind_chat_arguments,
)

from .paths import MCP_INTERNAL_CHAT_PATH, MCP_INTERNAL_CHAT_STREAM_PATH
from .schemas import error_result

_BACKEND_DEADLINE_RESPONSE_GRACE_SECONDS = 5
logger = logging.getLogger(__name__)
MCPChatProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


def execute_chat_tool(
    args: Mapping[str, Any] | None,
    *,
    scope: MCPRequestScope,
    config: MCPRuntimeConfig | None = None,
) -> dict[str, Any]:
    cfg = config or load_mcp_runtime_config()
    request_payload, headers, preparation_error = _prepare_chat_request(
        args,
        scope=scope,
    )
    if preparation_error is not None:
        return preparation_error

    try:
        response = requests.post(
            "{}{}".format(
                cfg.agentic_base_url.rstrip("/"),
                MCP_INTERNAL_CHAT_PATH,
            ),
            headers=headers,
            json=request_payload,
            timeout=(
                max(1, int(cfg.http_timeout_seconds))
                + _BACKEND_DEADLINE_RESPONSE_GRACE_SECONDS
            ),
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
        body = None
    return _normalize_backend_response(response.status_code, body)


async def execute_chat_tool_async(
    args: Mapping[str, Any] | None,
    *,
    scope: MCPRequestScope,
    progress_callback: MCPChatProgressCallback | None = None,
    config: MCPRuntimeConfig | None = None,
) -> dict[str, Any]:
    """Call the backend without blocking FastMCP's event loop."""

    payload = dict(args or {})
    cfg = config or load_mcp_runtime_config()
    request_payload, headers, preparation_error = _prepare_chat_request(
        payload,
        scope=scope,
    )
    if preparation_error is not None:
        return preparation_error
    effective_stream = payload.get("stream") is True and progress_callback is not None
    path = MCP_INTERNAL_CHAT_STREAM_PATH if effective_stream else MCP_INTERNAL_CHAT_PATH
    timeout = max(1, int(cfg.http_timeout_seconds)) + _BACKEND_DEADLINE_RESPONSE_GRACE_SECONDS
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if effective_stream:
                return await _execute_streaming_chat(
                    client,
                    "{}{}".format(cfg.agentic_base_url.rstrip("/"), path),
                    headers=headers,
                    request_payload=request_payload,
                    progress_callback=progress_callback,
                    token=scope.token,
                )
            response = await client.post(
                "{}{}".format(cfg.agentic_base_url.rstrip("/"), path),
                headers=headers,
                json=request_payload,
            )
    except httpx.HTTPError as exc:
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
        body = None
    return _normalize_backend_response(response.status_code, body)


def _prepare_chat_request(
    args: Mapping[str, Any] | None,
    *,
    scope: MCPRequestScope,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any] | None]:
    payload = dict(args or {})
    query = str(payload.get("query") or payload.get("request") or "").strip()
    if not query:
        return {}, {}, error_result(
            error_type="tool_input_error",
            error_code="INVALID_TOOL_ARGUMENTS",
            detail="query 不能为空",
            hint="请传入 chat(query)。",
            retriable=True,
        )
    if not scope.token:
        return {}, {}, error_result(
            error_type="auth_error",
            error_code="AUTH_TOKEN_MISSING",
            detail="MCP chat bridge 缺少原始 Bearer Token",
            retriable=False,
        )
    try:
        bound_arguments = bind_chat_arguments(
            arguments=(
                payload.get("arguments")
                if isinstance(payload.get("arguments"), dict)
                else None
            ),
            token_ontology_id=scope.ontology_id,
        )
    except MCPOntologyBindingError as exc:
        return {}, {}, error_result(
            error_type=exc.error_type,
            error_code=exc.code,
            detail=exc.detail,
            retriable=False,
        )
    request_payload = {
        "request": query,
        "session_id": str(payload.get("session_id") or "").strip(),
        "context": dict(payload.get("context") or {}),
        "arguments": bound_arguments,
        "requested_operation_key": str(
            payload.get("requested_operation_key") or ""
        ).strip(),
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
    return request_payload, headers, None


async def _execute_streaming_chat(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    request_payload: Mapping[str, Any],
    progress_callback: MCPChatProgressCallback,
    token: str,
) -> dict[str, Any]:
    try:
        async with client.stream(
            "POST",
            url,
            headers=dict(headers),
            json=dict(request_payload),
        ) as response:
            if response.status_code >= 400:
                body = _decode_json_bytes(await response.aread())
                return _normalize_backend_response(response.status_code, body)
            async for event_name, event_data in _iter_internal_sse(response):
                if event_name == "progress":
                    try:
                        await progress_callback(event_data)
                    except Exception:
                        logger.warning(
                            "MCP progress callback failed event=%s",
                            str(event_data.get("event") or "unknown"),
                            exc_info=True,
                        )
                    continue
                if event_name == "result":
                    status_code = int(event_data.get("status_code") or 500)
                    return _normalize_backend_response(
                        status_code,
                        event_data.get("body"),
                    )
    except httpx.HTTPError as exc:
        detail = str(exc).replace(token, "***")
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_UNAVAILABLE",
            detail=detail,
            retriable=True,
        )
    except (TypeError, ValueError):
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_INVALID_RESPONSE",
            detail="Backend workflow bridge 返回无效流式响应",
            retriable=True,
        )
    return error_result(
        error_type="backend_error",
        error_code="MCP_CHAT_BRIDGE_INVALID_RESPONSE",
        detail="Backend workflow bridge 未返回最终结果",
        retriable=True,
    )


async def _iter_internal_sse(
    response: httpx.Response,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    event_name = "message"
    data_lines: list[str] = []
    async for raw_line in response.aiter_lines():
        line = str(raw_line or "").rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, _decode_sse_data(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.lstrip(" ")
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, _decode_sse_data(data_lines)


def _decode_sse_data(data_lines: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads("\n".join(data_lines))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid backend SSE JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("backend SSE data must be an object")
    return payload


def _decode_json_bytes(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _normalize_backend_response(status_code: int, body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return error_result(
            error_type="backend_error",
            error_code="MCP_CHAT_BRIDGE_INVALID_RESPONSE",
            detail="Backend workflow bridge 响应必须是对象",
            retriable=int(status_code) >= 500,
        )
    if int(status_code) >= 400:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        return error_result(
            error_type=str(error.get("type") or "backend_error"),
            error_code=str(error.get("error_code") or "MCP_CHAT_BRIDGE_ERROR"),
            detail=str(
                error.get("detail")
                or body.get("detail")
                or "Backend workflow bridge 调用失败"
            ),
            retriable=bool(error.get("retriable", int(status_code) >= 500)),
            phase=str(error.get("phase") or "").strip(),
        )
    return body
