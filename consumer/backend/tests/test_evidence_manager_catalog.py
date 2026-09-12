"""Immutable generation, pointer, and catalog integration tests."""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from ontology.evidence.manager import (
    EvidenceIndexManager,
    EvidenceIndexManagerError,
    recover_interrupted_builds,
)
from ontology.evidence.resolver import EvidenceResolver
from ontology.evidence.service import EvidenceSearchService
from persistence.runtime_facts import RuntimeFactsStore


def _package(root: Path) -> Path:
    package = root / "versions" / "1.0.0"
    (package / "operations").mkdir(parents=True)
    (package / "index.md").write_text("# ECO\n", encoding="utf-8")
    (package / "operations" / "impact.md").write_text(
        """---
key: eco_impact
title: ECO影响分析
aliases: [设计变更影响制造物料]
---
# ECO影响分析
## 流程
追溯变更前后版本与制造物料。
""",
        encoding="utf-8",
    )
    return package


def _manifest():
    return {
        "kind": "ontology-package",
        "schema_version": "0.1",
        "id": "eco",
        "name": "ECO",
        "description": "变更影响",
        "language": "zh-CN",
        "scope": ["plm"],
    }


def test_manager_builds_registers_and_atomically_opens_generation(tmp_path: Path):
    manager = EvidenceIndexManager(tmp_path / "runtime")
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=2,
    )

    assert pointer["status"] == "ready"
    assert pointer["mutation_revision"] == 2
    index = manager.open_active("eco")
    response = EvidenceSearchService(index, workspace_id="workspace-a").search(
        ontology_id="eco", query="设计变更影响制造物料", limit=5
    )
    assert response["status"] == "resolved"
    evidence_id = response["hits"][0]["evidence_id"]
    routes = manager.catalog.lookup_routes(evidence_id)
    assert len(routes) == 1
    assert routes[0]["index_generation"] == pointer["active_generation"]


def test_manager_reuses_identical_generation_and_revoke_fails_closed(tmp_path: Path):
    manager = EvidenceIndexManager(tmp_path / "runtime")
    package = _package(tmp_path)
    first = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=1,
    )
    second = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=1,
    )
    assert first["active_generation"] == second["active_generation"]

    manager.revoke("eco", mutation_revision=2)
    assert manager.active_pointer("eco")["status"] == "stale"
    try:
        manager.open_active("eco")
    except EvidenceIndexManagerError as exc:
        assert exc.code == "INDEX_STALE"
    else:
        raise AssertionError("revoked pointer must not open an old generation")


def test_manager_rebuilds_corrupt_generation_and_recovery_removes_old_staging(tmp_path: Path):
    runtime = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime)
    package = _package(tmp_path)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=1,
    )
    generation_dir = runtime / "ontology-evidence" / "ontologies" / "eco" / "generations" / pointer["active_generation"]
    index_path = generation_dir / "index.sqlite3"
    index_path.chmod(0o644)
    index_path.write_bytes(b"broken")

    rebuilt = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=1,
    )
    assert rebuilt["active_generation"] == pointer["active_generation"]
    assert manager.open_active("eco").metadata()["ontology_id"] == "eco"

    staging = runtime / "ontology-evidence" / "ontologies" / "eco" / "tmp" / "idxjob_interrupted"
    staging.mkdir(parents=True)
    (staging / "partial").write_text("partial", encoding="utf-8")
    old = time.time() - 3600
    os.utime(staging, (old, old))
    result = recover_interrupted_builds(runtime, grace_seconds=60)
    assert result == {"scanned": 1, "removed": 1}
    assert not staging.exists()


def test_manager_single_flight_builds_one_generation_and_persists_transitions(
    tmp_path: Path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    package = _package(tmp_path)
    manager_a = EvidenceIndexManager(runtime)
    manager_b = EvidenceIndexManager(runtime)
    original_build = EvidenceIndexManager._build_generation
    manager_module = importlib.import_module("ontology.evidence.manager")
    original_write = manager_module._atomic_json_write
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_guard = threading.Lock()
    transitions: list[str] = []

    def delayed_build(self, **kwargs):
        nonlocal calls
        with calls_guard:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original_build(self, **kwargs)

    def capture_pointer(path, payload):
        if path.name == "active.json":
            transitions.append(str(payload.get("status") or ""))
        return original_write(path, payload)

    monkeypatch.setattr(EvidenceIndexManager, "_build_generation", delayed_build)
    monkeypatch.setattr("ontology.evidence.manager._atomic_json_write", capture_pointer)

    def build(manager):
        return manager.build(
            workspace_id="workspace-a",
            ontology_id="eco",
            ontology_version="1.0.0",
            ontology_dir=package,
            manifest=_manifest(),
            mutation_revision=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(build, manager_a)
        assert entered.wait(timeout=5)
        second = executor.submit(build, manager_b)
        time.sleep(0.05)
        release.set()
        first_pointer = first.result(timeout=10)
        second_pointer = second.result(timeout=10)

    assert calls == 1
    assert first_pointer["active_generation"] == second_pointer["active_generation"]
    assert transitions == ["queued", "building_base", "lexical_ready", "ready"]


def test_manager_records_base_failed_without_publishing_partial_generation(
    tmp_path: Path,
    monkeypatch,
):
    manager = EvidenceIndexManager(tmp_path / "runtime")

    def fail_build(*_args, **_kwargs):
        raise EvidenceIndexManagerError("INDEX_BUILD_FAILED", "synthetic failure")

    monkeypatch.setattr(manager, "_build_generation", fail_build)
    with pytest.raises(EvidenceIndexManagerError):
        manager.build(
            workspace_id="workspace-a",
            ontology_id="eco",
            ontology_version="1.0.0",
            ontology_dir=_package(tmp_path),
            manifest=_manifest(),
            mutation_revision=1,
        )

    pointer = manager.active_pointer("eco")
    assert pointer is not None
    assert pointer["status"] == "base_failed"
    assert pointer["base_status"] == "failed"
    assert pointer["active_generation"] is None
    assert pointer["error_code"] == "INDEX_BUILD_FAILED"


def test_manager_records_epoch_failed_when_source_planning_fails(
    tmp_path: Path,
    monkeypatch,
):
    manager = EvidenceIndexManager(tmp_path / "runtime")

    def fail_plan(**_kwargs):
        raise EvidenceIndexManagerError("INDEX_CAPACITY_EXCEEDED", "synthetic planning failure")

    monkeypatch.setattr("ontology.evidence.manager._build_plan", fail_plan)
    with pytest.raises(EvidenceIndexManagerError):
        manager.build(
            workspace_id="workspace-a",
            ontology_id="eco",
            ontology_version="1.0.0",
            ontology_dir=_package(tmp_path),
            manifest=_manifest(),
            mutation_revision=1,
        )

    pointer = manager.active_pointer("eco")
    assert pointer is not None
    assert pointer["status"] == "epoch_failed"
    assert pointer["epoch_status"] == "failed"
    assert pointer["base_status"] == "pending"
    assert pointer["active_generation"] is None
    assert pointer["error_code"] == "INDEX_CAPACITY_EXCEEDED"
    with pytest.raises(EvidenceIndexManagerError) as exc_info:
        manager.open_active("eco")
    assert exc_info.value.code == "INDEX_BUILD_FAILED"


def test_manager_rejects_pointer_and_generation_workspace_mismatch(tmp_path: Path):
    runtime = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime)
    manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=1,
    )
    pointer_path = runtime / "ontology-evidence" / "ontologies" / "eco" / "active.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["workspace_id"] = "workspace-b"
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(EvidenceIndexManagerError) as exc_info:
        manager.open_active("eco")

    assert exc_info.value.code == "INDEX_STALE"


def test_cleanup_waits_for_build_and_preserves_newly_published_generation(
    tmp_path: Path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    package = _package(tmp_path)
    setup_manager = EvidenceIndexManager(runtime)
    pointer = setup_manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=1,
    )
    generation = pointer["active_generation"]
    generation_dir = runtime / "ontology-evidence" / "ontologies" / "eco" / "generations" / generation
    setup_manager.revoke("eco", mutation_revision=1)
    old = time.time() - 10 * 86400
    os.utime(generation_dir, (old, old))

    build_manager = EvidenceIndexManager(runtime)
    cleanup_manager = EvidenceIndexManager(runtime)
    original_build = build_manager._build_single_flight
    entered = threading.Event()
    release = threading.Event()

    def delayed_build(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_build(**kwargs)

    monkeypatch.setattr(build_manager, "_build_single_flight", delayed_build)

    def rebuild():
        return build_manager.build(
            workspace_id="workspace-a",
            ontology_id="eco",
            ontology_version="1.0.0",
            ontology_dir=package,
            manifest=_manifest(),
            mutation_revision=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        build_future = executor.submit(rebuild)
        assert entered.wait(timeout=5)
        cleanup_future = executor.submit(
            cleanup_manager.cleanup,
            workspace_id="workspace-a",
            ontology_id="eco",
            dry_run=False,
            older_than_days=1,
            recent_count=0,
        )
        time.sleep(0.05)
        assert not cleanup_future.done()
        release.set()
        rebuilt = build_future.result(timeout=10)
        cleanup = cleanup_future.result(timeout=10)

    assert rebuilt["active_generation"] == generation
    assert cleanup["summary"]["deleted_generations"] == 0
    assert generation_dir.is_dir()
    assert build_manager.active_pointer("eco")["active_generation"] == generation


def test_runtime_fact_reference_holds_and_releases_immutable_generation(tmp_path: Path):
    runtime = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=1,
    )
    evidence_id = next(iter(manager.open_active("eco").iter_chunks()))["evidence_id"]
    generation = pointer["active_generation"]
    generation_dir = runtime / "ontology-evidence" / "ontologies" / "eco" / "generations" / generation
    fact_store = RuntimeFactsStore(runtime / "ontology-evidence" / "runtime-facts.sqlite3")
    fact = fact_store.put(
        {
            "workspace_id": "workspace-a",
            "run_id": "run-retention",
            "tool_call_id": "call-retention",
            "kind": "provider_query",
            "ontology_id": "eco",
            "ontology_epoch": pointer["expected_epoch"],
            "subject": "object:MPart",
            "status": "success",
            "summary": "grounded provider result",
            "provider": "test",
            "grounding_status": "grounded",
            "grounding_warnings": [],
            "provider_evidence": [],
            "derived_from": [evidence_id],
            "validity_class": "current_fact",
            "read_at": "2000-01-01T00:00:00+00:00",
            "created_at": "2000-01-01T00:00:00+00:00",
        }
    )

    manager.revoke("eco", mutation_revision=2)
    old = time.time() - 10 * 86400
    os.utime(generation_dir, (old, old))
    protected = manager.cleanup(
        workspace_id="workspace-a",
        ontology_id="eco",
        dry_run=False,
        older_than_days=1,
        recent_count=0,
    )

    assert protected["summary"]["deleted_generations"] == 0
    assert protected["summary"]["protected_by_reference"] == 1
    assert generation_dir.is_dir()
    assert EvidenceResolver(runtime, workspace_id="workspace-a").lookup(evidence_id)["evidence_id"] == evidence_id

    released = fact_store.cleanup(
        workspace_id="workspace-a",
        dry_run=False,
        older_than_days=1,
    )
    assert released["summary"]["deleted_items"] == 1
    assert (
        fact_store.lookup(
            workspace_id="workspace-a",
            evidence_id=fact["evidence_id"],
        )
        is None
    )

    removed = manager.cleanup(
        workspace_id="workspace-a",
        ontology_id="eco",
        dry_run=False,
        older_than_days=1,
        recent_count=0,
    )
    assert removed["summary"]["deleted_generations"] == 1
    assert not generation_dir.exists()


def test_cleanup_failure_leaves_catalog_safe_and_directory_retriable(
    tmp_path: Path,
    monkeypatch,
):
    import ontology.evidence.manager as manager_module

    runtime = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=1,
    )
    index = manager.open_active("eco")
    evidence_id = next(iter(index.iter_chunks()))["evidence_id"]
    generation = pointer["active_generation"]
    generation_dir = runtime / "ontology-evidence" / "ontologies" / "eco" / "generations" / generation
    manager.revoke("eco", mutation_revision=1)
    old = time.time() - 10 * 86400
    os.utime(generation_dir, (old, old))
    original_rmtree = manager_module.shutil.rmtree

    def fail_generation_delete(path, *args, **kwargs):
        if Path(path) == generation_dir:
            raise OSError("synthetic delete failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(manager_module.shutil, "rmtree", fail_generation_delete)
    with pytest.raises(OSError, match="synthetic delete failure"):
        manager.cleanup(
            workspace_id="workspace-a",
            ontology_id="eco",
            dry_run=False,
            older_than_days=1,
            recent_count=0,
        )

    assert generation_dir.is_dir()
    assert manager.catalog.lookup_routes(evidence_id) == []

    monkeypatch.setattr(manager_module.shutil, "rmtree", original_rmtree)
    retried = manager.cleanup(
        workspace_id="workspace-a",
        ontology_id="eco",
        dry_run=False,
        older_than_days=1,
        recent_count=0,
    )

    assert retried["summary"]["deleted_generations"] == 1
    assert not generation_dir.exists()
