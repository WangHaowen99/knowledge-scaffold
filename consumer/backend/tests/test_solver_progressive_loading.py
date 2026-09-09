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
    assert summary == public_solver_summary(
        # The public builder is expected to obtain the same projection from the
        # package while keeping full schemas out of the root context.
        __import__(
            "solver_coordination.capability_catalog",
            fromlist=["load_solver_capability_catalog"],
        ).load_solver_capability_catalog(tmp_path, manifest)
    )
    assert summary[0]["operation_key"] == "solve_route"
    assert summary[0]["load_policy"] == "progressive"
    assert summary[0]["next_action"] == {
        "resource": "operation",
        "key": "solve_route",
    }
    assert "input_schema" not in summary[0]
    assert "result_schema" not in summary[0]
    assert "decision_model" not in summary[0]
    assert "endpoint" not in json.dumps(summary, ensure_ascii=False)
    assert "credential" not in json.dumps(summary, ensure_ascii=False)
    assert "runtime_binding" not in json.dumps(summary, ensure_ascii=False)
    assert "solver_catalog" not in payload


def test_private_solver_capability_loader_retains_toolcard_schemas(tmp_path):
    from solver_coordination.capability_catalog import (
        load_public_solver_capabilities,
    )
    from test_solver_capability_catalog import _write_solver_package

    manifest = _write_solver_package(tmp_path)
    capabilities = load_public_solver_capabilities(tmp_path, manifest)

    assert len(capabilities) == 1
    assert capabilities[0]["operation_key"] == "solve_route"
    assert capabilities[0]["input_schema"]["required"] == ["origin"]
    assert capabilities[0]["result_schema"]["required"] == ["selected_route"]


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
