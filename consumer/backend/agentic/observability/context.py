"""Context helpers for background callbacks that need the current observer."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from .observer import NOOP_OBSERVER


@dataclass(frozen=True)
class ObservabilityContext:
    observer: object = NOOP_OBSERVER
    run_id: str = ""
    session_id: Optional[str] = None
    turn_id: Optional[int] = None
    parent_key: Optional[str] = None


_current_context: ContextVar[ObservabilityContext] = ContextVar(
    "agentic_observability_context",
    default=ObservabilityContext(),
)


def get_observability_context() -> ObservabilityContext:
    return _current_context.get()


@contextmanager
def bind_observability_context(
    *,
    observer: object,
    run_id: str,
    session_id: Optional[str],
    turn_id: Optional[int],
    parent_key: Optional[str] = None,
):
    token = _current_context.set(
        ObservabilityContext(
            observer=observer,
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            parent_key=parent_key,
        )
    )
    try:
        yield
    finally:
        _current_context.reset(token)
