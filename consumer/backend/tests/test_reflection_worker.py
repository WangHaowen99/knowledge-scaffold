"""Tests for ontology reflection persistence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.evolution.reflection_worker import (
    ReflectionOutput,
    ReflectionSnapshot,
    ReflectionWorker,
)
from llm import LLMPurpose


class _TaskLLM:
    is_configured = True

    def call(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            data=ReflectionOutput(
                operation_executed="OP_A",
                compliance_score=0.8,
                efficiency_score=0.7,
                new_procedure="read the operation markdown, then execute the script",
                new_pitfalls=["avoid retrying the same empty business_data_search query"],
            )
        )


class _ProposalBuffer:
    def __init__(self) -> None:
        self.records = []

    def recall(self, *args, **kwargs):
        del args, kwargs
        return []

    def append(self, **kwargs):
        self.records.append(kwargs)


def test_reflection_worker_persists_ontology_lessons_to_evolution_buffer(monkeypatch):
    buffer = _ProposalBuffer()
    task_llm = _TaskLLM()

    class _Router:
        def get_service(self, purpose):
            assert purpose == LLMPurpose.TASK
            return task_llm

    monkeypatch.setattr(
        "agent.evolution.reflection_worker.get_proposal_buffer",
        lambda: buffer,
    )
    monkeypatch.setattr(
        "agent.evolution.reflection_worker.get_llm_router",
        lambda: _Router(),
    )
    worker = ReflectionWorker(max_workers=1)

    result = asyncio.run(
        worker.run(
            ReflectionSnapshot(
            run_id="run-1",
            session_id="session-1",
            turn_id=1,
            user_id="alice",
            workspace_id="workspace-1",
            ontology_id="example",
            operation_hint=None,
            status="success",
            user_request="run OP_A",
            final_output="done",
            execution_summary=None,
            tool_events=[],
            iteration_count=1,
            duration_ms=100,
            )
        )
    )

    assert result == {"outcome": "completed", "written": 2}
    assert [record["kind"] for record in buffer.records] == ["procedure", "pitfall"]
    assert all(record["ontology_id"] == "example" for record in buffer.records)
    assert all(record["operation_key"] == "OP_A" for record in buffer.records)
