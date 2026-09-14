# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Standalone FastMCP server for MetaWise search/chat toolsets."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import inspect
import json
import logging
import os
import re
import threading
import time
import warnings
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable, Mapping, get_type_hints
from urllib.parse import parse_qs

from pydantic import Field

try:
    from fastmcp import Context
except ImportError:  # pragma: no cover - handled by _load_fastmcp
    Context = Any  # type: ignore[misc,assignment]

from integration.mcp.config import MCPRuntimeConfig, load_mcp_runtime_config, validate_mcp_runtime_config
from integration.mcp.scope import (
    MCPRequestScope,
    auth_error_payload,
    bind_mcp_endpoint_id,
    extract_bearer_token,
    get_bound_mcp_endpoint_id,
    resolve_mcp_scope_from_token,
)
from integration.mcp.service_catalog import load_mcp_service_catalog
from integration.observability.trace_context import bind_otel_traceparent
from utils.security.redaction import redact_sensitive_text
from workspace.workspaces import validate_mcp_endpoint_id

from .chat_adapter import execute_chat_tool_async
from .search_adapter import execute_search_tool

StringListInput = list[str] | str | None
DictInput = dict[str, Any] | str | None
DictListInput = list[dict[str, Any]] | str
PathPatternInput = list[Any] | str
OntologyIdInput = Annotated[
    str,
    Field(
        description="可选本体 ID。Token 已绑定知识包时可省略；显式值必须与 Token 绑定一致。旧未绑定 Token 仍必须传入。"
    ),
]
_MCP_ENDPOINT_STATE_KEY = "meta_wise_mcp_endpoint_id"
_MCP_SESSION_ID_HEADER = b"mcp-session-id"
_MCP_SESSION_IDLE_TIMEOUT_SECONDS = 30 * 60
_MCP_BINDING_CLEANUP_GRACE_SECONDS = 60
_MCP_BINDING_CLEANUP_INTERVAL_SECONDS = 60
_MCP_MAX_ACTIVE_SESSIONS = 10_000
_MCP_SESSION_CAPACITY_WARNING_RATIO = 0.8
_SSE_SESSION_ID_PATTERN = re.compile(
    rb"(?:[?&])session_id=([0-9a-fA-F]{32})(?:[^0-9a-fA-F]|$)",
)
logger = logging.getLogger(__name__)


@dataclass
class _WorkspaceEndpointSessionBinding:
    endpoint_id: str
    last_seen_at: float


class _MCPActiveSessionLimitError(RuntimeError):
    pass


def _load_mcp_dotenv() -> None:
    """Load the local MCP environment before startup migrations inspect it."""

    from integration.mcp.config import _load_local_dotenv

    _load_local_dotenv()


class _WorkspaceEndpointSessionBindings:
    """Keep FastMCP transport sessions on the endpoint that created them."""

    def __init__(
        self,
        *,
        idle_timeout_seconds: float | None = (_MCP_SESSION_IDLE_TIMEOUT_SECONDS + _MCP_BINDING_CLEANUP_GRACE_SECONDS),
        max_active_sessions: int = _MCP_MAX_ACTIVE_SESSIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds 必须为正数")
        if max_active_sessions <= 0:
            raise ValueError("max_active_sessions 必须为正整数")
        self._lock = threading.Lock()
        self._bindings: OrderedDict[
            str,
            _WorkspaceEndpointSessionBinding,
        ] = OrderedDict()
        self._reservations: dict[int, str] = {}
        self._next_reservation_id = 1
        self._idle_timeout_seconds = idle_timeout_seconds
        self._max_active_sessions = int(max_active_sessions)
        self._clock = clock
        self._capacity_warning_emitted = False

    def reserve_new_session(self, endpoint_id: str) -> int:
        now = self._clock()
        with self._lock:
            self._expire_idle_locked(now)
            if self._active_count_locked() >= self._max_active_sessions:
                raise _MCPActiveSessionLimitError(
                    "MCP active session limit reached",
                )
            reservation_id = self._next_reservation_id
            self._next_reservation_id += 1
            self._reservations[reservation_id] = endpoint_id
            self._warn_if_near_capacity_locked()
            return reservation_id

    def bind_or_validate(
        self,
        session_id: str,
        endpoint_id: str,
        *,
        reservation_id: int | None = None,
    ) -> bool:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return True
        now = self._clock()
        with self._lock:
            self._expire_idle_locked(now)
            binding = self._bindings.get(normalized_session_id)
            if binding is None:
                reserved_endpoint_id = None
                if reservation_id is not None:
                    reserved_endpoint_id = self._reservations.pop(
                        reservation_id,
                        None,
                    )
                if reserved_endpoint_id is not None and not hmac.compare_digest(
                    reserved_endpoint_id,
                    endpoint_id,
                ):
                    return False
                if reserved_endpoint_id is None and self._active_count_locked() >= self._max_active_sessions:
                    raise _MCPActiveSessionLimitError(
                        "MCP active session limit reached",
                    )
                binding = _WorkspaceEndpointSessionBinding(
                    endpoint_id=endpoint_id,
                    last_seen_at=now,
                )
                self._bindings[normalized_session_id] = binding
                self._warn_if_near_capacity_locked()
            else:
                if reservation_id is not None:
                    self._reservations.pop(reservation_id, None)
                if hmac.compare_digest(binding.endpoint_id, endpoint_id):
                    binding.last_seen_at = now
                    self._bindings.move_to_end(normalized_session_id)
        matches = hmac.compare_digest(binding.endpoint_id, endpoint_id)
        if not matches:
            logger.warning(
                "Rejecting MCP session %s on a different workspace endpoint",
                normalized_session_id[:16],
            )
        return matches

    def release(self, session_id: str, endpoint_id: str) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        with self._lock:
            binding = self._bindings.get(
                normalized_session_id,
            )
            if binding is not None and hmac.compare_digest(
                binding.endpoint_id,
                endpoint_id,
            ):
                self._bindings.pop(normalized_session_id, None)
                self._reset_capacity_warning_locked()

    def release_reservation(self, reservation_id: int | None) -> None:
        if reservation_id is None:
            return
        with self._lock:
            self._reservations.pop(reservation_id, None)
            self._reset_capacity_warning_locked()

    def expire_idle(self) -> int:
        with self._lock:
            expired = self._expire_idle_locked(self._clock())
            self._reset_capacity_warning_locked()
            return expired

    def active_count(self) -> int:
        with self._lock:
            return self._active_count_locked()

    def _active_count_locked(self) -> int:
        return len(self._bindings) + len(self._reservations)

    def _expire_idle_locked(self, now: float) -> int:
        if self._idle_timeout_seconds is None:
            return 0
        expired = 0
        while self._bindings:
            _, binding = next(iter(self._bindings.items()))
            if now - binding.last_seen_at < self._idle_timeout_seconds:
                break
            self._bindings.popitem(last=False)
            expired += 1
        return expired

    def _warn_if_near_capacity_locked(self) -> None:
        active_count = self._active_count_locked()
        warning_threshold = max(
            1,
            int(self._max_active_sessions * _MCP_SESSION_CAPACITY_WARNING_RATIO),
        )
        if active_count >= warning_threshold and not self._capacity_warning_emitted:
            logger.warning(
                "MCP active sessions reached %s/%s",
                active_count,
                self._max_active_sessions,
            )
            self._capacity_warning_emitted = True

    def _reset_capacity_warning_locked(self) -> None:
        warning_threshold = max(
            1,
            int(self._max_active_sessions * _MCP_SESSION_CAPACITY_WARNING_RATIO),
        )
        if self._active_count_locked() < warning_threshold:
            self._capacity_warning_emitted = False


def _request_mcp_session_id(scope: Mapping[str, Any]) -> str:
    for raw_name, raw_value in scope.get("headers") or []:
        if bytes(raw_name).lower() == _MCP_SESSION_ID_HEADER:
            return bytes(raw_value).decode("latin-1").strip()
    raw_query = bytes(scope.get("query_string") or b"")
    if not raw_query:
        return ""
    query = parse_qs(raw_query.decode("latin-1"), keep_blank_values=False)
    return str((query.get("session_id") or [""])[0]).strip()


def _response_mcp_session_id(message: Mapping[str, Any]) -> str:
    if message.get("type") != "http.response.start":
        return ""
    for raw_name, raw_value in message.get("headers") or []:
        if bytes(raw_name).lower() == _MCP_SESSION_ID_HEADER:
            return bytes(raw_value).decode("latin-1").strip()
    return ""


class _WorkspaceEndpointBindingApp:
    def __init__(
        self,
        app: Any,
        *,
        session_bindings: _WorkspaceEndpointSessionBindings,
        release_session_on_disconnect: bool = False,
        rewrite_to_mount_root: bool = False,
    ) -> None:
        self.app = app
        self.session_bindings = session_bindings
        self.release_session_on_disconnect = bool(release_session_on_disconnect)
        self.rewrite_to_mount_root = bool(rewrite_to_mount_root)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        from starlette.responses import JSONResponse

        raw_endpoint_id = str((scope.get("path_params") or {}).get("mcp_endpoint_id") or "").strip()
        try:
            endpoint_id = validate_mcp_endpoint_id(raw_endpoint_id)
        except ValueError:
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return

        request_session_id = _request_mcp_session_id(scope)
        reservation_id: int | None = None
        try:
            if request_session_id:
                session_matches = self.session_bindings.bind_or_validate(
                    request_session_id,
                    endpoint_id,
                )
            else:
                reservation_id = self.session_bindings.reserve_new_session(
                    endpoint_id,
                )
                session_matches = True
        except _MCPActiveSessionLimitError:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {
                        "code": -32000,
                        "message": "Server session capacity reached",
                    },
                },
                status_code=503,
            )
            await response(scope, receive, send)
            return

        if not session_matches:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {
                        "code": -32600,
                        "message": "Session not found",
                    },
                },
                status_code=404,
            )
            await response(scope, receive, send)
            return

        child_scope = dict(scope)
        child_state = dict(scope.get("state") or {})
        child_state[_MCP_ENDPOINT_STATE_KEY] = endpoint_id
        child_scope["state"] = child_state
        if self.rewrite_to_mount_root:
            request_path = str(scope.get("path") or "").rstrip("/")
            existing_root_path = str(scope.get("root_path") or "").rstrip("/")
            child_scope["root_path"] = "{}{}".format(
                existing_root_path,
                request_path,
            )
            child_scope["path"] = "/"
            child_scope["raw_path"] = b"/"

        response_status = 0
        response_session_id = ""
        connection_session_id = ""
        sse_response_buffer = bytearray()
        release_session_on_disconnect = self.release_session_on_disconnect and child_scope.get("method") == "GET"

        async def send_with_session_binding(message: dict[str, Any]) -> None:
            nonlocal connection_session_id, reservation_id
            nonlocal response_session_id, response_status
            is_initial_sse_body = all(
                (
                    not connection_session_id,
                    message.get("type") == "http.response.body",
                    bool(message.get("body")),
                )
            )
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status") or 0)
                response_session_id = _response_mcp_session_id(message)
                if response_session_id:
                    self.session_bindings.bind_or_validate(
                        response_session_id,
                        endpoint_id,
                        reservation_id=reservation_id,
                    )
                    reservation_id = None
            elif release_session_on_disconnect and is_initial_sse_body:
                if len(sse_response_buffer) < 8192:
                    sse_response_buffer.extend(bytes(message["body"]))
                match = _SSE_SESSION_ID_PATTERN.search(sse_response_buffer)
                if match is not None:
                    candidate_session_id = match.group(1).decode("ascii").lower()
                    if self.session_bindings.bind_or_validate(
                        candidate_session_id,
                        endpoint_id,
                        reservation_id=reservation_id,
                    ):
                        connection_session_id = candidate_session_id
                        reservation_id = None
            await send(message)

        try:
            with bind_mcp_endpoint_id(endpoint_id):
                await self.app(child_scope, receive, send_with_session_binding)
        finally:
            self.session_bindings.release_reservation(reservation_id)
            if connection_session_id:
                self.session_bindings.release(
                    connection_session_id,
                    endpoint_id,
                )

        should_release = response_status == 404 or (
            child_scope.get("method") == "DELETE" and 200 <= response_status < 300
        )
        if request_session_id and should_release:
            self.session_bindings.release(request_session_id, endpoint_id)


def create_mcp_server(config: MCPRuntimeConfig | None = None, *, toolset: str = "search") -> Any:
    cfg = config or load_mcp_runtime_config()
    validate_mcp_runtime_config(cfg)
    fast_mcp_class = _load_fastmcp()
    normalized_toolset = _normalize_toolset(toolset)
    mcp = fast_mcp_class("meta-wise-{}".format(normalized_toolset))

    def _with_scope(required_scope: str, caller: Callable[[MCPRequestScope], dict[str, Any]]) -> dict[str, Any]:
        try:
            scope = _resolve_scope(cfg, required_scope=required_scope)
        except Exception as exc:
            from integration.mcp.token_service import MCPTokenAuthError

            if isinstance(exc, MCPTokenAuthError):
                return auth_error_payload(exc)
            return {
                "ok": False,
                "error": {
                    "type": "auth_error",
                    "error_code": "INVALID_MCP_TOKEN",
                    "detail": str(exc),
                    "retriable": False,
                },
            }
        return _invoke_mcp_with_observation(
            scope,
            required_scope=required_scope,
            caller=caller,
        )

    async def _with_scope_async(
        required_scope: str,
        caller: Callable[[MCPRequestScope], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            scope = _resolve_scope(cfg, required_scope=required_scope)
        except Exception as exc:
            from integration.mcp.token_service import MCPTokenAuthError

            if isinstance(exc, MCPTokenAuthError):
                return auth_error_payload(exc)
            return {
                "ok": False,
                "error": {
                    "type": "auth_error",
                    "error_code": "INVALID_MCP_TOKEN",
                    "detail": str(exc),
                    "retriable": False,
                },
            }
        return await _invoke_mcp_with_observation_async(
            scope,
            required_scope=required_scope,
            caller=caller,
        )

    if normalized_toolset in {"search", "all"}:
        _register_search_tools(mcp, call_with_scope=_with_scope)
    if normalized_toolset in {"chat", "all"}:
        _register_chat_tool(mcp, call_with_scope=_with_scope_async)
    return mcp


def _invoke_mcp_with_observation(
    scope: MCPRequestScope,
    *,
    required_scope: str,
    caller: Callable[[MCPRequestScope], dict[str, Any]],
) -> dict[str, Any]:
    with bind_otel_traceparent(scope.traceparent, strict=False):
        with _mcp_request_observation(scope, required_scope=required_scope) as span:
            result = caller(scope)
            _mark_mcp_result_error(span, result)
            return result


async def _invoke_mcp_with_observation_async(
    scope: MCPRequestScope,
    *,
    required_scope: str,
    caller: Callable[[MCPRequestScope], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    with bind_otel_traceparent(scope.traceparent, strict=False):
        with _mcp_request_observation(scope, required_scope=required_scope) as span:
            result = await caller(scope)
            _mark_mcp_result_error(span, result)
            return result


@contextmanager
def _mcp_request_observation(
    scope: MCPRequestScope,
    *,
    required_scope: str,
):
    span = None
    token = None
    try:
        from integration.observability.provider import get_observer
        from integration.observability.workspace_router import get_default_workspace_router
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, set_span_in_context

        get_observer()
        router = get_default_workspace_router()
        attributes = {
            "agent.workspace_id": scope.workspace_id,
            "mcp.required_scope": required_scope,
        }
        if router is not None:
            attributes["agent.observability.router_id"] = router.router_id
        span = trace.get_tracer("onto.consumer.mcp").start_span(
            "mcp.request",
            kind=SpanKind.SERVER,
            attributes=attributes,
        )
        token = otel_context.attach(set_span_in_context(span))
    except Exception:
        logger.warning("Failed to start MCP OpenTelemetry span", exc_info=True)

    try:
        yield span
    except Exception as exc:
        _mark_mcp_span_error(
            span,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        raise
    finally:
        if token is not None:
            try:
                otel_context.detach(token)
            except Exception:
                logger.warning("Failed to detach MCP OpenTelemetry context", exc_info=True)
        if span is not None:
            try:
                span.end()
            except Exception:
                logger.warning("Failed to end MCP OpenTelemetry span", exc_info=True)


def _mark_mcp_result_error(span: Any, result: Any) -> None:
    if span is None or not isinstance(result, Mapping) or result.get("ok") is not False:
        return
    error = result.get("error")
    error_payload = error if isinstance(error, Mapping) else {}
    _mark_mcp_span_error(
        span,
        error_type=str(error_payload.get("type") or "mcp_error"),
        error_code=str(error_payload.get("error_code") or ""),
        detail=str(error_payload.get("detail") or error or "MCP request failed"),
    )


def _mark_mcp_span_error(
    span: Any,
    *,
    error_type: str,
    detail: str,
    error_code: str = "",
) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        safe_detail = redact_sensitive_text(detail).strip()[:1000]
        attributes: dict[str, Any] = {
            "mcp.response.ok": False,
            "error.type": error_type or "mcp_error",
        }
        if error_code:
            attributes["mcp.error_code"] = error_code
        if safe_detail:
            attributes["error.message"] = safe_detail
        span.set_attributes(attributes)
        span.add_event("mcp.request.failed", attributes=attributes)
        span.set_status(
            Status(
                StatusCode.ERROR,
                safe_detail or error_code or error_type or "MCP request failed",
            )
        )
    except Exception:
        logger.warning("Failed to mark MCP OpenTelemetry span as error", exc_info=True)


def _register_search_tools(
    mcp: Any, *, call_with_scope: Callable[[str, Callable[[MCPRequestScope], dict[str, Any]]], dict[str, Any]]
) -> None:
    @mcp.tool()
    def get_object_types(
        ontology_id: OntologyIdInput = "",
    ) -> dict[str, Any]:
        """获取当前本体绑定图数据中的全部实体/节点类型列表。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool("get_object_types", {"ontology_id": ontology_id}, scope=scope),
        )

    @mcp.tool()
    def get_relation_types(
        ontology_id: OntologyIdInput = "",
    ) -> dict[str, Any]:
        """获取当前本体绑定图数据中的全部语义关系类型列表。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool("get_relation_types", {"ontology_id": ontology_id}, scope=scope),
        )

    @mcp.tool()
    def get_nodes_schema(
        ontology_id: OntologyIdInput = "",
        element_types: Annotated[
            StringListInput, Field(description='可选节点类型数组，示例 ["Company", "Person"]；也兼容合法 JSON 字符串。')
        ] = None,
    ) -> dict[str, Any]:
        """获取节点类型及其属性字段 schema；只返回结构，不返回实例数据。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "get_nodes_schema", {"ontology_id": ontology_id, "element_types": element_types}, scope=scope
            ),
        )

    @mcp.tool()
    def get_edges_schema(
        ontology_id: OntologyIdInput = "",
        relation_types: Annotated[
            StringListInput, Field(description='可选关系类型数组，示例 ["包含", "影响"]；也兼容合法 JSON 字符串。')
        ] = None,
    ) -> dict[str, Any]:
        """获取关系 schema 和 relation_triples；路径查询前应优先查看关系三元组。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "get_edges_schema", {"ontology_id": ontology_id, "relation_types": relation_types}, scope=scope
            ),
        )

    @mcp.tool()
    def property_filter(
        queries: Annotated[DictListInput, Field(description="属性过滤子查询数组；也兼容合法 JSON 字符串。")],
        ontology_id: OntologyIdInput = "",
        max_total_rows: Annotated[int, Field(ge=1, le=5000, description="最大返回行数。")] = 500,
        entity: Annotated[str, Field(description="可选本体对象名，用于解析映射。")] = "",
    ) -> dict[str, Any]:
        """按属性过滤节点/关系。单个 filter_dict 内多个字段表示 AND；queries 表示多个独立子查询。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "property_filter",
                {"ontology_id": ontology_id, "entity": entity, "queries": queries, "max_total_rows": max_total_rows},
                scope=scope,
            ),
        )

    @mcp.tool()
    def pattern_search(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        path_pattern: Annotated[
            PathPatternInput,
            Field(description="节点-关系-节点路径模板；必须是一条路径的数组，也兼容合法 JSON 字符串。"),
        ],
        ontology_id: OntologyIdInput = "",
        return_vars: Annotated[
            StringListInput, Field(description='返回变量列表，例如 ["a", "r1", "b"]；也兼容合法 JSON 字符串。')
        ] = None,
        return_properties: Annotated[Any | None, Field(description="返回属性投影。")] = None,
        limit: Annotated[int, Field(ge=1, le=2000, description="返回条数上限。")] = 100,
        entity: Annotated[str, Field(description="可选本体对象名，用于解析映射。")] = "",
    ) -> dict[str, Any]:
        """按图结构路径匹配子图数据。

        适用场景：用户描述了明确实体关系路径，例如 A 类型节点经过 R 关系连接到 B 类型节点。
        建议先调用 get_edges_schema，参考 relation_triples 构造 path_pattern。

        path_pattern 固定骨架：
        [[节点选择器, 节点属性?], [关系选择器, 方向], [节点选择器, 节点属性?], ...]

        规则：
        - 每次只描述一条路径，不支持 [[path1], [path2]] 这种批量路径数组。
        - 一跳路径必须正好包含起点节点段、关系段、终点节点段 3 个顶层元素。
        - 每个节点段和关系段都必须是数组，不能把对象或字符串直接放在 path_pattern 顶层。
        - 方向只支持 "->"、"<-"、"--"；不关心方向时使用 "--"。
        - relation_triples 中 source_label=S, relation_type=R, target_label=T 表示 S -[R]-> T。
          从 S 查 T 用 [R, "->"]；从 T 查回 S 用 [R, "<-"]。
        - return_vars 按当前路径自动编号，一跳路径只有 a、r1、b。
        """
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "pattern_search",
                {
                    "ontology_id": ontology_id,
                    "entity": entity,
                    "path_pattern": path_pattern,
                    "return_vars": return_vars,
                    "return_properties": return_properties,
                    "limit": limit,
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def count_search(
        queries: Annotated[DictListInput, Field(description="计数子查询数组；也兼容合法 JSON 字符串。")],
        ontology_id: OntologyIdInput = "",
    ) -> dict[str, Any]:
        """统计满足条件的节点或关系数量，支持批量子查询。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "count_search", {"ontology_id": ontology_id, "queries": queries}, scope=scope
            ),
        )

    @mcp.tool()
    def aggregate_search(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        element_class: str,
        element_type: str,
        target_property: str,
        agg: str,
        ontology_id: OntologyIdInput = "",
        filter_dict: DictInput = None,
    ) -> dict[str, Any]:
        """对节点或关系字段做 SUM/AVG/MIN/MAX/COUNT 聚合，字段名应来自 schema。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "aggregate_search",
                {
                    "ontology_id": ontology_id,
                    "element_class": element_class,
                    "element_type": element_type,
                    "target_property": target_property,
                    "agg": agg,
                    "filter_dict": filter_dict or {},
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def sorted_search(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        element_class: str,
        element_type: str,
        ontology_id: OntologyIdInput = "",
        filter_dict: DictInput = None,
        return_properties: StringListInput = None,
        sort_by: StringListInput = None,
        ascending: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询节点或关系并按字段排序，适合最大/最小/最新/排名类问题。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "sorted_search",
                {
                    "ontology_id": ontology_id,
                    "element_class": element_class,
                    "element_type": element_type,
                    "filter_dict": filter_dict or {},
                    "return_properties": return_properties,
                    "sort_by": sort_by,
                    "ascending": ascending,
                    "limit": limit,
                    "offset": offset,
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def get_element_info(
        element_instance_id: str,
        ontology_id: OntologyIdInput = "",
        element_class: str = "",
        element_type: str = "",
    ) -> dict[str, Any]:
        """按节点或关系实例 ID 查询单个元素的完整详情。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "get_element_info",
                {
                    "ontology_id": ontology_id,
                    "element_instance_id": element_instance_id,
                    "element_class": element_class,
                    "element_type": element_type,
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def hop_search(
        entity_instance_id: str,
        ontology_id: OntologyIdInput = "",
        hop_num: int = 1,
        accurate_flag: bool = False,
    ) -> dict[str, Any]:
        """从指定节点实例出发查询 N 度邻域；entity_instance_id 应来自其他检索工具返回。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "hop_search",
                {
                    "ontology_id": ontology_id,
                    "entity_instance_id": entity_instance_id,
                    "hop_num": hop_num,
                    "accurate_flag": accurate_flag,
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def shortest_path(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        start_label: str,
        start_props: DictInput,
        end_label: str,
        ontology_id: OntologyIdInput = "",
        end_props: DictInput = None,
        max_depth: int = 15,
        directed: bool = False,
    ) -> dict[str, Any]:
        """查找两个节点之间的最短关联路径，start_props/end_props 用于定位起止节点。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "shortest_path",
                {
                    "ontology_id": ontology_id,
                    "start_label": start_label,
                    "start_props": start_props,
                    "end_label": end_label,
                    "end_props": end_props or {},
                    "max_depth": max_depth,
                    "directed": directed,
                },
                scope=scope,
            ),
        )

    @mcp.tool()
    def list_logic_titles(
        ontology_id: OntologyIdInput = "",
        entity_type: Annotated[str, Field(description="可选业务实体类型过滤。")] = "",
    ) -> dict[str, Any]:
        """列出当前本体绑定图数据中的业务逻辑标题目录。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "list_logic_titles", {"ontology_id": ontology_id, "entity_type": entity_type}, scope=scope
            ),
        )

    @mcp.tool()
    def get_logic_content(
        entity_instance_ids: Annotated[
            list[str] | str, Field(description="业务实体实例 ID 数组；也兼容合法 JSON 字符串。")
        ],
        ontology_id: OntologyIdInput = "",
        logic_titles: Annotated[
            StringListInput, Field(description="可选业务逻辑标题数组；也兼容合法 JSON 字符串。")
        ] = None,
    ) -> dict[str, Any]:
        """按实体实例 ID 和可选标题读取业务逻辑内容。"""
        return call_with_scope(
            "mcp:search",
            lambda scope: execute_search_tool(
                "get_logic_content",
                {
                    "ontology_id": ontology_id,
                    "entity_instance_ids": entity_instance_ids,
                    "logic_titles": logic_titles or [],
                },
                scope=scope,
            ),
        )


def _register_chat_tool(
    mcp: Any,
    *,
    call_with_scope: Callable[
        [str, Callable[[MCPRequestScope], Awaitable[dict[str, Any]]]],
        Awaitable[dict[str, Any]],
    ],
) -> None:
    async def chat(  # pylint: disable=huawei-too-many-arguments
        query: Annotated[str, Field(description="用户问题。")],
        ctx: Context,
        session_id: Annotated[str, Field(description="可选会话 ID。")] = "",
        model_profile_id: Annotated[str, Field(description="可选模型配置 ID。")] = "",
        arguments: Annotated[dict[str, Any] | None, Field(description="可选结构化参数。")] = None,
        requested_operation_key: Annotated[str, Field(description="可选确定性 Operation key。")] = "",
        include_trace: Annotated[bool, Field(description="是否返回脱敏后的执行 Trace，默认关闭。")] = False,
        selected_ontology_id: Annotated[str, Field(description="可选知识包 ID；首次调用时锁定到会话。")] = "",
        stream: Annotated[bool, Field(description="是否通过 MCP Progress 实时返回粗粒度运行轨迹，默认关闭。")] = False,
    ) -> dict[str, Any]:
        """与 MetaWise 主 Agent 进行单轮知识问答；需要确定性图数据时优先使用知识检索工具。"""
        args = {
            "query": query,
            "session_id": session_id,
            "model_profile_id": model_profile_id,
            "arguments": arguments or {},
            "requested_operation_key": requested_operation_key,
            "include_trace": include_trace,
            "stream": stream,
            "context": {"selected_ontology_id": selected_ontology_id.strip()} if selected_ontology_id.strip() else {},
        }
        progress_callback = None
        if stream and _context_progress_token(ctx) is not None:

            async def report_progress(event: dict[str, Any]) -> None:
                await ctx.report_progress(
                    progress=float(event.get("seq") or 0),
                    total=None,
                    message=json.dumps(
                        event,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )

            progress_callback = report_progress
        return await call_with_scope(
            "mcp:chat",
            lambda scope: execute_chat_tool_async(
                args,
                scope=scope,
                progress_callback=progress_callback,
            ),
        )

    chat.__annotations__ = get_type_hints(chat, include_extras=True)
    mcp.tool()(chat)


def _context_progress_token(ctx: Any) -> str | int | None:
    request_context = getattr(ctx, "request_context", None)
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, Mapping):
        value = meta.get("progressToken")
    else:
        value = getattr(meta, "progressToken", None)
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _resolve_scope(cfg: MCPRuntimeConfig, *, required_scope: str) -> MCPRequestScope:
    headers = _get_headers_best_effort()
    token = extract_bearer_token(headers) or os.getenv("MCP_BEARER_TOKEN", "").strip()
    request_endpoint_id = _get_request_mcp_endpoint_id_best_effort()
    endpoint_id = get_bound_mcp_endpoint_id() if request_endpoint_id is None else request_endpoint_id
    return resolve_mcp_scope_from_token(
        token,
        required_scope=required_scope,
        traceparent=str(headers.get("traceparent") or "").strip(),
        mcp_endpoint_id=endpoint_id,
    )


def _get_headers_best_effort() -> Mapping[str, str]:
    get_http_headers = _load_get_http_headers()
    if get_http_headers is None:
        return {}
    for kwargs in ({"include_all": True}, {"include": {"authorization"}}, {}):
        try:
            headers = get_http_headers(**kwargs)
            if isinstance(headers, Mapping):
                return {str(k).lower(): str(v) for k, v in headers.items()}
        except TypeError:
            continue
        except Exception:
            return {}
    return {}


def _get_request_mcp_endpoint_id_best_effort() -> str | None:
    get_http_request = _load_get_http_request()
    if get_http_request is None:
        return None
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    try:
        endpoint_id = (request.scope.get("state") or {}).get(_MCP_ENDPOINT_STATE_KEY)
        return validate_mcp_endpoint_id(endpoint_id)
    except Exception as exc:
        from integration.mcp.token_service import MCPTokenAuthError

        raise MCPTokenAuthError(
            "MCP_WORKSPACE_ENDPOINT_INVALID",
            "当前 MCP 请求缺少有效的 Endpoint 上下文",
        ) from exc


def create_mcp_http_root_app(config: MCPRuntimeConfig | None = None, *, transport: str | None = None):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    cfg = config or load_mcp_runtime_config()
    mounted_apps = []
    session_binding_registries: list[_WorkspaceEndpointSessionBindings] = []
    routes = []
    for item in load_mcp_service_catalog():
        for normalized_transport in _normalize_http_transports(transport or cfg.transport):
            endpoint_path = "/sse" if normalized_transport == "sse" else "/mcp"
            server = create_mcp_server(cfg, toolset=item.toolset)
            subapp = server.http_app(path="/", transport=normalized_transport)
            if normalized_transport == "streamable-http":
                _configure_streamable_http_session_idle_timeout(
                    subapp,
                    _MCP_SESSION_IDLE_TIMEOUT_SECONDS,
                )
            mounted_apps.append(subapp)
            session_bindings = _WorkspaceEndpointSessionBindings(
                idle_timeout_seconds=(
                    None
                    if normalized_transport == "sse"
                    else (_MCP_SESSION_IDLE_TIMEOUT_SECONDS + _MCP_BINDING_CLEANUP_GRACE_SECONDS)
                ),
            )
            session_binding_registries.append(session_bindings)
            public_path = "/{{mcp_endpoint_id}}{}{}".format(
                item.endpoint_prefix,
                endpoint_path,
            )
            routes.append(
                Route(
                    public_path,
                    endpoint=_WorkspaceEndpointBindingApp(
                        subapp,
                        session_bindings=session_bindings,
                        release_session_on_disconnect=(normalized_transport == "sse"),
                        rewrite_to_mount_root=True,
                    ),
                    name="{}-{}-exact".format(
                        item.service_id,
                        normalized_transport,
                    ),
                )
            )
            routes.append(
                Mount(
                    public_path,
                    app=_WorkspaceEndpointBindingApp(
                        subapp,
                        session_bindings=session_bindings,
                        release_session_on_disconnect=(normalized_transport == "sse"),
                    ),
                    name="{}-{}".format(item.service_id, normalized_transport),
                )
            )

    async def health(_request):
        return JSONResponse({"status": "ok"})

    routes.append(Route("/health", health, methods=["GET"]))

    @asynccontextmanager
    async def lifespan(_: Any):
        async with AsyncExitStack() as stack:
            for app in mounted_apps:
                await stack.enter_async_context(app.router.lifespan_context(app))
            cleanup_task = asyncio.create_task(
                _run_session_binding_cleanup(session_binding_registries),
            )
            try:
                yield
            finally:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except asyncio.CancelledError:
                    pass

    root_app = Starlette(routes=routes, lifespan=lifespan)
    root_app.state.mcp_session_binding_registries = tuple(
        session_binding_registries,
    )
    return root_app


def _configure_streamable_http_session_idle_timeout(
    app: Any,
    idle_timeout_seconds: float,
) -> None:
    for route in getattr(app, "routes", []):
        session_manager = getattr(
            getattr(route, "app", None),
            "session_manager",
            None,
        )
        if session_manager is not None:
            session_manager.session_idle_timeout = float(idle_timeout_seconds)
            return
    if type(app).__module__.startswith("fastmcp."):
        raise RuntimeError(
            "FastMCP Streamable HTTP app 缺少可配置的 session manager",
        )


async def _run_session_binding_cleanup(
    registries: list[_WorkspaceEndpointSessionBindings],
) -> None:
    while True:
        await asyncio.sleep(_MCP_BINDING_CLEANUP_INTERVAL_SECONDS)
        expired = sum(registry.expire_idle() for registry in registries)
        if expired:
            logger.info("Expired %s idle MCP session endpoint bindings", expired)


def _normalize_http_transports(value: str | None) -> tuple[str, ...]:
    transport = str(value or "both").strip().lower()
    if transport == "http":
        transport = "streamable-http"
    if transport == "both":
        return ("streamable-http", "sse")
    if transport in {"streamable-http", "sse"}:
        return (transport,)
    raise ValueError("HTTP 模式不支持 transport={!r}".format(value))


def run_server(
    config: MCPRuntimeConfig | None = None,
    *,
    _migrations_already_run: bool = False,
) -> None:
    import uvicorn
    from integration.observability.otel import initialize_open_telemetry
    from migrations.runner import run_startup_migrations

    if not _migrations_already_run:
        _load_mcp_dotenv()
        run_startup_migrations()
    # Run compatibility migrations before implicit config loading.  The
    # loader captures process environment values in the AppConfig snapshot;
    # loading it first could retain a historical LLM_MAX_TOKENS value that the
    # startup migration is about to pin.  Explicitly supplied configs remain
    # untouched and are still accepted by the standalone entry point.
    cfg = config or load_mcp_runtime_config()
    initialize_open_telemetry()
    app = create_mcp_http_root_app(cfg, transport=cfg.transport)
    uvicorn.run(app, host=cfg.server_host, port=cfg.server_port)


def _normalize_toolset(toolset: str | None) -> str:
    value = str(toolset or "search").strip().lower() or "search"
    if value not in {"search", "chat", "all"}:
        raise ValueError("未知 toolset: {}".format(toolset))
    return value


def _load_fastmcp():
    _suppress_fastmcp_authlib_deprecation_warning()
    try:
        from fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("未安装 fastmcp，请先安装后启动 MCP Server") from exc
    return FastMCP


def _load_get_http_headers():
    _suppress_fastmcp_authlib_deprecation_warning()
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:  # pragma: no cover - depends on fastmcp version
        return None
    return get_http_headers


def _load_get_http_request():
    _suppress_fastmcp_authlib_deprecation_warning()
    try:
        from fastmcp.server.dependencies import get_http_request
    except Exception:  # pragma: no cover - depends on fastmcp version
        return None
    return get_http_request


def _suppress_fastmcp_authlib_deprecation_warning() -> None:
    """Suppress a FastMCP dependency warning until FastMCP migrates off authlib.jose."""
    try:
        from authlib.deprecate import AuthlibDeprecationWarning
    except Exception:  # pragma: no cover - depends on optional dependency internals
        warning_category = Warning
    else:
        warning_category = AuthlibDeprecationWarning

    warnings.filterwarnings(
        "ignore",
        message=r"authlib\.jose module is deprecated.*",
        category=warning_category,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MetaWise MCP Server")
    parser.add_argument("--transport", default="", choices=["", "both", "streamable-http", "sse", "stdio"])
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--toolset", default="all", choices=["search", "chat", "all"])
    args = parser.parse_args()
    from migrations.runner import run_startup_migrations

    _load_mcp_dotenv()
    run_startup_migrations()
    cfg = load_mcp_runtime_config()
    if args.host or args.port or args.transport:
        cfg = MCPRuntimeConfig(
            **{
                **cfg.__dict__,
                "server_host": args.host or cfg.server_host,
                "server_port": args.port or cfg.server_port,
                "transport": args.transport or cfg.transport,
            }
        )
    if cfg.transport == "stdio":
        from integration.observability.otel import initialize_open_telemetry

        initialize_open_telemetry()
        server = create_mcp_server(cfg, toolset=args.toolset)
        run_fn = server.run
        params = inspect.signature(run_fn).parameters
        kwargs = {"transport": "stdio"} if "transport" in params else {}
        run_fn(**kwargs)
        return
    run_server(cfg, _migrations_already_run=True)


if __name__ == "__main__":
    main()
