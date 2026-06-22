"""
Agentic System - Dynamic Agent Orchestration

A general-purpose tool-using agent runtime with sub-agent orchestration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import create_app as create_app
    from .orchestrator import AgenticOrchestrator as AgenticOrchestrator


def __getattr__(name: str) -> Any:
    if name == "AgenticOrchestrator":
        from .orchestrator import AgenticOrchestrator

        return AgenticOrchestrator
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError("module 'agentic' has no attribute '{}'".format(name))


__all__ = ["AgenticOrchestrator", "create_app"]
