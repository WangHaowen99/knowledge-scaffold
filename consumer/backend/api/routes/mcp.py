"""MCP management endpoints."""

from __future__ import annotations

import os
import socket

from flask import Blueprint, jsonify, request

from api.protocols.mcp.paths import MCP_API_PREFIX, MCP_INTERNAL_CHAT_ROUTE
from integration.mcp.config import MCPRuntimeConfig, load_mcp_runtime_config
from integration.mcp.service_catalog import list_mcp_service_cards
from integration.mcp.token_service import (
    MCPTokenServiceError,
    create_mcp_token,
    list_mcp_tokens,
    normalize_scopes,
    revoke_mcp_token,
)
from api.auth.auth import get_auth_manager, get_current_user, require_auth
from api.protocols.mcp.workflow_bridge import execute_mcp_chat_workflow
from integration.mcp.scope import (
    auth_error_payload,
    extract_bearer_token,
    resolve_mcp_scope_from_token,
)
from integration.mcp.token_service import MCPTokenAuthError
from workspace.workspaces import (
    WorkspaceMCPEndpointError,
    get_workspace_mcp_endpoint_id,
    resolve_workspace_id,
    workspace_scope,
)


mcp_bp = Blueprint("mcp", __name__, url_prefix=MCP_API_PREFIX)


def _resolve_workspace_or_response():
    try:
        return resolve_workspace_id(request), None
    except KeyError as exc:
        return None, (jsonify({"detail": str(exc)}), 404)
    except ValueError as exc:
        return None, (jsonify({"detail": str(exc)}), 400)


@mcp_bp.route("/runtime/status", methods=["GET"])
@require_auth()
def mcp_runtime_status():
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    cfg = load_mcp_runtime_config()
    try:
        mcp_endpoint_id = get_workspace_mcp_endpoint_id(workspace_id)
    except WorkspaceMCPEndpointError as exc:
        return jsonify({"detail": exc.detail, "code": exc.code}), 503
    runtime_status = _probe_mcp_runtime_status(cfg)
    services = list_mcp_service_cards(
        cfg,
        mcp_endpoint_id=mcp_endpoint_id,
    )
    for item in services:
        item["running"] = runtime_status["running"]
        item["status_label"] = "在线" if runtime_status["running"] else "离线"
    return jsonify({
        "running": runtime_status["running"],
        "transport": cfg.transport,
        "public_base_url": cfg.effective_public_base_url(),
        "probe_url": runtime_status["probe_url"],
        "detail": runtime_status["detail"],
        "workspace_id": workspace_id,
        "mcp_endpoint_id": mcp_endpoint_id,
        "services": services,
    })


@mcp_bp.route("/tokens", methods=["GET"])
@require_auth()
def list_tokens_api():
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    with workspace_scope(workspace_id):
        return jsonify({"items": list_mcp_tokens(username=user.username, workspace_id=workspace_id)})


@mcp_bp.route("/tokens", methods=["POST"])
@require_auth()
def create_token_api():
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    cfg = load_mcp_runtime_config()
    expires_in = payload["expires_in"] if "expires_in" in payload else cfg.token_ttl_seconds
    try:
        item = create_mcp_token(
            username=user.username,
            workspace_id=workspace_id,
            name=str(payload.get("name") or "").strip(),
            scopes=normalize_scopes(payload.get("scopes")),
            expires_in=expires_in,
            config=cfg,
        )
    except MCPTokenServiceError as exc:
        return jsonify({"detail": str(exc)}), 400
    with workspace_scope(workspace_id):
        return jsonify(item), 201


@mcp_bp.route("/tokens/<token_id>", methods=["DELETE"])
@require_auth()
def revoke_token_api(token_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    with workspace_scope(workspace_id):
        revoked = revoke_mcp_token(
            username=user.username,
            workspace_id=workspace_id,
            token_id=token_id,
        )
    if not revoked:
        return jsonify({"detail": "Token 不存在或已吊销"}), 404
    return jsonify({"ok": True, "token_id": token_id})


@mcp_bp.route(MCP_INTERNAL_CHAT_ROUTE, methods=["POST"])
def execute_internal_mcp_chat():
    """Revalidate an MCP credential and enter the canonical Jiuwen workflow."""
    token = extract_bearer_token(request.headers)
    try:
        scope = resolve_mcp_scope_from_token(
            token,
            required_scope="mcp:chat",
            traceparent=str(request.headers.get("traceparent") or "").strip(),
        )
    except MCPTokenAuthError as exc:
        status_code = 401 if exc.code == "AUTH_TOKEN_MISSING" else 403
        if exc.code == "INVALID_TRACEPARENT":
            status_code = 400
        return jsonify(auth_error_payload(exc)), status_code

    requested_workspace = str(request.headers.get("X-Workspace-Id") or "").strip()
    if requested_workspace and requested_workspace != scope.workspace_id:
        return jsonify({
            "ok": False,
            "error": {
                "type": "auth_error",
                "error_code": "MCP_WORKSPACE_SCOPE_MISMATCH",
                "detail": "请求 workspace 与 MCP Token scope 不一致",
                "retriable": False,
            },
        }), 403

    auth_backend = get_auth_manager()
    user = auth_backend.get_user(scope.username) if auth_backend is not None else None
    if user is None:
        return jsonify({
            "ok": False,
            "error": {
                "type": "auth_error",
                "error_code": "MCP_SUBJECT_NOT_FOUND",
                "detail": "MCP Token subject 当前不存在",
                "retriable": False,
            },
        }), 403

    cfg = load_mcp_runtime_config()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"detail": "JSON body 必须是对象"}), 400
    from api.routes import get_orchestrator

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=get_orchestrator(),
        scope=scope,
        payload=payload,
        concurrency_limit=100 if str(user.role or "") == "admin" else 1,
        queue_wait_seconds=min(30, max(1, int(cfg.http_timeout_seconds))),
        execution_timeout_seconds=max(1, int(cfg.http_timeout_seconds)),
        trace_config=cfg,
    )
    return jsonify(body), status_code


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
