"""Contract tests for operation-centric solver package layout migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


def _write_operation(root: Path, key: str = "solve_route") -> None:
    operations = root / "operations"
    operations.mkdir(parents=True, exist_ok=True)
    (operations / f"{key}.md").write_text(
        "---\n"
        "resource: operation\n"
        f"key: {key}\n"
        "execution:\n"
        "  mode: solver_workflow\n"
        "  decision_model: route_choice\n"
        "---\n\nSolve the synthetic route.\n",
        encoding="utf-8",
    )


def _write_target_package(root: Path, *, marker: str = "operation-centric.v1") -> dict:
    _write_operation(root)
    operations = root / "operations"
    (operations / "route_choice.model.yaml").write_text(
        "kind: decision-model\nid: route_choice\n",
        encoding="utf-8",
    )
    (operations / "solve_route.input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"origin": {"type": "string"}}}),
        encoding="utf-8",
    )
    (operations / "solve_route.fact-snapshot.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"facts": {"type": "object"}}}),
        encoding="utf-8",
    )
    (operations / "solve_route.result.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"route": {"type": "string"}}}),
        encoding="utf-8",
    )
    (operations / "solve_route.presentation.yaml").write_text(
        "title: Route result\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "# Synthetic package\n\n"
        "| capability key | operation key | execution |\n"
        "| --- | --- | --- |\n"
        "| optimize_route | solve_route | solver_workflow |\n",
        encoding="utf-8",
    )
    (root / "operations" / "index.md").write_text(
        "# Operations\n\n"
        "| operation key | input | output |\n"
        "| --- | --- | --- |\n"
        "| solve_route | solve_route.input.schema.json | solve_route.result.schema.json |\n",
        encoding="utf-8",
    )
    manifest = {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": "knowledge-package-demo",
        "name": "Synthetic package",
        "description": "Synthetic package for layout tests",
        "version": "2.1.1",
        "solver_layout": marker,
        "capabilities": {
            "decision_models": [
                {
                    "id": "route_choice",
                    "operation_key": "solve_route",
                    "entry": "operations/route_choice.model.yaml",
                    "input_schema": "operations/solve_route.input.schema.json",
                    "fact_snapshot_schema": "operations/solve_route.fact-snapshot.schema.json",
                    "result_schema": "operations/solve_route.result.schema.json",
                    "presentation": "operations/solve_route.presentation.yaml",
                    "engine_requirements": {
                        "problem_family": "binary_ordered_route.v1",
                    },
                }
            ]
        },
    }
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return manifest


def _write_legacy_package(root: Path, *, status: str | None = None) -> dict:
    _write_operation(root)
    for directory in ("decision-models", "schemas", "presentations"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "decision-models" / "route-choice.yaml").write_text(
        "kind: decision-model\nid: route_choice\n",
        encoding="utf-8",
    )
    for filename, payload in (
        ("input.schema.json", {"type": "object"}),
        ("fact.schema.json", {"type": "object", "properties": {"facts": {}}}),
        ("result.schema.json", {"type": "object"}),
    ):
        (root / "schemas" / filename).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    (root / "presentations" / "result.yaml").write_text(
        "title: Route result\n", encoding="utf-8"
    )
    (root / "index.md").write_text(
        "# Synthetic package\n\nsolve_route\n", encoding="utf-8"
    )
    (root / "operations" / "index.md").write_text(
        "# Operations\n\nsolve_route\n", encoding="utf-8"
    )
    manifest = {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": "knowledge-package-demo",
        "name": "Synthetic package",
        "description": "Synthetic legacy package",
        "version": "2.1.0",
        "capabilities": {
            "decision_models": [
                {
                    "id": "route_choice",
                    "operation_key": "solve_route",
                    "entry": "decision-models/route-choice.yaml",
                    "input_schema": "schemas/input.schema.json",
                    "fact_snapshot_schema": "schemas/fact.schema.json",
                    "result_schema": "schemas/result.schema.json",
                    "presentation": "presentations/result.yaml",
                    "engine_requirements": {
                        "problem_family": "binary_ordered_route.v1",
                    },
                }
            ]
        },
    }
    if status is not None:
        manifest["status"] = status
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return manifest


def test_target_layout_validates_operation_sidecars_and_indexes(tmp_path):
    from ontology.solver_package_layout import validate_solver_package_layout

    manifest = _write_target_package(tmp_path)

    result = validate_solver_package_layout(tmp_path, manifest)

    assert result.layout == "operation-centric.v1"
    assert result.legacy is False
    assert [item.operation_key for item in result.operations] == ["solve_route"]
    assert result.operations[0].result_schema == Path(
        "operations/solve_route.result.schema.json"
    )


def test_target_layout_rejects_forbidden_solver_directories(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        validate_solver_package_layout,
    )

    manifest = _write_target_package(tmp_path)
    (tmp_path / "schemas").mkdir()

    with pytest.raises(SolverPackageLayoutError, match="schemas"):
        validate_solver_package_layout(tmp_path, manifest)


def test_target_layout_rejects_operation_model_identity_drift(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        validate_solver_package_layout,
    )

    manifest = _write_target_package(tmp_path)
    operation = tmp_path / "operations" / "solve_route.md"
    operation.write_text(
        operation.read_text(encoding="utf-8").replace(
            "decision_model: route_choice",
            "decision_model: another_model",
        ),
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="decision model"):
        validate_solver_package_layout(tmp_path, manifest)


def test_target_layout_rejects_invalid_json_schema(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        validate_solver_package_layout,
    )

    manifest = _write_target_package(tmp_path)
    (tmp_path / "operations" / "solve_route.input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"origin": {"type": "not-a-json-schema-type"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="valid local JSON Schema"):
        validate_solver_package_layout(tmp_path, manifest)


def test_import_rejects_unknown_solver_layout_marker(tmp_path):
    from ontology.package_import import validate_ontology_revision

    _write_target_package(tmp_path, marker="operation-centric.v999")

    with pytest.raises(ValueError, match="Unsupported solver package layout"):
        validate_ontology_revision(tmp_path)


def test_operation_centric_layout_is_enforced_at_package_import(tmp_path):
    from ontology.package_import import validate_ontology_revision

    _write_target_package(tmp_path)
    validate_ontology_revision(tmp_path)

    (tmp_path / "schemas").mkdir()
    with pytest.raises(ValueError, match="schemas"):
        validate_ontology_revision(tmp_path)


def test_index_manifest_and_operation_must_be_one_to_one(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        validate_solver_package_layout,
    )

    manifest = _write_target_package(tmp_path)
    (tmp_path / "operations" / "index.md").write_text(
        "# Operations\n\n| operation key |\n| --- |\n| other_operation |\n",
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="index"):
        validate_solver_package_layout(tmp_path, manifest)


def test_operation_centric_indexes_must_be_structured_tables(tmp_path):
    from ontology.package_import import validate_ontology_revision

    _write_target_package(tmp_path)
    (tmp_path / "index.md").write_text(
        "See solve_route for details.\n",
        encoding="utf-8",
    )
    (tmp_path / "operations" / "index.md").write_text(
        "See solve_route for details.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="index"):
        validate_ontology_revision(tmp_path)


def test_legacy_layout_remains_readable_without_target_marker(tmp_path):
    from ontology.solver_package_layout import validate_solver_package_layout

    manifest = _write_legacy_package(tmp_path)

    result = validate_solver_package_layout(tmp_path, manifest)

    assert result.legacy is True
    assert result.layout == "legacy"
    assert result.operations[0].decision_model == Path(
        "decision-models/route-choice.yaml"
    )


def test_declared_solver_path_must_be_package_contained(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        resolve_solver_sidecar_path,
    )

    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(SolverPackageLayoutError, match="contained"):
        resolve_solver_sidecar_path(tmp_path, "../outside.yaml")

    with pytest.raises(SolverPackageLayoutError, match="contained"):
        resolve_solver_sidecar_path(tmp_path, r"\outside.yaml")

    with pytest.raises(SolverPackageLayoutError, match="contained"):
        resolve_solver_sidecar_path(tmp_path, r"C:\outside.yaml")


def test_migration_dry_run_does_not_write_and_apply_creates_new_layout(tmp_path):
    from ontology.solver_package_layout import migrate_solver_package_layout

    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_legacy_package(source)

    dry_run = migrate_solver_package_layout(
        source,
        target,
        dry_run=True,
        target_version="2.2.0",
    )
    assert dry_run.dry_run is True
    assert not target.exists()
    assert any(item.endswith("operations/solve_route.model.yaml") for item in dry_run.planned_files)

    applied = migrate_solver_package_layout(
        source,
        target,
        dry_run=False,
        target_version="2.2.0",
    )
    assert applied.dry_run is False
    assert (target / "operations" / "solve_route.model.yaml").is_file()
    assert (target / "operations" / "solve_route.input.schema.json").is_file()
    assert not (target / "decision-models").exists()
    assert not (target / "schemas").exists()
    assert not (target / "presentations").exists()
    migrated_manifest = yaml.safe_load((target / "manifest.yaml").read_text(encoding="utf-8"))
    assert migrated_manifest["solver_layout"] == "operation-centric.v1"
    assert migrated_manifest["capabilities"]["decision_models"][0]["entry"] == (
        "operations/solve_route.model.yaml"
    )
    assert source.exists()


def test_migration_preserves_shared_indexes_and_rewrites_operation_document(tmp_path):
    from ontology.solver_package_layout import migrate_solver_package_layout

    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_legacy_package(source)
    (source / "index.md").write_text(
        "# Package\n\n| object key | title |\n| --- | --- |\n| tank | Tank |\n",
        encoding="utf-8",
    )
    (source / "operations" / "index.md").write_text(
        "# Operations\n\n| operation key | title |\n| --- | --- |\n| ordinary_op | Ordinary |\n",
        encoding="utf-8",
    )
    operation = source / "operations" / "solve_route.md"
    operation.write_text(
        operation.read_text(encoding="utf-8")
        + "\n| 契约 | 包内资源 |\n| --- | --- |\n"
        "| 输入 | `schemas/input.schema.json` |\n"
        "| 结果 | `schemas/result.schema.json` |\n",
        encoding="utf-8",
    )
    (source / "decision-models" / "route-choice.yaml").write_text(
        "kind: decision-model\nid: route_choice\n"
        "input_schema: schemas/input.schema.json\n"
        "fact_snapshot_schema: schemas/fact.schema.json\n"
        "result_schema: schemas/result.schema.json\n",
        encoding="utf-8",
    )
    (source / "presentations" / "result.yaml").write_text(
        "title: Route result\nsource_schema: schemas/result.schema.json\n",
        encoding="utf-8",
    )

    migrate_solver_package_layout(
        source,
        target,
        dry_run=False,
        target_version="2.2.0",
    )

    root_index = (target / "index.md").read_text(encoding="utf-8")
    operations_index = (target / "operations" / "index.md").read_text(encoding="utf-8")
    operation_text = (target / "operations" / "solve_route.md").read_text(encoding="utf-8")
    model_text = (target / "operations" / "solve_route.model.yaml").read_text(
        encoding="utf-8"
    )
    presentation_text = (
        target / "operations" / "solve_route.presentation.yaml"
    ).read_text(encoding="utf-8")
    assert "| tank | Tank |" in root_index
    assert "| ordinary_op | Ordinary |" in operations_index
    assert "solver-capabilities:v1:start" in root_index
    assert "operations/solve_route.input.schema.json" in operation_text
    assert "operations/solve_route.result.schema.json" in operation_text
    assert "schemas/input.schema.json" not in operation_text
    assert "schemas/result.schema.json" not in operation_text
    assert "operations/solve_route.input.schema.json" in model_text
    assert "operations/solve_route.fact-snapshot.schema.json" in model_text
    assert "operations/solve_route.result.schema.json" in model_text
    assert "operations/solve_route.result.schema.json" in presentation_text
    for legacy_directory in ("decision-models/", "schemas/", "presentations/"):
        assert legacy_directory not in model_text
        assert legacy_directory not in presentation_text


def test_dry_run_validates_staged_indexes_before_returning(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "source"
    _write_legacy_package(source)
    # An invalid non-UTF-8 index used to be invisible to dry-run planning;
    # staged validation must reject it before reporting success.
    (source / "index.md").write_bytes(b"\xff\xfe")

    with pytest.raises(SolverPackageLayoutError, match="Solver index"):
        migrate_solver_package_layout(
            source,
            tmp_path / "target",
            dry_run=True,
            target_version="2.2.0",
        )


def test_migration_refuses_unreferenced_case_variant_sidecar(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "source"
    _write_legacy_package(source)
    case_variant = source / "schemas" / "Input.schema.json"
    if case_variant.exists():
        pytest.skip("case-variant filenames require a case-sensitive filesystem")
    case_variant.write_text(
        json.dumps({"type": "object", "title": "Unrelated case variant"}),
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="unreferenced"):
        migrate_solver_package_layout(
            source,
            tmp_path / "target",
            dry_run=True,
            target_version="2.2.0",
        )


def test_migration_never_rewrites_released_revision_in_place(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "released"
    _write_legacy_package(source, status="released")

    with pytest.raises(SolverPackageLayoutError, match="new target revision"):
        migrate_solver_package_layout(source, source, dry_run=False)


def test_migration_rejects_target_inside_source_package(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "source"
    _write_legacy_package(source)

    with pytest.raises(SolverPackageLayoutError, match="outside the source"):
        migrate_solver_package_layout(
            source,
            source / "nested-target",
            dry_run=False,
            target_version="2.2.0",
        )

    assert not (source / "nested-target").exists()


def test_migration_rejects_distinct_target_for_already_migrated_package(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "source"
    _write_target_package(source)

    with pytest.raises(SolverPackageLayoutError, match="already uses"):
        migrate_solver_package_layout(
            source,
            tmp_path / "new-revision",
            dry_run=False,
            target_version="2.2.0",
        )

    assert not (tmp_path / "new-revision").exists()


def test_migration_refuses_in_place_apply_even_for_draft(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "draft"
    _write_legacy_package(source)

    with pytest.raises(SolverPackageLayoutError, match="new target revision"):
        migrate_solver_package_layout(source, source, dry_run=False)

    assert (source / "decision-models").exists()


def test_operation_centric_import_boundary_revalidates_layout(tmp_path):
    from ontology.package_import import validate_ontology_revision

    _write_target_package(tmp_path)
    assert validate_ontology_revision(tmp_path)["solver_layout"] == "operation-centric.v1"

    (tmp_path / "schemas").mkdir()
    with pytest.raises(ValueError, match="forbids directory"):
        validate_ontology_revision(tmp_path)


def test_operation_centric_layout_rejects_nested_operation_markdown(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        validate_solver_package_layout,
    )

    manifest = _write_target_package(tmp_path)
    nested = tmp_path / "operations" / "nested"
    nested.mkdir()
    (nested / "extra.md").write_text(
        "---\nresource: operation\nkey: extra_route\n"
        "execution:\n  mode: solver_workflow\n"
        "  decision_model: route_choice\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="flat"):
        validate_solver_package_layout(tmp_path, manifest)


def test_solver_operations_metadata_merges_with_decision_model_declaration(tmp_path):
    from ontology.solver_package_layout import validate_solver_package_layout

    manifest = _write_target_package(tmp_path)
    model = manifest["capabilities"]["decision_models"][0]
    model.pop("operation_key")
    manifest["capabilities"]["solver_operations"] = [
        {"operation": "solve_route", "decision_model": "route_choice"}
    ]
    (tmp_path / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    result = validate_solver_package_layout(tmp_path, manifest)

    assert result.operations[0].operation_key == "solve_route"


def test_release_boolean_also_blocks_in_place_migration(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "released-bool"
    manifest = _write_legacy_package(source)
    manifest["released"] = True
    (source / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(SolverPackageLayoutError, match="new target revision"):
        migrate_solver_package_layout(source, source, dry_run=False)


def test_cli_accepts_source_option_and_explicit_dry_run(tmp_path, capsys):
    from scripts.migrate_solver_package_layout import main

    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_legacy_package(source)

    assert main(
        [
            "--source",
            str(source),
            "--target",
            str(target),
            "--target-version",
            "2.2.0",
            "--dry-run",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True


def test_cli_rejects_apply_and_dry_run_together(tmp_path):
    from scripts.migrate_solver_package_layout import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([str(tmp_path), "--apply", "--dry-run"])


def test_migration_rejects_operation_key_that_can_escape_operations_directory(tmp_path):
    from ontology.solver_package_layout import (
        SolverPackageLayoutError,
        migrate_solver_package_layout,
    )

    source = tmp_path / "unsafe"
    _write_legacy_package(source)
    operation = source / "operations" / "solve_route.md"
    operation.write_text(
        operation.read_text(encoding="utf-8").replace(
            "key: solve_route", "key: ../escape"
        ),
        encoding="utf-8",
    )

    with pytest.raises(SolverPackageLayoutError, match="key"):
        migrate_solver_package_layout(
            source,
            tmp_path / "target",
            dry_run=True,
            target_version="2.2.0",
        )


def test_migration_cli_defaults_to_dry_run_and_can_apply(tmp_path, capsys):
    from scripts.migrate_solver_package_layout import main

    source = tmp_path / "source"
    target = tmp_path / "target"
    _write_legacy_package(source)

    assert main([str(source), "--target", str(target), "--target-version", "2.2.0"]) == 0
    dry_run_output = json.loads(capsys.readouterr().out)
    assert dry_run_output["dry_run"] is True
    assert not target.exists()

    assert main(
        [
            str(source),
            "--target",
            str(target),
            "--target-version",
            "2.2.0",
            "--apply",
        ]
    ) == 0
    applied_output = json.loads(capsys.readouterr().out)
    assert applied_output["dry_run"] is False
    assert target.is_dir()
