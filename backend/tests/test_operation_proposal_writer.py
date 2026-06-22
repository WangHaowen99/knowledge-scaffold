"""Tests for semantic operation proposal writes."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentic.evolution.models import Aggregate
from agentic.evolution.proposal_writer import append_operation_proposal_with_relations


def _aggregate(fp: str, content: str) -> Aggregate:
    return Aggregate(
        fingerprint=fp,
        workspace_id="workspace-1",
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        canonical_content=content,
        occurrences=1,
        active_endorsements=0,
        applied_count=0,
        rejection_count=0,
        first_seen=1.0,
        last_seen=1.0,
        sources={"passive": 1},
    )


class _Buffer:
    def __init__(self, candidates):
        self.candidates = candidates
        self.appended = []
        self.rejected = []

    def recall(self, *args, **kwargs):
        del args, kwargs
        return self.candidates

    def reject(self, ontology_id, operation_key, fingerprint, *, workspace_id=None):
        self.rejected.append((workspace_id, ontology_id, operation_key, fingerprint))

    def append(self, **kwargs):
        self.appended.append(kwargs)
        return _aggregate("new-fp", kwargs["content"])


def _mock_relation(monkeypatch, decisions):
    class _Router:
        def call(self, **kwargs):
            assert kwargs["request_label"] == "memory_relation_operation"
            return SimpleNamespace(content=json.dumps({"decisions": decisions}))

    monkeypatch.setattr(
        "agentic.evolution.proposal_writer.get_llm_router",
        lambda: _Router(),
    )


def test_passive_duplicate_reuses_existing_canonical_content(monkeypatch):
    existing = _aggregate("fp-existing", "执行脚本前先读取 operation 文档")
    buffer = _Buffer([existing])
    _mock_relation(
        monkeypatch,
        [
            {
                "fingerprint": "fp-existing",
                "relation": "duplicate",
                "reason": "same procedure",
            }
        ],
    )

    result = append_operation_proposal_with_relations(
        buffer=buffer,
        workspace_id="workspace-1",
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="先加载 operation markdown，再执行脚本",
        source="passive",
        run_id="run-1",
        user_id="alice",
    )

    assert result.relation == "duplicate"
    assert result.matched_fingerprint == "fp-existing"
    assert buffer.rejected == []
    assert buffer.appended[0]["content"] == "执行脚本前先读取 operation 文档"
    assert buffer.appended[0]["source"] == "passive"


def test_passive_refinement_rejects_old_candidate_before_append(monkeypatch):
    existing = _aggregate("fp-old", "执行脚本前先读取 operation 文档")
    buffer = _Buffer([existing])
    _mock_relation(
        monkeypatch,
        [
            {
                "fingerprint": "fp-old",
                "relation": "refines",
                "reason": "new item adds exact ordering",
            }
        ],
    )

    result = append_operation_proposal_with_relations(
        buffer=buffer,
        workspace_id="workspace-1",
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="先读取 operation 文档，确认输入字段后再执行脚本",
        source="passive",
    )

    assert result.replaced_fingerprints == ["fp-old"]
    assert result.relations == {"fp-old": "refines"}
    assert buffer.rejected == [("workspace-1", "example", "OP_A", "fp-old")]
    assert buffer.appended[0]["content"] == "先读取 operation 文档，确认输入字段后再执行脚本"
