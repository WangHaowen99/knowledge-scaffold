# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Model protocol limits for generated calls and persisted conversation replay."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.core import jiuwen_engine
from agent.core.jiuwen.common import model_safe_tool_call_id
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter
from agent.tools.base import ToolBatchResult


@pytest.mark.parametrize("length", [63, 64, 65, 256])
def test_call_id_limit_and_pair_stability(length):
    original = "a" * length
    converted = model_safe_tool_call_id(original)
    assert len(converted) <= 64
    assert model_safe_tool_call_id(original) == converted
    assert model_safe_tool_call_id(converted) == converted
    if length <= 64:
        assert converted == original
    else:
        assert converted != model_safe_tool_call_id(original[:-1] + "b")


def test_old_history_replays_with_paired_ids_without_mutating_persisted_data(monkeypatch):
    monkeypatch.setattr(jiuwen_engine, "_jiuwen_message_classes", lambda: {
        "assistant": SimpleNamespace, "tool": SimpleNamespace, "user": SimpleNamespace,
    })
    original_id = "jiuwen_agentic-" + "a" * 32 + "_ontology_context_1"
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": original_id, "type": "function",
            "function": {"name": "ontology_context", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": original_id, "content": "synthetic result"},
    ]
    before = deepcopy(messages)
    converted = [jiuwen_engine._to_jiuwen_history_message(item) for item in messages]
    assert len(converted[0].tool_calls[0]["id"]) <= 64
    assert converted[0].tool_calls[0]["id"] == converted[1].tool_call_id
    assert converted[1].content == "synthetic result"
    assert messages == before


def test_generated_tool_calls_are_bounded_unique_and_paired():
    state = {"run_id": "agentic-" + "a" * 32, "messages": [], "results": {}}

    def executor(calls, _context):
        return ToolBatchResult(messages=[{
            "role": "tool", "name": calls[0].name,
            "tool_call_id": calls[0].id, "content": "synthetic result",
        }])

    adapter = CurrentRuntimeToolAdapter(
        tool_name="ontology_context", state=state,
        context_factory=lambda: SimpleNamespace(), tool_batch_executor=executor,
    )

    async def invoke_twice():
        await adapter.invoke({})
        await adapter.invoke({})

    asyncio.run(invoke_twice())
    calls = [call["id"] for message in state["messages"] for call in message.get("tool_calls", [])]
    results = [message["tool_call_id"] for message in state["messages"] if message["role"] == "tool"]
    assert len(calls) == len(set(calls)) == 2
    assert all(len(value) <= 64 for value in calls)
    assert calls == results
