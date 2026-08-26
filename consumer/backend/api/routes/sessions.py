"""Chat session endpoints: CRUD, messages, title generation."""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import get_runtime_container, require_workspace_id
from api.exceptions import APIError
from api.request_parsing import read_json_object, read_optional_json_object
from llm import LLMPurpose, get_llm_router
from config import LimitsConfig
from config.system_config import SystemConfigValidationError, resolve_effective_llm_profile_config
from agent.runtime.contracts import RunScope, TERMINAL_RUN_STATUSES
from agent.runtime.errors import SessionBusyError
from agent.core.shared.modes import resolve_agent_mode
from ontology.ontologies import get_ontology_registry
from utils.logging import get_logger
from utils.identifiers import validate_session_id
from llm.llm_request_mode import normalize_llm_request_mode
from persistence.execution_store import (
    INTERACTION_CANCELLED_NOTICE,
    InteractionCheckpointError,
)
from persistence.deterministic_terminal import (
    recover_pending_deterministic_artifacts,
    recover_pending_deterministic_terminal,
)
from workspace.workspaces import workspace_scope

logger = get_logger("routes.sessions")

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
    if not callable(
        getattr(execution_store, "deterministic_terminal_transaction", None)
    ):
        return session
    if recover_pending_deterministic_terminal(
        execution_store,
        chat_store,
        session_id,
    ):
        session = chat_store.get_session(session_id)
    if callable(
        getattr(execution_store, "get_pending_deterministic_artifacts", None)
    ):
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
    return (
        isinstance(data, dict)
        and event.get("event") == "agent.v2"
        and data.get("protocol") == "agent-stream.v2"
    )


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
        if (
            status == "failed"
            and str(latest.get("error_code") or "") == "SERVICE_RESTARTED"
        ):
            status = "interrupted"
        if (
            status == "awaiting_input"
            and not latest.get("interaction_consumed_at")
        ):
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
            "last_seq": _last_durable_seq(
                getattr(session, "events", []) or []
            ),
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

    _chat_store, execution_store = _get_user_stores()

    def histories_by_session():
        return {
            session.id: execution_store.get_session_history(session.id)
            for session in sessions
        }

    histories = await runtime.short_io.run(histories_by_session)
    return {
        session.id: _session_runtime_payload(
            session,
            history=histories.get(session.id),
            live_snapshot=live_by_session.get(session.id),
        )
        for session in sessions
    }


@sessions_router.get("/sessions")
async def list_sessions(
    request: Request,
    current_user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    """List all chat sessions."""
    runtime = get_runtime_container(request)
    chat_store, _execution_store = _get_user_stores()
    sessions = await runtime.short_io.run(chat_store.list_sessions)
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = (
        await runtime.manager.list_runs(scope)
        if runtime.manager is not None
        else []
    )
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

    def create_and_prune():
        chat_store, execution_store = _get_user_stores()
        session = chat_store.create_session(
            title=title,
            metadata=metadata,
        )
        for deleted_session_id in chat_store.prune_old_sessions(
            LimitsConfig.from_env().session_max_per_user_workspace
        ):
            execution_store.delete_session(deleted_session_id)
        return session

    session = await runtime.short_io.run(create_and_prune)

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
    chat_store, execution_store = _get_user_stores()
    scope = RunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:read",),
    )
    snapshots = (
        await runtime.manager.list_runs(scope)
        if runtime.manager is not None
        else []
    )
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
                "error": (
                    "Session delta polling has been removed; reconnect through "
                    "the run event stream"
                ),
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
                "X-Tool-Output-Ref, X-Tool-Call-Id, "
                "X-Tool-Output-Size, X-Tool-Output-SHA256"
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

            chat_deleted, execution_deleted = await runtime.short_io.run(
                delete_from_stores
            )
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

    return {
        "messages": [m.to_dict() for m in messages]
    }


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
    chat_store, execution_store = _get_user_stores()
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
        turns = [
            turn
            for turn in turns
            if isinstance(turn.get("feedback"), dict)
            and bool(turn["feedback"].get("vote"))
        ]
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
        title = title.strip('"\'""''')
        if len(title) > 30:
            title = title[:30] + "..."

        # Update session title
        await runtime.short_io.run(
            chat_store.update_session_title,
            session_id,
            title,
        )

        logger.info("Generated title for session {}: {}".format(session_id, title))

        return {
            "title": title,
            "session_id": session_id
        }

    except Exception:
        logger.exception("Failed to generate title")
        # Fallback: use truncated message as title
        fallback_title = message[:20] + ("..." if len(message) > 20 else "")
        await runtime.short_io.run(
            chat_store.update_session_title,
            session_id,
            fallback_title,
        )

        return {
            "title": fallback_title,
            "session_id": session_id,
            "fallback": True
        }
