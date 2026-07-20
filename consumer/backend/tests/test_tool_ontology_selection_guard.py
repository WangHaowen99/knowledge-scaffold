"""Execution-boundary tests for explicitly selected ontology isolation."""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

import pytest

from config import AgentConfig
from agent.tools.base import ToolCaller, ToolContext, ToolInvocation, ToolOutcome
from agent.tools.registry import TOOL_SPECS, execute_tool_batch


ONTOLOGY_SCOPED_TOOLS = (
    "ontology_context",
    "business_data_search",
    "execute_script",
)


def _make_context(*, selected_ontology_id: Optional[str]) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-ontology-guard",
        session_id="session-ontology-guard",
        turn_id=1,
        agent_config=AgentConfig.from_env(),
        caller="main",
        selected_ontology_id=selected_ontology_id,
    )


def _valid_args(tool_name: str, ontology_id: str) -> Dict[str, Any]:
    if tool_name == "ontology_context":
        return {"ontology_id": ontology_id, "resource": "root"}
    if tool_name == "business_data_search":
        return {
            "ontology_id": ontology_id,
            "entity": "RefineryUnit",
            "query": "List every refinery unit",
        }
    if tool_name == "execute_script":
        return {
            "ontology_id": ontology_id,
            "operation_key": "production_plan",
            "arguments": {},
        }
    raise AssertionError("Unsupported test tool: {}".format(tool_name))


def _recording_tool(tool_name: str, calls: list[Dict[str, Any]]):
    original = TOOL_SPECS[tool_name]

    class _RecordingTool:
        name: str
        schema: Dict[str, Any]
        allowed_callers: Set[ToolCaller]
        concurrency_safe: bool
        max_parallelism: Optional[int]
        category: str

        def __init__(self) -> None:
            self.name = tool_name
            self.schema = original.schema
            self.allowed_callers = set(original.allowed_callers)
            self.concurrency_safe = original.concurrency_safe
            self.max_parallelism = original.max_parallelism
            self.category = original.category

        def execute(
            self,
            *,
            args: Dict[str, Any],
            context: ToolContext,
            tool_call_id: Optional[str] = None,
            event_emitter: Any = None,
        ) -> ToolOutcome:
            del context, event_emitter
            calls.append({"args": dict(args), "tool_call_id": tool_call_id})
            return ToolOutcome(content="executed")

    return _RecordingTool()


@pytest.mark.parametrize("tool_name", ONTOLOGY_SCOPED_TOOLS)
def test_selected_ontology_rejects_mismatched_ontology_tool_call(
    tool_name: str,
    monkeypatch,
) -> None:
    calls: list[Dict[str, Any]] = []
    monkeypatch.setitem(TOOL_SPECS, tool_name, _recording_tool(tool_name, calls))

    result = execute_tool_batch(
        [
            ToolInvocation(
                id="call-mismatch",
                name=tool_name,
                arguments=_valid_args(tool_name, "other-ontology"),
            )
        ],
        _make_context(selected_ontology_id="selected-ontology"),
    )

    assert calls == []
    assert len(result.messages) == 1
    content = result.messages[0]["content"]
    assert content.startswith("Error:")
    assert "selected-ontology" in content
    assert "other-ontology" in content


@pytest.mark.parametrize(
    ("selected_ontology_id", "tool_ontology_id"),
    [
        ("selected-ontology", "selected-ontology"),
        (None, "other-ontology"),
    ],
)
@pytest.mark.parametrize("tool_name", ONTOLOGY_SCOPED_TOOLS)
def test_ontology_tool_call_remains_allowed_when_scope_matches_or_is_not_selected(
    tool_name: str,
    selected_ontology_id: Optional[str],
    tool_ontology_id: str,
    monkeypatch,
) -> None:
    calls: list[Dict[str, Any]] = []
    monkeypatch.setitem(TOOL_SPECS, tool_name, _recording_tool(tool_name, calls))
    arguments = _valid_args(tool_name, tool_ontology_id)

    result = execute_tool_batch(
        [
            ToolInvocation(
                id="call-allowed",
                name=tool_name,
                arguments=arguments,
            )
        ],
        _make_context(selected_ontology_id=selected_ontology_id),
    )

    assert result.messages[0]["content"] == "executed"
    assert calls == [{"args": arguments, "tool_call_id": "call-allowed"}]
