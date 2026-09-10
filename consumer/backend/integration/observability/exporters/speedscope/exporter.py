"""Write session-scoped Speedscope evented profiles from runtime events."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability.speedscope_io import (
    resolve_speedscope_output_dir,
    speedscope_file_lock,
    speedscope_session_lock_path,
    speedscope_session_path,
)
from utils.logging import get_logger

logger = get_logger("observability.exporters.speedscope")

SPEEDSCOPE_SCHEMA = "https://www.speedscope.app/file-format-schema.json"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_PENDING_DROP_LIMIT = 10000
DEFAULT_PENDING_DROP_TTL_SECONDS = 3600.0

_START_EVENTS = {
    RuntimeEventType.RUN_STARTED: "run",
    RuntimeEventType.AGENT_LOOP_STARTED: "agent_loop",
    RuntimeEventType.AGENT_ITERATION_PREPARED: "iteration",
    RuntimeEventType.LLM_STARTED: "llm",
    RuntimeEventType.TOOL_BATCH_STARTED: "tool_batch",
    RuntimeEventType.TOOL_STARTED: "tool",
    RuntimeEventType.RUNTIME_PHASE_STARTED: "runtime_phase",
}
_END_EVENTS = {
    RuntimeEventType.RUN_FINISHED: "run",
    RuntimeEventType.RUN_FAILED: "run",
    RuntimeEventType.RUN_CANCELLED: "run",
    RuntimeEventType.AGENT_LOOP_FINISHED: "agent_loop",
    RuntimeEventType.AGENT_ITERATION_FINISHED: "iteration",
    RuntimeEventType.LLM_FINISHED: "llm",
    RuntimeEventType.LLM_FAILED: "llm",
    RuntimeEventType.TOOL_BATCH_FINISHED: "tool_batch",
    RuntimeEventType.TOOL_BATCH_FAILED: "tool_batch",
    RuntimeEventType.TOOL_FINISHED: "tool",
    RuntimeEventType.RUNTIME_PHASE_FINISHED: "runtime_phase",
    RuntimeEventType.RUNTIME_PHASE_FAILED: "runtime_phase",
}
_RUN_TERMINAL_EVENTS = {
    RuntimeEventType.RUN_FINISHED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}


@dataclass
class _Span:
    id: str
    raw_key: str
    kind: str
    name: str
    start_ms: int
    end_ms: Optional[int]
    parent_raw_key: Optional[str]
    status: str = ""
    caller: str = ""
    iteration: Optional[int] = None
    tool_call_id: str = ""
    request_label: str = ""
    root_tool_call_id: str = ""
    model: str = ""
    model_provider: str = ""
    display_name: str = ""
    tool_name: str = ""
    tool_names: tuple[str, ...] = ()
    component: str = ""
    phase_name: str = ""
    purpose: str = ""
    agent_mode: str = ""
    call_index: Optional[int] = None
    parent_span_id: Optional[str] = None
    parent_id: Optional[str] = None


@dataclass
class _RunTrace:
    run_id: str
    session_id: str
    user_id: str
    turn_id: Optional[int]
    first_ts_ms: int
    last_ts_ms: int
    spans: Dict[str, _Span] = field(default_factory=dict)
    active_by_raw_key: Dict[str, str] = field(default_factory=dict)
    raw_key_counts: Dict[str, int] = field(default_factory=dict)
    terminal: bool = False
    terminal_status: str = ""
    dropped_count: int = 0
    repair_count: int = 0
    terminal_seen_at: float = 0.0


@dataclass
class _PendingDrop:
    run_id: str
    session_id: str
    user_id: str
    turn_id: Optional[int]
    first_ts_ms: int
    last_ts_ms: int
    dropped_count: int = 0
    terminal: bool = False
    terminal_status: str = ""
    terminal_seen_at: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)


class SpeedscopeExporter:
    """Build valid Speedscope files without persisting rich runtime payloads."""

    def __init__(
        self,
        *,
        output_dir: str | Path | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        pending_drop_limit: int = DEFAULT_PENDING_DROP_LIMIT,
        pending_drop_ttl_seconds: float = DEFAULT_PENDING_DROP_TTL_SECONDS,
    ) -> None:
        self.output_dir = resolve_speedscope_output_dir(output_dir)
        self.retention_days = max(1, int(retention_days or DEFAULT_RETENTION_DAYS))
        self.pending_drop_limit = max(1, int(pending_drop_limit or DEFAULT_PENDING_DROP_LIMIT))
        self.pending_drop_ttl_seconds = max(
            1.0,
            float(pending_drop_ttl_seconds or DEFAULT_PENDING_DROP_TTL_SECONDS),
        )
        self._runs: Dict[str, _RunTrace] = {}
        self._pending_drops: Dict[str, _PendingDrop] = {}
        self._lock = threading.RLock()
        self._last_prune_day = ""

    @property
    def available(self) -> bool:
        return True

    def export(self, event: RuntimeEvent) -> None:
        if not str(event.run_id or "").strip() or not str(event.session_id or "").strip():
            return
        if event.type not in _START_EVENTS and event.type not in _END_EVENTS:
            return

        with self._lock:
            run = self._ensure_run(event)
            run.last_ts_ms = max(run.last_ts_ms, int(event.timestamp_ms))
            if event.type in _START_EVENTS:
                self._open_span(run, event, _START_EVENTS[event.type])
            else:
                self._close_span(run, event, _END_EVENTS[event.type])

            if event.type in _RUN_TERMINAL_EVENTS:
                run.terminal = True
                run.terminal_status = _normalized_status(event)
                run.terminal_seen_at = time.monotonic()

            # Some persistence phases happen immediately after run.finished.
            # Rewriting after each late event keeps the completed session trace whole.
            should_write = event.type in _RUN_TERMINAL_EVENTS or event.type in _END_EVENTS
            if run.terminal and should_write:
                self._write_run(run)
            self._discard_settled_runs()

    def record_drop(self, event: RuntimeEvent) -> None:
        run_id = str(getattr(event, "run_id", "") or "").strip()
        session_id = str(getattr(event, "session_id", "") or "").strip()
        payload = dict(getattr(event, "payload", {}) or {})
        user_id = str(payload.get("user_id") or "").strip()
        if not run_id or not session_id:
            return
        with self._lock:
            timestamp_ms = int(getattr(event, "timestamp_ms", 0) or time.time() * 1000)
            terminal = event.type in _RUN_TERMINAL_EVENTS
            now = time.monotonic()
            run = self._runs.get(run_id)
            if run is not None:
                run.dropped_count += 1
                run.last_ts_ms = max(run.last_ts_ms, timestamp_ms)
                if terminal:
                    run.terminal = True
                    run.terminal_status = _normalized_status(event)
                    run.terminal_seen_at = now
                return
            pending = self._pending_drops.get(run_id)
            if pending is None:
                pending = _PendingDrop(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=user_id,
                    turn_id=_optional_int(getattr(event, "turn_id", None)),
                    first_ts_ms=timestamp_ms,
                    last_ts_ms=timestamp_ms,
                )
                self._pending_drops[run_id] = pending
            pending.session_id = pending.session_id or session_id
            pending.user_id = pending.user_id or user_id
            if pending.turn_id is None:
                pending.turn_id = _optional_int(getattr(event, "turn_id", None))
            pending.first_ts_ms = min(pending.first_ts_ms, timestamp_ms)
            pending.last_ts_ms = max(pending.last_ts_ms, timestamp_ms)
            pending.dropped_count += 1
            pending.updated_at = now
            if terminal:
                pending.terminal = True
                pending.terminal_status = _normalized_status(event)
                pending.terminal_seen_at = now
            self._discard_pending_drops(now)

    def flush(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._discard_pending_drops(now)
            for run in self._runs.values():
                if run.terminal:
                    self._write_run(run)
            pending_terminal_runs = [
                self._run_from_pending_drop(pending) for pending in self._pending_drops.values() if pending.terminal
            ]
            for run in pending_terminal_runs:
                self._write_run(run)
                self._pending_drops.pop(run.run_id, None)
            self._discard_settled_runs()

    def close(self) -> None:
        self.flush()

    @staticmethod
    def _run_from_pending_drop(pending: _PendingDrop) -> _RunTrace:
        return _RunTrace(
            run_id=pending.run_id,
            session_id=pending.session_id,
            user_id=pending.user_id,
            turn_id=pending.turn_id,
            first_ts_ms=pending.first_ts_ms,
            last_ts_ms=max(pending.first_ts_ms, pending.last_ts_ms),
            terminal=True,
            terminal_status=pending.terminal_status,
            dropped_count=pending.dropped_count,
            terminal_seen_at=pending.terminal_seen_at or time.monotonic(),
        )

    def _discard_pending_drops(self, now: float) -> None:
        expired = [
            run_id
            for run_id, pending in self._pending_drops.items()
            if not pending.terminal and now - pending.updated_at >= self.pending_drop_ttl_seconds
        ]
        for run_id in expired:
            self._pending_drops.pop(run_id, None)

        while len(self._pending_drops) > self.pending_drop_limit:
            victim = min(
                self._pending_drops.values(),
                key=lambda pending: (
                    1 if pending.terminal else 0,
                    pending.updated_at,
                    pending.run_id,
                ),
            )
            self._pending_drops.pop(victim.run_id, None)

    def _ensure_run(self, event: RuntimeEvent) -> _RunTrace:
        run = self._runs.get(event.run_id)
        if run is not None:
            return run
        pending = self._pending_drops.pop(event.run_id, None)
        event_ts_ms = int(event.timestamp_ms)
        run = _RunTrace(
            run_id=event.run_id,
            session_id=str(event.session_id or (pending.session_id if pending else "") or ""),
            user_id=_event_user_id(event) or (pending.user_id if pending else ""),
            turn_id=(
                _optional_int(event.turn_id) if event.turn_id is not None else (pending.turn_id if pending else None)
            ),
            first_ts_ms=min(event_ts_ms, pending.first_ts_ms) if pending else event_ts_ms,
            last_ts_ms=max(event_ts_ms, pending.last_ts_ms) if pending else event_ts_ms,
            terminal=bool(pending.terminal) if pending else False,
            terminal_status=pending.terminal_status if pending else "",
            dropped_count=int(pending.dropped_count if pending else 0),
            terminal_seen_at=pending.terminal_seen_at if pending and pending.terminal else 0.0,
        )
        self._runs[event.run_id] = run
        return run

    @staticmethod
    def _open_span(run: _RunTrace, event: RuntimeEvent, kind: str) -> None:
        raw_key = _event_span_key(event, kind)
        previous_id = run.active_by_raw_key.get(raw_key)
        if previous_id:
            previous = run.spans.get(previous_id)
            if previous is not None and previous.end_ms is None:
                previous.end_ms = max(previous.start_ms, int(event.timestamp_ms))
                previous.status = "incomplete"
                run.repair_count += 1

        sequence = int(run.raw_key_counts.get(raw_key) or 0) + 1
        run.raw_key_counts[raw_key] = sequence
        span_id = event.span_id or "{}#{}".format(raw_key, sequence)
        if span_id in run.spans:
            span_id = "{}#{}".format(span_id, sequence)
        run.spans[span_id] = _span_from_event(
            event,
            span_id=span_id,
            raw_key=raw_key,
            kind=kind,
            start_ms=int(event.timestamp_ms),
            end_ms=None,
        )
        run.active_by_raw_key[raw_key] = span_id

    @staticmethod
    def _close_span(run: _RunTrace, event: RuntimeEvent, kind: str) -> None:
        raw_key = _event_span_key(event, kind)
        span_id = run.active_by_raw_key.pop(raw_key, None)
        span = run.spans.get(span_id or "")
        end_ms = int(event.timestamp_ms)
        if span is None:
            duration_ms = _event_duration_ms(event)
            sequence = int(run.raw_key_counts.get(raw_key) or 0) + 1
            run.raw_key_counts[raw_key] = sequence
            span_id = event.span_id or "{}#{}".format(raw_key, sequence)
            if span_id in run.spans:
                span_id = "{}#{}".format(span_id, sequence)
            span = _span_from_event(
                event,
                span_id=span_id,
                raw_key=raw_key,
                kind=kind,
                start_ms=max(run.first_ts_ms, end_ms - duration_ms),
                end_ms=end_ms,
            )
            run.spans[span_id] = span
            if not _event_has_duration(event) and kind != "run":
                run.repair_count += 1
        else:
            span.end_ms = max(span.start_ms, end_ms)
            span.status = _normalized_status(event)
            _merge_safe_span_fields(span, event)
            span.name = _span_name_from_span(span)

    def _write_run(self, run: _RunTrace) -> None:
        spans, incomplete = _materialize_spans(run)
        path = speedscope_session_path(
            self.output_dir,
            run.session_id,
            user_id=run.user_id or None,
        )
        with speedscope_file_lock(path):
            document = _load_session_document(path, run.session_id)
            active_profile_name = _active_profile_name(document)
            run_prefix = _run_profile_prefix(run)
            document["profiles"] = [
                profile for profile in document["profiles"] if not str(profile.get("name") or "").startswith(run_prefix)
            ]
            document = _compact_document_frames(document)
            frames = document["shared"]["frames"]
            frame_indexes = {
                (str(frame.get("name") or ""), str(frame.get("file") or "")): index
                for index, frame in enumerate(frames)
                if isinstance(frame, dict)
            }
            profiles = _build_profiles(
                run,
                spans,
                incomplete=incomplete,
                frames=frames,
                frame_indexes=frame_indexes,
            )
            first_new_profile = len(document["profiles"])
            document["profiles"].extend(profiles)
            if profiles and not _restore_active_profile(document, active_profile_name):
                document["activeProfileIndex"] = first_new_profile
            _atomic_write_json(path, document)
        self._prune_old_files(path)

    def _prune_old_files(self, current_path: Path) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime())
        if today == self._last_prune_day:
            return
        self._last_prune_day = today
        cutoff = time.time() - self.retention_days * 24 * 60 * 60
        for path in self.output_dir.rglob("session-*.speedscope.json"):
            if path == current_path:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    _unlink_speedscope_lock(path)
            except OSError:
                logger.warning("Failed to prune Speedscope trace %s", path, exc_info=True)

    def _discard_settled_runs(self) -> None:
        now = time.monotonic()
        stale = [
            run_id
            for run_id, run in self._runs.items()
            if run.terminal and run.terminal_seen_at and now - run.terminal_seen_at >= 60.0
        ]
        for run_id in stale:
            self._runs.pop(run_id, None)


def create_speedscope_exporter(
    config: Mapping[str, Any] | None = None,
    *,
    output_dir: str | Path | None = None,
) -> SpeedscopeExporter:
    config = config if isinstance(config, Mapping) else {}
    speedscope = config.get("speedscope") if isinstance(config.get("speedscope"), Mapping) else {}
    retention_days = speedscope.get("retention_days", DEFAULT_RETENTION_DAYS)
    return SpeedscopeExporter(output_dir=output_dir, retention_days=retention_days)


def _unlink_speedscope_lock(path: Path) -> None:
    try:
        speedscope_session_lock_path(path).unlink()
    except FileNotFoundError:
        return


def _span_from_event(
    event: RuntimeEvent,
    *,
    span_id: str,
    raw_key: str,
    kind: str,
    start_ms: int,
    end_ms: Optional[int],
) -> _Span:
    payload = dict(event.payload or {})
    return _Span(
        id=span_id,
        raw_key=raw_key,
        kind=kind,
        name=_span_name(kind, payload),
        start_ms=start_ms,
        end_ms=end_ms,
        parent_raw_key=str(event.parent_key or "").strip() or None,
        status=_normalized_status(event),
        caller=_bounded_label(payload.get("caller")),
        iteration=_optional_int(payload.get("iteration")),
        tool_call_id=_bounded_label(payload.get("tool_call_id")),
        request_label=_bounded_label(payload.get("request_label")),
        root_tool_call_id=_bounded_label(payload.get("root_tool_call_id")),
        model=_bounded_label(payload.get("model") or payload.get("response_model")),
        model_provider=_bounded_label(payload.get("model_provider")),
        display_name=_bounded_label(payload.get("display_name")),
        tool_name=_bounded_label(payload.get("tool_name")),
        tool_names=tuple(_bounded_label(name) for name in _list_labels(payload.get("tool_names"))),
        component=_bounded_label(payload.get("component")),
        phase_name=_bounded_label(payload.get("phase_name")),
        purpose=_bounded_label(payload.get("purpose")),
        agent_mode=_bounded_label(payload.get("agent_mode")),
        call_index=_optional_int(payload.get("call_index")),
        parent_span_id=str(event.parent_span_id or "").strip() or None,
    )


def _merge_safe_span_fields(span: _Span, event: RuntimeEvent) -> None:
    payload = dict(event.payload or {})
    span.status = _normalized_status(event) or span.status
    span.caller = span.caller or _bounded_label(payload.get("caller"))
    span.iteration = span.iteration if span.iteration is not None else _optional_int(payload.get("iteration"))
    span.tool_call_id = span.tool_call_id or _bounded_label(payload.get("tool_call_id"))
    span.request_label = span.request_label or _bounded_label(payload.get("request_label"))
    span.root_tool_call_id = span.root_tool_call_id or _bounded_label(payload.get("root_tool_call_id"))
    span.model = _prefer_label(span.model, payload.get("model") or payload.get("response_model"))
    span.model_provider = _prefer_label(span.model_provider, payload.get("model_provider"))
    span.display_name = _prefer_label(span.display_name, payload.get("display_name"))
    span.tool_name = _prefer_label(span.tool_name, payload.get("tool_name"))
    span.tool_names = span.tool_names or tuple(_bounded_label(name) for name in _list_labels(payload.get("tool_names")))
    span.component = span.component or _bounded_label(payload.get("component"))
    span.phase_name = span.phase_name or _bounded_label(payload.get("phase_name"))
    span.purpose = span.purpose or _bounded_label(payload.get("purpose"))
    span.agent_mode = span.agent_mode or _bounded_label(payload.get("agent_mode"))
    span.call_index = span.call_index if span.call_index is not None else _optional_int(payload.get("call_index"))
    span.parent_span_id = span.parent_span_id or str(event.parent_span_id or "").strip() or None


def _span_name_from_span(span: _Span) -> str:
    if span.kind == "run":
        return "Knowledge QA Run"
    if span.kind == "agent_loop":
        return _join_name("Agent Loop", span.caller or "main", span.agent_mode)
    if span.kind == "iteration":
        return _join_name(
            "Iteration {}".format(span.iteration or "?"),
            span.caller or "main",
            span.purpose,
            span.agent_mode,
        )
    if span.kind == "llm":
        model_label = _join_name(span.model_provider, span.model) or "model unknown"
        call = "call {}".format(span.call_index) if span.call_index is not None else ""
        return _join_name(
            "LLM",
            model_label,
            span.caller or "main",
            "iteration {}".format(span.iteration or "?"),
            call,
            span.request_label,
        )
    if span.kind == "tool_batch":
        tool_names = ", ".join(span.tool_names[:5])
        return _join_name(
            "Tool Batch",
            span.caller or "main",
            "iteration {}".format(span.iteration or "?"),
            tool_names,
        )
    if span.kind == "tool":
        return _join_name(
            "Tool",
            span.display_name or span.tool_name or "unknown",
            span.caller or "",
            "iteration {}".format(span.iteration) if span.iteration is not None else "",
        )
    if span.kind == "runtime_phase":
        return _join_name("Phase", span.component or "runtime", span.phase_name or "unknown")
    return span.kind


def _span_name(kind: str, payload: Mapping[str, Any]) -> str:
    caller = _bounded_label(payload.get("caller")) or "main"
    iteration = _optional_int(payload.get("iteration"))
    if kind == "run":
        return "Knowledge QA Run"
    if kind == "agent_loop":
        mode = _bounded_label(payload.get("agent_mode"))
        return _join_name("Agent Loop", caller, mode)
    if kind == "iteration":
        purpose = _bounded_label(payload.get("purpose"))
        mode = _bounded_label(payload.get("agent_mode"))
        return _join_name("Iteration {}".format(iteration or "?"), caller, purpose, mode)
    if kind == "llm":
        model = _bounded_label(payload.get("model") or payload.get("response_model"))
        provider = _bounded_label(payload.get("model_provider"))
        model_label = _join_name(provider, model) or "model unknown"
        call_index = _optional_int(payload.get("call_index"))
        call = "call {}".format(call_index) if call_index is not None else ""
        label = _bounded_label(payload.get("request_label"))
        return _join_name("LLM", model_label, caller, "iteration {}".format(iteration or "?"), call, label)
    if kind == "tool_batch":
        tool_names = ", ".join(_list_labels(payload.get("tool_names"))[:5])
        return _join_name("Tool Batch", caller, "iteration {}".format(iteration or "?"), tool_names)
    if kind == "tool":
        tool = _bounded_label(payload.get("display_name") or payload.get("tool_name")) or "unknown"
        return _join_name("Tool", tool)
    if kind == "runtime_phase":
        component = _bounded_label(payload.get("component")) or "runtime"
        phase = _bounded_label(payload.get("phase_name")) or "unknown"
        return _join_name("Phase", component, phase)
    return kind


def _event_span_key(event: RuntimeEvent, kind: str) -> str:
    explicit = str(event.span_key or "").strip()
    if explicit:
        return explicit
    payload = dict(event.payload or {})
    if kind == "run":
        return "run:{}".format(event.run_id)
    if kind == "runtime_phase":
        return "phase:{}:{}".format(
            _bounded_label(payload.get("component")) or "runtime",
            _bounded_label(payload.get("phase_name")) or "unknown",
        )
    caller = _bounded_label(payload.get("caller")) or "main"
    iteration = _optional_int(payload.get("iteration")) or 0
    return "{}:{}:{}".format(kind, caller, iteration)


def _event_duration_ms(event: RuntimeEvent) -> int:
    payload = dict(event.payload or {})
    for key in (
        "duration_ms",
        "elapsed_ms",
        "wall_duration_ms",
        "total_elapsed_ms",
        "total_ms",
        "stream_duration_ms",
    ):
        value = _optional_int(payload.get(key))
        if value is not None:
            return max(0, value)
    return 0


def _event_has_duration(event: RuntimeEvent) -> bool:
    payload = dict(event.payload or {})
    duration_keys = (
        "duration_ms",
        "elapsed_ms",
        "wall_duration_ms",
        "total_elapsed_ms",
        "total_ms",
        "stream_duration_ms",
    )
    return any(key in payload for key in duration_keys)


def _normalized_status(event: RuntimeEvent) -> str:
    payload = dict(event.payload or {})
    status = str(payload.get("status") or "").strip().lower()
    if status:
        return status
    if event.type in {
        RuntimeEventType.RUN_FAILED,
        RuntimeEventType.LLM_FAILED,
        RuntimeEventType.TOOL_BATCH_FAILED,
        RuntimeEventType.RUNTIME_PHASE_FAILED,
    }:
        return RuntimeStatus.ERROR
    if event.type == RuntimeEventType.RUN_CANCELLED:
        return RuntimeStatus.CANCELLED
    return ""


def _materialize_spans(run: _RunTrace) -> tuple[Dict[str, _Span], bool]:
    spans = copy.deepcopy(run.spans)
    root = _find_run_root(spans)
    if root is None:
        root = _Span(
            id="run:{}#synthetic".format(run.run_id),
            raw_key="run:{}".format(run.run_id),
            kind="run",
            name="Knowledge QA Run",
            start_ms=min((span.start_ms for span in spans.values()), default=run.first_ts_ms),
            end_ms=run.last_ts_ms,
            parent_raw_key=None,
            status=run.terminal_status,
        )
        spans[root.id] = root

    auto_closed = 0
    root.start_ms = min(root.start_ms, run.first_ts_ms)
    root.end_ms = max(root.end_ms or root.start_ms, run.last_ts_ms)
    root.status = run.terminal_status or root.status
    for span in spans.values():
        if span.end_ms is None:
            span.end_ms = max(span.start_ms, run.last_ts_ms)
            span.status = span.status or "incomplete"
            auto_closed += 1

    for span in spans.values():
        if span.id == root.id:
            continue
        span.parent_id = _resolve_parent_id(span, spans, root)
        if span.parent_id == span.id or _would_cycle(span, spans):
            span.parent_id = root.id

    # Extend ancestors to contain children. This repairs late terminal events
    # without altering the measured child durations.
    for _ in range(max(1, len(spans))):
        changed = False
        for span in spans.values():
            parent = spans.get(span.parent_id or "")
            if parent is None:
                continue
            new_start = min(parent.start_ms, span.start_ms)
            new_end = max(parent.end_ms or parent.start_ms, span.end_ms or span.start_ms)
            if new_start != parent.start_ms or new_end != parent.end_ms:
                parent.start_ms = new_start
                parent.end_ms = new_end
                changed = True
        if not changed:
            break

    incomplete = bool(run.dropped_count or run.repair_count or auto_closed)
    if incomplete:
        root.name = "{} [INCOMPLETE: dropped {}, repaired {}]".format(
            root.name,
            run.dropped_count,
            run.repair_count + auto_closed,
        )
    return spans, incomplete


def _find_run_root(spans: Mapping[str, _Span]) -> Optional[_Span]:
    roots = [span for span in spans.values() if span.kind == "run"]
    return min(roots, key=lambda span: span.start_ms) if roots else None


def _resolve_parent_id(span: _Span, spans: Mapping[str, _Span], root: _Span) -> str:
    if span.parent_span_id:
        parent = spans.get(span.parent_span_id)
        if parent is not None and parent.id != span.id:
            return parent.id

    if span.kind == "agent_loop" and span.root_tool_call_id:
        parent = _containing_tool_by_call_id(span, spans, span.root_tool_call_id)
        if parent is not None:
            return parent.id

    if span.kind == "tool" and span.request_label == "graph_internal_tool":
        parent = _containing_named_tool(span, spans, "business_data_search")
        if parent is not None:
            return parent.id

    desired_raw = _desired_parent_raw_key(span, root)
    candidates = [candidate for candidate in spans.values() if candidate.raw_key == desired_raw]
    parent = _best_parent(span, candidates)
    return parent.id if parent is not None else root.id


def _desired_parent_raw_key(span: _Span, root: _Span) -> str:
    if span.kind == "agent_loop":
        return root.raw_key
    if span.kind == "runtime_phase":
        return span.parent_raw_key or root.raw_key
    suffix = _caller_suffix_from_raw_key(span.raw_key, span.caller)
    if span.kind == "iteration":
        return span.parent_raw_key or "agent_loop:{}".format(suffix)
    if span.kind in {"llm", "tool_batch"} and span.iteration is not None:
        return "iteration:{}:{}".format(suffix, span.iteration)
    return span.parent_raw_key or root.raw_key


def _caller_suffix_from_raw_key(raw_key: str, fallback: str) -> str:
    for prefix in ("iteration:", "llm:", "tools:"):
        if not raw_key.startswith(prefix):
            continue
        remainder = raw_key[len(prefix) :]
        parts = remainder.split(":")
        if len(parts) >= 3 and parts[0] == "subagent":
            return "subagent:{}".format(parts[1])
        if parts:
            return parts[0]
    return fallback or "main"


def _best_parent(child: _Span, candidates: Iterable[_Span]) -> Optional[_Span]:
    options = [candidate for candidate in candidates if candidate.id != child.id]
    containing = []
    for candidate in options:
        contains_start = candidate.start_ms <= child.start_ms
        contains_end = (candidate.end_ms or candidate.start_ms) >= (child.end_ms or child.start_ms)
        if contains_start and contains_end:
            containing.append(candidate)
    if containing:
        return min(containing, key=lambda item: (item.end_ms or item.start_ms) - item.start_ms)
    return min(options, key=lambda item: abs(item.start_ms - child.start_ms), default=None)


def _containing_tool_by_call_id(child: _Span, spans: Mapping[str, _Span], call_id: str) -> Optional[_Span]:
    return _best_parent(
        child,
        [span for span in spans.values() if span.kind == "tool" and span.tool_call_id == call_id],
    )


def _containing_named_tool(child: _Span, spans: Mapping[str, _Span], tool_name: str) -> Optional[_Span]:
    marker = tool_name.lower()
    return _best_parent(
        child,
        [span for span in spans.values() if span.kind == "tool" and marker in span.name.lower()],
    )


def _would_cycle(span: _Span, spans: Mapping[str, _Span]) -> bool:
    seen = {span.id}
    current = span
    while current.parent_id:
        if current.parent_id in seen:
            return True
        seen.add(current.parent_id)
        parent = spans.get(current.parent_id)
        if parent is None:
            return False
        current = parent
    return False


def _build_profiles(
    run: _RunTrace,
    spans: Mapping[str, _Span],
    *,
    incomplete: bool,
    frames: list[dict[str, Any]],
    frame_indexes: Dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    root = _find_run_root(spans)
    if root is None:
        return []
    children = _children_by_parent(spans)
    leaves = [span for span in spans.values() if not children.get(span.id)] or [root]
    branches = [_branch_to_root(leaf, spans, root) for leaf in sorted(leaves, key=_span_sort_key)]
    lane_span_ids: list[set[str]] = []
    for branch in branches:
        branch_ids = {span.id for span in branch}
        for lane in lane_span_ids:
            merged = lane | branch_ids
            if _is_compatible_lane(merged, spans):
                lane.update(branch_ids)
                break
        else:
            lane_span_ids.append(set(branch_ids))

    profiles = []
    for lane_index, lane in enumerate(lane_span_ids, start=1):
        profile_name = "{} / Lane {}{}".format(
            _run_profile_prefix(run),
            lane_index,
            " [INCOMPLETE]" if incomplete else "",
        )
        events = _profile_events(
            root,
            lane,
            spans,
            frames=frames,
            frame_indexes=frame_indexes,
        )
        end_value = max(0.001, float((root.end_ms or root.start_ms) - root.start_ms))
        profiles.append(
            {
                "type": "evented",
                "name": profile_name,
                "unit": "milliseconds",
                "startValue": 0,
                "endValue": end_value,
                "events": events,
            }
        )
    return profiles


def _children_by_parent(spans: Mapping[str, _Span]) -> Dict[str, list[_Span]]:
    children: Dict[str, list[_Span]] = {}
    for span in spans.values():
        if span.parent_id:
            children.setdefault(span.parent_id, []).append(span)
    for rows in children.values():
        rows.sort(key=_span_sort_key)
    return children


def _branch_to_root(leaf: _Span, spans: Mapping[str, _Span], root: _Span) -> list[_Span]:
    branch = [leaf]
    seen = {leaf.id}
    current = leaf
    while current.id != root.id and current.parent_id:
        parent = spans.get(current.parent_id)
        if parent is None or parent.id in seen:
            break
        branch.append(parent)
        seen.add(parent.id)
        current = parent
    if root.id not in seen:
        branch.append(root)
    branch.reverse()
    return branch


def _is_compatible_lane(span_ids: set[str], spans: Mapping[str, _Span]) -> bool:
    rows = [spans[span_id] for span_id in span_ids]
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if not _overlaps(left, right):
                continue
            if _is_ancestor(left, right, spans) or _is_ancestor(right, left, spans):
                continue
            return False
    return True


def _overlaps(left: _Span, right: _Span) -> bool:
    left_end = left.end_ms or left.start_ms
    right_end = right.end_ms or right.start_ms
    return left.start_ms < right_end and right.start_ms < left_end


def _is_ancestor(candidate: _Span, child: _Span, spans: Mapping[str, _Span]) -> bool:
    current = child
    seen: set[str] = set()
    while current.parent_id and current.parent_id not in seen:
        if current.parent_id == candidate.id:
            return True
        seen.add(current.parent_id)
        parent = spans.get(current.parent_id)
        if parent is None:
            return False
        current = parent
    return False


def _profile_events(
    root: _Span,
    lane: set[str],
    spans: Mapping[str, _Span],
    *,
    frames: list[dict[str, Any]],
    frame_indexes: Dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    children = _children_by_parent({span_id: spans[span_id] for span_id in lane})
    events: list[dict[str, Any]] = []

    def visit(span: _Span) -> None:
        frame = _frame_index(span, frames, frame_indexes)
        events.append({"type": "O", "at": float(max(0, span.start_ms - root.start_ms)), "frame": frame})
        for child in children.get(span.id, []):
            visit(child)
        end_ms = max(span.start_ms, span.end_ms or span.start_ms)
        events.append({"type": "C", "at": float(max(0, end_ms - root.start_ms)), "frame": frame})

    visit(root)
    return events


def _frame_index(
    span: _Span,
    frames: list[dict[str, Any]],
    frame_indexes: Dict[tuple[str, str], int],
) -> int:
    status = str(span.status or "").lower()
    name = span.name
    if status in {"error", "failed", "cancelled", "incomplete"} and "INCOMPLETE" not in name:
        name = "{} [{}]".format(name, status.upper())
    file_name = "meta-wise/{}".format(span.kind)
    key = (name, file_name)
    index = frame_indexes.get(key)
    if index is not None:
        return index
    index = len(frames)
    frames.append({"name": name, "file": file_name})
    frame_indexes[key] = index
    return index


def _load_session_document(path: Path, session_id: str) -> dict[str, Any]:
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(document, dict)
                and document.get("$schema") == SPEEDSCOPE_SCHEMA
                and isinstance(document.get("profiles"), list)
                and isinstance(document.get("shared"), dict)
                and isinstance(document["shared"].get("frames"), list)
            ):
                return document
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable Speedscope session trace %s", path, exc_info=True)
    return {
        "$schema": SPEEDSCOPE_SCHEMA,
        "name": "Knowledge QA Session {}".format(session_id),
        "exporter": "meta-wise",
        "activeProfileIndex": 0,
        "shared": {"frames": []},
        "profiles": [],
    }


def _active_profile_name(document: Mapping[str, Any]) -> str:
    try:
        index = int(document.get("activeProfileIndex", 0))
    except (TypeError, ValueError):
        return ""
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or index < 0 or index >= len(profiles):
        return ""
    profile = profiles[index]
    return str(profile.get("name") or "") if isinstance(profile, dict) else ""


def _restore_active_profile(document: dict[str, Any], active_profile_name: str) -> bool:
    if active_profile_name:
        for index, profile in enumerate(document.get("profiles") or []):
            if isinstance(profile, dict) and str(profile.get("name") or "") == active_profile_name:
                document["activeProfileIndex"] = index
                return True
        return False
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        document["activeProfileIndex"] = 0
        return False
    try:
        index = int(document.get("activeProfileIndex", 0))
    except (TypeError, ValueError):
        index = 0
    if 0 <= index < len(profiles):
        return True
    document["activeProfileIndex"] = 0
    return False


def _compact_document_frames(document: dict[str, Any]) -> dict[str, Any]:
    old_frames = list(document.get("shared", {}).get("frames") or [])
    new_frames: list[dict[str, Any]] = []
    frame_indexes: dict[tuple[str, str], int] = {}
    index_map: dict[int, int] = {}

    def remap_frame(old_index: Any) -> Optional[int]:
        try:
            numeric_index = int(old_index)
        except (TypeError, ValueError):
            return None
        if numeric_index in index_map:
            return index_map[numeric_index]
        if numeric_index < 0 or numeric_index >= len(old_frames):
            return None
        frame = old_frames[numeric_index]
        if not isinstance(frame, dict):
            return None
        normalized = {
            "name": str(frame.get("name") or ""),
            "file": str(frame.get("file") or ""),
        }
        key = (normalized["name"], normalized["file"])
        new_index = frame_indexes.get(key)
        if new_index is None:
            new_index = len(new_frames)
            new_frames.append(normalized)
            frame_indexes[key] = new_index
        index_map[numeric_index] = new_index
        return new_index

    for profile in document.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        if profile.get("type") == "sampled":
            remapped_samples = []
            for sample in profile.get("samples") or []:
                if not isinstance(sample, list):
                    continue
                remapped = [remap_frame(index) for index in sample]
                remapped_samples.append([index for index in remapped if index is not None])
            profile["samples"] = remapped_samples
            continue
        for event in profile.get("events") or []:
            if not isinstance(event, dict) or "frame" not in event:
                continue
            new_index = remap_frame(event.get("frame"))
            if new_index is not None:
                event["frame"] = new_index

    document["shared"] = {"frames": new_frames}
    return document


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name("{}.{}.{}.tmp".format(path.name, os.getpid(), threading.get_ident()))
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _run_profile_prefix(run: _RunTrace) -> str:
    turn = run.turn_id if run.turn_id is not None else "?"
    return "Turn {} / Run {}".format(turn, run.run_id)


def _span_sort_key(span: _Span) -> tuple[int, int, str]:
    return (span.start_ms, span.end_ms or span.start_ms, span.id)


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_user_id(event: RuntimeEvent) -> str:
    payload = dict(getattr(event, "payload", {}) or {})
    return str(payload.get("user_id") or "").strip()


def _bounded_label(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _prefer_label(current: str, candidate: Any) -> str:
    value = _bounded_label(candidate)
    if not value:
        return current
    if not current or current.strip().lower() in {"unknown", "model unknown", "none", "null"}:
        return value
    return current


def _list_labels(value: Any, *, limit: int = 120) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    labels: list[str] = []
    for item in value:
        label = _bounded_label(item, limit=limit)
        if label:
            labels.append(label)
    return labels


def _join_name(*parts: Any) -> str:
    return " / ".join(str(part) for part in parts if str(part or "").strip())
