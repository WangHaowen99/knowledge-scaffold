"""Tests for the operation-evolution proposal buffer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.evolution import ProposalBuffer, compute_fingerprint


@pytest.fixture
def buf(tmp_path: Path) -> ProposalBuffer:
    return ProposalBuffer(str(tmp_path / "evo"))


def _append_passive(buf: ProposalBuffer, content: str = "先 X 再 Y", **kw):
    return buf.append(
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content=content,
        source="passive",
        **kw,
    )


def test_append_creates_aggregate_and_jsonl(buf: ProposalBuffer, tmp_path: Path):
    agg = _append_passive(buf)
    assert agg.occurrences == 1
    assert agg.active_endorsements == 0
    assert agg.status == "pending"

    op_dir = buf.op_dir("example", "OP_A")
    jsonl = op_dir / "proposals.jsonl"
    aggregate = op_dir / "aggregate.json"
    assert jsonl.exists() and aggregate.exists()
    raw_lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 1
    payload = json.loads(raw_lines[0])
    assert payload["fingerprint"] == agg.fingerprint
    assert payload["source"] == "passive"
    assert payload["created_at"]
    aggregate_payload = json.loads(aggregate.read_text(encoding="utf-8"))
    assert aggregate_payload[agg.fingerprint]["created_at"]
    assert aggregate_payload[agg.fingerprint]["updated_at"]


def test_dedupe_merges_identical_content(buf: ProposalBuffer):
    a1 = _append_passive(buf, content="先读 foo.md, 再调 bar")
    a2 = _append_passive(buf, content="先读 foo.md  再调 bar！")
    assert a1.fingerprint == a2.fingerprint
    assert a2.occurrences == 2


def test_active_chat_outranks_passive_canonical(buf: ProposalBuffer):
    _append_passive(buf, content="先 A 再 B")
    agg = buf.append(
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="先 a 再 B!",  # same fingerprint, different phrasing
        source="active_chat",
    )
    assert agg.active_endorsements == 1
    assert agg.canonical_content == "先 a 再 B!"
    assert agg.sources == {"passive": 1, "active_chat": 1}


def test_record_outcome_increments_applied_count(buf: ProposalBuffer):
    agg = _append_passive(buf)
    buf.record_outcome(
        [agg.fingerprint],
        success=True,
        ontology_id="example",
        operation_key="OP_A",
    )
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].applied_count == 1

    buf.record_outcome([agg.fingerprint], success=False, ontology_id="example", operation_key="OP_A")
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].applied_count == 1  # failure does not increment


def test_record_vote_up_and_down(buf: ProposalBuffer):
    agg = _append_passive(buf)
    buf.record_vote([agg.fingerprint], vote="up", ontology_id="example", operation_key="OP_A")
    buf.record_vote([agg.fingerprint], vote="down", ontology_id="example", operation_key="OP_A")
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].active_endorsements == 1
    assert aggs[0].rejection_count == 1


def test_record_vote_switches_and_clears_previous_vote(buf: ProposalBuffer):
    agg = _append_passive(buf)

    buf.record_vote(
        [agg.fingerprint],
        vote="up",
        previous_vote=None,
        ontology_id="example",
        operation_key="OP_A",
    )
    buf.record_vote(
        [agg.fingerprint],
        vote="up",
        previous_vote="up",
        ontology_id="example",
        operation_key="OP_A",
    )
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].active_endorsements == 1
    assert aggs[0].rejection_count == 0

    buf.record_vote(
        [agg.fingerprint],
        vote="down",
        previous_vote="up",
        ontology_id="example",
        operation_key="OP_A",
    )
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].active_endorsements == 0
    assert aggs[0].rejection_count == 1

    buf.record_vote(
        [agg.fingerprint],
        vote="none",
        previous_vote="down",
        ontology_id="example",
        operation_key="OP_A",
    )
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].active_endorsements == 0
    assert aggs[0].rejection_count == 0


def test_reject_status_blocks_passive_resurrection(buf: ProposalBuffer):
    agg = _append_passive(buf)
    buf.reject("example", "OP_A", agg.fingerprint)
    again = _append_passive(buf)
    assert again.status == "rejected", "passive duplicate must not resurrect rejected"

    active = buf.append(
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="先 X 再 Y",
        source="active_chat",
    )
    assert active.status == "pending", "explicit user override resurrects"


def test_edit_recomputes_fingerprint(buf: ProposalBuffer):
    agg = _append_passive(buf, content="原始内容")
    new = buf.edit("example", "OP_A", agg.fingerprint, "全新内容")
    assert new is not None
    assert new.fingerprint != agg.fingerprint
    assert new.occurrences == agg.occurrences
    aggs = {a.fingerprint: a for a in buf.list_aggregates("example", "OP_A")}
    assert agg.fingerprint not in aggs
    assert new.fingerprint in aggs


def test_recall_filters_by_kind_and_ranks(buf: ProposalBuffer):
    proc = _append_passive(buf, content="高频流程")
    for _ in range(2):
        buf.record_outcome([proc.fingerprint], success=True, ontology_id="example", operation_key="OP_A")
    buf.append(
        ontology_id="example",
        operation_key="OP_A",
        kind="pitfall",
        content="某个低频坑",
        source="passive",
    )
    procedures = buf.recall("example", operation_key="OP_A", kind="procedure", k=5)
    pitfalls = buf.recall("example", operation_key="OP_A", kind="pitfall", k=5)
    assert len(procedures) == 1 and procedures[0].fingerprint == proc.fingerprint
    assert len(pitfalls) == 1
    assert procedures[0].applied_count == 2


def test_object_level_schema_allows_missing_operation_key(buf: ProposalBuffer):
    agg = buf.append(
        ontology_id="example",
        operation_key=None,
        kind="schema",
        entity_key="TargetItem",
        content="TargetItem 的物料编码字段是 TargetItem编码。",
        source="active_chat",
    )

    assert agg.operation_key is None
    schemas = buf.recall("example", operation_key=None, kind="schema", entity_key="TargetItem", k=5)
    assert len(schemas) == 1
    assert schemas[0].fingerprint == agg.fingerprint


def test_ontology_level_procedure_allows_missing_operation_key(buf: ProposalBuffer):
    agg = buf.append(
        ontology_id="example",
        operation_key=None,
        kind="procedure",
        content="执行本体内流程时优先校验输入完整性。",
        source="active_chat",
    )

    assert agg.operation_key is None
    procedures = buf.recall("example", operation_key=None, kind="procedure", k=5)
    assert len(procedures) == 1
    assert procedures[0].fingerprint == agg.fingerprint


def test_recall_excludes_rejected(buf: ProposalBuffer):
    a = _append_passive(buf)
    buf.reject("example", "OP_A", a.fingerprint)
    assert buf.recall("example", operation_key="OP_A", kind="procedure", k=5) == []


def test_change_listener_fires_with_event_type(tmp_path: Path):
    events = []

    def listener(event, workspace_id, onto, op, fp):
        events.append((event, workspace_id, onto, op, fp))

    b = ProposalBuffer(str(tmp_path / "evo"), change_listener=listener)
    agg = b.append(
        ontology_id="example",
        operation_key="OP_A",
        kind="procedure",
        content="ABCDEF",
        source="passive",
    )
    b.record_outcome([agg.fingerprint], success=True, ontology_id="example", operation_key="OP_A")
    b.record_vote([agg.fingerprint], vote="up", ontology_id="example", operation_key="OP_A")
    b.endorse("example", "OP_A", agg.fingerprint)
    b.reject("example", "OP_A", agg.fingerprint)
    types = [e[0] for e in events]
    assert types == ["append", "outcome", "vote", "endorse", "reject"]
    assert all(e[1] is None for e in events)


def test_invalid_inputs_rejected(buf: ProposalBuffer):
    with pytest.raises(ValueError):
        buf.append(
            ontology_id="example", operation_key="OP_A",
            kind="bogus", content="xxxxx", source="passive",
        )
    with pytest.raises(ValueError):
        buf.append(
            ontology_id="example", operation_key="OP_A",
            kind="procedure", content="xxxxx", source="from-the-void",
        )
    with pytest.raises(ValueError):
        buf.append(
            ontology_id="", operation_key="OP_A",
            kind="procedure", content="xxxxx", source="passive",
        )
    with pytest.raises(ValueError):
        buf.append(
            ontology_id="example", operation_key="OP_A",
            kind="procedure", content="x", source="passive",
        )


def test_archive_applied_marks_status_and_writes_snapshot(buf: ProposalBuffer):
    agg = _append_passive(buf)
    buf.archive_applied("example", "OP_A", [agg.fingerprint])
    aggs = buf.list_aggregates("example", "OP_A")
    assert aggs[0].status == "applied"
    snapshot = buf.op_dir("example", "OP_A") / "applied" / "{}.json".format(agg.fingerprint)
    assert snapshot.exists()


def test_compute_fingerprint_partitions_by_op(buf: ProposalBuffer):
    fp1 = compute_fingerprint(
        kind="procedure", ontology_id="A", operation_key="OP_A",
        entity_key=None, content="同样的内容",
    )
    fp2 = compute_fingerprint(
        kind="procedure", ontology_id="A", operation_key="OP_B",
        entity_key=None, content="同样的内容",
    )
    assert fp1 != fp2
