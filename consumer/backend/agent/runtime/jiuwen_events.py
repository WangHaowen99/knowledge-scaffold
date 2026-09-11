"""Translate Jiuwen Engine callbacks into stable runtime events."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any, NamedTuple, Optional

from integration.observability.identity import resolve_runtime_identity

from .contracts import RuntimeInvocationContext
from .events import RuntimeEvent


_MODEL_STARTED_TYPES = frozenset({"llm.started", "model.started"})
_IGNORED_MODEL_REQUEST_LABELS = frozenset({"reasoning_summary"})
_PROTOCOL_EVENT_TYPES = frozenset(
    {
        "tool.started",
        "tool.progress",
        "tool.finished",
        "business.checkpoint",
        "solver.input.awaiting_confirmation",
        "solver.progress",
        "solver.verification.completed",
        "solver.verification.rejected",
        "solver.result.authoritative",
        "subagent.started",
        "subagent.delta",
        "subagent.progress",
        "subagent.finished",
        "subagent_group.started",
        "subagent_group.finished",
        "interaction.requested",
    }
)
_SOLVER_EVENT_STATUSES = {
    "solver.input.awaiting_confirmation": "awaiting_input",
    "solver.progress": "running",
    "solver.verification.completed": "completed",
    "solver.verification.rejected": "failed",
    "solver.result.authoritative": "completed",
}
_ENGINE_EVENT_TYPES = frozenset(
    {
        "iteration.completed",
        "reasoning.summary",
    }
)
_STATUS_ALIASES = {
    "success": "completed",
    "error": "failed",
    "canceled": "cancelled",
}
_COALESCIBLE_EVENT_TYPES = frozenset({"token.delta", "reasoning.delta"})
_MAX_COALESCED_TEXT_CHARS = 4096


class _RuntimeEventCoalescingKey(NamedTuple):
    """Stable, hashable identity for coalescing adjacent runtime deltas."""

    event_type: str
    span_key: str | None
    parent_key: str | None
    trace_id: str | None
    span_id: str | None
    parent_span_id: str | None
    source_node: str
    source_component: str
    channel: str
    iteration: str
    call_index: str
    reasoning_block_id: str


class RuntimeEventBufferFull(RuntimeError):
    """Raised when internal producers outrun the bounded runtime consumer."""

    error_code = "RUNTIME_EVENT_BUFFER_FULL"
    public_message = "Jiuwen runtime event buffer is full"
    retriable = False


class JiuwenRuntimeEventBridge:
    """Synchronous callback facade backed by one bounded asyncio queue."""

    def __init__(
        self,
        context: RuntimeInvocationContext,
        *,
        max_buffer_size: int = 1024,
        source_node: str = "jiuwen_react",
    ) -> None:
        if max_buffer_size < 1:
            raise ValueError("max_buffer_size must be positive")
        self.context = context
        self.source_node = str(source_node or "jiuwen_react")
        self.queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=int(max_buffer_size)
        )
        self.max_buffer_size = int(max_buffer_size)
        self._closed = object()
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._queued_event_tag = object()
        self._coalesced_event_tag = object()
        self._buffer_lock = threading.Lock()
        self._buffered_count = 0
        self._coalesced_events: dict[_RuntimeEventCoalescingKey, RuntimeEvent] = {}
        self._coalescing_generation = 0
        self._accepting = True
        self._model_spans: set[str] = set()
        self._dedupe_lock = threading.Lock()
        self._close_pending = False
        self._closed_enqueued = False
        self._pending_terminal: RuntimeEvent | None = None
        self._terminal_committed = False
        self._overflowed = False

    @property
    def enabled(self) -> bool:
        return True

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    def emit(self, event: Any) -> bool:
        """RuntimeObserver callback used by AgentRuntimeTrace."""
        event_type = str(getattr(event, "type", "") or "").strip().lower()
        if event_type not in _MODEL_STARTED_TYPES:
            return False
        payload = dict(getattr(event, "payload", None) or {})
        request_label = str(payload.get("request_label") or "").strip().lower()
        if request_label in _IGNORED_MODEL_REQUEST_LABELS:
            return False
        span_key = str(getattr(event, "span_key", "") or "").strip()
        with self._dedupe_lock:
            if span_key and span_key in self._model_spans:
                return False
            if span_key:
                self._model_spans.add(span_key)
        model_payload: dict[str, Any] = {}
        if self.context.model_profile_id:
            model_payload["model_profile_id"] = self.context.model_profile_id
        for key in ("iteration", "call_index"):
            if payload.get(key) is not None:
                model_payload[key] = payload[key]
        self._publish(
            self._event(
                "model.started",
                "running",
                model_payload,
                component="model",
                span_key=span_key or None,
                parent_key=str(getattr(event, "parent_key", "") or "") or None,
                trace_id=str(getattr(event, "trace_id", "") or "") or None,
                span_id=str(getattr(event, "span_id", "") or "") or None,
                parent_span_id=(
                    str(getattr(event, "parent_span_id", "") or "") or None
                ),
            )
        )
        return True

    def token_callback(
        self,
        channel: str,
        text: str,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> None:
        normalized_text = str(text or "")
        if not normalized_text:
            return
        normalized_channel = "reasoning" if channel == "reasoning" else "content"
        payload: dict[str, Any] = {"text": normalized_text}
        for key in ("iteration", "reasoning_block_id"):
            value = (meta or {}).get(key)
            if value not in (None, ""):
                payload[key] = value
        iteration = max(1, int(payload.get("iteration") or 1))
        call_index = max(1, int((meta or {}).get("call_index") or 1))
        caller_suffix = str((meta or {}).get("caller_suffix") or "main").strip() or "main"
        event = self._event(
            "reasoning.delta" if normalized_channel == "reasoning" else "token.delta",
            "running",
            payload,
            component="model",
            span_key="llm:{}:{}:{}".format(
                caller_suffix,
                iteration,
                call_index,
            ),
            parent_key="iteration:{}:{}".format(caller_suffix, iteration),
        )
        for start in range(0, len(normalized_text), _MAX_COALESCED_TEXT_CHARS):
            self._publish(
                event.model_copy(
                    update={
                        "payload": {
                            **payload,
                            "text": normalized_text[
                                start : start + _MAX_COALESCED_TEXT_CHARS
                            ],
                        }
                    }
                )
            )

    def protocol_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        normalized_type = str(event_type or "").strip().lower()
        if normalized_type not in _PROTOCOL_EVENT_TYPES:
            return
        normalized_payload = dict(payload or {})
        if normalized_type in _SOLVER_EVENT_STATUSES:
            status = _SOLVER_EVENT_STATUSES[normalized_type]
            component = "solver"
        elif normalized_type == "interaction.requested":
            status = "awaiting_input"
            component = "interaction"
        else:
            status = "running"
            component = "tool"
        caller = str(normalized_payload.get("caller") or "main").strip() or "main"
        if caller == "subagent":
            subagent_id = str(normalized_payload.get("subagent_id") or "").strip()
            caller = "subagent:{}".format(subagent_id) if subagent_id else "subagent"
        iteration = max(1, int(normalized_payload.get("iteration") or 1))
        tool_name = str(
            normalized_payload.get("tool_name") or normalized_payload.get("name") or "unknown_tool"
        ).strip() or "unknown_tool"
        tool_call_id = str(normalized_payload.get("tool_call_id") or "no_id").strip() or "no_id"
        span_prefix = "solver" if component == "solver" else "tool"
        self._publish(
            self._event(
                normalized_type,
                status,
                normalized_payload,
                component=component,
                span_key="{}:{}:{}:{}:{}".format(
                    span_prefix,
                    caller,
                    iteration,
                    tool_name,
                    tool_call_id,
                ),
                parent_key="iteration:{}:{}".format(caller, iteration),
            )
        )

    def engine_event(self, event: Any) -> None:
        """Forward selected legacy Engine events without duplicating callbacks."""
        if not isinstance(event, Mapping):
            return
        data = event.get("data")
        if not isinstance(data, Mapping):
            data = event
        normalized_type = str(
            data.get("type") or data.get("event_type") or ""
        ).strip().lower()
        if normalized_type not in _ENGINE_EVENT_TYPES:
            return
        payload = data.get("payload")
        normalized_payload = dict(payload) if isinstance(payload, Mapping) else {}
        source = data.get("source")
        normalized_source = dict(source) if isinstance(source, Mapping) else {}
        component = str(
            normalized_source.get("component")
            or ("iteration" if normalized_type == "iteration.completed" else "model")
        ).strip()
        source_node = str(
            normalized_source.get("node") or self.source_node
        ).strip()
        self._publish(
            self._event(
                normalized_type,
                "running",
                normalized_payload,
                component=component,
                source_node=source_node,
                span_key=str(data.get("span_key") or "") or None,
                parent_key=str(data.get("parent_key") or "") or None,
                trace_id=str(data.get("trace_id") or "") or None,
                span_id=str(data.get("span_id") or "") or None,
                parent_span_id=str(data.get("parent_span_id") or "") or None,
            )
        )

    def turn_started(self) -> RuntimeEvent:
        return self._event(
            "turn.started",
            "running",
            {"turn_id": self.context.turn_id},
            component="agent",
        )

    def turn_finished(
        self,
        status: str,
        payload: Mapping[str, Any],
    ) -> RuntimeEvent:
        normalized_status = self.normalize_status(status)
        return self._event(
            "turn.finished",
            normalized_status,
            {**dict(payload or {}), "status": normalized_status},
            component="agent",
        )

    async def get(self) -> RuntimeEvent | None:
        item = await self.queue.get()
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and item[0] is self._queued_event_tag
        ):
            with self._buffer_lock:
                self._buffered_count -= 1
            item = item[1]
        elif (
            isinstance(item, tuple)
            and len(item) == 2
            and item[0] is self._coalesced_event_tag
        ):
            with self._buffer_lock:
                item = self._coalesced_events.pop(item[1], None)
                self._buffered_count -= 1
        self._flush_pending()
        return None if item is self._closed else item

    def publish(self, event: RuntimeEvent) -> None:
        self._publish(event)

    def finish(self, event: RuntimeEvent) -> None:
        self._stop_accepting()
        if threading.get_ident() == self._loop_thread_id:
            self._finish_now(event)
            return
        self._loop.call_soon_threadsafe(self._finish_now, event)

    def close(self) -> None:
        self._stop_accepting()
        if threading.get_ident() == self._loop_thread_id:
            self._close_now()
            return
        self._loop.call_soon_threadsafe(self._close_now)

    def _publish(self, event: Any) -> None:
        coalescing_base_key = self._coalescing_key(event)
        coalescing_key: _RuntimeEventCoalescingKey | None = None
        overflow = False
        with self._buffer_lock:
            if not self._accepting:
                if self._overflowed:
                    raise RuntimeEventBufferFull(
                        "Jiuwen runtime event buffer is full"
                    )
                return
            if coalescing_base_key is not None:
                # A non-delta event starts a new text segment. This prevents
                # a delta emitted after tool.progress from being merged into
                # a marker that is still waiting ahead of that progress event.
                coalescing_key = (
                    *coalescing_base_key,
                    self._coalescing_generation,
                )
                previous = self._coalesced_events.get(coalescing_key)
                if previous is not None:
                    previous_text = str(previous.payload.get("text") or "")
                    current_text = str(event.payload.get("text") or "")
                    if (
                        len(previous_text) + len(current_text)
                        <= _MAX_COALESCED_TEXT_CHARS
                    ):
                        self._coalesced_events[coalescing_key] = (
                            self._merge_coalesced(previous, event)
                        )
                        return
                    self._coalescing_generation += 1
                    coalescing_key = (
                        *coalescing_base_key,
                        self._coalescing_generation,
                    )
            else:
                self._coalescing_generation += 1
            if self._buffered_count >= self.max_buffer_size:
                self._accepting = False
                self._overflowed = True
                overflow = True
            else:
                self._buffered_count += 1
                if coalescing_key is not None:
                    self._coalesced_events[coalescing_key] = event

        if overflow:
            if threading.get_ident() == self._loop_thread_id:
                self._record_overflow_now()
            else:
                self._loop.call_soon_threadsafe(self._record_overflow_now)
            # Callback producers must stop as well as the async consumer. In
            # particular, silently returning here lets a synchronous SDK/tool
            # worker continue emitting after the owning Agent task has been
            # cancelled, leaving an orphan thread during loop shutdown.
            raise RuntimeEventBufferFull(
                "Jiuwen runtime event buffer is full"
            )
        if threading.get_ident() == self._loop_thread_id:
            if coalescing_key is None:
                self._put_reserved_nowait(event)
            else:
                self._put_reserved_coalesced_nowait(coalescing_key)
            return
        try:
            callback = (
                self._put_reserved_nowait
                if coalescing_key is None
                else self._put_reserved_coalesced_nowait
            )
            callback_arg = event if coalescing_key is None else coalescing_key
            self._loop.call_soon_threadsafe(callback, callback_arg)
        except BaseException:
            # A producer may race loop shutdown after reserving a slot. Do not
            # leave the accounting permanently full if the callback cannot be
            # handed to the loop.
            if coalescing_key is not None:
                with self._buffer_lock:
                    self._coalesced_events.pop(coalescing_key, None)
            self._rollback_reserved_slot()
            raise

    def _put_reserved_nowait(self, event: Any) -> None:
        try:
            self.queue.put_nowait((self._queued_event_tag, event))
        except asyncio.QueueFull:
            # Queue fullness should be impossible while reservations are
            # correct. Treat it as an overflow rather than leaking a callback
            # exception from the event loop.
            self._rollback_reserved_slot()
            with self._buffer_lock:
                self._accepting = False
                self._overflowed = True
            self._record_overflow_now()

    def _put_reserved_coalesced_nowait(
        self,
        key: _RuntimeEventCoalescingKey,
    ) -> None:
        try:
            self.queue.put_nowait((self._coalesced_event_tag, key))
        except asyncio.QueueFull:
            with self._buffer_lock:
                self._coalesced_events.pop(key, None)
            self._rollback_reserved_slot()
            with self._buffer_lock:
                self._accepting = False
                self._overflowed = True
            self._record_overflow_now()

    def _rollback_reserved_slot(self) -> None:
        with self._buffer_lock:
            self._buffered_count = max(0, self._buffered_count - 1)

    @staticmethod
    def _coalescing_key(event: Any) -> _RuntimeEventCoalescingKey | None:
        if not isinstance(event, RuntimeEvent):
            return None
        event_type = str(event.event_type)
        if event_type not in _COALESCIBLE_EVENT_TYPES:
            return None
        payload = event.payload
        return _RuntimeEventCoalescingKey(
            event_type=event_type,
            span_key=event.span_key,
            parent_key=event.parent_key,
            trace_id=event.trace_id,
            span_id=event.span_id,
            parent_span_id=event.parent_span_id,
            source_node=str((event.source or {}).get("node") or ""),
            source_component=str((event.source or {}).get("component") or ""),
            channel=str(payload.get("channel") or ""),
            iteration=str(payload.get("iteration") or ""),
            call_index=str(payload.get("call_index") or ""),
            reasoning_block_id=str(payload.get("reasoning_block_id") or ""),
        )

    @staticmethod
    def _merge_coalesced(previous: RuntimeEvent, current: RuntimeEvent) -> RuntimeEvent:
        payload = dict(previous.payload)
        payload["text"] = "{}{}".format(
            str(payload.get("text") or ""),
            str(current.payload.get("text") or ""),
        )
        return previous.model_copy(update={"payload": payload})

    def _stop_accepting(self) -> None:
        with self._buffer_lock:
            self._accepting = False

    def _record_overflow_now(self) -> None:
        if self._terminal_committed:
            return
        self._stop_accepting()
        self._terminal_committed = True
        self._pending_terminal = self.turn_finished(
            "failed",
            {
                "error": {
                    "error_code": "RUNTIME_EVENT_BUFFER_FULL",
                    "message": "Jiuwen runtime event buffer is full",
                    "retriable": False,
                    "source": "runtime",
                    "details": {"max_buffer_size": self.max_buffer_size},
                }
            },
        )
        self._close_pending = True
        self._flush_pending()

    def _finish_now(self, event: RuntimeEvent) -> None:
        if self._terminal_committed:
            return
        self._terminal_committed = True
        self._stop_accepting()
        self._pending_terminal = event
        self._close_pending = True
        self._flush_pending()

    def _close_now(self) -> None:
        self._stop_accepting()
        self._close_pending = True
        self._flush_pending()

    def _flush_pending(self) -> None:
        with self._buffer_lock:
            if self._buffered_count:
                return
        if self._pending_terminal is not None and not self.queue.full():
            terminal = self._pending_terminal
            self._pending_terminal = None
            self.queue.put_nowait(terminal)
        if not self._close_pending:
            return
        if self._pending_terminal is not None:
            return
        if self._closed_enqueued:
            return
        if self.queue.full():
            return
        self.queue.put_nowait(self._closed)
        self._closed_enqueued = True

    def _event(
        self,
        event_type: str,
        status: str,
        payload: Mapping[str, Any],
        *,
        component: str,
        source_node: str | None = None,
        span_key: str | None = None,
        parent_key: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> RuntimeEvent:
        resolved_span_key = span_key
        if resolved_span_key is None and event_type in {"turn.started", "turn.finished"}:
            resolved_span_key = "run:{}".format(self.context.run_id)
        resolved_trace_id, resolved_span_id, resolved_parent_span_id = resolve_runtime_identity(
            self.context.run_id,
            span_key=resolved_span_key,
            parent_key=parent_key,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            event_type=event_type,
            materialize_span=False,
        )
        return RuntimeEvent(
            event_type=event_type,
            status=self.normalize_status(status),
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            turn_id=self.context.turn_id,
            source={
                "node": str(source_node or self.source_node),
                "component": component,
            },
            payload=dict(payload or {}),
            span_key=resolved_span_key,
            parent_key=parent_key,
            trace_id=resolved_trace_id,
            span_id=span_id or resolved_span_id,
            parent_span_id=parent_span_id or resolved_parent_span_id,
        )

    @staticmethod
    def normalize_status(status: Any) -> str:
        normalized = str(status or "").strip().lower()
        return _STATUS_ALIASES.get(normalized, normalized)
