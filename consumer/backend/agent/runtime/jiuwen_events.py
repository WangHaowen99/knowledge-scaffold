"""Translate Jiuwen Engine callbacks into stable runtime events."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from typing import Any, Optional

from .contracts import RuntimeInvocationContext
from .events import RuntimeEvent


_MODEL_STARTED_TYPES = frozenset({"llm.started", "model.started"})
_IGNORED_MODEL_REQUEST_LABELS = frozenset({"reasoning_summary"})
_PROTOCOL_EVENT_TYPES = frozenset(
    {
        "tool.started",
        "tool.progress",
        "tool.finished",
        "subagent.started",
        "subagent.delta",
        "subagent.progress",
        "subagent.finished",
        "subagent_group.started",
        "subagent_group.finished",
        "interaction.requested",
    }
)
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
        self._buffer_lock = threading.Lock()
        self._buffered_count = 0
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
        self._publish(
            self._event(
                "reasoning.delta" if normalized_channel == "reasoning" else "token.delta",
                "running",
                payload,
                component="model",
            )
        )

    def protocol_event(self, event_type: str, payload: Mapping[str, Any]) -> None:
        normalized_type = str(event_type or "").strip().lower()
        if normalized_type not in _PROTOCOL_EVENT_TYPES:
            return
        normalized_payload = dict(payload or {})
        if normalized_type == "interaction.requested":
            status = "awaiting_input"
            component = "interaction"
        else:
            status = "running"
            component = "tool"
        self._publish(
            self._event(
                normalized_type,
                status,
                normalized_payload,
                component=component,
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
        overflow = False
        with self._buffer_lock:
            if not self._accepting:
                if self._overflowed:
                    raise RuntimeEventBufferFull(
                        "Jiuwen runtime event buffer is full"
                    )
                return
            if self._buffered_count >= self.max_buffer_size:
                self._accepting = False
                self._overflowed = True
                overflow = True
            else:
                self._buffered_count += 1

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
            self._put_reserved_nowait(event)
            return
        try:
            self._loop.call_soon_threadsafe(self._put_reserved_nowait, event)
        except BaseException:
            # A producer may race loop shutdown after reserving a slot. Do not
            # leave the accounting permanently full if the callback cannot be
            # handed to the loop.
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

    def _rollback_reserved_slot(self) -> None:
        with self._buffer_lock:
            self._buffered_count = max(0, self._buffered_count - 1)

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
    ) -> RuntimeEvent:
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
        )

    @staticmethod
    def normalize_status(status: Any) -> str:
        normalized = str(status or "").strip().lower()
        return _STATUS_ALIASES.get(normalized, normalized)
