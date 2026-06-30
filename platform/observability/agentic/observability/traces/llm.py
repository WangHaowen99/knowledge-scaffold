"""Semantic observer facade for standalone LLM requests."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from ..observer import NOOP_OBSERVER


class LLMRuntimeTrace:
    """Business-facing facade for standalone LLM request events.

    Used by helper LLM calls that are outside the main agent iteration scope.
    Provider-specific mapping and I/O stay outside this facade.
    """

    def __init__(
        self,
        *,
        observer: Any = None,
        run_id: Optional[str],
        session_id: Optional[str],
        turn_id: Optional[int],
        span_key: str,
        parent_key: Optional[str] = None,
        request_label: Optional[str] = None,
    ) -> None:
        self.observer = observer or NOOP_OBSERVER
        self.run_id = str(run_id or "")
        self.session_id = session_id
        self.turn_id = turn_id
        self.span_key = span_key
        self.parent_key = parent_key
        self.request_label = request_label

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.observer, "enabled", True))

    def started(self, *, model: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self.enabled:
            return
        payload = {
            "request_label": self.request_label,
            "model": model,
        }
        payload.update(metadata or {})
        self._emit(
            RuntimeEventType.LLM_STARTED,
            parent_key=self.parent_key,
            payload=payload,
        )

    def finished(self, *, usage: Dict[str, Any], prompt_text: str, completion_text: str) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.LLM_FINISHED,
            payload={
                "status": RuntimeStatus.SUCCESS,
                "usage": usage or {},
                "prompt_text": prompt_text,
                "completion_text": completion_text,
            },
        )

    def failed(self, *, status: str = RuntimeStatus.ERROR) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.LLM_FAILED,
            payload={"status": status},
        )

    def _emit(
        self,
        event_type: str,
        *,
        parent_key: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.run_id or not self.enabled:
            return
        self.observer.emit(
            RuntimeEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                span_key=self.span_key,
                parent_key=parent_key,
                payload=payload or {},
            )
        )
