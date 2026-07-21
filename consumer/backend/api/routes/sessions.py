"""Chat session endpoints: CRUD, messages, title generation."""

from flask import Blueprint, request, jsonify, send_file
from flask_cors import cross_origin

from api.auth.auth import require_auth, get_current_user
from llm import LLMPurpose, get_llm_router
from config import LimitsConfig
from config.system_config import SystemConfigValidationError, resolve_effective_llm_profile_config
from agent.core.shared.modes import resolve_agent_mode
from ontology.ontologies import get_ontology_registry
from utils.logging import get_logger
from utils.identifiers import validate_session_id
from llm.llm_request_mode import normalize_llm_request_mode
from persistence.execution_store import (
    INTERACTION_CANCELLED_NOTICE,
    InteractionCheckpointError,
)
from workspace.workspaces import workspace_scope

logger = get_logger("routes.sessions")

sessions_bp = Blueprint("sessions", __name__, url_prefix="/agentic")


@sessions_bp.before_request
def _validate_session_path_parameter():
    session_id = (request.view_args or {}).get("session_id")
    if session_id is None:
        return None
    try:
        request.view_args["session_id"] = validate_session_id(session_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return None


def _get_orchestrator():
    from api.routes import get_orchestrator
    return get_orchestrator()


def _get_user_stores():
    from api.routes import get_request_stores
    return get_request_stores()


def _get_request_workspace_id():
    from api.routes import get_request_workspace_id
    return get_request_workspace_id()


def _get_run_registry():
    from api.routes.workflows import get_run_registry
    return get_run_registry()


def _resolve_selected_ontology_id(value, *, workspace_id: str):
    """Validate and canonicalize the optional session ontology selection."""
    selected_ontology_id = str(value or "").strip()
    if not selected_ontology_id:
        return None

    with workspace_scope(workspace_id):
        registry = get_ontology_registry()
        ontology = registry.get_ontology(selected_ontology_id)

    if ontology is None:
        raise ValueError("知识包不存在: {}".format(selected_ontology_id))
    if not bool(getattr(ontology, "enabled", False)):
        raise ValueError("知识包已禁用: {}".format(selected_ontology_id))
    return str(getattr(ontology, "id", selected_ontology_id) or selected_ontology_id)


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


def _last_durable_seq(events) -> int:
    last_seq = 0
    for event in events or []:
        if not _is_durable_agent_event(event):
            continue
        last_seq = max(last_seq, _coerce_seq((event.get("data") or {}).get("seq")))
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


def _handle_matches_scope(handle, *, user_id: str, workspace_id: str) -> bool:
    return (
        handle is not None
        and bool(user_id)
        and bool(workspace_id)
        and getattr(handle, "user_id", None) == user_id
        and getattr(handle, "workspace_id", None) == workspace_id
    )


def _run_matches_scope(run, *, user_id: str, workspace_id: str) -> bool:
    metadata = dict(getattr(run, "metadata", {}) or {})
    return (
        run is not None
        and bool(user_id)
        and bool(workspace_id)
        and metadata.get("user_id") == user_id
        and metadata.get("workspace_id") == workspace_id
    )


def _session_runtime_payload(
    session,
    handle=None,
    *,
    user_id: str,
    workspace_id: str,
) -> dict:
    active_run_id = str(getattr(session, "active_run_id", "") or "").strip()
    if not _handle_matches_scope(handle, user_id=user_id, workspace_id=workspace_id):
        handle = None
    if handle is None:
        registry = _get_run_registry()
        active_handle = registry.get(active_run_id) if active_run_id else None
        if not _handle_matches_scope(
            active_handle,
            user_id=user_id,
            workspace_id=workspace_id,
        ):
            active_handle = None
        handle = active_handle or registry.find_by_session(
            session.id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
    if handle is not None:
        runtime = handle.runtime_snapshot()
        status = runtime.get("status") or "running"
        if not handle.done.is_set():
            status = "running"
        return {
            "active_run_id": runtime.get("active_run_id") or active_run_id,
            "active_run_status": status,
            "last_seq": runtime.get("last_seq", 0),
        }
    if active_run_id:
        try:
            run = _get_orchestrator().run_store.get_run(active_run_id)
        except RuntimeError:
            # Session reads can run in isolated tests or maintenance contexts
            # where the process-wide orchestrator has not been initialized.
            run = None
        if not _run_matches_scope(run, user_id=user_id, workspace_id=workspace_id):
            run = None
        run_status = str(getattr(getattr(run, "status", None), "value", "") or "").strip()
        if run_status in {"queued", "running"}:
            return {
                "active_run_id": active_run_id,
                "active_run_status": run_status,
                "last_seq": _last_durable_seq(getattr(session, "events", []) or []),
            }
        return {
            "active_run_id": active_run_id,
            "active_run_status": "interrupted",
            "last_seq": _last_durable_seq(getattr(session, "events", []) or []),
        }
    status = _last_turn_status(getattr(session, "events", []) or [])
    return {
        "active_run_id": None,
        "active_run_status": status or "idle",
        "last_seq": _last_durable_seq(getattr(session, "events", []) or []),
    }


@sessions_bp.route("/sessions", methods=["GET"])
@require_auth()
def list_sessions():
    """List all chat sessions."""
    current_user = get_current_user()
    try:
        workspace_id = _get_request_workspace_id()
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user_id = current_user.username if current_user else ""
    chat_store, _execution_store = _get_user_stores()
    sessions = chat_store.list_sessions()
    # One locked pass over the registry, then O(1) lookups per session —
    # avoids an O(runs) scan (and lock acquire) for every session in the list.
    handles_by_session = _get_run_registry().snapshot_by_session(
        user_id=user_id,
        workspace_id=workspace_id,
    )

    return jsonify({
        "sessions": [
            {
                **s.to_dict(),
                **_session_runtime_payload(
                    s,
                    handles_by_session.get(s.id),
                    user_id=user_id,
                    workspace_id=workspace_id,
                ),
            }
            for s in sessions
        ]
    })


@sessions_bp.route("/sessions", methods=["POST"])
@require_auth()
def create_session():
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
    data = request.get_json() or {}
    title = data.get("title")
    agent_mode = resolve_agent_mode({"agent_mode": data.get("agent_mode")})
    llm_request_mode = normalize_llm_request_mode(data.get("llm_request_mode"))
    model_profile_id = str(data.get("model_profile_id") or "").strip()
    try:
        workspace_id = _get_request_workspace_id()
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    try:
        selected_ontology_id = _resolve_selected_ontology_id(
            data.get("selected_ontology_id"),
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    try:
        selected_profile = resolve_effective_llm_profile_config(
            model_profile_id or None,
            allow_default_fallback=not bool(model_profile_id),
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400

    chat_store, execution_store = _get_user_stores()
    metadata = {
        "agent_mode": agent_mode,
        "llm_request_mode": llm_request_mode,
        "model_profile_id": selected_profile["id"],
        "model_profile_ref": selected_profile.get("model_profile_ref"),
    }
    if selected_ontology_id:
        metadata["selected_ontology_id"] = selected_ontology_id
    session = chat_store.create_session(
        title=title,
        metadata=metadata,
    )
    for deleted_session_id in chat_store.prune_old_sessions(
        LimitsConfig.from_env().session_max_per_user_workspace
    ):
        execution_store.delete_session(deleted_session_id)

    return jsonify(session.to_dict()), 201


@sessions_bp.route("/sessions/<session_id>", methods=["GET"])
@require_auth()
def get_session(session_id: str):
    """Get a chat session with message history."""
    current_user = get_current_user()
    try:
        workspace_id = _get_request_workspace_id()
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user_id = current_user.username if current_user else ""
    chat_store, _execution_store = _get_user_stores()
    session = chat_store.get_session(session_id)

    if not session:
        return jsonify({"error": "Session not found"}), 404

    if request.args.get("since") is not None:
        return jsonify({
            "error": "Session delta polling has been removed; reconnect through the run event stream",
        }), 410

    return jsonify({
        **session.to_dict(include_messages=True),
        **_session_runtime_payload(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
        ),
    })


@sessions_bp.route(
    "/sessions/<session_id>/runs/<run_id>/tool-outputs/<output_ref>",
    methods=["GET"],
)
@require_auth()
def get_tool_output(session_id: str, run_id: str, output_ref: str):
    """Read a complete tool output within the authenticated session scope."""
    try:
        _get_request_workspace_id()
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    chat_store, _execution_store = _get_user_stores()
    if not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    try:
        artifact = chat_store.get_tool_output_file(session_id, run_id, output_ref)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not artifact:
        return jsonify({"error": "Tool output not found"}), 404

    response = send_file(
        artifact["artifact_path"],
        mimetype=artifact["response_content_type"],
        as_attachment=False,
        conditional=False,
        etag=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Tool-Output-Ref"] = artifact["output_ref"]
    response.headers["X-Tool-Call-Id"] = artifact.get("tool_call_id") or ""
    response.headers["X-Tool-Output-Size"] = str(artifact["output_size"])
    return response


@sessions_bp.route("/sessions/<session_id>", methods=["PATCH"])
@require_auth()
def update_session(session_id: str):
    """Update user-facing session attributes."""
    data = request.get_json(silent=True) or {}
    if "pinned" not in data:
        return jsonify({"error": "pinned is required"}), 400

    pinned = data.get("pinned")
    if not isinstance(pinned, bool):
        return jsonify({"error": "pinned must be a boolean"}), 400

    chat_store, _execution_store = _get_user_stores()
    session = chat_store.set_session_pinned(session_id, pinned)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    return jsonify(session.to_dict())


@sessions_bp.route("/sessions/<session_id>", methods=["DELETE"])
@require_auth()
def delete_session(session_id: str):
    """Delete a chat session."""
    current_user = get_current_user()
    chat_store, execution_store = _get_user_stores()
    chat_deleted = chat_store.delete_session(session_id)
    execution_deleted = execution_store.delete_session(session_id)

    if not chat_deleted and not execution_deleted:
        return jsonify({"error": "Session not found"}), 404

    return jsonify({"success": True})


@sessions_bp.route("/sessions/<session_id>/messages", methods=["GET"])
@require_auth()
def get_session_messages(session_id: str):
    """Get messages for a session."""
    limit = request.args.get("limit", type=int)

    chat_store, _execution_store = _get_user_stores()
    messages = chat_store.get_history(session_id, limit=limit)

    if not messages and not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    return jsonify({
        "messages": [m.to_dict() for m in messages]
    })


@sessions_bp.route(
    "/sessions/<session_id>/interactions/<interaction_id>/cancel",
    methods=["POST"],
)
@require_auth()
def cancel_session_interaction(session_id: str, interaction_id: str):
    """Cancel one pending interaction while preserving the conversation context."""
    chat_store, execution_store = _get_user_stores()
    if not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    try:
        cancellation = execution_store.cancel_pending_interaction(
            session_id,
            interaction_id,
        )
    except InteractionCheckpointError as exc:
        return jsonify({"error": str(exc)}), 409

    recorded = chat_store.record_interaction_cancellation(
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
        return jsonify({"error": "Failed to persist interaction cancellation"}), 500

    return jsonify({
        "status": "cancelled",
        **cancellation,
    })


@sessions_bp.route("/sessions/<session_id>/turns", methods=["GET"])
@require_auth()
def list_session_turns(session_id: str):
    """List durable turn records with optional status and feedback filters."""

    chat_store, execution_store = _get_user_stores()
    if not chat_store.get_session(session_id):
        return jsonify({"error": "Session not found"}), 404

    status = str(request.args.get("status") or "").strip() or None
    turns = execution_store.list_turns_by_status(session_id, status)
    if _is_truthy(request.args.get("has_feedback")):
        turns = [
            turn
            for turn in turns
            if isinstance(turn.get("feedback"), dict)
            and bool(turn["feedback"].get("vote"))
        ]
    return jsonify({"turns": turns, "count": len(turns)})


def _is_truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@sessions_bp.route("/sessions/<session_id>/generate-title", methods=["POST", "OPTIONS"])
@cross_origin()
@require_auth()
def generate_session_title(session_id: str):
    """Generate a title for a chat session based on user message."""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"})

    data = request.get_json() or {}
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "Message is required"}), 400

    chat_store, _execution_store = _get_user_stores()
    # Check if session exists
    session = chat_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    # Generate title using LLM
    try:
        prompt = "请用简短的中文（不超过15个字）总结以下用户问题的主题，直接输出标题，不要任何解释或标点符号：\n\n{}"

        response = get_llm_router().call(
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
        chat_store.update_session_title(session_id, title)

        logger.info("Generated title for session {}: {}".format(session_id, title))

        return jsonify({
            "title": title,
            "session_id": session_id
        })

    except Exception as e:
        logger.exception("Failed to generate title")
        # Fallback: use truncated message as title
        fallback_title = message[:20] + ("..." if len(message) > 20 else "")
        chat_store.update_session_title(session_id, fallback_title)

        return jsonify({
            "title": fallback_title,
            "session_id": session_id,
            "fallback": True
        })
