# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Semantic observer facade for one workflow run."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
import uuid

from agent.exceptions import WorkflowCancelledError
from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability.observer import NOOP_OBSERVER


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
        self.workspace_id = ""
        self.root_span_key = "run:{}".format(self.run_id)
        self._terminal = False
        self._released = False

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
        self.workspace_id = str(workspace_id or "").strip()
        self._emit(
            RuntimeEventType.RUN_STARTED,
            span_key=self.root_span_key,
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
        self.close(status=RuntimeStatus.SUCCESS)
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_FINISHED,
            span_key=self.root_span_key,
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
        self.close(status=RuntimeStatus.CANCELLED)
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_CANCELLED,
            span_key=self.root_span_key,
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
        self.close(status=RuntimeStatus.ERROR)
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.RUN_FAILED,
            span_key=self.root_span_key,
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
        span_key = self._phase_span_key(phase_name, component)
        self._emit_phase(
            RuntimeEventType.RUNTIME_PHASE_FINISHED,
            span_key=span_key,
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
        span_key = self._phase_span_key(phase_name, component)
        self._emit_phase(
            RuntimeEventType.RUNTIME_PHASE_STARTED,
            span_key=span_key,
            phase_name=phase_name,
            component=component,
            metrics=phase_metrics,
        )
        try:
            yield
        except WorkflowCancelledError:
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FAILED,
                span_key=span_key,
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
                span_key=span_key,
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
                span_key=span_key,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.SUCCESS,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics=phase_metrics,
            )

    def close(self, *, status: str = RuntimeStatus.ERROR) -> None:
        """Idempotently stop resources started for this run trace."""
        if self._terminal:
            return
        self._terminal = True

    def release(self) -> None:
        """Release the run from a retired observer after all late events finish."""
        if self._released:
            return
        self._released = True
        release_run = getattr(self.observer, "release_run", None)
        if callable(release_run):
            release_run(self.run_id)

    def _emit_phase(
        self,
        event_type: str,
        *,
        span_key: str,
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
        self._emit(
            event_type,
            span_key=span_key,
            parent_key=self.root_span_key,
            payload=payload,
        )

    def _emit(
        self,
        event_type: str,
        *,
        payload: dict,
        span_key: Optional[str] = None,
        parent_key: Optional[str] = None,
    ) -> None:
        if not self.run_id or not self.enabled:
            return
        event_payload = dict(payload or {})
        if self.workspace_id:
            event_payload.setdefault("workspace_id", self.workspace_id)
        self.observer.emit(
            RuntimeEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                span_key=span_key,
                parent_key=parent_key,
                payload=event_payload,
            )
        )

    @staticmethod
    def _phase_span_key(phase_name: str, component: str) -> str:
        return "phase:{}:{}:{}".format(
            _safe_span_token(component),
            _safe_span_token(phase_name),
            uuid.uuid4().hex[:12],
        )


def _short_output(value: Any) -> str:
    return str(value or "")[:2000]


def _safe_span_token(value: Any) -> str:
    token = str(value or "unknown").strip().replace(" ", "_")
    return token or "unknown"
