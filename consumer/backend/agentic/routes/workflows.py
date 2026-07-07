"""Workflow run endpoints: start/stream, status, cancel, sync."""

import json
import time
import uuid

from flask import Blueprint, Response, request, jsonify

from ..services.auth import require_auth, get_current_user
from ..config.system_config import (
    SystemConfigValidationError,
    get_public_llm_profiles,
    resolve_llm_profile_config,
)
from ..utils.logging import get_logger
from ..utils.streaming import PROTOCOL
from ..workspaces import workspace_scope

logger = get_logger("routes.workflows")

workflows_bp = Blueprint("workflows", __name__, url_prefix="/agentic")


def _get_orchestrator():
    from . import get_orchestrator
    return get_orchestrator()


def _get_request_workspace_id():
    from . import get_request_workspace_id
    return get_request_workspace_id()


def _run_tps_limit(orchestrator) -> int:
    agent_config = getattr(orchestrator, "agent_config", None)
    limits = getattr(agent_config, "limits", None)
    try:
        return max(1, int(getattr(limits, "run_tps", 50)))
    except (TypeError, ValueError):
        return 50


def _reject_if_run_rate_limited(orchestrator):
    limiter = getattr(orchestrator, "run_rate_limiter", None)
    try_acquire = getattr(limiter, "try_acquire", None)
    if callable(try_acquire) and not try_acquire():
        return jsonify({
            "error": "请求过于频繁，请稍后重试",
            "limit": _run_tps_limit(orchestrator),
        }), 429
    return None


def _external_trace_id_from_request():
    """Extract the W3C trace id from the incoming traceparent header, if valid.

    Lets the eval platform's injected trace id propagate so this run's Langfuse
    spans join the caller's trace. Returns None when absent or malformed.
    """
    raw = request.headers.get("traceparent") or ""
    parts = raw.split("-")
    if len(parts) >= 3:
        tid = parts[1].strip().lower()
        if len(tid) == 32 and all(c in "0123456789abcdef" for c in tid):
            return tid
    return None


def _ensure_run_owner(run_id: str, user_id: str) -> bool:
    orchestrator = _get_orchestrator()
    run = orchestrator.run_store.get_run(run_id)
    if not run:
        return False
    return run.metadata.get("user_id") == user_id


@workflows_bp.route("/model-profiles", methods=["GET"])
@require_auth()
def list_model_profiles():
    """List enabled model profiles available for chat selection."""
    return jsonify(get_public_llm_profiles())


@workflows_bp.route("/runs/stream", methods=["POST"])
@require_auth()
def start_workflow_stream():
    """
    Start an agentic workflow and stream events via SSE.

    Request body:
        {
            "request": "User request text",
            "context": {"optional": "context"},
            "session_id": "required-session-id"
        }

    Returns:
        SSE stream of workflow events
    """
    data = request.get_json() or {}
    user_request = data.get("request", "").strip()

    if not user_request:
        return jsonify({"error": "Request is required"}), 400

    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    user_context = data.get("context", {})
    arguments = data.get("arguments")
    session_id = str(data.get("session_id") or "").strip()
    requested_run_id = str(data.get("run_id") or "").strip() or None
    external_trace_id = _external_trace_id_from_request()
    workspace_id = _get_request_workspace_id()

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    requested_model_profile_id = str(user_context.get("model_profile_id") or "").strip()
    try:
        resolve_llm_profile_config(
            requested_model_profile_id or None,
            allow_default_fallback=not bool(requested_model_profile_id),
        )
    except SystemConfigValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    logger.info("Starting agentic workflow stream")

    orchestrator = _get_orchestrator()
    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response
    slot_manager = orchestrator.slot_manager
    limit = 100 if current_user.role == "admin" else 1

    # Queue — block until a slot is available, with 300 s timeout
    if not slot_manager.acquire(current_user.username, limit, timeout=300):
        return jsonify({
            "error": "排队超时，请稍后重试",
            "limit": limit,
        }), 503

    def generate():
        """Generate SSE events from workflow."""
        try:
            with workspace_scope(workspace_id):
                for event in orchestrator.run(
                    user_request=user_request,
                    user_context=user_context,
                    arguments=arguments,
                    session_id=session_id,
                    user_id=current_user.username,
                    workspace_id=workspace_id,
                    requested_run_id=requested_run_id,
                    external_trace_id=external_trace_id,
                ):
                    event_type = "agent.v2"
                    event_data = event.get("data", event)
                    if isinstance(event, dict):
                        if event.get("event") == "agent.v2" and isinstance(event.get("data"), dict):
                            event_data = event["data"]
                        elif event.get("protocol") == PROTOCOL:
                            event_data = event

                    yield f"event: {event_type}\n"
                    yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        except Exception as exc:
            logger.exception("Error in workflow stream")
            now_ms = int(time.time() * 1000)
            run_ref = "run_error_{}".format(now_ms)
            error_data = {
                "protocol": PROTOCOL,
                "event_id": "{}:1".format(run_ref),
                "seq": 1,
                "ts": now_ms,
                "session_id": session_id,
                "run_id": run_ref,
                "turn_id": 0,
                "type": "turn.finished",
                "source": {"node": "routes", "component": "workflow"},
                "payload": {
                    "status": "error",
                    "error": "Workflow stream failed: {}".format(str(exc)),
                },
            }
            yield f"event: agent.v2\n"
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"

        finally:
            slot_manager.release(current_user.username)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@workflows_bp.route("/runs/<run_id>/status", methods=["GET"])
@require_auth()
def get_run_status(run_id: str):
    """Get status of a workflow run."""
    current_user = get_current_user()
    if not current_user or not _ensure_run_owner(run_id, current_user.username):
        return jsonify({"error": "Run not found"}), 404

    orchestrator = _get_orchestrator()
    status = orchestrator.get_run_status(run_id)

    if not status:
        return jsonify({"error": "Run not found"}), 404

    return jsonify(status)


@workflows_bp.route("/runs/<run_id>/cancel", methods=["POST"])
@require_auth()
def cancel_run(run_id: str):
    """Cancel a running workflow."""
    current_user = get_current_user()
    if not current_user or not _ensure_run_owner(run_id, current_user.username):
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "unspecified"

    orchestrator = _get_orchestrator()
    success = orchestrator.cancel_run(run_id, reason=reason)

    if not success:
        return jsonify({"error": "Run not found"}), 404

    return jsonify({"success": True, "cancelled": True})


_FEEDBACK_VOTES = {"up", "down", "none"}


@workflows_bp.route("/runs/<run_id>/feedback", methods=["POST"])
@require_auth()
def submit_run_feedback(run_id: str):
    """Vote on memories that were injected into this turn.

    Body: ``{"vote": "up"|"down"|"none"}``. Pure indicator update — no reflection
    re-pass, no proposal creation. New proposals come from
    `memory` (active_chat) or reflection (passive); this
    route only adjusts active_endorsements / rejection_count for fingerprints
    that were already injected via near_point_recall.
    """
    current_user = get_current_user()
    if not current_user or not _ensure_run_owner(run_id, current_user.username):
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json(silent=True) or {}
    vote = str(data.get("vote") or data.get("feedback") or "").strip().lower()
    if vote not in _FEEDBACK_VOTES:
        return jsonify({"error": "Invalid vote", "allowed": sorted(_FEEDBACK_VOTES)}), 400

    orchestrator = _get_orchestrator()
    voted = orchestrator.vote_on_run_memories(run_id, vote=vote)
    return jsonify({
        "success": True,
        "vote": vote,
        "fingerprints_voted": voted,
    })


@workflows_bp.route("/runs/sync", methods=["POST"])
@require_auth()
def start_workflow_sync():
    """
    Synchronous (non-streaming) agentic workflow endpoint.

    Runs the full agent loop, collects all events, and returns the
    final answer in a single JSON response.

    Request body:
        {
            "request": "User request text",
            "context": {"optional": "context"},
            "session_id": "optional-session-id"
        }

    Returns:
        {
            "status": "success" | "error",
            "final_answer": "...",
            "session_id": "...",
            "run_id": "..."
        }
    """
    data = request.get_json() or {}
    user_request = data.get("request", "").strip()

    if not user_request:
        return jsonify({"error": "Request is required"}), 400

    user_context = data.get("context", {})
    requested_model_profile_id = str(user_context.get("model_profile_id") or "").strip()
    try:
        resolve_llm_profile_config(
            requested_model_profile_id or None,
            allow_default_fallback=not bool(requested_model_profile_id),
        )
    except SystemConfigValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    user_context = data.get("context", {})
    arguments = data.get("arguments")
    session_id = str(data.get("session_id") or "").strip() or f"sync_{uuid.uuid4().hex[:8]}"
    external_trace_id = _external_trace_id_from_request()
    workspace_id = _get_request_workspace_id()

    logger.info("Starting agentic workflow sync, session=%s", session_id)

    orchestrator = _get_orchestrator()
    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response
    slot_manager = orchestrator.slot_manager
    limit = 100 if current_user.role == "admin" else 1

    # Queue — block until a slot is available, with 300 s timeout
    if not slot_manager.acquire(current_user.username, limit, timeout=300):
        return jsonify({
            "error": "排队超时，请稍后重试",
            "limit": limit,
        }), 503

    final_output = ""
    run_id = None
    last_status = "success"

    try:
        with workspace_scope(workspace_id):
            for event in orchestrator.run(
                user_request=user_request,
                user_context=user_context,
                arguments=arguments,
                session_id=session_id,
                user_id=current_user.username,
                workspace_id=workspace_id,
                external_trace_id=external_trace_id,
            ):
                event_data = event.get("data", event) if isinstance(event, dict) else event
                if not isinstance(event_data, dict):
                    continue

                event_run_id = event_data.get("run_id")
                if event_run_id:
                    run_id = event_run_id

                if event_data.get("type") == "turn.finished":
                    payload = event_data.get("payload", {})
                    last_status = payload.get("status", "success")
                    if "final_output" in payload:
                        final_output = payload["final_output"]
    except Exception as exc:
        logger.exception("Error in workflow sync")
        return jsonify({
            "status": "error",
            "error": str(exc),
            "final_answer": "",
            "session_id": session_id,
            "run_id": run_id,
        }), 500
    finally:
        slot_manager.release(current_user.username)

    return jsonify({
        "status": last_status,
        "final_answer": final_output,
        "session_id": session_id,
        "run_id": run_id,
    })
