from __future__ import annotations

import json
from pathlib import Path

from integration.solver_operation_runtime import ControlledScriptOperationRuntime
from ontology.ontologies import invalidate_ontology_registry
from solver_coordination.candidate_service import create_candidate_set
from solver_coordination.candidate_store import CandidateSetStore
from solver_coordination.canonical import sha256_digest
from solver_coordination.run_ledger import SolverRunLedger
from solver_coordination.runtime import build_operation_validation_service
from workspace.workspaces import create_workspace, get_workspace_paths


def _write_package(workspace_id: str, package_id: str, revision: str) -> Path:
    package_root = get_workspace_paths(workspace_id).ontologies_dir / package_id
    version_root = package_root / "versions" / revision
    (version_root / "operations").mkdir(parents=True, exist_ok=True)
    (version_root / "scripts").mkdir(parents=True, exist_ok=True)
    (package_root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                f"id: {package_id}",
                "name: Synthetic Solver Package",
                "description: Synthetic solver integration fixture",
                f"version: {revision}",
                f"active_version: {revision}",
                "versions:",
                f"  - {revision}",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version_root / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                f"id: {package_id}",
                "name: Synthetic Solver Package",
                "description: Synthetic solver integration fixture",
                f"version: {revision}",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version_root / "solver-capabilities.yaml").write_text(
        """
capabilities:
  solver_operations:
    - operation: solve_demo
      validation_operation: validate_demo
      problem_family: binary_ordered_route.v1
      validator_revision: ontology_operation_batch.v1
      rules_revision: rules_demo_1
""".strip(),
        encoding="utf-8",
    )
    (version_root / "operations" / "validate_demo.md").write_text(
        """
---
resource: operation
key: validate_demo
execution:
  mode: validation_workflow
  validates_operation: solve_demo
---
""".strip(),
        encoding="utf-8",
    )
    (version_root / "scripts" / "validate_demo.md").write_text(
        """
---
resource: script
key: validate_demo_script
title: Synthetic validation
bound_operation: validate_demo
entrypoint: validate_demo.py
---

| name | flag | type | required |
| --- | --- | --- | --- |
| request_json | --request-json | string | true |
""".strip(),
        encoding="utf-8",
    )
    (version_root / "scripts" / "validate_demo.py").write_text(
        """
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument('--request-json', required=True)
request = json.loads(parser.parse_args().request_json)
assert request['fact_snapshot']['payload']['synthetic_threshold'] == 7
print(json.dumps({
    'schema_version': 'solver_coordination.validation_operation.v1',
    'results': [
        {'candidate_id': item['candidate_id'], 'status': 'feasible'}
        for item in request['candidate_set']['candidates']
    ],
}))
""".strip(),
        encoding="utf-8",
    )
    invalidate_ontology_registry(workspace_id=workspace_id)
    return version_root


def test_production_service_executes_real_frozen_operation_and_persists_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    workspace_id = "solver_runtime_integration"
    owner_id = "synthetic_operator"
    package_id = "knowledge_package_solver_demo"
    revision = "1.0.0"
    create_workspace("Synthetic Solver Workspace", workspace_id=workspace_id, set_as_default=True)
    version_root = _write_package(workspace_id, package_id, revision)

    candidate_set = create_candidate_set(
        plan_payload={
            "items": [
                {
                    "item_order": 1,
                    "item_id": "item_synthetic_001",
                    "quantity": "1.0",
                    "duration": "2.0",
                }
            ],
            "option_domain": ["OPTION_0", "OPTION_1"],
            "max_transitions": 1,
        },
        knowledge_package_id=package_id,
        knowledge_package_revision=revision,
        source_run_id="solve_runtime_integration",
    )
    ledger = SolverRunLedger(workspace_id)
    candidate_ref, _artifact = CandidateSetStore(workspace_id, ledger=ledger).put(
        candidate_set,
        owner_id=owner_id,
    )

    facts = {"synthetic_threshold": 7}
    snapshot = {
        "schema_version": "solver_coordination.fact_snapshot.v1",
        "artifact_type": "fact_snapshot",
        "source_run_id": candidate_set.source_run_id,
        "knowledge_package_id": package_id,
        "knowledge_package_revision": revision,
        "rules_revision": "rules_demo_1",
        "fact_payload": facts,
        "fact_snapshot_digest": sha256_digest({"fact_payload": facts}),
    }
    snapshot_path = (
        get_workspace_paths(workspace_id).runtime_dir
        / "solver_coordination"
        / "fact_snapshots"
        / "runtime-integration.json"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    ledger.register_artifact(
        artifact_digest=snapshot["fact_snapshot_digest"],
        artifact_type="fact_snapshot",
        artifact_ref="fact_snapshot_{}".format(snapshot["fact_snapshot_digest"].rsplit(":", 1)[-1]),
        artifact_path=snapshot_path.relative_to(get_workspace_paths(workspace_id).runtime_dir).as_posix(),
        owner_id=owner_id,
        source_run_id=candidate_set.source_run_id,
    )

    result = build_operation_validation_service(operation_runtime=ControlledScriptOperationRuntime()).run(
        workspace_id=workspace_id,
        owner_id=owner_id,
        candidate_set_ref=candidate_ref,
        source_run_id=candidate_set.source_run_id,
        candidate_set_digest=candidate_set.candidate_set_digest,
        validation_run_id="validation_runtime_integration",
        validator_revision="ontology_operation_batch.v1",
    )

    assert result.result_count == candidate_set.candidate_count
    assert result.selection_status == "published"
    assert all(item.evidence_ref.startswith("validation_evidence_") for item in result.all_candidate_results)
    evidence_rows = ledger.find_artifacts(
        owner_id=owner_id,
        source_run_id="validation_runtime_integration",
        artifact_type="validation_evidence",
    )
    assert len(evidence_rows) == 1
    assert Path(get_workspace_paths(workspace_id).runtime_dir / evidence_rows[0]["artifact_path"]).is_file()
    assert version_root.is_dir()
