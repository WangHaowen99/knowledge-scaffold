"""Connection policy applied at the Agent model-client boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from typing import Any


MODEL_READ_TIMEOUT_ENV = "AGENT_LLM_READ_TIMEOUT_SECONDS"
DEFAULT_MODEL_READ_TIMEOUT_SECONDS = 180.0


class ModelConnectionPolicyError(ValueError):
    """Raised when a model connection policy cannot be applied safely."""


@dataclass(frozen=True)
class ModelConnectionPolicy:
    timeout: float
    max_retries: int = 1


def _positive_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ModelConnectionPolicyError("{} must be a positive number".format(label))
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelConnectionPolicyError(
            "{} must be a positive number".format(label)
        ) from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ModelConnectionPolicyError("{} must be a positive number".format(label))
    return parsed


def resolve_model_connection_policy(
    *,
    configured_timeout: Any = None,
    environ: Mapping[str, str] | None = None,
) -> ModelConnectionPolicy:
    """Resolve one bounded model timeout without weakening configured limits."""

    environment = os.environ if environ is None else environ
    raw_floor = environment.get(MODEL_READ_TIMEOUT_ENV)
    floor = (
        DEFAULT_MODEL_READ_TIMEOUT_SECONDS
        if raw_floor is None
        else _positive_seconds(raw_floor, MODEL_READ_TIMEOUT_ENV)
    )
    configured = (
        floor
        if configured_timeout is None or configured_timeout == ""
        else _positive_seconds(configured_timeout, "configured_timeout")
    )
    return ModelConnectionPolicy(timeout=max(configured, floor), max_retries=1)


__all__ = [
    "DEFAULT_MODEL_READ_TIMEOUT_SECONDS",
    "MODEL_READ_TIMEOUT_ENV",
    "ModelConnectionPolicy",
    "ModelConnectionPolicyError",
    "resolve_model_connection_policy",
]
