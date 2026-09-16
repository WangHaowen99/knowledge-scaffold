# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json
from pathlib import Path

from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import EvidenceIndexManager, read_active_pointer_snapshot
from ontology.evidence.worker import (
    EvidenceIndexWorker,
    process_ontology_changed_event,
    run_evidence_sweeper_once,
)
from ontology.mutations import MutationStore, OntologyMutationCoordinator, PreparedOntologyMutation
from ontology.ontologies import load_ontology_manifest_data
from workspace.workspaces import create_workspace, get_workspace_paths


def _workspace_with_ontology(tmp_path, monkeypatch, workspace_id="workspace-a"):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id=workspace_id, set_as_default=True)
    paths = get_workspace_paths(workspace_id)
    root = paths.ontologies_dir / "onto-a"
    version = root / "versions" / "1.0.0"
    (version / "objects").mkdir(parents=True)
    (root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: onto-a",
                "name: Ontology A",
                "description: Worker test ontology",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
                "enabled: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (version / "index.md").write_text(
        "---\nresource: root\nkey: root\ntitle: Ontology A\n---\n# Ontology A\n",
        encoding="utf-8",
    )
    (version / "objects" / "part.md").write_text(
        "---\nresource: object\nkey: part\ntitle: Part\n---\n# Part\n\n## 属性\n编号。\n",
        encoding="utf-8",
    )
    return paths, root, version


def _enabled_config():
    return EvidenceConfig(enabled=True, rollout_mode="all", auto_build_enabled=True)


def test_event_consumer_builds_and_publishes_current_revision(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module

    paths, _root, _version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: _enabled_config()))
    coordinator = OntologyMutationCoordinator(paths.runtime_dir)
    result = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_IMPORTED",
            before_version=None,
            after_version="1.0.0",
            before_enabled=False,
            after_enabled=True,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )

    processed = process_ontology_changed_event(paths.runtime_dir, result.event)

    assert processed["status"] == "ready"
    pointer = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert pointer["status"] == "ready"
    assert pointer["mutation_revision"] == 1
    assert pointer["expected_epoch"] == processed["ontology_epoch"]


def test_old_event_is_ignored_and_latest_revision_wins(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module

    paths, _root, version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: _enabled_config()))
    coordinator = OntologyMutationCoordinator(paths.runtime_dir)
    first = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_IMPORTED",
            before_version=None,
            after_version="1.0.0",
            before_enabled=False,
            after_enabled=True,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )
    object_path = version / "objects" / "part.md"
    previous = object_path.read_text(encoding="utf-8")

    def commit_change():
        object_path.write_text(previous + "新增字段。\n", encoding="utf-8")

    def rollback_change():
        object_path.write_text(previous, encoding="utf-8")

    second = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_FILE_CHANGED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=commit_change,
            rollback=rollback_change,
        )
    )

    assert process_ontology_changed_event(paths.runtime_dir, first.event)["status"] == "stale"
    assert process_ontology_changed_event(paths.runtime_dir, second.event)["status"] == "ready"
    pointer = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert pointer["mutation_revision"] == 2


def test_disabled_config_acknowledges_without_creating_index_files(tmp_path, monkeypatch):
    paths, _root, _version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(
        EvidenceConfig,
        "load",
        classmethod(lambda cls: EvidenceConfig(enabled=False, rollout_mode="off")),
    )
    coordinator = OntologyMutationCoordinator(paths.runtime_dir)
    result = coordinator.commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="ONTOLOGY_IMPORTED",
            before_version=None,
            after_version="1.0.0",
            before_enabled=False,
            after_enabled=True,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )

    assert process_ontology_changed_event(paths.runtime_dir, result.event)["status"] == "disabled"
    assert not (paths.runtime_dir / "ontology-evidence").exists()


def test_sweeper_detects_external_drift_and_routes_rebuild_through_revision_outbox(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module

    paths, root, version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    config = _enabled_config()
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: config))
    EvidenceIndexManager(paths.runtime_dir).build(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        ontology_version="1.0.0",
        ontology_dir=version,
        manifest=load_ontology_manifest_data(root),
        mutation_revision=0,
    )
    object_path = version / "objects" / "part.md"
    object_path.write_text(object_path.read_text(encoding="utf-8") + "进程外变更。\n", encoding="utf-8")

    assert run_evidence_sweeper_once(config=config) == 1
    stale = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert stale["status"] == "epoch_pending"
    assert stale["mutation_revision"] == 1

    stats = EvidenceIndexWorker().run_once()
    assert stats["dispatched"] == 1
    ready = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert ready["status"] == "ready"
    assert ready["mutation_revision"] == 1


def test_auto_build_disabled_persists_manual_required(tmp_path, monkeypatch):
    paths, root, version = _workspace_with_ontology(tmp_path, monkeypatch)
    config = EvidenceConfig(enabled=True, rollout_mode="all", auto_build_enabled=False)
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: config))
    EvidenceIndexManager(paths.runtime_dir).build(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        ontology_version="1.0.0",
        ontology_dir=version,
        manifest=load_ontology_manifest_data(root),
        mutation_revision=0,
    )
    result = OntologyMutationCoordinator(paths.runtime_dir).commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="EXTERNAL_DRIFT_DETECTED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )

    assert process_ontology_changed_event(paths.runtime_dir, result.event)["status"] == "manual_required"
    pointer = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert pointer is not None
    assert pointer["status"] == "manual_required"
    assert pointer["active_generation"] is None


def test_sweeper_restores_same_revision_manual_pointer_after_auto_build_enabled(
    tmp_path,
    monkeypatch,
):
    import ontology.ontologies as ontologies_module

    paths, root, version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    manual = EvidenceConfig(
        enabled=True,
        rollout_mode="all",
        auto_build_enabled=False,
        sweeper_enabled=True,
    )
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: manual))
    EvidenceIndexManager(paths.runtime_dir).build(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        ontology_version="1.0.0",
        ontology_dir=version,
        manifest=load_ontology_manifest_data(root),
        mutation_revision=0,
    )
    result = OntologyMutationCoordinator(paths.runtime_dir).commit(
        PreparedOntologyMutation(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type="EXTERNAL_DRIFT_DETECTED",
            before_version="1.0.0",
            after_version="1.0.0",
            before_enabled=True,
            after_enabled=True,
            commit=lambda: None,
            rollback=lambda: None,
        )
    )
    assert process_ontology_changed_event(paths.runtime_dir, result.event)["status"] == "manual_required"

    automatic = EvidenceConfig(
        enabled=True,
        rollout_mode="all",
        auto_build_enabled=True,
        sweeper_enabled=True,
    )
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: automatic))

    assert run_evidence_sweeper_once(config=automatic) == 1
    pending = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert pending["status"] == "epoch_pending"
    assert pending["mutation_revision"] == 2

    stats = EvidenceIndexWorker().run_once()
    assert stats["dispatched"] >= 1
    ready = read_active_pointer_snapshot(paths.runtime_dir, "onto-a")
    assert ready["status"] == "ready"
    assert ready["mutation_revision"] == 2


def test_sweeper_recovers_pointer_revision_ahead_of_store_without_auto_build(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module

    paths, root, version = _workspace_with_ontology(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    config = EvidenceConfig(
        enabled=True,
        rollout_mode="all",
        auto_build_enabled=False,
        sweeper_enabled=True,
    )
    monkeypatch.setattr(EvidenceConfig, "load", classmethod(lambda cls: config))
    manager = EvidenceIndexManager(paths.runtime_dir)
    manager.build(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        ontology_version="1.0.0",
        ontology_dir=version,
        manifest=load_ontology_manifest_data(root),
        mutation_revision=0,
    )
    MutationStore(paths.runtime_dir / "ontology-mutations.sqlite3").ensure_state(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        active_version="1.0.0",
        enabled=True,
    )
    pointer_path = paths.runtime_dir / "ontology-evidence" / "ontologies" / "onto-a" / "active.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer.update({"mutation_revision": 1, "status": "epoch_pending", "active_generation": None})
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    assert run_evidence_sweeper_once(config=config) == 1
    state = MutationStore(paths.runtime_dir / "ontology-mutations.sqlite3").get_state(
        workspace_id="workspace-a",
        ontology_id="onto-a",
    )
    assert state is not None and state["mutation_revision"] == 1
