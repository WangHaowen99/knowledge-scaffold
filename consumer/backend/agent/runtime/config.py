"""Validated limits for one ASGI-owned asynchronous runtime."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from typing import Any


def _positive_float(name: str, default: float) -> float:
    raw = str(os.getenv(name, default)).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float_value(name: str, raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class NativeAsyncRuntimeConfig:
    """Capacity and deadline policy owned by exactly one ASGI lifespan."""

    shutdown_total_deadline_seconds: float = 30.0

    subscriber_queue_size: int = 512
    max_subscribers_per_run: int = 3
    max_subscribers_total: int = 100
    completed_replay_ttl_seconds: float = 120.0

    short_io_workers: int = 8
    short_io_max_pending: int = 64
    short_io_capacity_wait_seconds: float = 1.0
    short_io_operation_timeout_seconds: float = 30.0

    external_io_workers: int = 16
    external_io_max_pending: int = 16
    external_io_capacity_wait_seconds: float = 0.1
    external_io_operation_timeout_seconds: float = 300.0

    # Tool calls are isolated from generic external I/O because a slow tool
    # must not consume the capacity needed by persistence or protocol work.
    tool_io_workers: int = 8
    tool_io_max_pending: int = 16
    tool_io_capacity_wait_seconds: float = 0.1
    tool_io_operation_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        positive_float_fields = (
            "shutdown_total_deadline_seconds",
            "completed_replay_ttl_seconds",
            "short_io_capacity_wait_seconds",
            "short_io_operation_timeout_seconds",
            "external_io_capacity_wait_seconds",
            "external_io_operation_timeout_seconds",
            "tool_io_capacity_wait_seconds",
            "tool_io_operation_timeout_seconds",
        )
        positive_int_fields = (
            "subscriber_queue_size",
            "max_subscribers_per_run",
            "max_subscribers_total",
            "short_io_workers",
            "short_io_max_pending",
            "external_io_workers",
            "external_io_max_pending",
            "tool_io_workers",
            "tool_io_max_pending",
        )
        for field_name in positive_float_fields:
            _positive_float_value(field_name, getattr(self, field_name))
        for field_name in positive_int_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_subscribers_total < self.max_subscribers_per_run:
            raise ValueError(
                "max_subscribers_total must be greater than or equal to "
                "max_subscribers_per_run"
            )

    @classmethod
    def from_env(cls) -> "NativeAsyncRuntimeConfig":
        return cls(
            shutdown_total_deadline_seconds=_positive_float(
                "AGENTIC_SHUTDOWN_TOTAL_DEADLINE_SECONDS",
                30.0,
            ),
            subscriber_queue_size=_positive_int(
                "AGENTIC_SSE_SUBSCRIBER_QUEUE_SIZE",
                512,
            ),
            max_subscribers_per_run=_positive_int(
                "AGENTIC_SSE_MAX_SUBSCRIBERS_PER_RUN",
                3,
            ),
            max_subscribers_total=_positive_int(
                "AGENTIC_SSE_MAX_SUBSCRIBERS_TOTAL",
                100,
            ),
            completed_replay_ttl_seconds=_positive_float(
                "AGENTIC_RUN_COMPLETED_REPLAY_TTL_SECONDS",
                120.0,
            ),
            short_io_workers=_positive_int("AGENTIC_SHORT_IO_WORKERS", 8),
            short_io_max_pending=_positive_int(
                "AGENTIC_SHORT_IO_MAX_PENDING",
                64,
            ),
            short_io_capacity_wait_seconds=_positive_float(
                "AGENTIC_SHORT_IO_CAPACITY_WAIT_SECONDS",
                1.0,
            ),
            short_io_operation_timeout_seconds=_positive_float(
                "AGENTIC_SHORT_IO_OPERATION_TIMEOUT_SECONDS",
                30.0,
            ),
            external_io_workers=_positive_int(
                "AGENTIC_EXTERNAL_IO_WORKERS",
                16,
            ),
            external_io_max_pending=_positive_int(
                "AGENTIC_EXTERNAL_IO_MAX_PENDING",
                16,
            ),
            external_io_capacity_wait_seconds=_positive_float(
                "AGENTIC_EXTERNAL_IO_CAPACITY_WAIT_SECONDS",
                0.1,
            ),
            external_io_operation_timeout_seconds=_positive_float(
                "AGENTIC_EXTERNAL_IO_OPERATION_TIMEOUT_SECONDS",
                300.0,
            ),
            tool_io_workers=_positive_int("AGENTIC_TOOL_IO_WORKERS", 8),
            tool_io_max_pending=_positive_int(
                "AGENTIC_TOOL_IO_MAX_PENDING",
                16,
            ),
            tool_io_capacity_wait_seconds=_positive_float(
                "AGENTIC_TOOL_IO_CAPACITY_WAIT_SECONDS",
                0.1,
            ),
            tool_io_operation_timeout_seconds=_positive_float(
                "AGENTIC_TOOL_IO_OPERATION_TIMEOUT_SECONDS",
                600.0,
            ),
        )

    @classmethod
    def from_agent_config(cls, agent_config: Any) -> "NativeAsyncRuntimeConfig":
        config = cls.from_env()
        if agent_config is None:
            return config
        updates: dict[str, Any] = {}
        field_env_names = {
            "shutdown_total_deadline_seconds": "AGENTIC_SHUTDOWN_TOTAL_DEADLINE_SECONDS",
            "subscriber_queue_size": "AGENTIC_SSE_SUBSCRIBER_QUEUE_SIZE",
            "max_subscribers_per_run": "AGENTIC_SSE_MAX_SUBSCRIBERS_PER_RUN",
            "max_subscribers_total": "AGENTIC_SSE_MAX_SUBSCRIBERS_TOTAL",
            "completed_replay_ttl_seconds": "AGENTIC_RUN_COMPLETED_REPLAY_TTL_SECONDS",
            "short_io_workers": "AGENTIC_SHORT_IO_WORKERS",
            "short_io_max_pending": "AGENTIC_SHORT_IO_MAX_PENDING",
            "short_io_capacity_wait_seconds": "AGENTIC_SHORT_IO_CAPACITY_WAIT_SECONDS",
            "short_io_operation_timeout_seconds": "AGENTIC_SHORT_IO_OPERATION_TIMEOUT_SECONDS",
            "external_io_workers": "AGENTIC_EXTERNAL_IO_WORKERS",
            "external_io_max_pending": "AGENTIC_EXTERNAL_IO_MAX_PENDING",
            "external_io_capacity_wait_seconds": "AGENTIC_EXTERNAL_IO_CAPACITY_WAIT_SECONDS",
            "external_io_operation_timeout_seconds": "AGENTIC_EXTERNAL_IO_OPERATION_TIMEOUT_SECONDS",
            "tool_io_workers": "AGENTIC_TOOL_IO_WORKERS",
            "tool_io_max_pending": "AGENTIC_TOOL_IO_MAX_PENDING",
            "tool_io_capacity_wait_seconds": "AGENTIC_TOOL_IO_CAPACITY_WAIT_SECONDS",
            "tool_io_operation_timeout_seconds": "AGENTIC_TOOL_IO_OPERATION_TIMEOUT_SECONDS",
        }
        attribute_names = {
            "subscriber_queue_size": "sse_subscriber_queue_size",
            "max_subscribers_per_run": "sse_max_subscribers_per_run",
            "max_subscribers_total": "sse_max_subscribers_total",
        }
        for field_name, env_name in field_env_names.items():
            if os.getenv(env_name) is not None:
                continue
            attribute_name = attribute_names.get(field_name, field_name)
            if hasattr(agent_config, attribute_name):
                updates[field_name] = getattr(agent_config, attribute_name)
        if (
            "tool_io_operation_timeout_seconds" not in updates
            and os.getenv("AGENTIC_TOOL_IO_OPERATION_TIMEOUT_SECONDS") is None
        ):
            updates["tool_io_operation_timeout_seconds"] = getattr(
                agent_config,
                "tool_timeout_seconds",
                config.tool_io_operation_timeout_seconds,
            )
        return replace(config, **updates)
