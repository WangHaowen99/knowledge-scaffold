"""Runtime Fact persistence and Grounding lookup tests."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent.core.shared.runtime_fact_projector import (
    RuntimeFactPersistencePending,
    _RuntimeFactBatcher,
    capture_business_data_snapshot,
    project_business_data_result,
)
from agent.tools.base import ToolContext
from config import AgentConfig
from ontology.evidence.manager import EvidenceIndexManager
from persistence.runtime_facts import RuntimeFactsStore, RuntimeFactsStoreError
from utils.read_docs import build_grounding_subject_key, merge_read_documents_registry


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "objects").mkdir(parents=True)
    (package / "index.md").write_text("# ECO\n", encoding="utf-8")
    (package / "objects" / "MPart.md").write_text(
        """---
key: MPart
title: 制造物料
---
# 制造物料
## 字段
物料编号与工艺状态。
""",
        encoding="utf-8",
    )
    return package


def _manifest() -> dict[str, object]:
    return {
        "kind": "ontology-package",
        "schema_version": "0.1",
        "id": "eco",
        "name": "ECO",
        "description": "变更影响",
        "language": "zh-CN",
    }


def _ready(tmp_path: Path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime_dir)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=9,
    )
    monkeypatch.setattr(
        "agent.core.shared.runtime_fact_projector.get_workspace_paths",
        lambda _workspace: SimpleNamespace(runtime_dir=runtime_dir),
    )
    return runtime_dir, manager, pointer


def _context(read_documents) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-fact",
        session_id="session-fact",
        turn_id=2,
        agent_config=AgentConfig.from_env(),
        caller="main",
        agent_iteration=3,
        workspace_id="workspace-a",
        selected_ontology_id="eco",
        read_documents=read_documents,
    )


def _enable(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", "all")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_MODE", "sync")


def _ungrounded_fact_command(tool_call_id: str) -> dict[str, object]:
    return {
        "workspace_id": "workspace-a",
        "run_id": "run-batcher",
        "tool_call_id": tool_call_id,
        "kind": "provider_query",
        "ontology_id": "eco",
        "status": "success",
        "summary": "batcher test",
        "grounding_status": "unverified",
        "grounding_warnings": ["TEST_ONLY"],
        "provider_evidence": [],
        "derived_from": [],
        "validity_class": "current_fact",
    }


def _registry(pointer, *, include_binding: bool) -> dict:
    source_hash = "sha256:" + "c" * 64
    root = {
        "path": "ontologies/eco/manifest.yaml",
        "doc_type": "manifest",
        "ontology_id": "eco",
        "source": "preload",
        "ontology_epoch": pointer["expected_epoch"],
        "mutation_revision": pointer["mutation_revision"],
    }
    records = [root]
    if include_binding:
        evidence_id = "ont_ev_" + "1" * 40
        subject = build_grounding_subject_key(
            workspace_id="workspace-a",
            ontology_id="eco",
            ontology_epoch=pointer["expected_epoch"],
            resource="object",
            resource_key="MPart",
        )
        records.append(
            {
                "path": "ontologies/eco/objects/MPart.md",
                "doc_type": "object_doc",
                "ontology_id": "eco",
                "source": "ontology_context",
                "ontology_epoch": pointer["expected_epoch"],
                "mutation_revision": pointer["mutation_revision"],
                "source_content_sha256": source_hash,
                "_grounding_bindings": [
                    {
                        "evidence_id": evidence_id,
                        "subject_key": subject,
                        "workspace_id": "workspace-a",
                        "ontology_id": "eco",
                        "ontology_version": "1.0.0",
                        "ontology_epoch": pointer["expected_epoch"],
                        "mutation_revision": pointer["mutation_revision"],
                        "index_generation": pointer["active_generation"],
                        "resource": "object",
                        "resource_key": "MPart",
                        "section_anchor": "制造物料/字段",
                        "source_content_sha256": source_hash,
                        "loaded_turn_id": 2,
                        "loaded_iteration": 1,
                    }
                ],
            }
        )
    return merge_read_documents_registry(None, records)


def _provider_content() -> str:
    return json.dumps(
        {
            "success": True,
            "provider": "neo4j",
            "summary": "命中 2 条图谱业务数据。",
            "result_count": 2,
            "records": [{"id": "A"}, {"id": "B"}],
            "evidence": [
                {
                    "evidence_id": "graph_ev_001",
                    "provider": "neo4j",
                }
            ],
        },
        ensure_ascii=False,
    )


def test_projector_persists_grounded_fact_and_normalizes_provider_evidence(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    snapshot, mode = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-grounded",
        provider="neo4j",
    )
    assert mode == "enabled"
    assert snapshot is not None

    receipt = project_business_data_result(snapshot=snapshot, content=_provider_content())
    assert receipt["evidence_capability"] == "enabled"
    runtime_evidence = receipt["runtime_evidence"]
    assert runtime_evidence["evidence_id"].startswith("ev_")
    assert runtime_evidence["grounding_status"] == "grounded"
    assert runtime_evidence["derived_from"] == ["ont_ev_" + "1" * 40]

    store = RuntimeFactsStore(runtime_dir / "ontology-evidence" / "runtime-facts.sqlite3")
    fact = store.lookup(workspace_id="workspace-a", evidence_id=runtime_evidence["evidence_id"])
    assert fact is not None
    assert fact["row_count"] == 2
    assert fact["summary"] == "命中 2 条图谱业务数据。"
    assert fact["provenance"]["query_digest"].startswith("sha256:")
    assert fact["provenance"]["provider_evidence"] == [
        {
            "kind": "provider_query_result",
            "provider": "neo4j",
            "provider_evidence_ref": "graph_ev_001",
        }
    ]
    assert fact["evidence_id"] != "graph_ev_001"


def test_idempotent_replay_does_not_upgrade_historical_unverified_fact(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    first_snapshot, _ = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=False)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-replay",
        provider="neo4j",
    )
    assert first_snapshot is not None
    first = project_business_data_result(snapshot=first_snapshot, content=_provider_content())
    assert first["runtime_evidence"]["grounding_status"] == "unverified"
    assert first["runtime_evidence"]["grounding_warnings"] == ["DETAIL_NOT_LOADED"]

    second_snapshot, _ = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-replay",
        provider="neo4j",
    )
    assert second_snapshot is not None
    second = project_business_data_result(snapshot=second_snapshot, content=_provider_content())
    assert second["runtime_evidence"]["evidence_id"] == first["runtime_evidence"]["evidence_id"]
    assert second["runtime_evidence"]["grounding_status"] == "unverified"
    assert second["runtime_evidence"]["derived_from"] == []

    store = RuntimeFactsStore(runtime_dir / "ontology-evidence" / "runtime-facts.sqlite3")
    fact = store.lookup(workspace_id="workspace-a", evidence_id=second["runtime_evidence"]["evidence_id"])
    assert fact["grounding_warnings"] == ["DETAIL_NOT_LOADED"]


def test_pointer_change_after_dispatch_marks_fact_stale(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    _runtime_dir, manager, pointer = _ready(tmp_path, monkeypatch)
    snapshot, _ = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-stale",
        provider="neo4j",
    )
    assert snapshot is not None
    manager.revoke("eco", mutation_revision=10)

    receipt = project_business_data_result(snapshot=snapshot, content=_provider_content())
    assert receipt["runtime_evidence"]["grounding_status"] == "unverified"
    assert receipt["runtime_evidence"]["grounding_warnings"] == ["GROUNDING_CONTEXT_STALE"]
    assert receipt["runtime_evidence"]["derived_from"] == []


def test_runtime_fact_mode_off_does_not_create_snapshot(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_MODE", "off")
    _runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    snapshot, mode = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-off",
        provider="neo4j",
    )
    assert snapshot is None
    assert mode == "disabled"


def test_runtime_fact_sqlite_failure_is_best_effort(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    _runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    snapshot, _ = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-sqlite-failure",
        provider="neo4j",
    )
    assert snapshot is not None

    class LockedRuntimeFactsStore:
        def __init__(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "agent.core.shared.runtime_fact_projector.RuntimeFactsStore",
        LockedRuntimeFactsStore,
    )

    receipt = project_business_data_result(snapshot=snapshot, content=_provider_content())

    assert receipt["evidence_capability"] == "persist_failed"
    assert receipt["error_code"] == "RUNTIME_FACT_PERSIST_FAILED"


def test_runtime_fact_in_flight_timeout_reports_pending_receipt(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_MODE", "batched")
    _runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    snapshot, _ = capture_business_data_snapshot(
        context=_context(_registry(pointer, include_binding=True)),
        args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
        tool_call_id="call-pending",
        provider="neo4j",
    )
    assert snapshot is not None

    class InFlightBatcher:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def submit(self, *_args, **_kwargs):
            raise RuntimeFactPersistencePending("write remains in progress")

    monkeypatch.setattr(
        "agent.core.shared.runtime_fact_projector._batcher_for",
        lambda *_args, **_kwargs: InFlightBatcher(),
    )

    receipt = project_business_data_result(snapshot=snapshot, content=_provider_content())

    assert receipt["evidence_capability"] == "pending"
    assert receipt["error_code"] == "RUNTIME_FACT_PERSISTENCE_UNKNOWN"
    assert receipt["runtime_evidence"]["persistence_status"] == "pending"
    assert receipt["runtime_evidence"]["evidence_id"] is None


def test_runtime_fact_store_applies_configured_busy_timeout(tmp_path: Path):
    store = RuntimeFactsStore(
        tmp_path / "runtime-facts.sqlite3",
        timeout_ms=37,
    )

    with store.connect_for_test() as connection:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert busy_timeout == 37


def test_batched_mode_groups_parallel_writes_and_returns_persisted_receipts(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_MODE", "batched")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_BATCH_SIZE", "2")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_FLUSH_INTERVAL_MS", "100")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_RUNTIME_FACT_WRITE_TIMEOUT_MS", "500")
    runtime_dir, _manager, pointer = _ready(tmp_path, monkeypatch)
    snapshots = []
    for suffix in ("a", "b"):
        snapshot, _ = capture_business_data_snapshot(
            context=_context(_registry(pointer, include_binding=True)),
            args={"ontology_id": "eco", "entity": "MPart", "query": "查询受影响物料"},
            tool_call_id="call-batched-" + suffix,
            provider="neo4j",
        )
        assert snapshot is not None
        snapshots.append(snapshot)

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda snapshot: project_business_data_result(
                    snapshot=snapshot,
                    content=_provider_content(),
                ),
                snapshots,
            )
        )

    assert all(item["evidence_capability"] == "enabled" for item in receipts)
    evidence_ids = {item["runtime_evidence"]["evidence_id"] for item in receipts}
    assert len(evidence_ids) == 2
    store = RuntimeFactsStore(runtime_dir / "ontology-evidence" / "runtime-facts.sqlite3")
    assert all(store.lookup(workspace_id="workspace-a", evidence_id=item) for item in evidence_ids)


def test_batched_timeout_before_dequeue_cancels_the_write(tmp_path: Path):
    path = tmp_path / "ontology-evidence" / "runtime-facts.sqlite3"
    batcher = _RuntimeFactBatcher(
        path,
        batch_size=10,
        flush_interval_ms=50,
        write_timeout_ms=500,
    )

    with pytest.raises(RuntimeFactsStoreError, match="timed out before persistence"):
        batcher.submit(_ungrounded_fact_command("call-canceled"), timeout_ms=5)

    time.sleep(0.08)
    with RuntimeFactsStore(path).connect_for_test() as connection:
        persisted = connection.execute(
            "SELECT COUNT(*) FROM runtime_facts WHERE tool_call_id='call-canceled'"
        ).fetchone()[0]
    assert persisted == 0


def test_batched_timeout_during_write_returns_pending_and_allows_late_completion(
    tmp_path: Path,
    monkeypatch,
):
    import agent.core.shared.runtime_fact_projector as projector_module

    path = tmp_path / "ontology-evidence" / "runtime-facts.sqlite3"
    started = threading.Event()
    release = threading.Event()
    original_store = RuntimeFactsStore

    class BlockingRuntimeFactsStore:
        def __init__(self, store_path, **kwargs):
            self.delegate = original_store(store_path, **kwargs)

        def put_many(self, commands):
            started.set()
            assert release.wait(timeout=5)
            return self.delegate.put_many(commands)

    monkeypatch.setattr(projector_module, "RuntimeFactsStore", BlockingRuntimeFactsStore)
    batcher = _RuntimeFactBatcher(
        path,
        batch_size=1,
        flush_interval_ms=1,
        write_timeout_ms=500,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        submitted = executor.submit(
            batcher.submit,
            _ungrounded_fact_command("call-in-flight"),
            timeout_ms=20,
        )
        assert started.wait(timeout=2)
        with pytest.raises(RuntimeFactPersistencePending):
            submitted.result(timeout=2)
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with original_store(path).connect_for_test() as connection:
            persisted = connection.execute(
                "SELECT COUNT(*) FROM runtime_facts WHERE tool_call_id='call-in-flight'"
            ).fetchone()[0]
        if persisted:
            break
        time.sleep(0.01)
    assert persisted == 1
