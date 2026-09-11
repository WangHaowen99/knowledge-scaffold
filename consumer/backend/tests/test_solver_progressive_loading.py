"""Progressive loading contract for solver capability discovery."""

from __future__ import annotations

import json


def test_public_solver_summary_is_schema_free(tmp_path):
    from ontology.ontologies import OntologyLoader
    from ontology.ontology_catalog import build_root_payload
    from solver_coordination.capability_catalog import public_solver_summary
    from test_solver_capability_catalog import _write_solver_package

    manifest = _write_solver_package(tmp_path)
    ontology = OntologyLoader.load_ontology(tmp_path)
    assert ontology is not None

    payload = build_root_payload(
        ontology=ontology,
        ontology_dir=tmp_path,
        include_solver_catalog=False,
    )

    summary = payload["solver_summary"]
    expected = public_solver_summary(
        # The schema-free builder is semantically equivalent to projecting a
        # full private record for the common discovery fields.
        __import__(
            "solver_coordination.capability_catalog",
            fromlist=["load_solver_capability_catalog"],
        ).load_solver_capability_catalog(tmp_path, manifest)
    )
    assert summary == expected
    assert summary[0]["operation_key"] == "solve_route"
    assert summary[0]["load_policy"] == "progressive"
    assert summary[0]["next_action"] == {
        "resource": "operation",
        "key": "solve_route",
    }
    assert "input_schema" not in summary[0]
    assert "result_schema" not in summary[0]
    assert "decision_model" not in summary[0]
    assert "contract_digest" not in summary[0]
    assert "endpoint" not in json.dumps(summary, ensure_ascii=False)
    assert "credential" not in json.dumps(summary, ensure_ascii=False)
    assert "runtime_binding" not in json.dumps(summary, ensure_ascii=False)
    assert "solver_catalog" not in payload


def test_private_solver_capability_loader_retains_toolcard_schemas(tmp_path):
    from solver_coordination.capability_catalog import (
        SolverContractResolver,
        load_public_solver_capabilities,
    )
    from test_solver_capability_catalog import _write_solver_package

    manifest = _write_solver_package(tmp_path)
    capabilities = load_public_solver_capabilities(tmp_path, manifest)

    assert len(capabilities) == 1
    assert capabilities[0]["operation_key"] == "solve_route"
    assert capabilities[0]["input_schema"]["required"] == ["origin"]
    assert capabilities[0]["result_schema"]["required"] == ["selected_route"]

    resolved = SolverContractResolver(tmp_path, manifest).load_operation("solve_route")
    assert resolved is not None
    assert resolved["input_schema"]["required"] == ["origin"]
    assert SolverContractResolver(tmp_path, manifest).load_operation("missing") is None


def test_public_root_payload_uses_summary_by_default_for_progressive_context(tmp_path):
    from ontology.ontologies import OntologyLoader
    from ontology.ontology_catalog import build_root_payload
    from test_solver_capability_catalog import _write_solver_package

    _write_solver_package(tmp_path)
    ontology = OntologyLoader.load_ontology(tmp_path)
    assert ontology is not None

    payload = build_root_payload(ontology=ontology, ontology_dir=tmp_path)

    assert payload["solver_summary"][0]["operation_key"] == "solve_route"
    assert "input_schema" not in payload["solver_summary"][0]
    assert "solver_catalog" not in payload


def test_summary_loader_does_not_require_full_schema_loader(monkeypatch, tmp_path):
    from solver_coordination import capability_catalog
    from test_solver_capability_catalog import _write_solver_package

    manifest = _write_solver_package(tmp_path)
    monkeypatch.setattr(
        capability_catalog,
        "load_solver_capability_catalog",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("summary loading must not materialize full contracts")
        ),
    )

    summary = capability_catalog.load_solver_summary_catalog(tmp_path, manifest)

    assert summary[0]["operation_key"] == "solve_route"
    assert "input_schema" not in summary[0]
    assert "result_schema" not in summary[0]


def test_compact_summary_preserves_progressive_next_action(tmp_path):
    from ontology.ontologies import OntologyLoader
    from ontology.ontology_catalog import build_root_payload, format_root_payload_compact
    from test_solver_capability_catalog import _write_solver_package

    _write_solver_package(tmp_path)
    ontology = OntologyLoader.load_ontology(tmp_path)
    assert ontology is not None

    compact = "\n".join(
        format_root_payload_compact(
            ontology_id=ontology.id,
            root_payload=build_root_payload(ontology=ontology, ontology_dir=tmp_path),
        )
    )

    assert "next_action: ontology_context(resource=operation, key=solve_route)" in compact


def test_summary_and_full_catalog_merge_solver_operation_metadata(tmp_path):
    from solver_coordination.capability_catalog import (
        load_public_solver_capabilities,
        load_solver_summary_catalog,
    )
    from test_solver_capability_catalog import _write_solver_package

    manifest = _write_solver_package(tmp_path)
    model = manifest["capabilities"]["decision_models"][0]
    requirements = model.pop("engine_requirements")
    manifest["capabilities"]["solver_operations"] = [
        {
            "operation": "solve_route",
            "decision_model": "route_choice",
            "problem_family": requirements["problem_family"],
            "capabilities": requirements["capabilities"],
            "validator_revision": "route-validator.v2",
        }
    ]

    summary = load_solver_summary_catalog(tmp_path, manifest)
    full = load_public_solver_capabilities(tmp_path, manifest)

    assert summary[0]["problem_family"] == "binary_ordered_route.v1"
    assert summary[0]["validator_revision"] == "route-validator.v2"
    assert full[0]["problem_family"] == summary[0]["problem_family"]
    assert full[0]["engine_capabilities"] == ["candidate_validation"]
