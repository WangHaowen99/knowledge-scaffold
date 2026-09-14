# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Chat session endpoints: CRUD, messages, title generation."""

import asyncio
import threading

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import RunScope, TERMINAL_RUN_STATUSES
from agent.runtime.errors import SessionBusyError
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import get_runtime_container, require_workspace_id
from api.exceptions import APIError
from api.request_parsing import read_json_object, read_optional_json_object
from config import LimitsConfig
from config.system_config import SystemConfigValidationError, resolve_effective_llm_profile_config
from llm import LLMPurpose, get_llm_router
from llm.llm_request_mode import normalize_llm_request_mode
from ontology.ontologies import get_ontology_registry
from persistence.deterministic_terminal import (
    recover_pending_deterministic_artifacts,
    recover_pending_deterministic_terminal,
)
from persistence.execution_store import (
    INTERACTION_CANCELLED_NOTICE,
    InteractionCheckpointError,
)
from persistence.session_summary_index import InvalidSessionCursor, SessionSummary
from utils.identifiers import validate_session_id
from utils.logging import get_logger
from workspace.workspaces import workspace_scope

logger = get_logger("routes.sessions")
_PRUNE_CONVERGENCE_LOCK = threading.Lock()
_ACTIVE_PRUNE_CONVERGENCES: set[tuple[str, str]] = set()
_SESSION_INDEX_FALLBACK_MAX = 1000


async def _converge_session_prune_batches(
    runtime,
    prune_batch,
    *,
    batch_size: int = 25,
    convergence_key: tuple[str, str] | None = None,
    deadline_seconds: float = 5.0,
    max_no_progress: int = 6,
    initial_backoff_seconds: float = 0.05,
    max_backoff_seconds: float = 0.8,
) -> int:
    """Continue bounded prune batches as runtime-owned derived work."""

    claimed = False
    if convergence_key is not None:
        with _PRUNE_CONVERGENCE_LOCK:
            if convergence_key in _ACTIVE_PRUNE_CONVERGENCES:
                return 0
            _ACTIVE_PRUNE_CONVERGENCES.add(convergence_key)
            claimed = True
    deleted_total = 0
    no_progress_count = 0
    backoff = max(0.0, float(initial_backoff_seconds))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.01, float(deadline_seconds))
    try:
        while loop.time() < deadline:
            outcome = await runtime.short_io.run(prune_batch, batch_size)
            deleted = tuple(outcome.deleted_session_ids)
            deleted_total += len(deleted)
            if not outcome.has_more:
                return deleted_total
            if outcome.no_progress:
                no_progress_count += 1
                if no_progress_count >= max(1, int(max_no_progress)):
                    return deleted_total
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return deleted_total
                delay = min(backoff, max(0.0, remaining))
                if delay:
                    await asyncio.sleep(delay)
                backoff = min(
                    max(0.0, float(max_backoff_seconds)),
                    backoff * 2 if backoff else 0,
                )
            else:
                no_progress_count = 0
                backoff = max(0.0, float(initial_backoff_seconds))
                await asyncio.sleep(0)
        return deleted_total
    finally:
        if claimed:
            with _PRUNE_CONVERGENCE_LOCK:
                _ACTIVE_PRUNE_CONVERGENCES.discard(convergence_key)


def _validate_session_path_parameter(
    session_id: str | None = None,
    _user: AuthUser = Depends(require_authenticated_user),
) -> None:
    if session_id is None:
        return
    try:
        validate_session_id(session_id)
    except ValueError as exc:
        raise APIError(400, {"error": str(exc)}) from exc


sessions_router = APIRouter(
    prefix="/agentic",
    dependencies=[Depends(_validate_session_path_parameter)],
)


def _get_user_stores():
    from api.routes import get_request_stores

    return get_request_stores()


def _get_session_after_terminal_recovery(
    chat_store,
    execution_store,
    session_id: str,
    active_run_ids: set[str] | frozenset[str] = frozenset(),
):
    session = chat_store.get_session(session_id)
    if session is None:
        return None
    if not callable(getattr(execution_store, "deterministic_terminal_transaction", None)):
        return session
    if recover_pending_deterministic_terminal(
        execution_store,
        chat_store,
        session_id,
    ):
        session = chat_store.get_session(session_id)
    if callable(getattr(execution_store, "get_pending_deterministic_artifacts", None)):
        recover_pending_deterministic_artifacts(
            execution_store,
            chat_store,
            session_id,
            run_is_active=lambda run_id: run_id in active_run_ids,
        )
    return session


def _collect_active_run_ids(snapshots) -> set[str]:
    terminal_statuses = set()
    for status in TERMINAL_RUN_STATUSES:
        terminal_statuses.add(status.value)

    active_run_ids = set()
    for snapshot in snapshots:
        raw_status = getattr(snapshot.status, "value", snapshot.status)
        if str(raw_status) not in terminal_statuses:
            active_run_ids.add(snapshot.run_id)
    return active_run_ids


def _resolve_selected_ontology_selection(value, *, workspace_id: str):
    """Validate and canonicalize the optional session ontology selection."""
    selected_ontology_id = str(value or "").strip()
    if not selected_ontology_id:
        return None, None, None

    with workspace_scope(workspace_id):
        registry = get_ontology_registry(workspace_id=workspace_id)
        ontology = registry.get_ontology(selected_ontology_id)

    if ontology is None:
        raise ValueError("知识包不存在: {}".format(selected_ontology_id))
    if not bool(getattr(ontology, "enabled", False)):
        raise ValueError("知识包已禁用: {}".format(selected_ontology_id))
    resolved_ontology_id = str(getattr(ontology, "id", selected_ontology_id) or selected_ontology_id)
    resolved_ontology_version = str(
        getattr(ontology, "active_version", "") or getattr(ontology, "version", "") or ""
    ).strip()
    resolved_ontology_name = str(getattr(ontology, "name", "") or resolved_ontology_id).strip() or resolved_ontology_id
    return resolved_ontology_id, resolved_ontology_version or None, resolved_ontology_name


def _coerce_seq(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_durable_agent_event(event) -> bool:
    # Includes token.delta: the main frontend rebuilds chat messages from
    # events, so a reconnecting poll must receive streamed tokens to render
    # the assistant's live text.
    data = event.get("data") if isinstance(event, dict) else None
    return isinstance(data, dict) and event.get("event") == "agent.v2" and data.get("protocol") == "agent-stream.v2"


def _last_durable_seq(events, *, run_id: str | None = None) -> int:
    last_seq = 0
    for event in events or []:
        if not _is_durable_agent_event(event):
            continue
        data = event.get("data") or {}
        if run_id is not None and str(data.get("run_id") or "") != run_id:
            continue
        last_seq = max(last_seq, _coerce_seq(data.get("seq")))
    return last_seq


def _last_turn_status(events) -> str:
    for event in reversed(list(events or [])):
        if event.get("event") == "chat_message":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            if metadata.get("type") == "interaction_cancelled":
                return "cancelled"
        if not _is_durable_agent_event(event):
            continue
        data = event.get("data") or {}
        if str(data.get("type") or "") != "turn.finished":
            continue
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        status = str(payload.get("status") or "").strip()
        if status:
            return status
    return ""


def _session_runtime_payload(
    session,
    *,
    history,
    live_snapshot=None,
) -> dict:
    if live_snapshot is not None:
        run_id = live_snapshot.run_id
        return {
            "active_run_id": run_id,
            "active_run_status": live_snapshot.status.value,
            "last_seq": max(
                int(live_snapshot.last_seq),
                _last_durable_seq(
                    getattr(session, "events", []) or [],
                    run_id=run_id,
                ),
            ),
        }
    turns = list((history or {}).get("turns") or [])
    latest = turns[-1] if turns else None
    if isinstance(latest, dict):
        status = str(latest.get("status") or "")
        run_id = str(latest.get("run_id") or "").strip() or None
        if status == "success":
            status = "completed"
        if status == "failed" and str(latest.get("error_code") or "") == "SERVICE_RESTARTED":
            status = "interrupted"
        if status == "awaiting_input" and not latest.get("interaction_consumed_at"):
            return {
                "active_run_id": run_id,
                "active_run_status": status,
                "last_seq": _last_durable_seq(
                    getattr(session, "events", []) or [],
                    run_id=run_id,
                ),
                "pending_interaction": latest.get("pending_interaction"),
            }
        return {
            "active_run_id": None,
            "active_run_status": status or "idle",
            "last_seq": _last_durable_seq(getattr(session, "events", []) or []),
        }
    status = _last_turn_status(getattr(session, "events", []) or [])
    return {
        "active_run_id": None,
        "active_run_status": status or "idle",
        "last_seq": _last_durable_seq(getattr(session, "events", []) or []),
    }


async def _runtime_payloads_for_sessions(
    sessions,
    *,
    runtime,
    current_user: AuthUser,
    workspace_id: str,
) -> dict[str, dict]:
    manager = runtime.manager
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = await manager.list_runs(scope) if manager is not None else []
    live_by_session = {}
    for snapshot in snapshots:
        current = live_by_session.get(snapshot.session_id)
        if current is None or snapshot.turn_id > current.turn_id:
            live_by_session[snapshot.session_id] = snapshot

    def histories_by_session():
        _chat_store, execution_store = _get_user_stores()
        return {session.id: execution_store.get_session_history(session.id) for session in sessions}

    histories = await runtime.short_io.run(histories_by_session)
    return {
        session.id: _session_runtime_payload(
            session,
            history=histories.get(session.id),
            live_snapshot=live_by_session.get(session.id),
        )
        for session in sessions
    }


def _summary_payload(summary: SessionSummary) -> dict:
    payload = summary.to_dict()
    payload.pop("pinned", None)
    payload.pop("last_run_status", None)
    payload.pop("last_event_sequence", None)
    pending_interaction = payload.pop("pending_interaction", None)
    payload["active_run_status"] = summary.last_run_status
    payload["last_seq"] = summary.last_event_sequence
    if pending_interaction is not None:
        payload["pending_interaction"] = pending_interaction
    return payload


async def _indexed_session_payloads(
    summaries,
    *,
    runtime,
    current_user: AuthUser,
    workspace_id: str,
    chat_store,
    execution_store,
) -> list[dict]:
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = await runtime.manager.list_runs(scope) if runtime.manager is not None else []
    active_run_ids = _collect_active_run_ids(snapshots)
    live_by_session = {}
    for snapshot in snapshots:
        current = live_by_session.get(snapshot.session_id)
        if current is None or snapshot.turn_id > current.turn_id:
            live_by_session[snapshot.session_id] = snapshot

    recovery_candidates = [
        summary for summary in summaries if summary.active_run_id or summary.pending_interaction is not None
    ]

    def recover_page_candidates():
        recovered = {}
        for summary in recovery_candidates:
            session = _get_session_after_terminal_recovery(
                chat_store,
                execution_store,
                summary.session_id,
                active_run_ids,
            )
            if session is None:
                continue
            history = execution_store.get_session_history(summary.session_id)
            if history is not None:
                recovered[summary.session_id] = _session_runtime_payload(
                    session,
                    history=history,
                )
        return recovered

    recovered_payloads = await runtime.short_io.run(recover_page_candidates) if recovery_candidates else {}
    payloads = []
    for summary in summaries:
        payload = _summary_payload(summary)
        payload.update(recovered_payloads.get(summary.session_id, {}))
        live_snapshot = live_by_session.get(summary.session_id)
        if live_snapshot is not None:
            payload.update(
                {
                    "active_run_id": live_snapshot.run_id,
                    "active_run_status": live_snapshot.status.value,
                    "last_seq": max(
                        int(live_snapshot.last_seq),
                        int(payload.get("last_seq") or 0),
                    ),
                }
            )
            payload.pop("pending_interaction", None)
        payloads.append(payload)
    return payloads


def _bounded_authoritative_sessions(chat_store, *, requested_limit: int | None):
    """Read a bounded authoritative page while the summary index rebuilds."""

    read_limit = _SESSION_INDEX_FALLBACK_MAX
    if requested_limit is not None:
        read_limit = min(_SESSION_INDEX_FALLBACK_MAX, max(1, int(requested_limit)))
    read_limit += 1
    list_sessions = getattr(chat_store, "list_sessions", None)
    if not callable(list_sessions):
        raise RuntimeError("authoritative session listing is unavailable")
    sessions = list_sessions(limit=read_limit)
    sessions = list(sessions or [])
    return sessions[:read_limit - 1], len(sessions) >= read_limit


@sessions_router.get("/sessions")
async def list_sessions(
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
):
    """List all chat sessions."""
    if cursor and limit is None:
        return JSONResponse(
            {"error": "limit is required when cursor is provided"},
            status_code=400,
        )
    runtime = get_runtime_container(request)
    chat_store, execution_store = await runtime.short_io.run(_get_user_stores)
    summary_index = getattr(chat_store, "session_summary_index", None)
    if summary_index is not None:

        def read_index_page():
            load_persisted = getattr(
                summary_index,
                "load_persisted_for_read",
                None,
            )
            if callable(load_persisted):
                if not load_persisted():
                    request_rebuild = getattr(
                        summary_index,
                        "request_rebuild",
                        None,
                    )
                    if callable(request_rebuild):
                        request_rebuild()
                    raise RuntimeError("session summary index is rebuilding")
            elif summary_index.state != "ready":
                request_rebuild = getattr(summary_index, "request_rebuild", None)
                if callable(request_rebuild):
                    request_rebuild()
                raise RuntimeError("session summary index is rebuilding")
            if limit is None:
                return summary_index.list_all(), None
            current_page = summary_index.list_page(limit=limit, cursor=cursor)
            return current_page.items, current_page

        try:
            summaries, page = await runtime.short_io.run(read_index_page)
        except InvalidSessionCursor as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            # An empty store can publish an empty index immediately. This
            # keeps the first request fast while preserving the ready-state
            # contract for newly created workspaces.
            has_sessions = getattr(chat_store, "has_sessions", None)
            rebuild = getattr(summary_index, "rebuild", None)
            if callable(has_sessions) and callable(rebuild):
                try:
                    if not await runtime.short_io.run(has_sessions):
                        await runtime.short_io.run(rebuild)
                        summaries, page = await runtime.short_io.run(read_index_page)
                        sessions_payload = await _indexed_session_payloads(
                            summaries,
                            runtime=runtime,
                            current_user=current_user,
                            workspace_id=workspace_id,
                            chat_store=chat_store,
                            execution_store=execution_store,
                        )
                        response = {"sessions": sessions_payload}
                        if page is not None:
                            response.update(
                                {
                                    "next_cursor": page.next_cursor,
                                    "has_more": page.has_more,
                                    "index_revision": page.index_revision,
                                }
                            )
                        return response
                except Exception:
                    logger.exception("Empty session summary index recovery failed")
            # Keep the list usable while a cold, corrupt, or dirty projection
            # rebuilds in the background. The authoritative store read is
            # deliberately bounded so recovery cannot turn into an unbounded
            # request-path scan.
            try:
                sessions, truncated = await runtime.short_io.run(
                    _bounded_authoritative_sessions,
                    chat_store,
                    requested_limit=limit,
                )
                payloads = await _runtime_payloads_for_sessions(
                    sessions,
                    runtime=runtime,
                    current_user=current_user,
                    workspace_id=workspace_id,
                )
                response = {
                    "sessions": [
                        {
                            **session.to_dict(),
                            **payloads[session.id],
                        }
                        for session in sessions
                    ],
                    "index_status": "rebuilding",
                }
                if truncated:
                    response["index_fallback_truncated"] = True
                # An index cursor cannot be safely continued against a moving
                # authoritative store. Omit pagination metadata and let the
                # client retry once the status returns to ready.
                return response
            except Exception:
                logger.exception("Session summary index unavailable")
                return JSONResponse(
                    {"error": "session summary index unavailable"},
                    status_code=503,
                )
        else:
            sessions_payload = await _indexed_session_payloads(
                summaries,
                runtime=runtime,
                current_user=current_user,
                workspace_id=workspace_id,
                chat_store=chat_store,
                execution_store=execution_store,
            )
            response = {"sessions": sessions_payload}
            if page is not None:
                response.update(
                    {
                        "next_cursor": page.next_cursor,
                        "has_more": page.has_more,
                        "index_revision": page.index_revision,
                    }
                )
            return response

    sessions = await runtime.short_io.run(chat_store.list_sessions)
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = await runtime.manager.list_runs(scope) if runtime.manager is not None else []
    active_run_ids = _collect_active_run_ids(snapshots)

    def recover_sessions() -> None:
        current_chat_store, execution_store = _get_user_stores()
        for current in sessions:
            _get_session_after_terminal_recovery(
                current_chat_store,
                execution_store,
                current.id,
                active_run_ids,
            )

    await runtime.short_io.run(recover_sessions)
    sessions = await runtime.short_io.run(chat_store.list_sessions)
    payloads = await _runtime_payloads_for_sessions(
        sessions,
        runtime=runtime,
        current_user=current_user,
        workspace_id=workspace_id,
    )

    return {
        "sessions": [
            {
                **s.to_dict(),
                **payloads[s.id],
            }
            for s in sessions
        ]
    }


@sessions_router.post("/sessions")
async def create_session(
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """
    Create a new chat session.

    Request body:
        {
            "title": "Optional session title",
            "agent_mode": "quick|thinking|multi_agent",
            "llm_request_mode": "stream|sync",
            "selected_ontology_id": "Optional enabled ontology id"
        }
    """
    data = await read_json_object(request)
    runtime = get_runtime_container(request)
    title = data.get("title")
    agent_mode = resolve_agent_mode({"agent_mode": data.get("agent_mode")})
    llm_request_mode = normalize_llm_request_mode(data.get("llm_request_mode"))
    model_profile_id = str(data.get("model_profile_id") or "").strip()
    try:
        (
            selected_ontology_id,
            selected_ontology_version,
            selected_ontology_name,
        ) = await runtime.short_io.run(
            _resolve_selected_ontology_selection,
            data.get("selected_ontology_id"),
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    try:
        selected_profile = await runtime.short_io.run(
            resolve_effective_llm_profile_config,
            model_profile_id or None,
            allow_default_fallback=not bool(model_profile_id),
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    metadata = {
        "agent_mode": agent_mode,
        "llm_request_mode": llm_request_mode,
        "model_profile_id": selected_profile["id"],
        "model_profile_ref": selected_profile.get("model_profile_ref"),
    }
    if selected_ontology_id:
        metadata["selected_ontology_id"] = selected_ontology_id
        metadata["selected_ontology_version"] = selected_ontology_version
        metadata["selected_ontology_name"] = selected_ontology_name

    prune_batch_size = 25

    def prune_batch(batch_size):
        from api.routes import prune_request_sessions_batch

        return prune_request_sessions_batch(
            max_sessions=LimitsConfig.from_env().session_max_per_user_workspace,
            batch_size=batch_size,
        )

    def create_and_prune():
        chat_store, _execution_store = _get_user_stores()
        return chat_store.create_session(
            title=title,
            metadata=metadata,
        )

    session = await runtime.short_io.run(create_and_prune)
    supervisor = getattr(runtime, "supervisor", None)
    start_derived = getattr(supervisor, "start_derived", None)
    if callable(start_derived):
        operation = _converge_session_prune_batches(
            runtime,
            prune_batch,
            batch_size=prune_batch_size,
            convergence_key=(workspace_id, current_user.username),
        )
        try:
            start_derived(
                operation,
                name="session-prune-convergence-{}".format(session.id),
            )
        except Exception:
            operation.close()
            logger.warning("Could not schedule session prune convergence", exc_info=True)

    return JSONResponse(session.to_dict(), status_code=201)


@sessions_router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """Get a chat session with message history."""
    runtime = get_runtime_container(request)
    chat_store, execution_store = await runtime.short_io.run(_get_user_stores)
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = await runtime.manager.list_runs(scope) if runtime.manager is not None else []
    active_run_ids = _collect_active_run_ids(snapshots)
    session = await runtime.short_io.run(
        _get_session_after_terminal_recovery,
        chat_store,
        execution_store,
        session_id,
        active_run_ids,
    )

    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    if "since" in request.query_params:
        return JSONResponse(
            {
                "error": "Session delta polling has been removed; reconnect through the run event stream",
            },
            status_code=410,
        )

    payloads = await _runtime_payloads_for_sessions(
        [session],
        runtime=runtime,
        current_user=current_user,
        workspace_id=workspace_id,
    )
    return {
        **session.to_dict(include_messages=True),
        **payloads[session.id],
    }


@sessions_router.get(
    "/sessions/{session_id}/runs/{run_id}/tool-outputs/{output_ref}",
)
async def get_tool_output(
    session_id: str,
    run_id: str,
    output_ref: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    """Read a complete tool output within the authenticated session scope."""
    runtime = get_runtime_container(request)
    chat_store, _execution_store = await runtime.short_io.run(_get_user_stores)
    if not await runtime.short_io.run(chat_store.get_session, session_id):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    try:
        artifact = await runtime.short_io.run(
            chat_store.get_tool_output_file,
            session_id,
            run_id,
            output_ref,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not artifact:
        return JSONResponse({"error": "Tool output not found"}, status_code=404)

    response = FileResponse(
        path=artifact["artifact_path"],
        media_type=artifact["response_content_type"],
        headers={
            "Cache-Control": "no-store",
            "X-Tool-Output-Ref": artifact["output_ref"],
            "X-Tool-Call-Id": artifact.get("tool_call_id") or "",
            "X-Tool-Output-Size": str(artifact["output_size"]),
            "X-Tool-Output-SHA256": str(artifact.get("sha256") or ""),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Access-Control-Expose-Headers": (
                "X-Tool-Output-Ref, X-Tool-Call-Id, X-Tool-Output-Size, X-Tool-Output-SHA256"
            ),
        },
    )
    return response


@sessions_router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    """Update user-facing session attributes."""
    data = await read_optional_json_object(request)
    if "pinned" not in data:
        return JSONResponse({"error": "pinned is required"}, status_code=400)

    pinned = data.get("pinned")
    if not isinstance(pinned, bool):
        return JSONResponse(
            {"error": "pinned must be a boolean"},
            status_code=400,
        )

    def set_pinned():
        chat_store, _execution_store = _get_user_stores()
        return chat_store.set_session_pinned(session_id, pinned)

    session = await get_runtime_container(request).short_io.run(set_pinned)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return session.to_dict()


@sessions_router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """Delete a chat session."""
    runtime = get_runtime_container(request)
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:run",),
    )
    try:
        async with runtime.manager.session_guard(scope, session_id):
            await runtime.manager.assert_session_available(scope, session_id)

            def delete_from_stores():
                chat_store, execution_store = _get_user_stores()
                return (
                    chat_store.delete_session(session_id),
                    execution_store.delete_session(session_id),
                )

            chat_deleted, execution_deleted = await runtime.short_io.run(delete_from_stores)
    except SessionBusyError as exc:
        return JSONResponse(
            {
                "error": "Session has an inflight Run",
                "error_code": "SESSION_RUN_ACTIVE",
                "session_id": session_id,
                "run_id": exc.run_id,
            },
            status_code=409,
        )

    if not chat_deleted and not execution_deleted:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return {"success": True}


@sessions_router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    request: Request,
    limit: int | None = Query(default=None),
    _workspace_id: str = Depends(require_workspace_id),
):
    """Get messages for a session."""

    runtime = get_runtime_container(request)
    chat_store, _execution_store = await runtime.short_io.run(_get_user_stores)
    messages = await runtime.short_io.run(
        chat_store.get_history,
        session_id,
        limit=limit,
    )

    if not messages and not await runtime.short_io.run(
        chat_store.get_session,
        session_id,
    ):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return {"messages": [m.to_dict() for m in messages]}


@sessions_router.post(
    "/sessions/{session_id}/interactions/{interaction_id}/cancel",
)
async def cancel_session_interaction(
    session_id: str,
    interaction_id: str,
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """Cancel one pending interaction while preserving the conversation context."""
    if not str(request.headers.get("Idempotency-Key") or "").strip():
        return JSONResponse(
            {
                "error": "Idempotency-Key is required",
                "error_code": "INVALID_ARGUMENT",
            },
            status_code=400,
        )
    runtime = get_runtime_container(request)
    chat_store, execution_store = await runtime.short_io.run(_get_user_stores)
    if not await runtime.short_io.run(chat_store.get_session, session_id):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:run",),
    )
    try:
        async with runtime.manager.session_guard(scope, session_id):
            await runtime.manager.assert_session_available(scope, session_id)
            cancellation = await runtime.short_io.run(
                execution_store.cancel_pending_interaction,
                session_id,
                interaction_id,
            )
    except InteractionCheckpointError as exc:
        code = str(getattr(exc, "code", "INTERACTION_CONFLICT"))
        payload = {"error": str(exc), "error_code": code}
        existing_run_id = getattr(exc, "existing_run_id", None)
        if existing_run_id:
            payload["run_id"] = existing_run_id
        return JSONResponse(
            payload,
            status_code=404 if code == "INTERACTION_NOT_FOUND" else 409,
        )
    except SessionBusyError as exc:
        return JSONResponse(
            {
                "error": "Session has an inflight Run",
                "error_code": "SESSION_BUSY",
                "run_id": exc.run_id,
            },
            status_code=409,
        )

    recorded = await runtime.short_io.run(
        chat_store.record_interaction_cancellation,
        session_id,
        interaction_id,
        INTERACTION_CANCELLED_NOTICE,
        cancelled_at=cancellation["cancelled_at"],
    )
    if not recorded:
        logger.error(
            "Failed to persist interaction cancellation for session=%s interaction=%s",
            session_id,
            interaction_id,
        )
        return JSONResponse(
            {"error": "Failed to persist interaction cancellation"},
            status_code=500,
        )

    return {
        "status": "cancelled",
        **cancellation,
    }


@sessions_router.get("/sessions/{session_id}/turns")
async def list_session_turns(
    session_id: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    """List durable turn records with optional status and feedback filters."""

    runtime = get_runtime_container(request)
    chat_store, execution_store = await runtime.short_io.run(_get_user_stores)
    if not await runtime.short_io.run(chat_store.get_session, session_id):
        return JSONResponse({"error": "Session not found"}, status_code=404)

    status = str(request.query_params.get("status") or "").strip() or None
    turns = await runtime.short_io.run(
        execution_store.list_turns_by_status,
        session_id,
        status,
    )
    if _is_truthy(request.query_params.get("has_feedback")):
        feedback_turns = []
        for turn in turns:
            if isinstance(turn.get("feedback"), dict) and bool(turn["feedback"].get("vote")):
                feedback_turns.append(turn)
        turns = feedback_turns
    return {"turns": turns, "count": len(turns)}


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@sessions_router.post("/sessions/{session_id}/generate-title")
async def generate_session_title(
    session_id: str,
    request: Request,
    _workspace_id: str = Depends(require_workspace_id),
):
    """Generate a title for a chat session based on user message."""
    data = await read_json_object(request)
    message = data.get("message", "").strip()

    if not message:
        return JSONResponse({"error": "Message is required"}, status_code=400)

    runtime = get_runtime_container(request)
    chat_store, _execution_store = await runtime.short_io.run(_get_user_stores)
    # Check if session exists
    session = await runtime.short_io.run(chat_store.get_session, session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    # Generate title using LLM
    try:
        prompt = "请用简短的中文（不超过15个字）总结以下用户问题的主题，直接输出标题，不要任何解释或标点符号：\n\n{}"

        response = await runtime.external_io.run(
            get_llm_router().call,
            purpose=LLMPurpose.TASK,
            messages=[{"role": "user", "content": prompt.format(message)}],
            temperature=0.5,
            max_tokens=50,
            enable_reasoning=False,
        )

        title = response.content.strip()
        # Clean up title - remove quotes, punctuation at the end
        title = title.strip('"\'""')
        if len(title) > 30:
            title = title[:30] + "..."

        # Update session title
        await runtime.short_io.run(
            chat_store.update_session_title,
            session_id,
            title,
        )

        logger.info("Generated title for session %s: %s", session_id, title)

        return {"title": title, "session_id": session_id}

    except Exception:
        logger.exception("Failed to generate title")
        # Fallback: use truncated message as title
        fallback_title = message[:20] + ("..." if len(message) > 20 else "")
        await runtime.short_io.run(
            chat_store.update_session_title,
            session_id,
            fallback_title,
        )

        return {"title": fallback_title, "session_id": session_id, "fallback": True}
