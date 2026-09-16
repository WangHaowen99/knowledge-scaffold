# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Synthetic coverage for ontology-package v0.2 solver capabilities."""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml
from backend_synthetic_credentials import synthetic_secret
from internal_access import internal


def _write_manifest(
    package_root: Path,
    *,
    # solver-chunk-1
    schema_version: str,
    version: str = "1.0.0",
) -> dict:
    manifest = {
        "kind": "ontology-package",
        "schema_version": schema_version,
        "id": "knowledge-package-demo",
        "name": "Knowledge package demo",
        "description": "Synthetic solver package",
        "version": version,
        "language": "en",
        "capabilities": {
            "decision_models": [
                {
                    "id": "route_choice",
                    "entry": "decision-models/route-choice.yaml",
                    "input_schema": "schemas/solver-input.schema.json",
                    "result_schema": "schemas/solver-result.schema.json",
                    "presentation": "presentations/solver-result.yaml",
                    "engine_requirements": {
                        # solver-chunk-2
                        "problem_family": "binary_ordered_route.v1",
                        "capabilities": ["candidate_validation"],
                    },
                }
            ]
        },
    }
    (package_root / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest


def _write_solver_package(
    package_root: Path,
    *,
    schema_version: str = "0.2",
    version: str = "1.0.0",
) -> dict:
    for directory in ("operations", "decision-models", "schemas", "presentations"):
        # solver-chunk-3
        (package_root / directory).mkdir(parents=True, exist_ok=True)
    manifest = _write_manifest(
        package_root,
        schema_version=schema_version,
        version=version,
    )
    (package_root / "decision-models" / "route-choice.yaml").write_text(
        "kind: decision-model\nid: route_choice\nvariables: [route_a, route_b]\n",
        encoding="utf-8",
    )
    (package_root / "schemas" / "solver-input.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"origin": {"type": "string"}},
                "required": ["origin"],
            }
        ),
        encoding="utf-8",
    )
    # solver-chunk-4
    (package_root / "schemas" / "solver-result.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {"selected_route": {"type": "string"}},
                "required": ["selected_route"],
            }
        ),
        encoding="utf-8",
    )
    (package_root / "presentations" / "solver-result.yaml").write_text("title: Route choice\n", encoding="utf-8")
    (package_root / "operations" / "solve-route.md").write_text(
        """---
resource: operation
key: solve_route
title: Solve route
execution:
  mode: solver_workflow
# solver-chunk-5
  decision_model: route_choice
  validator_revision: route-validator.v1
---
Solve the synthetic route problem.
""",
        encoding="utf-8",
    )
    (package_root / "operations" / "disabled.md").write_text(
        """---
resource: operation
key: disabled_solver
enabled: false
execution:
  mode: solver_workflow
  decision_model: route_choice
---
Disabled.
""",
        encoding="utf-8",
    )
    # solver-chunk-6
    (package_root / "operations" / "malformed.md").write_text(
        "---\nresource: operation\nkey: malformed_solver\n---\nMissing execution.\n",
        encoding="utf-8",
    )
    return manifest


def _import_ontology_routes(monkeypatch):
    langdetect = types.ModuleType("langdetect")

    class DetectorFactory:
        seed = 0

    class LangDetectException(Exception):
        pass

    def detect_language(_text):
        return "en"

    def detect_languages(_text):
        return []

    langdetect.DetectorFactory = DetectorFactory
    langdetect.LangDetectException = LangDetectException
    langdetect.detect = detect_language
    langdetect.detect_langs = detect_languages
    # solver-chunk-7
    monkeypatch.setitem(sys.modules, "langdetect", langdetect)
    return importlib.import_module("api.routes.ontologies")


@pytest.mark.parametrize("schema_version", ["0.1", "0.2"])
def test_runtime_loader_accepts_supported_schema_versions(tmp_path, schema_version):
    from ontology.ontologies import OntologyLoader

    package_root = tmp_path / schema_version.replace(".", "-")
    package_root.mkdir()
    _write_manifest(package_root, schema_version=schema_version)

    ontology = OntologyLoader.load_ontology(package_root)

    assert ontology is not None
    assert ontology.schema_version == schema_version


def test_runtime_loader_rejects_unknown_schema_version(tmp_path):
    from ontology.ontologies import OntologyLoader

    # solver-chunk-8

    package_root = tmp_path / "unknown"
    package_root.mkdir()
    _write_manifest(package_root, schema_version="0.3")

    assert OntologyLoader.load_ontology(package_root) is None


def test_catalog_parses_v02_solver_contract_and_filters_operations(tmp_path):
    from solver_coordination.capability_catalog import (
        load_solver_capability_catalog,
        public_solver_catalog,
    )

    manifest = _write_solver_package(tmp_path)
    records = load_solver_capability_catalog(tmp_path, manifest)

    assert len(records) == 1
    record = records[0]
    assert record.package_id == "knowledge-package-demo"
    # solver-chunk-9
    assert record.package_revision == "1.0.0"
    assert record.operation_key == "solve_route"
    assert record.decision_model_id == "route_choice"
    assert record.problem_family == "binary_ordered_route.v1"
    assert record.validator_revision == "route-validator.v1"
    assert record.enabled is True
    assert record.input_schema["required"] == ("origin",)
    for digest in (
        record.package_digest,
        record.operation_digest,
        record.decision_model_digest,
        record.input_schema_digest,
        record.result_schema_digest,
        record.contract_digest,
    ):
        assert digest.startswith("sha256:solver-coordination.v1:")
    with pytest.raises(TypeError):
        record.input_schema["type"] = "array"
    with pytest.raises(FrozenInstanceError):
        record.operation_key = "changed"
    # solver-chunk-10

    public = public_solver_catalog(records)
    assert [entry["operation_key"] for entry in public] == ["solve_route"]
    serialized = json.dumps(public, ensure_ascii=False)
    assert "decision-models/route-choice.yaml" not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint", "https://service.example.net/solve"),
        ("token", "your-api-key-here"),
        ("secret", synthetic_secret("solver_capability_catalog-credential-01")),
        ("platform", "synthetic-platform"),
    ],
)
def test_catalog_rejects_runtime_binding_fields(tmp_path, field, value):
    from solver_coordination.capability_catalog import (  # solver-chunk-11
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    manifest = _write_solver_package(tmp_path)
    manifest["capabilities"]["decision_models"][0]["runtime_binding"] = {field: value}

    with pytest.raises(SolverCapabilityCatalogError, match="runtime_binding"):
        load_solver_capability_catalog(tmp_path, manifest)


def test_package_digest_ignores_disabled_and_unreferenced_models(tmp_path):
    from solver_coordination.capability_catalog import load_solver_capability_catalog

    manifest = _write_solver_package(tmp_path)
    baseline = load_solver_capability_catalog(tmp_path, manifest)[0].package_digest
    manifest["capabilities"]["decision_models"].extend(
        [
            {
                "id": "disabled_model",
                # solver-chunk-12
                "enabled": False,
                "entry": "../outside.yaml",
                "input_schema": "../outside-input.json",
                "result_schema": "../outside-result.json",
            },
            {
                "id": "unreferenced_model",
                "entry": "missing-model.yaml",
                "input_schema": "missing-input.json",
                "result_schema": "missing-result.json",
                "engine_requirements": {"problem_family": "unused.v1"},
            },
        ]
    )

    unchanged = load_solver_capability_catalog(tmp_path, manifest)[0].package_digest
    assert unchanged == baseline

    (tmp_path / "schemas" / "solver-input.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"ratio": {"minimum": 0.5}}}),
        # solver-chunk-13
        encoding="utf-8",
    )
    changed = load_solver_capability_catalog(tmp_path, manifest)[0].package_digest
    assert changed != baseline


def test_decision_model_digest_uses_canonical_document_identity(tmp_path):
    from solver_coordination.canonical import sha256_digest
    from solver_coordination.capability_catalog import load_solver_capability_catalog

    manifest = _write_solver_package(tmp_path)
    model_path = tmp_path / "decision-models" / "route-choice.yaml"
    baseline = load_solver_capability_catalog(tmp_path, manifest)[0]
    parsed_model = yaml.safe_load(model_path.read_text(encoding="utf-8"))
    assert baseline.decision_model_digest == sha256_digest(parsed_model)

    model_path.write_text(
        """# Formatting and key order must not change document identity.
variables:
  - route_a
# solver-chunk-14
  - route_b
id: route_choice
kind: decision-model
""",
        encoding="utf-8",
    )
    reformatted = load_solver_capability_catalog(tmp_path, manifest)[0]
    assert reformatted.decision_model_digest == baseline.decision_model_digest
    assert reformatted.package_digest == baseline.package_digest
    assert reformatted.contract_digest == baseline.contract_digest

    model_path.write_text(
        "kind: decision-model\nid: route_choice\nvariables: [route_a, route_b, route_c]\n",
        encoding="utf-8",
    )
    changed = load_solver_capability_catalog(tmp_path, manifest)[0]
    assert changed.decision_model_digest != baseline.decision_model_digest
    assert changed.package_digest != baseline.package_digest
    assert changed.contract_digest != baseline.contract_digest


# solver-chunk-15


def test_v01_has_no_solver_capabilities_and_unknown_version_is_rejected(tmp_path):
    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    manifest = _write_solver_package(tmp_path, schema_version="0.1")
    assert load_solver_capability_catalog(tmp_path, manifest) == ()

    manifest["schema_version"] = "0.3"
    with pytest.raises(SolverCapabilityCatalogError, match="schema version"):
        load_solver_capability_catalog(tmp_path, manifest)


def test_catalog_rejects_capability_paths_outside_package(tmp_path):
    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    # solver-chunk-16

    manifest = _write_solver_package(tmp_path)
    manifest["capabilities"]["decision_models"][0]["input_schema"] = "../outside.json"

    with pytest.raises(SolverCapabilityCatalogError, match="contained"):
        load_solver_capability_catalog(tmp_path, manifest)


def test_fact_snapshot_schema_changes_solver_contract_digest(tmp_path):
    from solver_coordination.capability_catalog import load_solver_capability_catalog

    manifest = _write_solver_package(tmp_path)
    fact_path = tmp_path / "schemas" / "solver-fact-snapshot.schema.json"
    fact_path.write_text(
        json.dumps({"type": "object", "properties": {"facts": {"type": "object"}}}),
        encoding="utf-8",
    )
    manifest["capabilities"]["decision_models"][0]["fact_snapshot_schema"] = (
        "schemas/solver-fact-snapshot.schema.json"
    )

    baseline = load_solver_capability_catalog(tmp_path, manifest)[0]
    fact_path.write_text(
        json.dumps({"type": "object", "required": ["facts"]}),
        encoding="utf-8",
    )
    changed = load_solver_capability_catalog(tmp_path, manifest)[0]

    assert changed.contract_digest != baseline.contract_digest
    assert changed.package_digest != baseline.package_digest


def test_root_payload_exposes_public_solver_catalog(tmp_path):
    from ontology.ontologies import OntologyLoader
    from ontology.ontology_catalog import build_root_payload, format_root_payload_compact

    _write_solver_package(tmp_path)
    ontology = OntologyLoader.load_ontology(tmp_path)
    assert ontology is not None

    payload = build_root_payload(
        ontology=ontology,
        ontology_dir=tmp_path,
        include_solver_catalog=True,
    )

    assert payload["solver_catalog"][0]["operation_key"] == "solve_route"
    compact = "\n".join(
        # solver-chunk-17
        format_root_payload_compact(ontology_id="knowledge-package-demo", root_payload=payload)
    )
    assert "solver_catalog:" in compact
    assert "solve_route" in compact


def test_shared_install_manifest_indexes_versions_without_capabilities(tmp_path):
    from ontology.package_import import build_versioned_install_manifest

    source = tmp_path / "source"
    source.mkdir()
    manifest = _write_solver_package(source)

    installed = build_versioned_install_manifest(
        source_manifest=manifest,
        existing_manifest=None,
        ontology_id=manifest["id"],
        ontology_name=manifest["name"],
        # solver-chunk-18
        ontology_description=manifest["description"],
        target_version="1.0.0",
    )
    switched = build_versioned_install_manifest(
        source_manifest={**manifest, "version": "2.0.0"},
        existing_manifest=installed,
        ontology_id=manifest["id"],
        ontology_name=manifest["name"],
        ontology_description=manifest["description"],
        target_version="2.0.0",
    )
    downloaded = yaml.safe_load(yaml.safe_dump(switched, sort_keys=False))

    assert str(downloaded["schema_version"]) == "0.2"
    assert downloaded["active_version"] == "2.0.0"
    assert downloaded["versions"] == ["1.0.0", "2.0.0"]
    assert "capabilities" not in downloaded


def test_switch_uses_selected_revision_manifest_for_schema_and_catalog(tmp_path):
    # solver-chunk-19
    from ontology.ontologies import load_ontology_manifest_data, set_ontology_active_version
    from ontology.ontology_catalog import build_root_payload

    ontology_dir = tmp_path / "mixed"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    version1.mkdir(parents=True)
    version2.mkdir(parents=True)
    v01_manifest = _write_manifest(version1, schema_version="0.1")
    v01_manifest.pop("capabilities")
    (version1 / "manifest.yaml").write_text(yaml.safe_dump(v01_manifest, sort_keys=False), encoding="utf-8")
    v02_manifest = _write_solver_package(version2)
    shared = {
        **v02_manifest,
        "version": "2.0.0",
        "active_version": "2.0.0",
        "versions": ["1.0.0", "2.0.0"],
    }
    # solver-chunk-20
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    selected_v01 = set_ontology_active_version(ontology_dir, "1.0.0")
    assert selected_v01.schema_version == "0.1"
    assert "solver_catalog" not in build_root_payload(
        ontology=selected_v01,
        ontology_dir=version1,
    )
    active_manifest = load_ontology_manifest_data(ontology_dir)
    assert active_manifest["active_version"] == "1.0.0"
    assert "capabilities" not in active_manifest

    selected_v02 = set_ontology_active_version(ontology_dir, "2.0.0")
    assert selected_v02.schema_version == "0.2"
    assert (
        build_root_payload(
            ontology=selected_v02,
            ontology_dir=version2,
            include_solver_catalog=True,
        )["solver_catalog"][0]["operation_key"]
        == "solve_route"
    )
    assert "capabilities" not in load_ontology_manifest_data(ontology_dir)
    # solver-chunk-21
    assert v01_manifest["schema_version"] == "0.1"


def test_real_install_switch_and_download_preserve_revision_manifests(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import set_ontology_active_version
    from ontology.ontology_catalog import build_root_payload

    source1 = tmp_path / "source-v01"
    source1.mkdir()
    v01_manifest = _write_manifest(
        source1,
        schema_version="0.1",
        version="1.0.0",
    )
    v01_manifest.pop("capabilities")
    (source1 / "manifest.yaml").write_text(yaml.safe_dump(v01_manifest, sort_keys=False), encoding="utf-8")
    (source1 / "operations").mkdir()
    # solver-chunk-22
    (source1 / "operations" / "index.md").write_text("# v0.1\n", encoding="utf-8")

    source2 = tmp_path / "source-v02"
    source2.mkdir()
    v02_manifest = _write_solver_package(source2, version="2.0.0")
    ontology_dir = tmp_path / "installed"

    version1, _revision1 = internal(routes).install_ontology_version_package(
        source_root=source1,
        ontology_dir=ontology_dir,
        ontology_id=v01_manifest["id"],
        ontology_name=v01_manifest["name"],
        ontology_description=v01_manifest["description"],
        target_version="1.0.0",
        existed=False,
        workspace_id="default",
    )
    version2, _revision2 = internal(routes).install_ontology_version_package(
        source_root=source2,
        ontology_dir=ontology_dir,
        ontology_id=v02_manifest["id"],
        # solver-chunk-23
        ontology_name=v02_manifest["name"],
        ontology_description=v02_manifest["description"],
        target_version="2.0.0",
        existed=True,
        workspace_id="default",
    )
    assert (version1 / "manifest.yaml").is_file()
    assert (version2 / "manifest.yaml").is_file()
    assert "capabilities" not in yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))

    selected_v01 = set_ontology_active_version(ontology_dir, "1.0.0")
    assert selected_v01.schema_version == "0.1"
    assert Path(selected_v01.manifest_path) == version1 / "manifest.yaml"
    assert "solver_catalog" not in build_root_payload(
        ontology=selected_v01,
        ontology_dir=version1,
    )
    with zipfile.ZipFile(
        internal(routes).build_ontology_zip_buffer(ontology_dir, version1, version="1.0.0")
        # solver-chunk-24
    ) as archive:
        assert archive.namelist().count("installed/manifest.yaml") == 1
        downloaded_v01 = yaml.safe_load(archive.read("installed/manifest.yaml").decode("utf-8"))
    assert str(downloaded_v01["schema_version"]) == "0.1"
    assert "capabilities" not in downloaded_v01

    selected_v02 = set_ontology_active_version(ontology_dir, "2.0.0")
    assert selected_v02.schema_version == "0.2"
    assert Path(selected_v02.manifest_path) == version2 / "manifest.yaml"
    assert (
        build_root_payload(
            ontology=selected_v02,
            ontology_dir=version2,
            include_solver_catalog=True,
        )["solver_catalog"][0]["operation_key"]
        == "solve_route"
    )
    archive_buffer = internal(routes).build_ontology_zip_buffer(
        ontology_dir,
        version2,
        version="2.0.0",
    )
    with zipfile.ZipFile(archive_buffer) as archive:
        assert archive.namelist().count("installed/manifest.yaml") == 1
        downloaded_v02 = yaml.safe_load(
            archive.read("installed/manifest.yaml").decode("utf-8")
            # solver-chunk-25
        )
    assert str(downloaded_v02["schema_version"]) == "0.2"
    assert downloaded_v02["capabilities"] == v02_manifest["capabilities"]


def test_unknown_schema_is_rejected_by_persistent_version_paths(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import (
        ensure_ontology_versions,
        load_ontology_manifest_data,
        set_ontology_active_version,
    )
    from ontology.package_import import build_versioned_install_manifest

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    manifest = _write_manifest(unknown, schema_version="0.3")
    with pytest.raises(ValueError, match="schema version"):
        load_ontology_manifest_data(unknown)
    with pytest.raises(ValueError, match="schema version"):
        # solver-chunk-26
        ensure_ontology_versions(unknown)
    with pytest.raises(ValueError, match="schema version"):
        build_versioned_install_manifest(
            source_manifest=manifest,
            existing_manifest=None,
            ontology_id=manifest["id"],
            ontology_name=manifest["name"],
            ontology_description=manifest["description"],
            target_version="1.0.0",
        )
    with pytest.raises(ValueError, match="schema version"):
        internal(routes).install_ontology_version_package(
            source_root=unknown,
            ontology_dir=tmp_path / "unknown-install",
            ontology_id=manifest["id"],
            ontology_name=manifest["name"],
            ontology_description=manifest["description"],
            target_version="1.0.0",
            existed=False,
            workspace_id="default",
        )
    # solver-chunk-27

    supported = tmp_path / "supported"
    version1 = supported / "versions" / "1.0.0"
    version2 = supported / "versions" / "2.0.0"
    version1.mkdir(parents=True)
    version2.mkdir(parents=True)
    v01 = _write_manifest(version1, schema_version="0.1")
    v01.pop("capabilities")
    (version1 / "manifest.yaml").write_text(yaml.safe_dump(v01, sort_keys=False), encoding="utf-8")
    _write_manifest(version2, schema_version="0.3", version="2.0.0")
    shared = {
        **v01,
        "active_version": "1.0.0",
        "versions": ["1.0.0", "2.0.0"],
    }
    (supported / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")
    # solver-chunk-28
    with pytest.raises(ValueError, match="schema version"):
        set_ontology_active_version(supported, "2.0.0")
    assert load_ontology_manifest_data(supported)["active_version"] == "1.0.0"
    with pytest.raises(ValueError, match="schema version"):
        internal(routes).manifest_for_download(supported, "2.0.0")


def test_ensure_versions_migrates_manifest_into_immutable_revision(tmp_path):
    from ontology.ontologies import ensure_ontology_versions, load_ontology_manifest_data

    ontology_dir = tmp_path / "legacy-layout"
    ontology_dir.mkdir()
    manifest = _write_solver_package(ontology_dir)

    ensure_ontology_versions(ontology_dir)

    version_manifest = yaml.safe_load(
        (ontology_dir / "versions" / "1.0.0" / "manifest.yaml").read_text(encoding="utf-8")
        # solver-chunk-29
    )
    assert str(version_manifest["schema_version"]) == "0.2"
    assert version_manifest["capabilities"] == manifest["capabilities"]
    assert "capabilities" not in load_ontology_manifest_data(ontology_dir)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.update({"endpoint": "https://service.example.net/solve"}),
        lambda manifest: manifest["capabilities"]["decision_models"][0].update({"token": "your-api-key-here"}),
        lambda manifest: manifest["capabilities"].update({"bindings": [{"platform": "synthetic-platform"}]}),
        lambda manifest: manifest.update(
            {"runtime_binding": [synthetic_secret("solver_capability_catalog-credential-01")]}
        ),
    ],
    ids=["top-level-endpoint", "model-token", "list-binding", "list-runtime-binding"],
)
# solver-chunk-30
def test_runtime_binding_data_is_rejected_across_package_paths(tmp_path, monkeypatch, mutate):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import load_ontology_manifest_data
    from ontology.package_import import (
        build_versioned_install_manifest,
        inspect_importable_package,
        normalize_imported_package,
    )
    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    source = tmp_path / "source"
    source.mkdir()
    manifest = _write_solver_package(source)
    mutate(manifest)
    (source / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    # solver-chunk-31

    with pytest.raises(SolverCapabilityCatalogError, match="runtime binding"):
        load_solver_capability_catalog(source, manifest)
    assert inspect_importable_package(source) is None
    with pytest.raises(ValueError, match="ontology package"):
        normalize_imported_package(source, tmp_path / "normalized")
    with pytest.raises(ValueError, match="runtime binding"):
        build_versioned_install_manifest(
            source_manifest=manifest,
            existing_manifest=None,
            ontology_id=manifest["id"],
            ontology_name=manifest["name"],
            ontology_description=manifest["description"],
            target_version="1.0.0",
        )
    with pytest.raises(ValueError, match="runtime binding"):
        load_ontology_manifest_data(source)

    ontology_dir = tmp_path / "download"
    version_dir = ontology_dir / "versions" / "1.0.0"
    # solver-chunk-32
    version_dir.mkdir(parents=True)
    valid_shared = _write_manifest(ontology_dir, schema_version="0.2")
    valid_shared.pop("capabilities")
    valid_shared.update({"active_version": "1.0.0", "versions": ["1.0.0"]})
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(valid_shared, sort_keys=False), encoding="utf-8")
    (version_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime binding"):
        internal(routes).manifest_for_download(ontology_dir, "1.0.0")


def test_versioned_revision_manifest_paths_fail_closed_when_missing_or_symlink(
    tmp_path,
    monkeypatch,
):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import OntologyLoader, get_ontology_version_dir

    # solver-chunk-33

    ontology_dir = tmp_path / "strict-versioned"
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    shared = _write_manifest(ontology_dir, schema_version="0.2")
    shared.pop("capabilities")
    shared.update({"active_version": "1.0.0", "versions": ["1.0.0"]})
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    assert OntologyLoader.load_ontology(ontology_dir) is None
    with pytest.raises(ValueError, match="revision manifest"):
        get_ontology_version_dir(ontology_dir, "1.0.0", create=False)
    with pytest.raises(ValueError, match="revision manifest"):
        internal(routes).manifest_for_download(ontology_dir, "1.0.0")

    version_manifest = version_dir / "manifest.yaml"
    version_manifest.write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")
    original_is_symlink = Path.is_symlink
    # solver-chunk-34

    def _is_version_dir_symlink(path):
        return original_is_symlink(path) or path == version_dir

    monkeypatch.setattr(Path, "is_symlink", _is_version_dir_symlink)
    with pytest.raises(ValueError, match="revision"):
        get_ontology_version_dir(ontology_dir, "1.0.0", create=False)
    assert OntologyLoader.load_ontology(ontology_dir) is None
    with pytest.raises(ValueError, match="revision directory"):
        internal(routes).manifest_for_download(ontology_dir, "1.0.0")


def test_existing_v01_version_dirs_backfill_revision_manifests_before_validation(
    tmp_path,
    monkeypatch,
):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import OntologyLoader, set_ontology_active_version

    ontology_dir = tmp_path / "legacy-versioned"
    # solver-chunk-35
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "1.1.0"
    for version_dir in (version1, version2):
        (version_dir / "operations").mkdir(parents=True)
        (version_dir / "operations" / "index.md").write_text("# Operations\n", encoding="utf-8")

    shared = _write_manifest(ontology_dir, schema_version="0.1")
    shared.pop("capabilities")
    shared.update({"active_version": "1.0.0", "versions": ["1.0.0", "1.1.0"]})
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    assert not (version1 / "manifest.yaml").exists()
    assert not (version2 / "manifest.yaml").exists()

    loaded = OntologyLoader.load_ontology(ontology_dir)
    assert loaded is not None
    # solver-chunk-36
    assert loaded.active_version == "1.0.0"
    version1_manifest = yaml.safe_load((version1 / "manifest.yaml").read_text(encoding="utf-8"))
    assert version1_manifest["version"] == "1.0.0"
    assert "active_version" not in version1_manifest
    assert "versions" not in version1_manifest

    downloaded = yaml.safe_load(internal(routes).manifest_for_download(ontology_dir, "1.1.0"))
    assert downloaded["version"] == "1.1.0"
    assert (version2 / "manifest.yaml").is_file()

    switched = set_ontology_active_version(ontology_dir, "1.1.0")
    assert switched.active_version == "1.1.0"
    version2_manifest = yaml.safe_load((version2 / "manifest.yaml").read_text(encoding="utf-8"))
    assert version2_manifest["version"] == "1.1.0"
    assert "active_version" not in version2_manifest
    assert "versions" not in version2_manifest


# solver-chunk-37


def test_unknown_schema_is_rejected_for_any_kind_before_switch_state_changes(
    tmp_path,
):
    from ontology.ontologies import (
        load_ontology_manifest_data,
        set_ontology_active_version,
    )
    from ontology.package_import import inspect_importable_package

    legacy_unknown = tmp_path / "legacy-unknown"
    legacy_unknown.mkdir()
    legacy_manifest = _write_manifest(legacy_unknown, schema_version="0.3")
    legacy_manifest["kind"] = "ontology"
    (legacy_unknown / "manifest.yaml").write_text(yaml.safe_dump(legacy_manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="schema version"):
        load_ontology_manifest_data(legacy_unknown)
    # solver-chunk-38
    assert inspect_importable_package(legacy_unknown) is None

    ontology_dir = tmp_path / "switch-unknown"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    version1.mkdir(parents=True)
    version2.mkdir(parents=True)
    v01 = _write_manifest(version1, schema_version="0.1")
    v01.pop("capabilities")
    (version1 / "manifest.yaml").write_text(yaml.safe_dump(v01, sort_keys=False), encoding="utf-8")
    invalid_revision = _write_manifest(version2, schema_version="0.3", version="2.0.0")
    invalid_revision["kind"] = "ontology"
    (version2 / "manifest.yaml").write_text(yaml.safe_dump(invalid_revision, sort_keys=False), encoding="utf-8")
    shared = {
        **v01,
        "active_version": "1.0.0",
        # solver-chunk-39
        "versions": ["1.0.0", "2.0.0"],
    }
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="schema version"):
        set_ontology_active_version(ontology_dir, "2.0.0")
    assert load_ontology_manifest_data(ontology_dir)["active_version"] == "1.0.0"


def test_global_enabled_state_wins_over_revision_manifest(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import OntologyLoader, load_ontology_manifest_data
    from ontology.ontology_catalog import build_root_payload

    ontology_dir = tmp_path / "globally-disabled"
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    revision = _write_solver_package(version_dir)
    # solver-chunk-40
    revision["enabled"] = True
    (version_dir / "manifest.yaml").write_text(yaml.safe_dump(revision, sort_keys=False), encoding="utf-8")
    shared = {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": revision["id"],
        "name": revision["name"],
        "description": revision["description"],
        "version": "1.0.0",
        "active_version": "1.0.0",
        "versions": ["1.0.0"],
        "language": "en",
        "enabled": False,
    }
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    # solver-chunk-41
    ontology = OntologyLoader.load_ontology(ontology_dir)
    assert ontology is not None
    assert ontology.enabled is False
    assert "solver_catalog" not in build_root_payload(
        ontology=ontology,
        ontology_dir=version_dir,
    )
    downloaded = yaml.safe_load(internal(routes).manifest_for_download(ontology_dir, "1.0.0"))
    assert downloaded["enabled"] is False
    assert load_ontology_manifest_data(ontology_dir)["enabled"] is False


@pytest.mark.parametrize("artifact", ["input", "result", "presentation"])
def test_runtime_fields_in_public_contract_artifacts_are_rejected(tmp_path, artifact):
    from ontology.package_import import normalize_imported_package
    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    # solver-chunk-42
    manifest = _write_solver_package(tmp_path)
    if artifact == "input":
        (tmp_path / "schemas" / "solver-input.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "x-runtime": {"endpoint": "https://service.example.net/solve"},
                }
            ),
            encoding="utf-8",
        )
    elif artifact == "result":
        (tmp_path / "schemas" / "solver-result.schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "x-runtime": {"secret": synthetic_secret("solver_capability_catalog-credential-01")},
                }
            ),
            encoding="utf-8",
            # solver-chunk-43
        )
    else:
        (tmp_path / "presentations" / "solver-result.yaml").write_text(
            "title: Route choice\nplatform: synthetic-platform\n",
            encoding="utf-8",
        )

    with pytest.raises(SolverCapabilityCatalogError, match="runtime binding"):
        load_solver_capability_catalog(tmp_path, manifest)
    target = tmp_path.parent / "normalized-{}".format(artifact)
    with pytest.raises(ValueError, match="runtime binding"):
        normalize_imported_package(tmp_path, target)
    assert not target.exists()


def test_operation_runtime_field_is_rejected_before_install_persistence(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.package_import import normalize_imported_package

    source = tmp_path / "source"
    # solver-chunk-44
    source.mkdir()
    manifest = _write_solver_package(source)
    (source / "operations" / "solve-route.md").write_text(
        """---
resource: operation
key: solve_route
endpoint: https://service.example.net/solve
execution:
  mode: solver_workflow
  decision_model: route_choice
---
Invalid runtime-bound operation.
""",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized"
    with pytest.raises(ValueError, match="runtime binding"):
        normalize_imported_package(source, normalized)
    assert not normalized.exists()

    # solver-chunk-45
    installed = tmp_path / "installed"
    with pytest.raises(ValueError, match="runtime binding"):
        internal(routes).install_ontology_version_package(
            source_root=source,
            ontology_dir=installed,
            ontology_id=manifest["id"],
            ontology_name=manifest["name"],
            ontology_description=manifest["description"],
            target_version="1.0.0",
            existed=False,
            workspace_id="default",
        )
    assert not installed.exists()


def test_operation_runtime_field_is_rejected_by_load_and_download(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)
    from ontology.ontologies import OntologyLoader

    ontology_dir = tmp_path / "installed"
    version_dir = ontology_dir / "versions" / "1.0.0"
    # solver-chunk-46
    version_dir.mkdir(parents=True)
    revision = _write_solver_package(version_dir)
    (version_dir / "operations" / "solve-route.md").write_text(
        """---
resource: operation
key: solve_route
endpoint: https://service.example.net/solve
execution:
  mode: solver_workflow
  decision_model: route_choice
---
Invalid runtime-bound operation.
""",
        encoding="utf-8",
    )
    shared = {key: value for key, value in revision.items() if key != "capabilities"}
    # solver-chunk-47
    shared.update({"active_version": "1.0.0", "versions": ["1.0.0"]})
    (ontology_dir / "manifest.yaml").write_text(yaml.safe_dump(shared, sort_keys=False), encoding="utf-8")

    assert OntologyLoader.load_ontology(ontology_dir) is None
    with pytest.raises(ValueError, match="runtime binding"):
        internal(routes).manifest_for_download(ontology_dir, "1.0.0")


def test_status_mutation_validates_before_write_and_uses_atomic_replace(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    unknown_manifest = _write_manifest(unknown, schema_version="0.3")
    unknown_before = (unknown / "manifest.yaml").read_bytes()
    with pytest.raises(ValueError, match="schema version"):
        internal(routes).set_ontology_enabled(unknown, False)
    assert (unknown / "manifest.yaml").read_bytes() == unknown_before
    # solver-chunk-48

    symlinked = tmp_path / "symlinked"
    symlinked.mkdir()
    _write_manifest(symlinked, schema_version="0.1")
    symlink_manifest = symlinked / "manifest.yaml"
    symlink_before = symlink_manifest.read_bytes()
    original_is_symlink = Path.is_symlink
    with monkeypatch.context() as patcher:
        patcher.setattr(
            Path,
            "is_symlink",
            lambda path: original_is_symlink(path) or path == symlink_manifest,
        )
        with pytest.raises(ValueError, match="manifest"):
            internal(routes).set_ontology_enabled(symlinked, False)
    assert symlink_manifest.read_bytes() == symlink_before

    valid = tmp_path / "valid"
    valid.mkdir()
    _write_manifest(valid, schema_version="0.1")
    # solver-chunk-49
    replace_calls = []
    original_replace = os.replace

    def _replace(source, target):
        replace_calls.append((Path(source), Path(target)))
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", _replace)
    ontology = internal(routes).set_ontology_enabled(valid, False)
    assert ontology.enabled is False
    assert replace_calls
    assert replace_calls[-1][1] == valid / "manifest.yaml"


@pytest.mark.parametrize(
    "relative_path",
    [Path("operations/solve-route.md"), Path("operations/uploaded.md")],
)
def test_operation_writes_validate_a_staged_revision_before_mutating_live_files(
    tmp_path,
    # solver-chunk-50
    monkeypatch,
    relative_path,
):
    routes = _import_ontology_routes(monkeypatch)
    version_dir = tmp_path / "revision"
    version_dir.mkdir()
    _write_solver_package(version_dir)
    live_path = version_dir / relative_path
    original = live_path.read_bytes() if live_path.exists() else None
    invalid_content = """---
resource: operation
key: unsafe_operation
endpoint: https://runtime.example.net/solve
execution:
  mode: solver_workflow
  decision_model: route_choice
---
Must not reach the live revision.
"""

    # solver-chunk-51
    with pytest.raises(ValueError, match="runtime binding"):
        internal(routes).stage_and_commit_revision_file(
            version_dir,
            relative_path,
            invalid_content,
        )

    if original is None:
        assert not live_path.exists()
    else:
        assert live_path.read_bytes() == original


def test_nested_operation_markdown_is_rejected_consistently(tmp_path):
    from ontology.package_import import normalize_imported_package
    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    # solver-chunk-52
    manifest = _write_solver_package(tmp_path)
    nested = tmp_path / "operations" / "nested"
    nested.mkdir()
    (nested / "extra.md").write_text(
        "---\nresource: operation\nkey: extra\n---\nNested operation.\n",
        encoding="utf-8",
    )

    with pytest.raises(SolverCapabilityCatalogError, match="flat"):
        load_solver_capability_catalog(tmp_path, manifest)
    target = tmp_path.parent / "normalized-nested"
    with pytest.raises(ValueError, match="flat"):
        normalize_imported_package(tmp_path, target)
    assert not target.exists()


def test_import_root_discovery_rejects_invalid_v02_revision_early(tmp_path, monkeypatch):
    routes = _import_ontology_routes(monkeypatch)

    extract_root = tmp_path / "extracted"
    # solver-chunk-53
    package_root = extract_root / "package"
    package_root.mkdir(parents=True)
    _write_solver_package(package_root)
    (package_root / "operations" / "solve-route.md").write_text(
        """---
resource: operation
key: solve_route
endpoint: https://service.example.net/solve
execution:
  mode: solver_workflow
  decision_model: route_choice
---
Invalid import revision.
""",
        encoding="utf-8",
    )

    with pytest.raises(routes.OntologyImportError, match="runtime binding"):
        internal(routes).find_converted_ontology_root(extract_root)


# solver-chunk-54
