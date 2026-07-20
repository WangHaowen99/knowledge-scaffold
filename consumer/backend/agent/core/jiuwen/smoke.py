"""Smoke checks for the Jiuwen ReAct runtime."""

from __future__ import annotations

import argparse
import json
from typing import Any, Optional

from config import AppConfig, AgentConfig
from workspace.workspaces import get_workspace_project_root
from .tools import get_jiuwen_tool_infos
from ..shared.modes import resolve_agent_mode
from ..shared.tool_context import build_tool_context
from ..jiuwen_engine import (
    JiuwenBackendUnavailable,
    create_default_jiuwen_agent,
    register_current_runtime_tools,
    unregister_current_runtime_tools,
)


_DEFAULT_REGISTER_TOOLS = register_current_runtime_tools


def run_jiuwen_smoke(
    *,
    llm_config: Optional[Any] = None,
    agent_config: Optional[AgentConfig] = None,
    agent_mode: str = "quick",
    create_agent: Any = create_default_jiuwen_agent,
    register_tools: Any = _DEFAULT_REGISTER_TOOLS,
    unregister_tools: Optional[Any] = None,
    tool_infos: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Construct a Jiuwen ReAct agent and register current-runtime tools.

    This deliberately does not call ``agent.invoke``. It verifies the import,
    construction, and tool registration boundary without making an LLM request.
    """

    resolved_llm_config = llm_config or AppConfig.from_env().llm
    resolved_agent_config = agent_config or AgentConfig.from_env()
    state = {
        "run_id": "jiuwen-smoke-run",
        "session_id": "jiuwen-smoke-session",
        "current_turn_id": 1,
        "user_request": "jiuwen smoke check",
        "messages": [],
        "results": {},
        "user_context": {"agent_mode": resolve_agent_mode({"agent_mode": agent_mode})},
    }
    agent = create_agent(llm_config=resolved_llm_config)
    infos = tool_infos
    if infos is None and agent_mode:
        infos = get_jiuwen_tool_infos(
            "main",
            include_orchestration=resolve_agent_mode({"agent_mode": agent_mode}) == "multi_agent",
            include_web_search=bool(getattr(getattr(resolved_agent_config, "web_search", None), "enabled", False)),
        )
    cleanup = unregister_tools
    if cleanup is None and register_tools is _DEFAULT_REGISTER_TOOLS:
        cleanup = unregister_current_runtime_tools

    cards: list[Any] = []
    try:
        cards = register_tools(
            agent,
            state=state,
            context_factory=lambda: _build_smoke_tool_context(
                state,
                agent_config=resolved_agent_config,
            ),
            tool_infos=infos,
        )
        return {
            "ok": True,
            "agent_id": str(getattr(getattr(agent, "card", None), "id", "") or ""),
            "registered_tool_count": len(cards),
            "registered_tool_names": [str(getattr(card, "name", "") or "") for card in cards],
        }
    finally:
        if cards and callable(cleanup):
            cleanup(agent, cards)


def _build_smoke_tool_context(state: dict[str, Any], *, agent_config: AgentConfig) -> Any:
    return build_tool_context(
        state,
        memory_manager=None,
        agent_config=agent_config,
        caller="main",
        agent_iteration=1,
        workspace_id=None,
        project_root=str(get_workspace_project_root(None)),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-check the Jiuwen ReAct runtime.")
    parser.add_argument(
        "--agent-mode",
        choices=["quick", "thinking", "multi_agent"],
        default="quick",
        help="Tool exposure mode to validate.",
    )
    args = parser.parse_args(argv)
    try:
        payload = run_jiuwen_smoke(agent_mode=args.agent_mode)
        exit_code = 0
    except JiuwenBackendUnavailable as exc:
        payload = {"ok": False, "error": str(exc)}
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
