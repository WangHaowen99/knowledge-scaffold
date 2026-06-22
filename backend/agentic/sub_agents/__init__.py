"""Sub-agents module: homogeneous workers with parallel fan-out orchestration."""

from .base import SubAgent
from .manager import (
    SubAgentManager,
    SubAgentTaskSpec,
    clear_subagent_manager,
    clear_subagent_manager_registry,
    get_subagent_manager,
)

__all__ = [
    "SubAgent",
    "SubAgentManager",
    "SubAgentTaskSpec",
    "clear_subagent_manager",
    "clear_subagent_manager_registry",
    "get_subagent_manager",
]
