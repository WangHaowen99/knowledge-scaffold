"""Local observability performance analysis endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

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
from api.auth.auth import (
    AuthUser,
    require_admin_user,
    require_authenticated_user,
)
from api.auth.service_auth import modeling_service_authorized
from api.dependencies import require_workspace_id
from integration.observability.speedscope_files import find_speedscope_session_file
from utils.identifiers import validate_session_id
from workspace.workspaces import (
    get_current_workspace_id,
    resolve_workspace_id,
    workspace_scope,
)

performance_router = APIRouter()


def get_request_stores():
    from api.routes import get_request_stores as resolve_request_stores

    return resolve_request_stores()


def get_request_workspace_id() -> str:
    return get_current_workspace_id()


@performance_router.get("/agentic/sessions/{session_id}/performance")
def get_session_performance(
    session_id: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    chat_store, _execution_store = get_request_stores()
    if not chat_store.get_session(session_id):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    start_ms, end_ms = parse_query_window(request.query_params, default_window_ms=MAX_WINDOW_MS)
    limit_runs = parse_limit(
        request.query_params.get("limit_runs"),
        default=50,
        maximum=MAX_RUN_LIST_LIMIT,
    )
    return analyze_session(
        session_id,
        start_ms=start_ms,
        end_ms=end_ms,
        limit_runs=limit_runs,
        workspace_id=get_request_workspace_id(),
    )


@performance_router.get("/agentic/sessions/{session_id}/performance/availability")
def get_session_performance_availability(
    session_id: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    chat_store, _execution_store = get_request_stores()
    if not chat_store.get_session(session_id):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    start_ms, end_ms = parse_query_window(request.query_params, default_window_ms=MAX_WINDOW_MS)
    return check_session_availability(
        session_id,
        start_ms=start_ms,
        end_ms=end_ms,
        workspace_id=get_request_workspace_id(),
    )


@performance_router.get(
    "/agentic/sessions/{session_id}/performance/speedscope",
)
def download_session_speedscope(
    session_id: str,
    current_user: AuthUser = Depends(require_authenticated_user),
    _workspace_id: str = Depends(require_workspace_id),
):
    """Download the current workspace session's combined Speedscope trace."""
    try:
        normalized_session_id = validate_session_id(session_id)
        workspace_id = get_request_workspace_id()
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    if current_user.role == "admin":
        path = find_speedscope_session_file(
            workspace_id,
            normalized_session_id,
            any_user=True,
        )
    else:
        chat_store, _execution_store = get_request_stores()
        if not chat_store.get_session(normalized_session_id):
            return JSONResponse({"error": "Session not found"}, status_code=404)
        path = find_speedscope_session_file(
            workspace_id,
            normalized_session_id,
            user_id=current_user.username,
        )

    if path is None:
        return JSONResponse(
            {
                "error": "Speedscope trace not found",
                "code": "SPEEDSCOPE_NOT_FOUND",
            },
            status_code=404,
        )
    return FileResponse(
        path,
        media_type="application/json",
        filename="{}.speedscope.json".format(normalized_session_id),
        headers={"Cache-Control": "no-store"},
    )


@performance_router.get("/agentic/performance/runs/{run_id}")
def get_run_performance(
    run_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
):
    workspace_id = None
    if current_user.role != "admin":
        try:
            workspace_id = resolve_workspace_id(request)
        except ValueError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        with workspace_scope(workspace_id):
            if not _current_session_owns_run(run_id, request):
                return JSONResponse({"error": "Run not found"}, status_code=404)
            workspace_id = get_request_workspace_id()

    window_kwargs = {"default_window_ms": MAX_WINDOW_MS} if current_user.role != "admin" else {}
    start_ms, end_ms = parse_query_window(request.query_params, **window_kwargs)
    if current_user.role == "admin":
        workspace_id = str(request.query_params.get("workspace_id") or "").strip() or None
    body = analyze_run_detail(run_id, start_ms=start_ms, end_ms=end_ms, workspace_id=workspace_id)
    if not body.get("timeline"):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return body


@performance_router.get(
    "/agentic/internal/modeling/performance/runs/{run_id}",
)
def get_modeling_run_performance(run_id: str, request: Request):
    """Return one workspace-scoped trace to the trusted Modeling service."""
    if not modeling_service_authorized(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        workspace_id = resolve_workspace_id(request)
    except KeyError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    start_ms, end_ms = parse_query_window(request.query_params)
    body = analyze_run_detail(
        run_id,
        start_ms=start_ms,
        end_ms=end_ms,
        workspace_id=workspace_id,
    )
    if not body.get("timeline"):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return body


@performance_router.get(
    "/api/local-observability/overview",
    dependencies=[Depends(require_admin_user)],
)
def get_local_observability_overview(request: Request):
    start_ms, end_ms = parse_query_window(request.query_params)
    return analyze_global_overview(
        start_ms=start_ms,
        end_ms=end_ms,
        **_filters_from_request(request),
    )


@performance_router.get(
    "/api/local-observability/runs",
    dependencies=[Depends(require_admin_user)],
)
def list_local_observability_runs(request: Request):
    start_ms, end_ms = parse_query_window(request.query_params)
    return analyze_global_runs(
        start_ms=start_ms,
        end_ms=end_ms,
        limit=parse_limit(request.query_params.get("limit"), default=50, maximum=MAX_RUN_LIST_LIMIT),
        offset=parse_limit(request.query_params.get("offset"), default=1, maximum=1_000_000) - 1
        if request.query_params.get("offset") is not None
        else 0,
        **_filters_from_request(request),
    )


def _filters_from_request(request: Request) -> dict[str, str | None]:
    return {
        key: str(request.query_params.get(key) or "").strip() or None
        for key in ("workspace_id", "status", "model_provider", "agent_mode")
    }


def _current_session_owns_run(run_id: str, request: Request) -> bool:
    session_id = str(request.query_params.get("session_id") or "").strip()
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
