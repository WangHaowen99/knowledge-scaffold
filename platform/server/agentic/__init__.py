"""
Agentic System - Dynamic Agent Orchestration

A general-purpose tool-using agent runtime with sub-agent orchestration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    return start.parents[2]


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
_AGENTIC_NAMESPACE_DIRS = (
    _REPO_ROOT / "runtime" / "agent" / "agentic",
    _REPO_ROOT / "runtime" / "tools" / "agentic",
    _REPO_ROOT / "runtime" / "data_search" / "agentic",
    _REPO_ROOT / "runtime" / "graph_search" / "agentic",
    _REPO_ROOT / "runtime" / "direct_execution" / "agentic",
    _REPO_ROOT / "runtime" / "memory" / "agentic",
    _REPO_ROOT / "runtime" / "web_search" / "agentic",
    _REPO_ROOT / "platform" / "config" / "agentic",
    _REPO_ROOT / "platform" / "workspace" / "agentic",
    _REPO_ROOT / "platform" / "auth" / "agentic",
    _REPO_ROOT / "platform" / "stores" / "agentic",
    _REPO_ROOT / "platform" / "protocols" / "agentic",
    _REPO_ROOT / "platform" / "observability" / "agentic",
)
for _path in _AGENTIC_NAMESPACE_DIRS:
    if _path.is_dir():
        _path_str = str(_path)
        if _path_str not in __path__:
            __path__.append(_path_str)

_PYTHONPATH_ROOTS = (
    _REPO_ROOT / "modeling" / "skills",
)
for _path in _PYTHONPATH_ROOTS:
    if _path.is_dir():
        _path_str = str(_path)
        if _path_str not in sys.path:
            sys.path.insert(0, _path_str)

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
