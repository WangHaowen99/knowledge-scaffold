"""Workflow run endpoints: start/stream, status, cancel, sync."""

import json
import math
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from flask import Blueprint, Response, request, jsonify, stream_with_context

from agent.core.shared.interaction_policy import set_user_interaction_capability
from api.auth.auth import require_auth, get_current_user
from api.final_output import coerce_final_output_to_text
from config.system_config import (
    SystemConfigValidationError,
    get_public_llm_profiles,
    resolve_effective_llm_profile_config,
)
from persistence.run_queue import QueuedRunAdmission, RunAdmissionQueue
from persistence.run_store import RunStatus
from integration.observability.trace_context import (
    TraceParentValidationError,
    W3CTraceParent,
    parse_traceparent,
)
from utils.logging import get_logger
from utils.identifiers import validate_session_id
from utils.streaming import PROTOCOL, build_agent_event
from workspace.workspaces import (
    WorkspaceNotFoundError,
    enforce_workspace_access,
    workspace_scope,
)
from api.routes.errors import workspace_not_found_response

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


def _ensure_run_owner(run_id: str, user_id: str) -> bool:
    request_workspace_id = _get_request_workspace_id()
    orchestrator = _get_orchestrator()
    run = orchestrator.run_store.get_run(run_id)
    if not run:
        return False
    metadata = dict(getattr(run, "metadata", {}) or {})
    if metadata.get("user_id") != user_id:
        return False
    workspace_id = str(metadata.get("workspace_id") or "").strip()
    if not workspace_id:
        return False
    if request_workspace_id != workspace_id:
        return False
    enforce_workspace_access(workspace_id)
    return True


_RUN_TERMINAL = object()
_RUN_RECONNECT = object()
_RUN_COMPLETED_TTL_SECONDS = max(0.0, float(os.getenv("AGENTIC_RUN_COMPLETED_TTL", "120")))
_SSE_HEARTBEAT_SECONDS = max(1.0, float(os.getenv("AGENTIC_SSE_HEARTBEAT_SECONDS", "15")))
_SSE_SUBSCRIBER_QUEUE_SIZE = max(1, int(os.getenv("AGENTIC_SSE_SUBSCRIBER_QUEUE_SIZE", "512")))


def _extract_agent_envelope(event: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    if event.get("protocol") == PROTOCOL:
        return event
    if event.get("event") == "agent.v2" and isinstance(event.get("data"), dict):
        data = event["data"]
        if data.get("protocol") == PROTOCOL:
            return data
    return None


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


def _encode_heartbeat_sse(handle: "AgenticRunHandle") -> str:
    snapshot = handle.runtime_snapshot()
    return "event: heartbeat\ndata: {}\n\n".format(
        json.dumps(
            {
                "run_id": handle.active_run_id or handle.run_id,
                "last_seq": snapshot.get("last_seq", 0),
                "status": snapshot.get("status", "running"),
                "ts": int(time.time() * 1000),
            },
            ensure_ascii=False,
        )
    )


def _run_status_from_envelope(envelope: Dict[str, Any]) -> Optional[str]:
    if str(envelope.get("type") or "") != "turn.finished":
        return None
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    status = str(payload.get("status") or "").strip()
    return status or "success"


def _mark_session_active(orchestrator, *, session_id: str, run_id: str, user_id: str, workspace_id: str) -> None:
    try:
        chat_store, _execution_store = orchestrator._get_stores(user_id, workspace_id)  # pylint: disable=protected-access
        set_active_run = getattr(chat_store, "set_active_run", None)
        if callable(set_active_run):
            set_active_run(session_id, run_id)
    except Exception:
        logger.debug(
            "Skipping active-run marker for session_id=%s run_id=%s",
            session_id,
            run_id,
            exc_info=True,
        )


def _clear_session_active(orchestrator, *, session_id: str, run_id: str, user_id: str, workspace_id: str) -> None:
    try:
        chat_store, _execution_store = orchestrator._get_stores(user_id, workspace_id)  # pylint: disable=protected-access
        get_active_run = getattr(chat_store, "get_active_run", None)
        clear_active_run = getattr(chat_store, "clear_active_run", None)
        if callable(get_active_run) and get_active_run(session_id) not in {None, "", run_id}:
            return
        if callable(clear_active_run):
            clear_active_run(session_id)
    except Exception:
        logger.debug(
            "Skipping active-run clear for session_id=%s run_id=%s",
            session_id,
            run_id,
            exc_info=True,
        )


class AgenticRunHandle:
    """Owns one workflow run independently from transient HTTP subscribers."""

    def __init__(
        self,
        *,
        run_id: str,
        user_request: str,
        user_context: Dict[str, Any],
        arguments: Optional[Dict[str, Any]],
        session_id: str,
        user_id: str,
        workspace_id: str,
        orchestrator: Any,
        slot_manager: Any,
        slot_limit_user_id: str,
        traceparent: str = "",
        event_observer: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.run_id = run_id
        self.active_run_id = run_id
        self.user_request = user_request
        self.user_context = dict(user_context or {})
        self.arguments = arguments
        self.session_id = session_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.orchestrator = orchestrator
        self.slot_manager = slot_manager
        self.slot_limit_user_id = slot_limit_user_id
        self.traceparent = str(traceparent or "")
        self.event_observer = event_observer
        self.done = threading.Event()
        self._lock = threading.RLock()
        self._subscribers: Dict[str, "queue.Queue[Any]"] = {}
        self._durable: list[Dict[str, Any]] = []
        self._last_seq = 0
        self._last_turn_id = 0
        self._terminal_status: Optional[str] = None
        self._terminal_payload: Dict[str, Any] = {}
        self._completed_at: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="agentic-run-{}".format(self.run_id),
            daemon=True,
        )
        self._thread.start()

    def _emit(self, envelope: Dict[str, Any]) -> None:
        event_run_id = str(envelope.get("run_id") or "").strip()
        if event_run_id:
            self.active_run_id = event_run_id
        turn_id = envelope.get("turn_id")
        if isinstance(turn_id, int):
            self._last_turn_id = turn_id

        terminal_status = _run_status_from_envelope(envelope)
        seq = envelope.get("seq")
        try:
            seq_int = int(seq)
        except (TypeError, ValueError):
            seq_int = 0

        with self._lock:
            # Buffer every envelope for cursor replay. Each subscriber has a
            # bounded live queue; a slow subscriber is disconnected and resumes
            # from its last delivered SSE id instead of growing memory forever.
            self._durable.append(envelope)
            if seq_int > self._last_seq:
                self._last_seq = seq_int
            if terminal_status:
                self._terminal_status = terminal_status
                payload = envelope.get("payload")
                if isinstance(payload, dict):
                    self._terminal_payload = dict(payload)
            subscribers = list(self._subscribers.items())

        if self.event_observer is not None:
            try:
                self.event_observer(envelope)
            except Exception:
                logger.warning("Workflow event observer failed", exc_info=True)

        for sub_id, sub_queue in subscribers:
            try:
                sub_queue.put_nowait(envelope)
            except queue.Full:
                with self._lock:
                    if self._subscribers.get(sub_id) is sub_queue:
                        self._subscribers.pop(sub_id, None)
                while True:
                    try:
                        sub_queue.get_nowait()
                    except queue.Empty:
                        break
                sub_queue.put_nowait(_RUN_RECONNECT)

    def _build_error_envelope(self, exc: BaseException) -> Dict[str, Any]:
        return build_agent_event(
            run_id=self.active_run_id or self.run_id,
            session_id=self.session_id,
            turn_id=self._last_turn_id or 0,
            event_type="turn.finished",
            source={"node": "routes", "component": "workflow"},
            payload={
                "status": "error",
                "error": "Workflow stream failed: {}".format(str(exc)),
            },
        )

    def _run(self) -> None:
        try:
            _mark_session_active(
                self.orchestrator,
                session_id=self.session_id,
                run_id=self.run_id,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
            )
            with workspace_scope(self.workspace_id):
                for event in self.orchestrator.run(
                    user_request=self.user_request,
                    user_context=self.user_context,
                    arguments=self.arguments,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    workspace_id=self.workspace_id,
                    requested_run_id=self.run_id,
                    traceparent=self.traceparent,
                ):
                    envelope = _extract_agent_envelope(event)
                    if envelope:
                        self._emit(envelope)
        except Exception as exc:
            logger.exception("Error in workflow run handle")
            try:
                self._emit(self._build_error_envelope(exc))
            except Exception:
                logger.exception("Failed to emit workflow run-handle error envelope")
        finally:
            with self._lock:
                if self._terminal_status is None:
                    self._terminal_status = "interrupted"
                self._completed_at = time.time()
                subscribers = list(self._subscribers.values())
                self._subscribers.clear()
                self.done.set()

            _clear_session_active(
                self.orchestrator,
                session_id=self.session_id,
                run_id=self.run_id,
                user_id=self.user_id,
                workspace_id=self.workspace_id,
            )
            try:
                self.slot_manager.release(self.slot_limit_user_id)
            except Exception:
                logger.warning("Failed to release run slot for %s", self.slot_limit_user_id, exc_info=True)
            for sub_queue in subscribers:
                try:
                    sub_queue.put_nowait(_RUN_TERMINAL)
                except queue.Full:
                    while True:
                        try:
                            sub_queue.get_nowait()
                        except queue.Empty:
                            break
                    sub_queue.put_nowait(_RUN_RECONNECT)

    def subscribe(
        self,
        *,
        after_seq: int = 0,
    ) -> Tuple[str, "queue.Queue[Any]", list[Dict[str, Any]], bool]:
        sub_id = uuid.uuid4().hex
        sub_queue: "queue.Queue[Any]" = queue.Queue(maxsize=_SSE_SUBSCRIBER_QUEUE_SIZE)
        cursor = max(0, int(after_seq or 0))
        with self._lock:
            replay = [
                envelope
                for envelope in self._durable
                if _coerce_seq(envelope.get("seq")) > cursor
            ]
            finished = self.done.is_set()
            if not finished:
                self._subscribers[sub_id] = sub_queue
        return sub_id, sub_queue, replay, finished

    def unsubscribe(self, sub_id: str) -> None:
        with self._lock:
            self._subscribers.pop(sub_id, None)

    def runtime_snapshot(self) -> Dict[str, Any]:
        """Cheap status/last_seq for list enrichment without building deltas."""
        with self._lock:
            status = self._terminal_status or ("completed" if self.done.is_set() else "running")
            return {
                "active_run_id": self.active_run_id or self.run_id,
                "status": status,
                "last_seq": self._last_seq,
            }

    def terminal_payload(self) -> Dict[str, Any]:
        """Return the terminal envelope payload without rebuilding event replay."""
        with self._lock:
            return dict(self._terminal_payload)


class AgenticRunRegistry:
    """Process-local live run registry keyed by the client-visible run id."""

    def __init__(self, *, completed_ttl: float = _RUN_COMPLETED_TTL_SECONDS) -> None:
        self._runs: Dict[str, AgenticRunHandle] = {}
        self._lock = threading.Lock()
        self.completed_ttl = completed_ttl

    def _expired_locked(self, handle: AgenticRunHandle, now: float) -> bool:
        return (
            handle.done.is_set()
            and handle._completed_at is not None
            and now - handle._completed_at > self.completed_ttl
        )

    def _sweep_locked(self) -> None:
        now = time.time()
        for run_id in [rid for rid, handle in self._runs.items() if self._expired_locked(handle, now)]:
            self._runs.pop(run_id, None)

    def get(self, run_id: str) -> Optional[AgenticRunHandle]:
        clean = str(run_id or "").strip()
        if not clean:
            return None
        with self._lock:
            self._sweep_locked()
            handle = self._runs.get(clean)
            if handle is not None and self._expired_locked(handle, time.time()):
                self._runs.pop(clean, None)
                return None
            return handle

    @staticmethod
    def _matches_scope(
        handle: AgenticRunHandle,
        *,
        user_id: str,
        workspace_id: str,
    ) -> bool:
        return (
            bool(user_id)
            and bool(workspace_id)
            and handle.user_id == user_id
            and handle.workspace_id == workspace_id
        )

    def find_by_session(
        self,
        session_id: str,
        *,
        user_id: str,
        workspace_id: str,
    ) -> Optional[AgenticRunHandle]:
        clean = str(session_id or "").strip()
        if not clean:
            return None
        with self._lock:
            self._sweep_locked()
            for handle in self._runs.values():
                if handle.session_id == clean and self._matches_scope(
                    handle,
                    user_id=user_id,
                    workspace_id=workspace_id,
                ):
                    return handle
        return None

    def snapshot_by_session(
        self,
        *,
        user_id: str,
        workspace_id: str,
    ) -> Dict[str, AgenticRunHandle]:
        """Return all live handles keyed by session_id in one locked pass.

        Lets list_sessions enrich every session without doing an O(runs) scan
        (plus lock acquire) per session.
        """
        with self._lock:
            self._sweep_locked()
            return {
                handle.session_id: handle
                for handle in self._runs.values()
                if handle.session_id
                and self._matches_scope(
                    handle,
                    user_id=user_id,
                    workspace_id=workspace_id,
                )
            }

    def add(self, handle: AgenticRunHandle) -> AgenticRunHandle:
        # Register and start under the same lock so there is no window in which
        # the handle is visible in the registry but its thread hasn't started --
        # a reattacher in that window would block on an empty subscriber queue
        # with no TERMINAL ever arriving. start() only spawns a daemon thread
        # and does not re-enter the registry lock, so holding it is safe.
        with self._lock:
            self._sweep_locked()
            existing = self._runs.get(handle.run_id)
            if existing is not None and not existing.done.is_set():
                return existing
            self._runs[handle.run_id] = handle
            try:
                handle.start()
            except Exception:
                # Don't leave a never-started zombie that would hang reattachers.
                self._runs.pop(handle.run_id, None)
                raise
        return handle


def _coerce_seq(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_RUN_REGISTRY = AgenticRunRegistry()


def get_run_registry() -> AgenticRunRegistry:
    return _RUN_REGISTRY


def _get_run_admission_queue(orchestrator: Any) -> RunAdmissionQueue:
    admission_queue = getattr(orchestrator, "run_admission_queue", None)
    if isinstance(admission_queue, RunAdmissionQueue):
        return admission_queue
    admission_queue = RunAdmissionQueue(orchestrator.slot_manager)
    setattr(orchestrator, "run_admission_queue", admission_queue)
    return admission_queue


def _run_links(run_id: str) -> Dict[str, str]:
    return {
        "status": "/agentic/runs/{}/status".format(run_id),
        "events": "/agentic/runs/{}/events".format(run_id),
        "cancel": "/agentic/runs/{}/cancel".format(run_id),
    }


def _queued_run_payload(orchestrator: Any, run_id: str) -> Dict[str, Any]:
    run = orchestrator.run_store.get_run(run_id)
    admission_queue = _get_run_admission_queue(orchestrator)
    position = admission_queue.position(run_id)
    metadata = dict(getattr(run, "metadata", {}) or {})
    return {
        "run_id": run_id,
        "session_id": metadata.get("session_id"),
        "status": run.status.value if run is not None else "queued",
        "queue": {
            "position": position,
            "limit": metadata.get("admission_limit"),
            "queued_at": metadata.get("queued_at"),
        },
        "links": _run_links(run_id),
    }


def _stream_response(handle: AgenticRunHandle, *, after_seq: int = 0) -> Response:
    sub_id, sub_queue, replay, finished = handle.subscribe(after_seq=after_seq)

    def generate() -> Iterator[str]:
        try:
            yield "retry: 2000\n\n"
            for envelope in replay:
                yield _encode_agent_sse(envelope)
            if finished:
                return
            while True:
                try:
                    item = sub_queue.get(timeout=_SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield _encode_heartbeat_sse(handle)
                    continue
                if item is _RUN_TERMINAL or item is _RUN_RECONNECT:
                    break
                if isinstance(item, dict):
                    yield _encode_agent_sse(item)
        finally:
            handle.unsubscribe(sub_id)

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

    try:
        trace_context = _traceparent_from_request()
    except TraceParentValidationError as exc:
        return _invalid_traceparent_response(exc)

    user_context = set_user_interaction_capability(
        dict(data.get("context") or {}),
        supported=True,
    )
    arguments = data.get("arguments")
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
    admission_queue = _get_run_admission_queue(orchestrator)
    limit = 100 if current_user.role == "admin" else 1
    run_id = requested_run_id or "agentic-{}".format(uuid.uuid4().hex)
    registry = get_run_registry()

    handle = registry.get(run_id)
    if handle is not None and (
        handle.user_id != current_user.username or handle.workspace_id != workspace_id
    ):
        return jsonify({"error": "Run not found"}), 404

    if handle is not None:
        return _stream_response(handle)

    existing_run = orchestrator.run_store.get_run(run_id)
    if existing_run is not None:
        if (
            existing_run.metadata.get("user_id") != current_user.username
            or existing_run.metadata.get("workspace_id") != workspace_id
        ):
            return jsonify({"error": "Run not found"}), 404
        if existing_run.status == RunStatus.QUEUED:
            return jsonify(_queued_run_payload(orchestrator, run_id)), 202
        if existing_run.status == RunStatus.RUNNING:
            payload = _queued_run_payload(orchestrator, run_id)
            payload["status"] = "running"
            return jsonify(payload), 202
        return jsonify({"error": "Run already finished", "run_id": run_id}), 409

    rate_limit_response = _reject_if_run_rate_limited(orchestrator)
    if rate_limit_response is not None:
        return rate_limit_response

    run_metadata = {
        "source": "agentic_stream",
        "session_id": session_id,
        "user_id": current_user.username,
        "workspace_id": workspace_id,
        "admission_limit": limit,
        **_trace_metadata(trace_context),
    }
    handle = AgenticRunHandle(
        run_id=run_id,
        user_request=user_request,
        user_context=user_context,
        arguments=arguments,
        session_id=session_id,
        user_id=current_user.username,
        workspace_id=workspace_id,
        orchestrator=orchestrator,
        slot_manager=admission_queue,
        slot_limit_user_id=current_user.username,
        traceparent=trace_context.value if trace_context is not None else "",
    )

    if admission_queue.try_acquire(current_user.username, limit):
        orchestrator.run_store.create_run(
            run_id=run_id,
            user_request=user_request,
            metadata=run_metadata,
        )
        try:
            registered_handle = registry.add(handle)
        except Exception:
            orchestrator.run_store.fail_run(run_id, "Failed to start workflow run")
            admission_queue.release(current_user.username)
            raise
        if registered_handle is not handle:
            admission_queue.release(current_user.username)
        return _stream_response(registered_handle)

    queued_metadata = {
        **run_metadata,
        "queued_at": int(time.time() * 1000),
    }
    orchestrator.run_store.queue_run(
        run_id=run_id,
        user_request=user_request,
        metadata=queued_metadata,
    )
    _mark_session_active(
        orchestrator,
        session_id=session_id,
        run_id=run_id,
        user_id=current_user.username,
        workspace_id=workspace_id,
    )

    def admit_queued_run() -> None:
        try:
            queued_run = orchestrator.run_store.get_run(run_id)
            if queued_run is None or queued_run.status != RunStatus.QUEUED:
                admission_queue.release(current_user.username)
                return
            orchestrator.run_store.create_run(
                run_id=run_id,
                user_request=user_request,
                metadata=run_metadata,
            )
            registered_handle = registry.add(handle)
            if registered_handle is not handle:
                admission_queue.release(current_user.username)
        except Exception:
            logger.exception("Failed to admit queued run %s", run_id)
            orchestrator.run_store.fail_run(run_id, "Failed to start queued workflow run")
            _clear_session_active(
                orchestrator,
                session_id=session_id,
                run_id=run_id,
                user_id=current_user.username,
                workspace_id=workspace_id,
            )
            admission_queue.release(current_user.username)

    position = admission_queue.enqueue(
        QueuedRunAdmission(
            run_id=run_id,
            user_id=current_user.username,
            limit=limit,
            on_admit=admit_queued_run,
        )
    )
    if position is None:
        admitted_handle = registry.get(run_id)
        if admitted_handle is not None:
            return _stream_response(admitted_handle)
    return jsonify(_queued_run_payload(orchestrator, run_id)), 202


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

    status["links"] = _run_links(run_id)
    if status.get("status") == RunStatus.QUEUED.value:
        run = orchestrator.run_store.get_run(run_id)
        metadata = dict(getattr(run, "metadata", {}) or {})
        status["queue"] = {
            "position": _get_run_admission_queue(orchestrator).position(run_id),
            "limit": metadata.get("admission_limit"),
            "queued_at": metadata.get("queued_at"),
        }

    return jsonify(status)


@workflows_bp.route("/runs/<run_id>/events", methods=["GET"])
@require_auth()
def stream_run_events(run_id: str):
    """Attach or reattach to one admitted run using an SSE sequence cursor."""
    current_user = get_current_user()
    if not current_user or not _ensure_run_owner(run_id, current_user.username):
        return jsonify({"error": "Run not found"}), 404

    orchestrator = _get_orchestrator()
    run = orchestrator.run_store.get_run(run_id)
    if run is not None and run.status == RunStatus.QUEUED:
        return jsonify(_queued_run_payload(orchestrator, run_id)), 409

    handle = get_run_registry().get(run_id)
    if handle is None:
        return jsonify({
            "error": "Run event stream is no longer available",
            "run_id": run_id,
            "status": run.status.value if run is not None else "not_found",
        }), 410

    cursor_raw = request.headers.get("Last-Event-ID") or request.args.get("after_seq") or 0
    return _stream_response(handle, after_seq=_coerce_seq(cursor_raw))


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
    run = orchestrator.run_store.get_run(run_id)
    was_queued = _get_run_admission_queue(orchestrator).cancel(run_id)
    success = orchestrator.cancel_run(run_id, reason=reason)

    if not success:
        return jsonify({"error": "Run not found"}), 404

    if was_queued and run is not None:
        metadata = dict(run.metadata or {})
        session_id = str(metadata.get("session_id") or "").strip()
        workspace_id = str(metadata.get("workspace_id") or "").strip()
        if session_id and workspace_id:
            _clear_session_active(
                orchestrator,
                session_id=session_id,
                run_id=run_id,
                user_id=current_user.username,
                workspace_id=workspace_id,
            )

    return jsonify({"success": True, "cancelled": True, "was_queued": was_queued})


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
                traceparent=trace_context.value if trace_context is not None else "",
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
        "final_answer": coerce_final_output_to_text(final_output),
        "session_id": session_id,
        "run_id": run_id,
    })
