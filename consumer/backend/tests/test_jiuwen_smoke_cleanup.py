"""Resource cleanup contracts for the OpenJiuwen smoke check."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import agent.core.jiuwen.smoke as smoke
import pytest


class _FakeAbilityManager:
    def add(self, _cards):
        return None


def _agent(agent_id: str):
    return SimpleNamespace(
        card=SimpleNamespace(id=agent_id),
        ability_manager=_FakeAbilityManager(),
    )


def _tool_info(name: str) -> dict:
    return {
        "name": name,
        "description": "Smoke cleanup test tool",
        "parameters": {"type": "object", "properties": {}},
    }


def _agent_config():
    return SimpleNamespace(web_search=SimpleNamespace(enabled=False))


def test_jiuwen_smoke_runs_injected_cleanup_for_injected_registration():
    agent = _agent("smoke-injected-agent")
    cards = [SimpleNamespace(id="smoke-card", name="smoke_tool")]
    cleanup_calls = []

    result = smoke.run_jiuwen_smoke(
        llm_config=SimpleNamespace(),
        agent_config=_agent_config(),
        create_agent=lambda **_kwargs: agent,
        register_tools=lambda *_args, **_kwargs: cards,
        unregister_tools=lambda cleanup_agent, cleanup_cards: cleanup_calls.append((cleanup_agent, cleanup_cards)),
        tool_infos=[_tool_info("smoke_tool")],
    )

    assert result["registered_tool_count"] == 1
    assert cleanup_calls == [(agent, cards)]


def test_jiuwen_smoke_leaves_injected_validator_side_effect_free(monkeypatch):
    agent = _agent("smoke-validator-agent")
    cards = [SimpleNamespace(id="validator-card", name="validated_tool")]
    monkeypatch.setattr(
        smoke,
        "unregister_current_runtime_tools",
        lambda *_args, **_kwargs: pytest.fail("validator must not use runtime cleanup"),
    )

    result = smoke.run_jiuwen_smoke(
        llm_config=SimpleNamespace(),
        agent_config=_agent_config(),
        create_agent=lambda **_kwargs: agent,
        register_tools=lambda *_args, **_kwargs: cards,
        tool_infos=[_tool_info("validated_tool")],
    )

    assert result["registered_tool_names"] == ["validated_tool"]


def test_jiuwen_smoke_default_registration_removes_real_runner_resource():
    from openjiuwen.core.runner import Runner

    agent_id = "smoke-cleanup-{}".format(uuid.uuid4().hex)
    tool_name = "smoke_cleanup_tool"
    tool_id = "{}.tool.{}".format(agent_id, tool_name)
    agent = _agent(agent_id)

    assert Runner.resource_mgr.get_tool(tool_id, tag=agent_id) is None

    result = smoke.run_jiuwen_smoke(
        llm_config=SimpleNamespace(),
        agent_config=_agent_config(),
        create_agent=lambda **_kwargs: agent,
        tool_infos=[_tool_info(tool_name)],
    )

    assert result["registered_tool_names"] == [tool_name]
    assert Runner.resource_mgr.get_tool(tool_id, tag=agent_id) is None
