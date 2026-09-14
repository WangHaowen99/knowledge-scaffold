"""MCP management endpoints."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent.runtime.contracts import USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
from api.auth.auth import (
    AuthUser,
    get_auth_manager,
    require_admin_user,
    require_authenticated_user,
)
from api.request_parsing import read_json_object
from api.protocols.mcp.paths import (
    MCP_API_PREFIX,
    MCP_INTERNAL_CHAT_ROUTE,
    MCP_INTERNAL_CHAT_STREAM_ROUTE,
)
from api.protocols.mcp.workflow_bridge import execute_mcp_chat_workflow_async
from integration.mcp.config import MCPRuntimeConfig, load_mcp_runtime_config
from integration.mcp.scope import (
    MCPRequestScope,
    auth_error_payload,
    extract_bearer_token,
    resolve_mcp_scope_from_token,
)
from integration.mcp.service_catalog import list_mcp_service_cards
from integration.mcp.ontology_binding import MCPOntologyBindingError
from integration.mcp.token_service import (
    MCPTokenAuthError,
    MCPTokenServiceError,
    create_mcp_token,
    list_mcp_tokens,
    normalize_scopes,
    revoke_mcp_token,
)
from workspace.workspaces import (
    WorkspaceMCPEndpointError,
    get_workspace_mcp_endpoint_id,
    get_workspace_runtime_config,
    resolve_workspace_id,
    update_workspace_runtime_config,
    workspace_scope,
)

mcp_router = APIRouter(prefix=MCP_API_PREFIX)
_MCPChatAuthorization = tuple[MCPRequestScope, Any] | JSONResponse
MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT = 5
MCP_STREAM_USER_WORKSPACE_CONCURRENCY_MAX = 32


def _mcp_stream_user_workspace_concurrency(workspace_id: str) -> int:
    runtime_config = get_workspace_runtime_config(workspace_id)
    mcp_config = runtime_config.get("mcp")
    if not isinstance(mcp_config, dict):
        return MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT
    value = mcp_config.get(
        "stream_user_workspace_concurrency",
        MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT,
    )
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MCP_STREAM_USER_WORKSPACE_CONCURRENCY_MAX
    ):
        return MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT
    return value


def _runtime(request: Request):
    container = getattr(request.app.state, "runtime_container", None)
    if container is not None:
        return container
    from api.routes import get_agent_execution_runtime

    return get_agent_execution_runtime()


async def _short_io(request: Request, function, *args, **kwargs):
    return await _runtime(request).short_io.run(function, *args, **kwargs)


async def _resolve_authenticated_workspace(
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
) -> str:
    """Resolve workspace access only after the native auth dependency succeeds."""
    try:
        return await _short_io(request, resolve_workspace_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _load_mcp_runtime_status(
    workspace_id: str,
) -> dict[str, object] | JSONResponse:
    cfg = load_mcp_runtime_config()
    try:
        mcp_endpoint_id = get_workspace_mcp_endpoint_id(workspace_id)
    except WorkspaceMCPEndpointError as exc:
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code},
            status_code=503,
        )
    runtime_status = _probe_mcp_runtime_status(cfg)
    services = list_mcp_service_cards(
        cfg,
        mcp_endpoint_id=mcp_endpoint_id,
    )
    for item in services:
        item["running"] = runtime_status["running"]
        item["status_label"] = "在线" if runtime_status["running"] else "离线"
    return {
        "running": runtime_status["running"],
        "transport": cfg.transport,
        "public_base_url": cfg.effective_public_base_url(),
        "probe_url": runtime_status["probe_url"],
        "detail": runtime_status["detail"],
        "workspace_id": workspace_id,
        "mcp_endpoint_id": mcp_endpoint_id,
        "services": services,
    }


@mcp_router.get("/runtime/status")
async def mcp_runtime_status(
    request: Request,
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    return await _short_io(request, _load_mcp_runtime_status, workspace_id)


@mcp_router.get(
    "/runtime/concurrency",
    dependencies=[Depends(require_authenticated_user)],
)
async def get_mcp_concurrency_api(
    request: Request,
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    value = await _short_io(
        request,
        _mcp_stream_user_workspace_concurrency,
        workspace_id,
    )
    return {
        "stream_user_workspace_concurrency": value,
        "default": MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT,
        "min": 1,
        "max": MCP_STREAM_USER_WORKSPACE_CONCURRENCY_MAX,
    }


def _update_mcp_concurrency(
    workspace_id: str,
    value: Any,
) -> dict[str, int]:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MCP_STREAM_USER_WORKSPACE_CONCURRENCY_MAX
    ):
        raise ValueError("stream_user_workspace_concurrency must be an integer between 1 and 32")
    updated = update_workspace_runtime_config(
        workspace_id,
        {"mcp": {"stream_user_workspace_concurrency": value}},
    )
    return {
        "stream_user_workspace_concurrency": int(updated["mcp"]["stream_user_workspace_concurrency"]),
        "default": MCP_STREAM_USER_WORKSPACE_CONCURRENCY_DEFAULT,
        "min": 1,
        "max": MCP_STREAM_USER_WORKSPACE_CONCURRENCY_MAX,
    }


@mcp_router.patch(
    "/runtime/concurrency",
    dependencies=[Depends(require_admin_user)],
)
async def update_mcp_concurrency_api(
    request: Request,
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    payload = await read_json_object(request)
    if "stream_user_workspace_concurrency" not in payload:
        return JSONResponse(
            {"detail": "stream_user_workspace_concurrency is required"},
            status_code=400,
        )
    try:
        return await _short_io(
            request,
            _update_mcp_concurrency,
            workspace_id,
            payload["stream_user_workspace_concurrency"],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


def _list_tokens(*, username: str, workspace_id: str) -> list[dict[str, Any]]:
    with workspace_scope(workspace_id):
        return list_mcp_tokens(username=username, workspace_id=workspace_id)


@mcp_router.get("/tokens")
async def list_tokens_api(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    items = await _short_io(
        request,
        _list_tokens,
        username=user.username,
        workspace_id=workspace_id,
    )
    return {"items": items}


def _create_token(
    *,
    username: str,
    workspace_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ontology_id = ""
    if "ontology_id" in payload:
        raw_ontology_id = payload.get("ontology_id")
        if not isinstance(raw_ontology_id, str) or not raw_ontology_id.strip():
            raise MCPOntologyBindingError(
                "MCP_ONTOLOGY_ID_INVALID",
                "ontology_id 必须是非空字符串",
            )
        ontology_id = raw_ontology_id.strip()
    cfg = load_mcp_runtime_config()
    expires_in = payload["expires_in"] if "expires_in" in payload else cfg.token_ttl_seconds
    with workspace_scope(workspace_id):
        return create_mcp_token(
            username=username,
            workspace_id=workspace_id,
            name=str(payload.get("name") or "").strip(),
            scopes=normalize_scopes(payload.get("scopes")),
            expires_in=expires_in,
            ontology_id=ontology_id,
            config=cfg,
        )


@mcp_router.post("/tokens", status_code=201)
async def create_token_api(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    payload = await read_json_object(request)
    try:
        return await _short_io(
            request,
            _create_token,
            username=user.username,
            workspace_id=workspace_id,
            payload=payload,
        )
    except MCPOntologyBindingError as exc:
        return JSONResponse(
            {"detail": exc.detail, "error_code": exc.code},
            status_code=exc.status_code,
        )
    except MCPTokenServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _revoke_token(*, username: str, workspace_id: str, token_id: str) -> bool:
    with workspace_scope(workspace_id):
        return revoke_mcp_token(
            username=username,
            workspace_id=workspace_id,
            token_id=token_id,
        )


@mcp_router.delete("/tokens/{token_id}")
async def revoke_token_api(
    request: Request,
    token_id: str,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(_resolve_authenticated_workspace),
):
    revoked = await _short_io(
        request,
        _revoke_token,
        username=user.username,
        workspace_id=workspace_id,
        token_id=token_id,
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="Token 不存在或已吊销")
    return {"ok": True, "token_id": token_id}


async def _authorize_internal_mcp_chat(request: Request) -> _MCPChatAuthorization:
    """Resolve the MCP principal from headers before the body is consumed."""
    token = extract_bearer_token(request.headers)
    try:
        scope = await _short_io(
            request,
            resolve_mcp_scope_from_token,
            token,
            required_scope="mcp:chat",
            traceparent=str(request.headers.get("traceparent") or "").strip(),
        )
    except MCPTokenAuthError as exc:
        status_code = 401 if exc.code == "AUTH_TOKEN_MISSING" else 403
        if exc.code == "INVALID_TRACEPARENT":
            status_code = 400
        return JSONResponse(
            content=auth_error_payload(exc),
            status_code=status_code,
        )

    requested_workspace = str(request.headers.get("X-Workspace-Id") or "").strip()
    if requested_workspace and requested_workspace != scope.workspace_id:
        return JSONResponse(
            content={
                "ok": False,
                "error": {
                    "type": "auth_error",
                    "error_code": "MCP_WORKSPACE_SCOPE_MISMATCH",
                    "detail": "请求 workspace 与 MCP Token scope 不一致",
                    "retriable": False,
                },
            },
            status_code=403,
        )

    auth_backend = get_auth_manager(request)
    user = await _short_io(request, auth_backend.get_user, scope.username) if auth_backend is not None else None
    if user is None:
        return JSONResponse(
            content={
                "ok": False,
                "error": {
                    "type": "auth_error",
                    "error_code": "MCP_SUBJECT_NOT_FOUND",
                    "detail": "MCP Token subject 当前不存在",
                    "retriable": False,
                },
            },
            status_code=403,
        )
    return scope, user


@mcp_router.post(MCP_INTERNAL_CHAT_ROUTE)
async def execute_internal_mcp_chat(
    request: Request,
    authorization: _MCPChatAuthorization = Depends(_authorize_internal_mcp_chat),
):
    """Enter the canonical Jiuwen workflow after native header authentication."""
    if isinstance(authorization, JSONResponse):
        return authorization
    scope, _user = authorization

    cfg = await _short_io(request, load_mcp_runtime_config)
    try:
        payload = await read_json_object(request)
    except HTTPException as exc:
        return JSONResponse(
            content={"detail": exc.detail},
            status_code=exc.status_code,
        )
    from api.routes import get_user_stores

    _chat_store, execution_store = await _short_io(
        request,
        get_user_stores,
        scope.username,
        scope.workspace_id,
    )
    body, status_code = await execute_mcp_chat_workflow_async(
        runtime=_runtime(request),
        execution_store=execution_store,
        scope=scope,
        payload=payload,
        concurrency_limit=USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
        queue_wait_seconds=min(30, max(1, int(cfg.http_timeout_seconds))),
        execution_timeout_seconds=max(1, int(cfg.http_timeout_seconds)),
        trace_config=cfg,
    )
    return JSONResponse(content=body, status_code=status_code)


@mcp_router.post(MCP_INTERNAL_CHAT_STREAM_ROUTE)
async def stream_internal_mcp_chat(
    request: Request,
    authorization: _MCPChatAuthorization = Depends(_authorize_internal_mcp_chat),
):
    """Stream coarse public progress and one terminal result to the MCP server."""

    if isinstance(authorization, JSONResponse):
        return authorization
    scope, user = authorization
    cfg = await _short_io(request, load_mcp_runtime_config)
    try:
        payload = await read_json_object(request)
    except HTTPException as exc:
        return JSONResponse(
            content={"detail": exc.detail},
            status_code=exc.status_code,
        )

    from api.routes import get_user_stores

    _chat_store, execution_store = await _short_io(
        request,
        get_user_stores,
        scope.username,
        scope.workspace_id,
    )
    ordinary_limit = await _short_io(
        request,
        _mcp_stream_user_workspace_concurrency,
        scope.workspace_id,
    )
    queue_size = int(getattr(cfg, "chat_progress_queue_size", 128))
    heartbeat_seconds = int(getattr(cfg, "chat_progress_heartbeat_seconds", 15))
    max_message_bytes = int(getattr(cfg, "chat_progress_max_message_bytes", 4096))
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=max(16, queue_size))
    progress_was_dropped = False

    async def publish_progress(event: dict[str, Any]) -> None:
        nonlocal progress_was_dropped
        safe_event = _bounded_internal_progress_event(
            event,
            max_bytes=max_message_bytes,
        )
        if safe_event is None:
            progress_was_dropped = True
            return
        item = ("progress", safe_event)
        if str(safe_event.get("event") or "") in {
            "tool.completed",
            "run.completed",
            "run.failed",
        }:
            await event_queue.put(item)
            return
        try:
            event_queue.put_nowait(item)
        except asyncio.QueueFull:
            progress_was_dropped = True

    async def execute_workflow() -> None:
        try:
            body, status_code = await execute_mcp_chat_workflow_async(
                runtime=_runtime(request),
                execution_store=execution_store,
                scope=scope,
                payload=payload,
                concurrency_limit=(
                    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT if str(user.role or "") == "admin" else ordinary_limit
                ),
                queue_wait_seconds=min(
                    30,
                    max(1, int(cfg.http_timeout_seconds)),
                ),
                execution_timeout_seconds=max(
                    1,
                    int(cfg.http_timeout_seconds),
                ),
                trace_config=cfg,
                progress_sink=publish_progress,
            )
        except Exception:
            body = {
                "ok": False,
                "error": {
                    "type": "backend_error",
                    "error_code": "MCP_CHAT_BRIDGE_ERROR",
                    "detail": "Backend workflow bridge failed",
                    "retriable": True,
                },
            }
            status_code = 500
        if progress_was_dropped:
            body = dict(body)
            body["progress_truncated"] = True
        await event_queue.put(
            (
                "result",
                {"status_code": status_code, "body": body},
            )
        )

    workflow_task = asyncio.create_task(execute_workflow())

    async def generate():
        try:
            while True:
                try:
                    event_name, event_data = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=max(5, heartbeat_seconds),
                    )
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield _encode_internal_sse(event_name, event_data)
                if event_name == "result":
                    return
        finally:
            if not workflow_task.done():
                workflow_task.cancel()
            await asyncio.gather(workflow_task, return_exceptions=True)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _encode_internal_sse(event_name: str, payload: dict[str, Any]) -> str:
    return "event: {}\ndata: {}\n\n".format(
        str(event_name or "message"),
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def _bounded_internal_progress_event(
    event: dict[str, Any],
    *,
    max_bytes: int,
) -> dict[str, Any] | None:
    payload = dict(event or {})
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(serialized) <= max(1024, int(max_bytes)):
        return payload
    payload["summary"] = "运行轨迹内容已裁剪"
    payload["warning_code"] = "PROGRESS_TRUNCATED"
    payload["truncated"] = True
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload if len(serialized) <= max(1024, int(max_bytes)) else None


def _probe_mcp_runtime_status(cfg: MCPRuntimeConfig) -> dict[str, object]:
    host = _normalize_probe_host(cfg.server_host)
    port = int(cfg.server_port)
    timeout = _read_status_timeout()
    probe_url = "tcp://{}:{}".format(host, port)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "running": True,
                "probe_url": probe_url,
                "detail": "tcp_ok",
            }
    except OSError as exc:
        return {
            "running": False,
            "probe_url": probe_url,
            "detail": str(exc),
        }


def _normalize_probe_host(host: str) -> str:
    value = str(host or "").strip()
    if value in {"", "0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return value.strip("[]")


def _read_status_timeout() -> float:
    raw = str(os.getenv("MCP_SERVER_STATUS_TIMEOUT", "") or "").strip()
    if not raw:
        return 0.8
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 0.8
