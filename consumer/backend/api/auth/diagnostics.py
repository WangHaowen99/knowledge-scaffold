"""Bounded authentication diagnostics without identity or credential values."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from uuid import uuid4

import jwt
import requests
from starlette.exceptions import HTTPException

from utils.logging import get_logger


logger = get_logger("auth.diagnostics")


@dataclass
class _AuthDiagnosticState:
    operation: str
    attempt_id: str
    started_at: float
    failure: dict | None = None


_diagnostic_state: ContextVar[_AuthDiagnosticState | None] = ContextVar(
    "auth_diagnostic_state", default=None,
)


def _error_type(error: Exception) -> str:
    # Classify using known library classes, never exception text or custom names.
    for error_class in (
        requests.exceptions.JSONDecodeError,
        requests.ConnectTimeout,
        requests.ReadTimeout,
        requests.Timeout,
        requests.ConnectionError,
        requests.RequestException,
        jwt.ExpiredSignatureError,
        jwt.ImmatureSignatureError,
        jwt.InvalidAudienceError,
        jwt.InvalidIssuerError,
        jwt.InvalidSignatureError,
        jwt.MissingRequiredClaimError,
        jwt.DecodeError,
        jwt.PyJWTError,
        HTTPException,
        ValueError,
        TypeError,
    ):
        if isinstance(error, error_class):
            return error_class.__name__
    return "Exception"


def record_auth_failure(
    stage: str,
    reason: str,
    *,
    error: Exception | None = None,
    upstream_status: int | None = None,
    replace: bool = True,
) -> None:
    """Remember code-defined classifications for the current auth attempt."""
    state = _diagnostic_state.get()
    if state is None or (state.failure is not None and not replace):
        return
    state.failure = {
        "auth_stage": stage,
        "auth_reason": reason,
        "auth_error_type": _error_type(error) if error is not None else None,
        "auth_upstream_status": upstream_status,
    }


@contextmanager
def auth_diagnostic_context(operation: str):
    """Emit one sanitized failure per attempt, including across runtime pools."""
    if _diagnostic_state.get() is not None:
        yield
        return
    state = _AuthDiagnosticState(operation, uuid4().hex, time.monotonic())
    token = _diagnostic_state.set(state)
    try:
        yield
    except Exception as exc:
        fields = dict(state.failure or {
            "auth_stage": "authentication",
            "auth_reason": "unexpected_error",
            "auth_error_type": None,
            "auth_upstream_status": None,
        })
        fields["auth_error_type"] = fields["auth_error_type"] or _error_type(exc)
        fields.update(
            auth_operation=state.operation,
            auth_attempt_id=state.attempt_id,
            auth_elapsed_ms=round(max(0.0, time.monotonic() - state.started_at) * 1000, 3),
        )
        logger.warning(
            "Authentication failed: operation=%s attempt_id=%s stage=%s reason=%s "
            "error_type=%s upstream_status=%s elapsed_ms=%s",
            fields["auth_operation"], fields["auth_attempt_id"], fields["auth_stage"],
            fields["auth_reason"], fields["auth_error_type"],
            fields["auth_upstream_status"], fields["auth_elapsed_ms"],
            extra={**fields, "suppress_log_context": True},
            exc_info=False,
        )
        raise
    finally:
        _diagnostic_state.reset(token)
