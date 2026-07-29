"""A2A adapter endpoints (agent card, task CRUD, and task event stream)."""

from __future__ import annotations

import json
import time
from typing import Any, Dict

from flask import Blueprint, Response, current_app, jsonify, request

from agent.core.shared.interaction_policy import set_user_interaction_capability
from api.protocols.a2a.mapper import map_agent_v2_event_to_a2a_update
from api.protocols.a2a.models import A2ATaskCreateRequest
from api.protocols.a2a.service import A2ATaskService
from api.auth.auth import get_current_user, require_auth
from utils.identifiers import validate_session_id
from utils.logging import get_logger
from workspace.workspaces import enforce_workspace_access, workspace_scope


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
    chat_store, execution_store = _get_user_stores()
    current_user = get_current_user()
    workspace_id = _get_request_workspace_id()

    return A2ATaskService(
        orchestrator=orchestrator,
        chat_store=chat_store,
        execution_store=execution_store,
        user_id=(current_user.username if current_user else None),
        workspace_id=workspace_id,
        flask_app=current_app._get_current_object(),
    )


def _task_access_matches(
    service: A2ATaskService,
    task_id: str,
    *,
    session_id: str | None = None,
) -> bool:
    """Bind a task lookup to its persisted owner and workspace."""
    orchestrator = getattr(service, "orchestrator", None)
    run_store = getattr(orchestrator, "run_store", None)
    if run_store is None:
        return False

    run = run_store.get_run(task_id)
    if run is None:
        return False
    metadata = dict(getattr(run, "metadata", {}) or {})
    persisted_user_id = str(metadata.get("user_id") or "").strip()
    persisted_workspace_id = str(metadata.get("workspace_id") or "").strip()
    if not persisted_user_id or persisted_user_id != str(service.user_id or "").strip():
        return False
    if not persisted_workspace_id or persisted_workspace_id != str(service.workspace_id or "").strip():
        return False
    persisted_session_id = str(metadata.get("session_id") or "").strip()
    if session_id and persisted_session_id != session_id:
        return False
    enforce_workspace_access(persisted_workspace_id)
    return True


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

    chat_store, _execution_store = _get_user_stores()
    session = chat_store.get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    filtered = []
    for event in list(getattr(session, "events", []) or []):
        if event.get("event") != "agent.v2":
            continue
        run_id = str((event.get("data") or {}).get("run_id") or "").strip()
        if run_id != task_id:
            continue
        filtered.append(event)

    def generate():
        for event in filtered:
            update = map_agent_v2_event_to_a2a_update(event)
            yield "event: task.update\n"
            yield "data: {}\n\n".format(json.dumps(update, ensure_ascii=False))

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
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

    current_user = get_current_user()
    orchestrator = _get_orchestrator()
    workspace_id = _get_request_workspace_id()
    if task_id and orchestrator.run_store.get_run(task_id) is not None:
        return jsonify({"error": "task_id already exists"}), 400

    def generate():
        try:
            with workspace_scope(workspace_id):
                for event in orchestrator.run(
                    user_request=message,
                    user_context=context,
                    session_id=session_id,
                    user_id=(current_user.username if current_user else None),
                    workspace_id=workspace_id,
                    requested_run_id=task_id,
                ):
                    update = map_agent_v2_event_to_a2a_update(event)
                    yield "event: task.update\n"
                    yield "data: {}\n\n".format(json.dumps(update, ensure_ascii=False))
        except Exception as exc:
            logger.exception("A2A stream failed")
            now_ms = int(time.time() * 1000)
            update = {
                "task_id": task_id,
                "session_id": session_id,
                "event_type": "task.error",
                "timestamp": now_ms,
                "source": {"node": "a2a", "component": "routes"},
                "payload": {"error": "{}".format(str(exc))},
            }
            yield "event: task.update\n"
            yield "data: {}\n\n".format(json.dumps(update, ensure_ascii=False))

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
