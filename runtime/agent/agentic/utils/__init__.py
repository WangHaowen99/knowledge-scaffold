"""Agentic system utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .logging import get_logger as get_logger
    from .streaming import PROTOCOL as PROTOCOL
    from .streaming import SSE_EVENT as SSE_EVENT
    from .streaming import build_agent_event as build_agent_event
    from .streaming import clear_run_sequence as clear_run_sequence
    from .streaming import emit_agent_event as emit_agent_event


def __getattr__(name: str) -> Any:
    if name == "get_logger":
        from .logging import get_logger

        return get_logger
    if name in {"PROTOCOL", "SSE_EVENT", "build_agent_event", "emit_agent_event", "clear_run_sequence"}:
        from .streaming import (
            PROTOCOL,
            SSE_EVENT,
            build_agent_event,
            clear_run_sequence,
            emit_agent_event,
        )

        return {
            "PROTOCOL": PROTOCOL,
            "SSE_EVENT": SSE_EVENT,
            "build_agent_event": build_agent_event,
            "emit_agent_event": emit_agent_event,
            "clear_run_sequence": clear_run_sequence,
        }[name]
    raise AttributeError("module 'agentic.utils' has no attribute '{}'".format(name))


__all__ = [
    "get_logger",
    "PROTOCOL",
    "SSE_EVENT",
    "build_agent_event",
    "emit_agent_event",
    "clear_run_sequence",
]
