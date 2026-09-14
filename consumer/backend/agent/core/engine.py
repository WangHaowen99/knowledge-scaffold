"""Stable agent-loop interface backed exclusively by openJiuwen."""

from __future__ import annotations

from typing import Any

from .jiuwen_engine import JiuwenReactLoopEngine
from .shared.modes import VALID_AGENT_MODES, resolve_agent_mode


class AgentLoopEngine(JiuwenReactLoopEngine):
    """Compatibility name for the repository's only agent-loop runtime."""

    valid_agent_modes = VALID_AGENT_MODES

    @staticmethod
    def resolve_agent_mode(user_context: Any) -> str:
        return resolve_agent_mode(user_context)
