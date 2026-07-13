"""Tests for ontology package import-boundary normalization."""

from __future__ import annotations

import os

import pytest

from ontology.ontology_catalog import find_script_entry, parse_script_catalog
from ontology.package_import import inspect_importable_package, normalize_imported_package


V01_MANIFEST = """\
kind: ontology-package
schema_version: 0.1
id: example-demo
name: Example Demo
description: Legacy package for import tests
version: 1.2.3
language: zh-CN
"""


def _write_v01_package(root):
    (root / "objects").mkdir(parents=True)
    (root / "operations").mkdir()
    (root / "scripts").mkdir()
    (root / "manifest.yaml").write_text(V01_MANIFEST, encoding="utf-8")
    (root / "index.md").write_text(
        "---\nresource: root\n---\n\n# Example domain\n",
        encoding="utf-8",
    )
    (root / "objects" / "index.md").write_text(
        "---\nresource: object_index\n---\n\n| object key | title |\n| --- | --- |\n| Part | Part |\n",
        encoding="utf-8",
    )
    (root / "operations" / "index.md").write_text(
        "---\nresource: operation_index\n---\n\n| operation key | title |\n| --- | --- |\n| QueryPart | Query part |\n",
        encoding="utf-8",
    )
    (root / "scripts" / "query_part.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "scripts" / "query_part.md").write_text(
        """\
---
resource: script
key: query_part
entrypoint: query_part.py
bound_operation: QueryPart
---

| 参数 key | CLI 参数 | 类型 | 必填 | 说明 | 别名 |
| --- | --- | --- | --- | --- | --- |
| part_code | --part-code | string | 是 | Part code | code，part |
""",
        encoding="utf-8",
    )


def test_inspect_importable_package_accepts_v01_without_relaxing_runtime_manifest(tmp_path):
    _write_v01_package(tmp_path)

    package = inspect_importable_package(tmp_path)

    assert package is not None
    assert package.ontology_id == "example-demo"
    assert package.version == "1.2.3"
    assert package.source_format == "v0.1"


@pytest.mark.parametrize("schema_version", (None, "0.2", "9.9"))
def test_inspect_importable_package_rejects_unsupported_v01_schema(tmp_path, schema_version):
    _write_v01_package(tmp_path)
    manifest = V01_MANIFEST
    if schema_version is None:
        manifest = manifest.replace("schema_version: 0.1\n", "")
    else:
        manifest = manifest.replace("schema_version: 0.1", "schema_version: {}".format(schema_version))
    (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")

    assert inspect_importable_package(tmp_path) is None


def test_inspect_importable_package_accepts_v01_without_recommended_language(tmp_path):
    _write_v01_package(tmp_path)
    (tmp_path / "manifest.yaml").write_text(
        V01_MANIFEST.replace("language: zh-CN\n", ""),
        encoding="utf-8",
    )

    package = inspect_importable_package(tmp_path)

    assert package is not None
    assert package.source_format == "v0.1"


def test_normalize_v01_package_generates_current_catalogs_and_script_mapping(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)

    result = normalize_imported_package(source, target)

    assert result.generated_files == (
        "objects-list.md",
        "operations-list.md",
        "ONTOLOGY.md",
        "scripts-list.md",
    )
    assert result.warnings == ()
    assert (target / "ONTOLOGY.md").read_text(encoding="utf-8") == "# Example domain\n"
    assert (target / "objects-list.md").read_text(encoding="utf-8").startswith("| object key")
    assert (target / "operations-list.md").read_text(encoding="utf-8").startswith("| operation key")

    catalog = parse_script_catalog(
        (target / "scripts-list.md").read_text(encoding="utf-8"),
        ontology_dir=target,
    )
    entry = find_script_entry(catalog, "QueryPart")
    assert entry is not None
    assert entry["_script_path"] == "scripts/query_part.py"
    assert entry["parameters"] == [
        {
            "name": "part_code",
            "flag": "--part-code",
            "required": True,
            "type": "string",
            "multiple": False,
            "description": "Part code",
            "aliases": ["code", "part"],
        }
    ]


def test_normalize_current_package_preserves_existing_catalogs(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    source.mkdir()
    (source / "manifest.yaml").write_text(
        V01_MANIFEST.replace("ontology-package", "ontology"),
        encoding="utf-8",
    )
    (source / "objects-list.md").write_text("custom objects\n", encoding="utf-8")
    (source / "operations-list.md").write_text("custom operations\n", encoding="utf-8")
    (source / "scripts-list.md").write_text("custom scripts\n", encoding="utf-8")

    result = normalize_imported_package(source, target)

    assert result.generated_files == ()
    assert (target / "objects-list.md").read_text(encoding="utf-8") == "custom objects\n"
    assert (target / "operations-list.md").read_text(encoding="utf-8") == "custom operations\n"
    assert (target / "scripts-list.md").read_text(encoding="utf-8") == "custom scripts\n"


def test_normalize_rejects_unsafe_script_entrypoint(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    (source / "scripts" / "query_part.md").write_text(
        "---\nbound_operation: QueryPart\nentrypoint: ../secret.py\n---\n",
        encoding="utf-8",
    )

    result = normalize_imported_package(source, target)

    assert not (target / "scripts-list.md").exists()
    assert result.warnings == ("Rejected unsafe script entrypoint in 'query_part.md'",)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_normalize_rejects_symbolic_links(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    (source / "linked.py").symlink_to(source / "scripts" / "query_part.py")

    with pytest.raises(ValueError, match="symbolic links"):
        normalize_imported_package(source, target)
