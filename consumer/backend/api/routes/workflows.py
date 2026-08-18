"""Workflow run endpoints: start/stream, status, cancel, sync."""

import json
import math
import os
import queue
import time
import uuid
from typing import Any, Dict, Iterator, Optional

from flask import Blueprint, Response, request, jsonify, stream_with_context

from agent.core.shared.interaction_policy import set_user_interaction_capability
from api.auth.auth import require_auth, get_current_user
from api.final_output import coerce_final_output_to_text
from config.system_config import (
    SystemConfigValidationError,
    get_public_llm_profiles,
    resolve_effective_llm_profile_config,
)
from integration.observability.trace_context import (
    TraceParentValidationError,
    W3CTraceParent,
    parse_traceparent,
)
from utils.logging import get_logger
from utils.identifiers import validate_session_id
from workspace.workspaces import WorkspaceNotFoundError
from api.routes.errors import workspace_not_found_response
from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.bootstrap import call_async_run, get_async_run_manager
from agent.runtime.contracts import (
    RunScope as AsyncRunScope,
    RuntimeInvocationContext,
)
from agent.runtime.errors import (
    DuplicateInteractionError,
    EventReplayUnavailableError,
    IdempotencyConflictError,
    InteractionConflictError,
    RunNotFoundError,
    RunConflictError,
)
from agent.runtime.event_bus import STREAM_CLOSED, STREAM_RECONNECT
from agent.runtime.bridge import (
    legacy_public_event_envelope,
    status_to_legacy,
    wait_for_run,
)
from llm.llm_request_mode import normalize_llm_request_mode

logger = get_logger("routes.workflows")

workflows_bp = Blueprint("workflows", __name__, url_prefix="/agentic")


def _get_orchestrator():
    from api.routes import get_orchestrator
    return get_orchestrator()


def _get_request_workspace_id():
    from api.routes import get_request_workspace_id
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


def _traceparent_from_request() -> Optional[W3CTraceParent]:
    return parse_traceparent(request.headers.get("traceparent"))


def _invalid_traceparent_response(exc: TraceParentValidationError):
    return jsonify({
        "error": str(exc),
        "error_code": "INVALID_TRACEPARENT",
    }), 400


def _trace_metadata(trace_context: Optional[W3CTraceParent]) -> Dict[str, str]:
    if trace_context is None:
        return {}
    return {
        "traceparent": trace_context.value,
        "external_trace_id": trace_context.trace_id,
        "external_parent_span_id": trace_context.parent_id,
        "external_trace_flags": trace_context.trace_flags,
    }


_SSE_HEARTBEAT_SECONDS = max(1.0, float(os.getenv("AGENTIC_SSE_HEARTBEAT_SECONDS", "15")))


def _json_safe_sse_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe_sse_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_sse_value(item) for item in value]
    return value


def _encode_agent_sse(envelope: Dict[str, Any]) -> str:
    return "id: {}\nevent: agent.v2\ndata: {}\n\n".format(
        _coerce_seq(envelope.get("seq")),
        json.dumps(
            _json_safe_sse_value(envelope),
            ensure_ascii=False,
            allow_nan=False,
        ),
    )


def _coerce_seq(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _run_links(run_id: str) -> Dict[str, str]:
    return {
        "status": "/agentic/runs/{}/status".format(run_id),
        "events": "/agentic/runs/{}/events".format(run_id),
        "cancel": "/agentic/runs/{}/cancel".format(run_id),
        "interact": "/agentic/runs/{}/interact".format(run_id),
    }


def _async_error(
    error_code: str,
    detail: str,
    status_code: int,
    *,
    retriable: bool = False,
):
    return jsonify({
        "error": {
            "type": "async_run_error",
            "error_code": error_code,
            "detail": detail,
            "retriable": retriable,
        }
    }), status_code


def _async_scope_for_action(username: str, workspace_id: str, action: str) -> AsyncRunScope:
    required = {
        "run": "agent:run",
        "read": "agent:read",
        "cancel": "agent:cancel",
        "interact": "agent:interact",
    }.get(action)
    scopes = (required,) if required else ()
    return AsyncRunScope(username=username, workspace_id=workspace_id, scopes=scopes)


def _async_snapshot(run_id: str, username: str, workspace_id: str):
    manager = get_async_run_manager()
    return call_async_run(
        manager.get_run(
            run_id,
            _async_scope_for_action(username, workspace_id, "read"),
        ),
    )


def _encode_public_sse(event) -> str:
    payload = event.to_dict()
    return "id: {}\nevent: {}\ndata: {}\n\n".format(
        event.seq,
        event.event_type,
        json.dumps(
            _json_safe_sse_value(payload),
            ensure_ascii=False,
            allow_nan=False,
        ),
    )


def _encode_public_heartbeat(run_id: str, last_seq: int) -> str:
    return "event: heartbeat\ndata: {}\n\n".format(json.dumps({
        "run_id": run_id,
        "last_seq": last_seq,
        "ts": int(time.time() * 1000),
    }, ensure_ascii=False))


def _async_stream_response(subscription) -> Response:
    manager = get_async_run_manager()

    def generate() -> Iterator[str]:
        last_seq = 0
        try:
            yield "retry: 2000\n\n"
            for event in subscription.replay:
                last_seq = event.seq
                yield _encode_public_sse(event)
                if event.event_type == "turn.finished":
                    return
            if subscription.closed:
                return
            while True:
                try:
                    item = subscription.queue.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield _encode_public_heartbeat(subscription.run_id, last_seq)
                    continue
                if item is STREAM_CLOSED or item is STREAM_RECONNECT:
                    break
                if hasattr(item, "to_dict"):
                    last_seq = int(getattr(item, "seq", last_seq))
                    yield _encode_public_sse(item)
                    if getattr(item, "event_type", "") == "turn.finished":
                        return
        finally:
            try:
                call_async_run(manager.unsubscribe(subscription))
            except Exception:
                logger.debug("Failed to unsubscribe async run stream", exc_info=True)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _legacy_async_stream_response(subscription) -> Response:
    """Preserve the old agent.v2 SSE envelope over PublicEvent storage."""
    manager = get_async_run_manager()

    def generate() -> Iterator[str]:
        last_seq = 0
        try:
            yield "retry: 2000\n\n"
            for event in subscription.replay:
                last_seq = event.seq
                yield _encode_agent_sse(legacy_public_event_envelope(event))
                if event.event_type == "turn.finished":
                    return
            if subscription.closed:
                return
            while True:
                try:
                    item = subscription.queue.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield _encode_public_heartbeat(subscription.run_id, last_seq)
                    continue
                if item is STREAM_CLOSED or item is STREAM_RECONNECT:
                    return
                if not hasattr(item, "event_type"):
                    return
                last_seq = item.seq
                yield _encode_agent_sse(legacy_public_event_envelope(item))
                if item.event_type == "turn.finished":
                    return
        finally:
            try:
                call_async_run(manager.unsubscribe(subscription))
            except Exception:
                logger.debug(
                    "Failed to unsubscribe compatible async stream",
                    exc_info=True,
                )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@workflows_bp.route("/model-profiles", methods=["GET"])
@require_auth()
def list_model_profiles():
    """List enabled model profiles available for chat selection."""
    try:
        workspace_id = _get_request_workspace_id()
    except WorkspaceNotFoundError as exc:
        return workspace_not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(get_public_llm_profiles(workspace_id=workspace_id))


@workflows_bp.route("/runs", methods=["POST"])
@require_auth()
def create_async_workflow_run():
    """Create a detached workflow run and return its subscription links."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _async_error("INVALID_ARGUMENT", "请求体必须是 JSON object", 400)

    user_request = data.get("request")
    if not isinstance(user_request, str) or not user_request.strip():
        return _async_error("INVALID_ARGUMENT", "request is required", 400)
    arguments = data.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return _async_error("INVALID_ARGUMENT", "arguments 必须是 object 或 null", 400)
    include_trace = data.get("include_trace", False)
    if not isinstance(include_trace, bool):
        return _async_error("INVALID_ARGUMENT", "include_trace 必须是 boolean", 400)

    current_user = get_current_user()
    if current_user is None:
        return _async_error("UNAUTHORIZED", "Unauthorized", 401)
    workspace_id = _get_request_workspace_id()
    raw_session_id = data.get("session_id")
    try:
        session_id = validate_session_id(
            raw_session_id
            if raw_session_id not in (None, "")
            else "session_{}".format(uuid.uuid4().hex[:16])
        )
    except ValueError as exc:
        return _async_error("INVALID_ARGUMENT", str(exc), 400)

    model_profile_id = str(data.get("model_profile_id") or "").strip()
    try:
        resolve_effective_llm_profile_config(
            model_profile_id or None,
            allow_default_fallback=not bool(model_profile_id),
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return _async_error("INVALID_ARGUMENT", str(exc), 422)
    try:
        trace_context = _traceparent_from_request()
    except TraceParentValidationError as exc:
        return _async_error("INVALID_TRACEPARENT", str(exc), 400)

    orchestrator = _get_orchestrator()
    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response

    run_id = "agentic-{}".format(uuid.uuid4().hex)
    context = RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        workspace_id=workspace_id,
        username=current_user.username,
        query=user_request.strip(),
        agent_mode=resolve_agent_mode({"agent_mode": data.get("agent_mode")}),
        llm_request_mode=normalize_llm_request_mode(data.get("llm_request_mode")),
        model_profile_id=model_profile_id or None,
        arguments=dict(arguments) if arguments is not None else None,
        include_trace=include_trace,
        traceparent=trace_context.value if trace_context is not None else "",
        concurrency_limit=100 if current_user.role == "admin" else 1,
    )
    manager = get_async_run_manager()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip() or None
    try:
        snapshot, _created = call_async_run(
            manager.create_run(
                context,
                _async_scope_for_action(current_user.username, workspace_id, "run"),
                idempotency_key=idempotency_key,
            )
        )
    except IdempotencyConflictError as exc:
        return _async_error(exc.error_code, str(exc), 409)
    except RunConflictError as exc:
        return _async_error(exc.error_code, str(exc), 409)
    except PermissionError:
        return _async_error("FORBIDDEN", "无权创建该运行", 403)

    payload = snapshot.to_view().to_dict()
    payload["links"] = _run_links(snapshot.run_id)
    return jsonify(payload), 202


@workflows_bp.route("/runs/stream", methods=["POST"])
@require_auth()
def start_workflow_stream():
    """Compatibility POST SSE endpoint backed by the async run manager."""
    data = request.get_json() or {}
    user_request = data.get("request", "").strip()

    if not user_request:
        return jsonify({"error": "Request is required"}), 400

    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        trace_context = _traceparent_from_request()
    except TraceParentValidationError as exc:
        return _invalid_traceparent_response(exc)

    user_context = set_user_interaction_capability(
        dict(data.get("context") or {}),
        supported=True,
    )
    arguments = data.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return jsonify({"error": "arguments 必须是 object 或 null"}), 400
    raw_session_id = data.get("session_id")
    requested_run_id = str(data.get("run_id") or "").strip() or None
    workspace_id = _get_request_workspace_id()

    if raw_session_id in (None, ""):
        return jsonify({"error": "session_id is required"}), 400
    try:
        session_id = validate_session_id(raw_session_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    requested_model_profile_id = str(user_context.get("model_profile_id") or "").strip()
    try:
        resolve_effective_llm_profile_config(
            requested_model_profile_id or None,
            allow_default_fallback=not bool(requested_model_profile_id),
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    logger.info("Starting agentic workflow stream")

    orchestrator = _get_orchestrator()
    limit = 100 if current_user.role == "admin" else 1
    run_id = requested_run_id or "agentic-{}".format(uuid.uuid4().hex)
    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response

    context = RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        workspace_id=workspace_id,
        username=current_user.username,
        turn_id=1,
        query=user_request,
        agent_mode=resolve_agent_mode(user_context),
        llm_request_mode=normalize_llm_request_mode(
            user_context.get("llm_request_mode")
        ),
        model_profile_id=requested_model_profile_id or None,
        arguments=dict(arguments) if arguments is not None else None,
        include_trace=data.get("include_trace") is True,
        supports_user_interaction=True,
        traceparent=trace_context.value if trace_context is not None else "",
        concurrency_limit=limit,
        admission_timeout_seconds=300.0,
    )
    manager = get_async_run_manager()
    try:
        snapshot, _created = call_async_run(
            manager.create_run(
                context,
                _async_scope_for_action(
                    current_user.username,
                    workspace_id,
                    "run",
                ),
            )
        )
        subscription = call_async_run(
            manager.subscribe(
                snapshot.run_id,
                _async_scope_for_action(
                    current_user.username,
                    workspace_id,
                    "read",
                ),
                after_seq=0,
            )
        )
    except RunConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    except PermissionError:
        return jsonify({"error": "Forbidden"}), 403
    return _legacy_async_stream_response(subscription)


@workflows_bp.route("/runs/<run_id>/status", methods=["GET"])
@require_auth()
def get_run_status(run_id: str):
    """Get status of a workflow run."""
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Run not found"}), 404

    workspace_id = _get_request_workspace_id()
    snapshot = _async_snapshot(run_id, current_user.username, workspace_id)
    if snapshot is None:
        return _async_error("RUN_NOT_FOUND", "Run not found", 404)
    payload = snapshot.to_view().to_dict()
    payload["links"] = _run_links(run_id)
    return jsonify(payload)


@workflows_bp.route("/runs/<run_id>/events", methods=["GET"])
@require_auth()
def stream_run_events(run_id: str):
    """Attach or reattach to one admitted run using an SSE sequence cursor."""
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Run not found"}), 404

    workspace_id = _get_request_workspace_id()
    if _async_snapshot(run_id, current_user.username, workspace_id) is None:
        return _async_error("RUN_NOT_FOUND", "Run not found", 404)
    header_cursor = request.headers.get("Last-Event-ID")
    query_cursor = request.args.get("after_seq")
    if header_cursor not in (None, "") and query_cursor not in (None, ""):
        try:
            if int(header_cursor) != int(query_cursor):
                return _async_error(
                    "INVALID_ARGUMENT",
                    "Last-Event-ID 与 after_seq 不能冲突",
                    400,
                )
        except (TypeError, ValueError):
            return _async_error("INVALID_ARGUMENT", "after_seq 必须是非负整数", 400)
    cursor_raw = header_cursor or query_cursor or 0
    try:
        cursor = int(cursor_raw)
        if cursor < 0:
            raise ValueError
    except (TypeError, ValueError):
        return _async_error("INVALID_ARGUMENT", "after_seq 必须是非负整数", 400)
    manager = get_async_run_manager()
    try:
        subscription = call_async_run(manager.subscribe(
            run_id,
            _async_scope_for_action(current_user.username, workspace_id, "read"),
            after_seq=cursor,
        ))
    except EventReplayUnavailableError as exc:
        return _async_error(exc.error_code, str(exc), 410)
    except RunNotFoundError:
        return _async_error("RUN_NOT_FOUND", "Run not found", 404)
    return _async_stream_response(subscription)


@workflows_bp.route("/runs/<run_id>/cancel", methods=["POST"])
@require_auth()
def cancel_run(run_id: str):
    """Cancel a running workflow."""
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "unspecified"

    workspace_id = _get_request_workspace_id()
    try:
        snapshot = call_async_run(get_async_run_manager().cancel_run(
            run_id,
            _async_scope_for_action(current_user.username, workspace_id, "cancel"),
            reason=reason,
        ))
    except RunNotFoundError:
        return _async_error("RUN_NOT_FOUND", "Run not found", 404)
    payload = snapshot.to_view().to_dict()
    payload["links"] = _run_links(run_id)
    return jsonify(payload)


@workflows_bp.route("/runs/<run_id>/interact", methods=["POST"])
@require_auth()
def interact_with_run(run_id: str):
    """Validate and resume one asynchronous HITL checkpoint."""
    current_user = get_current_user()
    if current_user is None:
        return _async_error("UNAUTHORIZED", "Unauthorized", 401)
    workspace_id = _get_request_workspace_id()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _async_error("INVALID_ARGUMENT", "请求体必须是 JSON object", 400)
    interaction_id = str(data.get("interaction_id") or "").strip()
    if not interaction_id:
        return _async_error("INVALID_ARGUMENT", "interaction_id is required", 400)
    if "value" not in data:
        return _async_error("INVALID_ARGUMENT", "value is required", 400)
    option_id = data.get("option_id")
    if option_id is not None and not isinstance(option_id, str):
        return _async_error("INVALID_ARGUMENT", "option_id 必须是 string 或 null", 400)
    try:
        snapshot = call_async_run(get_async_run_manager().interact(
            run_id,
            _async_scope_for_action(current_user.username, workspace_id, "interact"),
            interaction_id=interaction_id,
            value=data.get("value"),
            option_id=option_id,
        ))
    except RunNotFoundError:
        return _async_error("RUN_NOT_FOUND", "Run not found", 404)
    except DuplicateInteractionError as exc:
        return _async_error(exc.error_code, str(exc), 409)
    except InteractionConflictError as exc:
        return _async_error(exc.error_code, str(exc), 409)
    payload = snapshot.to_view().to_dict()
    payload["links"] = _run_links(run_id)
    return jsonify(payload)


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
    if not current_user:
        return jsonify({"error": "Run not found"}), 404
    workspace_id = _get_request_workspace_id()
    if _async_snapshot(run_id, current_user.username, workspace_id) is None:
        return jsonify({"error": "Run not found"}), 404

    data = request.get_json(silent=True) or {}
    vote = str(data.get("vote") or data.get("feedback") or "").strip().lower()
    if vote not in _FEEDBACK_VOTES:
        return jsonify({"error": "Invalid vote", "allowed": sorted(_FEEDBACK_VOTES)}), 400

    orchestrator = _get_orchestrator()
    voted = orchestrator.vote_on_run_memories(run_id, vote=vote)
    persisted = orchestrator.persist_run_feedback(run_id, vote=vote)
    return jsonify({
        "success": True,
        "vote": vote,
        "fingerprints_voted": voted,
        "feedback_persisted": persisted,
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
    include_trace = data.get("include_trace") is True

    if not user_request:
        return jsonify({"error": "Request is required"}), 400

    user_context = set_user_interaction_capability(
        dict(data.get("context") or {}),
        supported=False,
    )
    workspace_id = _get_request_workspace_id()
    requested_model_profile_id = str(user_context.get("model_profile_id") or "").strip()
    try:
        resolve_effective_llm_profile_config(
            requested_model_profile_id or None,
            allow_default_fallback=not bool(requested_model_profile_id),
            workspace_id=workspace_id,
        )
    except SystemConfigValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        trace_context = _traceparent_from_request()
    except TraceParentValidationError as exc:
        return _invalid_traceparent_response(exc)

    arguments = data.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return jsonify({"error": "arguments 必须是 object 或 null"}), 400
    raw_session_id = data.get("session_id")
    try:
        session_id = validate_session_id(
            raw_session_id if raw_session_id not in (None, "") else f"sync_{uuid.uuid4().hex[:8]}"
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    logger.info("Starting agentic workflow sync, session=%s", session_id)

    orchestrator = _get_orchestrator()
    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response
    limit = 100 if current_user.role == "admin" else 1
    run_id = "sync_{}".format(uuid.uuid4().hex)
    tool_calls = []
    tool_failures = []
    scope = AsyncRunScope(
        username=current_user.username,
        workspace_id=workspace_id,
        scopes=("agent:run", "agent:read", "agent:cancel"),
    )
    context = RuntimeInvocationContext(
        run_id=run_id,
        session_id=session_id,
        turn_id=1,
        workspace_id=workspace_id,
        username=current_user.username,
        query=user_request,
        agent_mode=resolve_agent_mode(user_context),
        llm_request_mode=normalize_llm_request_mode(
            user_context.get("llm_request_mode")
        ),
        model_profile_id=requested_model_profile_id or None,
        arguments=dict(arguments) if arguments is not None else None,
        include_trace=include_trace,
        supports_user_interaction=False,
        traceparent=trace_context.value if trace_context is not None else "",
        concurrency_limit=limit,
        admission_timeout_seconds=300.0,
    )
    manager = get_async_run_manager()
    try:
        call_async_run(manager.create_run(context, scope))

        def collect(event) -> None:
            if not include_trace:
                return
            payload = event.payload if isinstance(event.payload, dict) else {}
            if event.event_type == "tool.started":
                tool_name = str(payload.get("name") or payload.get("tool_name") or "").strip()
                if tool_name:
                    tool_calls.append(tool_name)
            elif event.event_type == "tool.finished" and str(
                payload.get("status") or ""
            ).strip().lower() in {"error", "failed"}:
                tool_name = str(payload.get("name") or payload.get("tool_name") or "").strip()
                raw_error = payload.get("error")
                error = (
                    str(raw_error.get("message") or "")
                    if isinstance(raw_error, dict)
                    else str(raw_error or "")
                ).strip()
                if tool_name and error:
                    tool_failures.append({
                        "tool_name": tool_name,
                        "error": error[:1024],
                    })

        snapshot = wait_for_run(
            manager,
            call_async_run,
            run_id,
            scope,
            timeout_seconds=900,
            on_event=collect,
        )
    except TimeoutError:
        try:
            call_async_run(manager.cancel_run(run_id, scope, reason="sync_timeout"))
        except Exception:
            logger.debug("Failed to cancel timed out sync run", exc_info=True)
        return jsonify({
            "status": "error",
            "error": "运行超时",
            "final_answer": "",
            "session_id": session_id,
            "run_id": run_id,
        }), 504
    except Exception:
        logger.exception("Error in workflow sync")
        return jsonify({
            "status": "error",
            "error": "运行失败",
            "final_answer": "",
            "session_id": session_id,
            "run_id": run_id,
        }), 500

    runtime_result = snapshot.result
    final_output = (
        runtime_result.content
        if runtime_result is not None and runtime_result.content is not None
        else (
            runtime_result.structured_content
            if runtime_result is not None
            else None
        )
    )
    last_status = status_to_legacy(snapshot.status)
    last_error = snapshot.error.message if snapshot.error is not None else ""
    response_body = {
        "status": last_status,
        "final_answer": coerce_final_output_to_text(final_output),
        "session_id": session_id,
        "run_id": run_id,
    }
    if last_error:
        response_body["error"] = last_error
    if snapshot.pending_interaction is not None:
        response_body["pending_user_interaction"] = snapshot.pending_interaction
    if include_trace:
        response_body["execution_trace"] = {"tool_calls": tool_calls}
        if tool_failures:
            response_body["execution_trace"]["tool_failures"] = tool_failures
    return jsonify(response_body)
