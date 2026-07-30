"""Local observability performance analysis endpoints."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from utils.perf_analysis.service import (
    MAX_WINDOW_MS,
    MAX_RUN_LIST_LIMIT,
    analyze_global_overview,
    analyze_global_runs,
    analyze_run_detail,
    analyze_session,
    check_session_availability,
    parse_limit,
    parse_query_window,
)
from api.auth.auth import get_current_user, require_auth, require_role
from api.auth.service_auth import modeling_service_authorized
from workspace.workspaces import resolve_workspace_id

performance_bp = Blueprint("performance", __name__)


def get_request_stores():
    from api.routes import get_request_stores as resolve_request_stores

    return resolve_request_stores()


def get_request_workspace_id() -> str:
    return resolve_workspace_id(request)


@performance_bp.route("/agentic/sessions/<session_id>/performance", methods=["GET"])
@require_auth()
def get_session_performance(session_id: str):
    chat_store, _execution_store = get_request_stores()
    if not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    start_ms, end_ms = parse_query_window(request.args, default_window_ms=MAX_WINDOW_MS)
    limit_runs = parse_limit(
        request.args.get("limit_runs"),
        default=50,
        maximum=MAX_RUN_LIST_LIMIT,
    )
    return jsonify(
        analyze_session(
            session_id,
            start_ms=start_ms,
            end_ms=end_ms,
            limit_runs=limit_runs,
            workspace_id=get_request_workspace_id(),
        )
    )


@performance_bp.route("/agentic/sessions/<session_id>/performance/availability", methods=["GET"])
@require_auth()
def get_session_performance_availability(session_id: str):
    chat_store, _execution_store = get_request_stores()
    if not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    start_ms, end_ms = parse_query_window(request.args, default_window_ms=MAX_WINDOW_MS)
    return jsonify(
        check_session_availability(
            session_id,
            start_ms=start_ms,
            end_ms=end_ms,
            workspace_id=get_request_workspace_id(),
        )
    )


@performance_bp.route("/agentic/performance/runs/<run_id>", methods=["GET"])
@require_auth()
def get_run_performance(run_id: str):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Run not found"}), 404
    if current_user.role != "admin" and not _current_session_owns_run(run_id):
        return jsonify({"error": "Run not found"}), 404

    window_kwargs = {"default_window_ms": MAX_WINDOW_MS} if current_user.role != "admin" else {}
    start_ms, end_ms = parse_query_window(request.args, **window_kwargs)
    workspace_id = None
    if current_user.role == "admin":
        workspace_id = str(request.args.get("workspace_id") or "").strip() or None
    else:
        workspace_id = get_request_workspace_id()
    body = analyze_run_detail(run_id, start_ms=start_ms, end_ms=end_ms, workspace_id=workspace_id)
    if not body.get("timeline"):
        return jsonify({"error": "Run not found"}), 404
    return jsonify(body)


@performance_bp.route(
    "/agentic/internal/modeling/performance/runs/<run_id>", methods=["GET"]
)
def get_modeling_run_performance(run_id: str):
    """Return one workspace-scoped trace to the trusted Modeling service.

    Evaluation may outlive the user's access token. This endpoint uses the
    deployment-fixed service credential and never accepts a workspace through
    the URL or request body.
    """
    if not modeling_service_authorized(request):
        return jsonify({"error": "Forbidden"}), 403

    workspace_id = get_request_workspace_id()
    start_ms, end_ms = parse_query_window(request.args)
    body = analyze_run_detail(
        run_id,
        start_ms=start_ms,
        end_ms=end_ms,
        workspace_id=workspace_id,
    )
    if not body.get("timeline"):
        return jsonify({"error": "Run not found"}), 404
    return jsonify(body)


@performance_bp.route("/api/local-observability/overview", methods=["GET"])
@require_role("admin")
def get_local_observability_overview():
    start_ms, end_ms = parse_query_window(request.args)
    return jsonify(
        analyze_global_overview(
            start_ms=start_ms,
            end_ms=end_ms,
            **_filters_from_request(),
        )
    )


@performance_bp.route("/api/local-observability/runs", methods=["GET"])
@require_role("admin")
def list_local_observability_runs():
    start_ms, end_ms = parse_query_window(request.args)
    return jsonify(
        analyze_global_runs(
            start_ms=start_ms,
            end_ms=end_ms,
            limit=parse_limit(request.args.get("limit"), default=50, maximum=MAX_RUN_LIST_LIMIT),
            offset=parse_limit(request.args.get("offset"), default=1, maximum=1_000_000) - 1
            if request.args.get("offset") is not None
            else 0,
            **_filters_from_request(),
        )
    )


def _filters_from_request() -> dict[str, str | None]:
    return {
        key: str(request.args.get(key) or "").strip() or None
        for key in ("workspace_id", "status", "model_provider", "agent_mode")
    }


def _current_session_owns_run(run_id: str) -> bool:
    session_id = str(request.args.get("session_id") or "").strip()
    if not session_id:
        return False
    chat_store, execution_store = get_request_stores()
    if not chat_store.get_session(session_id):
        return False
    history = execution_store.get_session_history(session_id)
    turns = history.get("turns") if isinstance(history, dict) else None
    return any(_turn_run_id(turn) == run_id for turn in turns or ())


def _turn_run_id(turn: Any) -> str:
    return str(turn.get("run_id") or "") if isinstance(turn, dict) else ""
