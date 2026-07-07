"""Semantic observer facade for one workflow run."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from ...exceptions import WorkflowCancelledError
from ..events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from ..observer import NOOP_OBSERVER


class RunRuntimeTrace:
    """Business-facing facade for one workflow run.

    Orchestrator code uses this for run terminal state. Provider-specific
    mapping and I/O stay outside this facade.
    """

    def __init__(
        self,
        *,
        observer: Any = None,
        run_id: str,
        session_id: Optional[str],
        turn_id: Optional[int],
    ) -> None:
        self.observer = observer or NOOP_OBSERVER
        self.run_id = str(run_id or "")
        self.session_id = session_id
        self.turn_id = turn_id

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.observer, "enabled", True))

    def started(
        self,
        *,
        user_id: Optional[str],
        user_query: str,
        workspace_id: str,
        model: Optional[str] = None,
        model_provider: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_STARTED,
            payload={
                "user_id": user_id,
                "user_query": user_query,
                "workspace_id": workspace_id,
                "model": model,
                "model_provider": model_provider,
                **dict(metadata or {}),
            },
        )

    def finished(
        self,
        *,
        final_answer: Any,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_FINISHED,
            payload={
                "final_answer": _short_output(final_answer),
                "status": RuntimeStatus.SUCCESS,
                "duration_ms": duration_ms,
            },
        )

    def cancelled(
        self,
        *,
        final_answer: Any,
        cancel_reason: str,
        duration_ms: Optional[int] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_CANCELLED,
            payload={
                "final_answer": _short_output(final_answer),
                "status": RuntimeStatus.CANCELLED,
                "cancel_reason": cancel_reason,
                "duration_ms": duration_ms,
                "error_type": error_type,
                "error_message": str(error_message or "")[:1000],
            },
        )

    def failed(
        self,
        *,
        final_answer: Any,
        error_type: str,
        error_message: str,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_FAILED,
            payload={
                "final_answer": _short_output(final_answer),
                "status": RuntimeStatus.ERROR,
                "error_type": error_type,
                "error_message": str(error_message or "")[:1000],
                "duration_ms": duration_ms,
            },
        )

    def record_phase(
        self,
        *,
        phase_name: str,
        component: str,
        status: str = RuntimeStatus.SUCCESS,
        duration_ms: Optional[int] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._emit_phase(
            RuntimeEventType.RUNTIME_PHASE_FINISHED,
            phase_name=phase_name,
            component=component,
            status=status,
            duration_ms=duration_ms,
            metrics=dict(metrics or {}),
        )

    @contextmanager
    def phase_scope(
        self,
        *,
        phase_name: str,
        component: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Iterator[None]:
        started_at = time.perf_counter()
        phase_metrics = dict(metrics or {})
        self._emit_phase(
            RuntimeEventType.RUNTIME_PHASE_STARTED,
            phase_name=phase_name,
            component=component,
            metrics=phase_metrics,
        )
        try:
            yield
        except WorkflowCancelledError:
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FAILED,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.CANCELLED,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics=phase_metrics,
            )
            raise
        except Exception as exc:
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FAILED,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.ERROR,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics={
                    **phase_metrics,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:300],
                },
            )
            raise
        else:
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FINISHED,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.SUCCESS,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics=phase_metrics,
            )

    def _emit_phase(
        self,
        event_type: str,
        *,
        phase_name: str,
        component: str,
        metrics: Dict[str, Any],
        status: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "phase_name": phase_name,
            "component": component,
            **metrics,
        }
        if status is not None:
            payload["status"] = status
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self._emit(event_type, payload=payload)

    def _emit(self, event_type: str, *, payload: dict) -> None:
        if not self.run_id or not self.enabled:
            return
        self.observer.emit(
            RuntimeEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                payload=payload,
            )
        )


def _short_output(value: Any) -> str:
    return str(value or "")[:2000]
