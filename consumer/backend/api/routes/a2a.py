"""A2A adapter endpoints (agent card, task CRUD, and task event stream)."""

from __future__ import annotations

import json
import queue
from typing import Any, Dict

from flask import Blueprint, Response, jsonify, request

from agent.core.shared.interaction_policy import set_user_interaction_capability
from api.protocols.a2a.mapper import map_public_event_to_a2a_update
from api.protocols.a2a.models import A2ATaskCreateRequest
from api.protocols.a2a.service import A2ATaskService
from api.auth.auth import get_current_user, require_auth
from utils.identifiers import validate_session_id
from utils.logging import get_logger
from agent.runtime.errors import EventReplayUnavailableError, RunConflictError
from agent.runtime.event_bus import STREAM_CLOSED, STREAM_RECONNECT


logger = get_logger("routes.a2a")

a2a_bp = Blueprint("a2a", __name__)


def _get_orchestrator():
    from api.routes import get_orchestrator

    return get_orchestrator()


def _get_user_stores():
    from api.routes import get_request_stores

    return get_request_stores()


def _get_request_workspace_id():
    from api.routes import get_request_workspace_id

    return get_request_workspace_id()


def _build_task_service() -> A2ATaskService:
    orchestrator = _get_orchestrator()
    chat_store, _execution_store = _get_user_stores()
    current_user = get_current_user()
    workspace_id = _get_request_workspace_id()

    from agent.runtime.bootstrap import (
        async_manager_matches,
        call_async_run,
        get_async_run_manager,
    )

    if not async_manager_matches(orchestrator):
        raise RuntimeError("A2A async runtime is not bound to the current orchestrator")
    async_manager = get_async_run_manager()
    async_call = call_async_run
    return A2ATaskService(
        chat_store=chat_store,
        user_id=(current_user.username if current_user else None),
        workspace_id=workspace_id,
        async_manager=async_manager,
        async_call=async_call,
    )


def _task_access_matches(
    service: A2ATaskService,
    task_id: str,
    *,
    session_id: str | None = None,
) -> bool:
    """Bind a task lookup to its persisted owner and workspace."""
    try:
        from agent.runtime.contracts import RunScope

        snapshot = service.async_call(
            service.async_manager.get_run(
                task_id,
                RunScope(
                    username=str(service.user_id or ""),
                    workspace_id=str(service.workspace_id or ""),
                    scopes=("agent:read",),
                ),
            )
        )
        return snapshot is not None and (
            not session_id or snapshot.session_id == session_id
        )
    except Exception:
        logger.debug("A2A async task ownership lookup failed", exc_info=True)
        return False


def _extract_message(payload: Dict[str, Any]) -> str:
    message = str(payload.get("message") or "").strip()
    if message:
        return message

    # Soft-compatible shape: input.parts[0].text
    input_payload = payload.get("input")
    if isinstance(input_payload, dict):
        parts = input_payload.get("parts")
        if isinstance(parts, list) and parts:
            first = parts[0] or {}
            if isinstance(first, dict):
                text = str(first.get("text") or "").strip()
                if text:
                    return text
    return ""


def _event_cursor() -> int:
    raw = request.headers.get("Last-Event-ID") or request.args.get("after_seq") or 0
    try:
        cursor = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("after_seq must be a non-negative integer") from exc
    if cursor < 0:
        raise ValueError("after_seq must be a non-negative integer")
    return cursor


def _encode_async_task_update(event) -> str:
    update = map_public_event_to_a2a_update(event)
    return "id: {}\nevent: task.update\ndata: {}\n\n".format(
        event.seq,
        json.dumps(update, ensure_ascii=False),
    )


@a2a_bp.route("/.well-known/a2a/agent-card.json", methods=["GET"])
def get_agent_card():
    """Public agent discovery endpoint."""
    base_url = str(request.host_url or "").rstrip("/")
    card = {
        "name": "onto-agent",
        "description": "A2A adapter over agent-stream.v2 runtime",
        "version": "0.1.0",
        "url": base_url,
        "skills": [
            "ontology_context",
            "skill",
            "business_data_search",
            "execute_script",
            "spawn_sub_agent",
            "memory",
        ],
        "capabilities": {
            "streaming": True,
            "cancellation": True,
        },
        "endpoints": {
            "create_task": "/a2a/tasks",
            "get_task": "/a2a/tasks/{task_id}",
            "cancel_task": "/a2a/tasks/{task_id}",
            "task_events": "/a2a/tasks/{task_id}/events",
            "stream_create": "/a2a/tasks/stream",
        },
        "authentication": {
            "type": "bearer",
            "notes": "Same JWT auth as /agentic/* endpoints",
        },
    }
    return jsonify(card)


@a2a_bp.route("/a2a/tasks", methods=["POST"])
@require_auth()
def create_task():
    payload = request.get_json(silent=True) or {}
    message = _extract_message(payload)
    if not message:
        return jsonify({"error": "message is required"}), 400

    req = A2ATaskCreateRequest(
        message=message,
        session_id=payload.get("session_id") or None,
        task_id=str(payload.get("task_id") or "").strip() or None,
        context=set_user_interaction_capability(
            dict(payload.get("context") or {}),
            supported=False,
        ),
    )

    service = _build_task_service()
    try:
        snapshot = service.create_task(req)
    except RunConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"task": snapshot.to_dict()}), 202


@a2a_bp.route("/a2a/tasks/<task_id>", methods=["GET"])
@require_auth()
def get_task(task_id: str):
    service = _build_task_service()
    session_id = str(request.args.get("session_id") or "").strip() or None
    try:
        snapshot = service.get_task(task_id, session_id=session_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if snapshot.state.value == "not_found":
        return jsonify({"task": snapshot.to_dict()}), 404
    if not _task_access_matches(service, task_id, session_id=session_id):
        return jsonify({"error": "Task not found"}), 404
    return jsonify({"task": snapshot.to_dict()})


@a2a_bp.route("/a2a/tasks/<task_id>", methods=["DELETE"])
@require_auth()
def cancel_task(task_id: str):
    service = _build_task_service()
    snapshot = service.get_task(task_id)
    if snapshot.state.value == "not_found" or not _task_access_matches(service, task_id):
        return jsonify({"error": "Task not found"}), 404
    ok = service.cancel_task(task_id)
    if not ok:
        return jsonify({"error": "Task not found"}), 404
    snapshot = service.get_task(task_id)
    return jsonify({"success": True, "task": snapshot.to_dict()})


@a2a_bp.route("/a2a/tasks/<task_id>/events", methods=["GET"])
@require_auth()
def replay_task_events(task_id: str):
    """Replay persisted task events from chat session storage as A2A updates."""
    service = _build_task_service()
    snapshot = service.get_task(task_id)
    if snapshot.state.value == "not_found":
        return jsonify({"error": "Task not found"}), 404
    session_id = str(request.args.get("session_id") or "").strip() or None
    if session_id:
        try:
            session_id = validate_session_id(session_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if not session_id:
        session_id = snapshot.session_id
        if not session_id:
            return jsonify({"error": "session_id is required"}), 400
    if not _task_access_matches(service, task_id, session_id=session_id):
        return jsonify({"error": "Task not found"}), 404

    from agent.runtime.contracts import RunScope

    try:
        after_seq = _event_cursor()
        subscription = service.async_call(
            service.async_manager.subscribe(
                task_id,
                RunScope(
                    username=str(service.user_id or ""),
                    workspace_id=str(service.workspace_id or ""),
                    scopes=("agent:read",),
                ),
                after_seq=after_seq,
            )
        )
    except RunConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except EventReplayUnavailableError:
        return jsonify({"error": "Task event stream is no longer available"}), 410
    except Exception:
        logger.exception("A2A task event subscription failed: %s", task_id)
        return jsonify({"error": "Task not found"}), 404

    def generate_async_events():
        try:
            for event in subscription.replay:
                yield _encode_async_task_update(event)
                if event.event_type == "turn.finished":
                    return
            if subscription.closed:
                return
            while True:
                try:
                    item = subscription.queue.get(timeout=15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is STREAM_CLOSED or item is STREAM_RECONNECT:
                    return
                if not hasattr(item, "event_type"):
                    return
                yield _encode_async_task_update(item)
                if item.event_type == "turn.finished":
                    break
        finally:
            service.async_call(service.async_manager.unsubscribe(subscription))

    return Response(
        generate_async_events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@a2a_bp.route("/a2a/tasks/stream", methods=["POST"])
@require_auth()
def create_task_and_stream():
    """Create and stream a task in one call, mapping agent.v2 -> task.update."""
    payload = request.get_json(silent=True) or {}
    message = _extract_message(payload)
    if not message:
        return jsonify({"error": "message is required"}), 400

    task_id = str(payload.get("task_id") or "").strip() or None
    session_id = str(payload.get("session_id") or "").strip()
    context = set_user_interaction_capability(
        dict(payload.get("context") or {}),
        supported=False,
    )

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    try:
        session_id = validate_session_id(session_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    service = _build_task_service()
    try:
        after_seq = _event_cursor()
        snapshot = service.create_task(
            A2ATaskCreateRequest(
                message=message,
                session_id=session_id,
                task_id=task_id,
                context=context,
            )
        )
        task_id = snapshot.id
        from agent.runtime.contracts import RunScope

        subscription = service.async_call(
            service.async_manager.subscribe(
                task_id,
                RunScope(
                    username=str(service.user_id or ""),
                    workspace_id=str(service.workspace_id or ""),
                    scopes=("agent:read",),
                ),
                after_seq=after_seq,
            )
        )

        def generate_async_create_stream():
            try:
                for event in subscription.replay:
                    yield _encode_async_task_update(event)
                    if event.event_type == "turn.finished":
                        return
                if subscription.closed:
                    return
                while True:
                    try:
                        item = subscription.queue.get(timeout=15)
                    except queue.Empty:
                        yield ": heartbeat\n\n"
                        continue
                    if item is STREAM_CLOSED or item is STREAM_RECONNECT:
                        return
                    if not hasattr(item, "event_type"):
                        return
                    yield _encode_async_task_update(item)
                    if item.event_type == "turn.finished":
                        return
            finally:
                service.async_call(service.async_manager.unsubscribe(subscription))

        return Response(
            generate_async_create_stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except RunConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except EventReplayUnavailableError:
        return jsonify({"error": "Task event stream is no longer available"}), 410

    return jsonify({"error": "Task stream could not be created"}), 500
