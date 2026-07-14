"""Tests for the v0.1-only ontology import boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from ontology.ontology_catalog import find_script_entry, load_script_catalog
from ontology.package_import import inspect_importable_package, normalize_imported_package


V01_MANIFEST = """\
kind: ontology-package
schema_version: 0.1
id: example-demo
name: Example Demo
description: Package for import tests
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


def _write_legacy_package(root):
    (root / "objects").mkdir(parents=True)
    (root / "operations").mkdir()
    (root / "scripts").mkdir()
    (root / "manifest.yaml").write_text(
        V01_MANIFEST.replace("kind: ontology-package\nschema_version: 0.1\n", "kind: ontology\n"),
        encoding="utf-8",
    )
    (root / "ONTOLOGY.md").write_text("# Legacy Example\n", encoding="utf-8")
    (root / "objects-list.md").write_text("| key | title |\n| --- | --- |\n| Part | Part |\n", encoding="utf-8")
    (root / "operations-list.md").write_text(
        "| key | title |\n| --- | --- |\n| QueryPart | Query part |\n",
        encoding="utf-8",
    )
    (root / "scripts" / "query_part.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "scripts-list.md").write_text(
        "- `QueryPart` -> `scripts/query_part.py`\n"
        "  `parameters`: `[{\"name\":\"part_code\",\"flag\":\"--part-code\",\"required\":true,\"type\":\"string\"}]`\n",
        encoding="utf-8",
    )


def test_inspect_importable_package_accepts_v01(tmp_path):
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


def test_normalize_v01_keeps_only_new_layout_and_completes_required_indexes(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    (source / "objects-list.md").write_text("must disappear\n", encoding="utf-8")

    result = normalize_imported_package(source, target)

    assert result.warnings == ()
    assert not list(target.glob("*-list.md"))
    assert not (target / "ONTOLOGY.md").exists()
    for relative in (
        "index.md",
        "objects/index.md",
        "operations/index.md",
        "rules/index.md",
        "metrics/index.md",
        "scripts/index.md",
        "mappings/object-data-mappings.json",
    ):
        assert (target / relative).is_file()

    entry = find_script_entry(load_script_catalog(target), "QueryPart")
    assert entry is not None
    assert entry["_script_path"] == "scripts/query_part.py"
    assert entry["parameters"][0]["name"] == "part_code"


def test_normalize_legacy_package_converts_lists_and_script_mapping_once(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_legacy_package(source)

    result = normalize_imported_package(source, target)

    assert result.warnings == ()
    manifest = yaml.safe_load((target / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["kind"] == "ontology-package"
    assert str(manifest["schema_version"]) == "0.1"
    assert "Legacy Example" in (target / "index.md").read_text(encoding="utf-8")
    assert "QueryPart" in (target / "operations" / "index.md").read_text(encoding="utf-8")
    assert not list(target.glob("*-list.md"))
    assert not (target / "ONTOLOGY.md").exists()

    entry = find_script_entry(load_script_catalog(target), "QueryPart")
    assert entry is not None
    assert entry["_script_path"] == "scripts/query_part.py"
    assert entry["parameters"] == [
        {
            "name": "part_code",
            "flag": "--part-code",
            "required": True,
            "type": "string",
            "multiple": False,
        }
    ]


@pytest.mark.skipif(
    not Path("/var").is_symlink() or not Path("/private/var").is_dir(),
    reason="requires the macOS /var -> /private/var alias",
)
def test_normalize_legacy_package_handles_macos_var_alias(tmp_path):
    source = tmp_path / "source"
    _write_legacy_package(source)

    try:
        relative_tmp_path = tmp_path.resolve().relative_to(Path("/private/var"))
    except ValueError:
        pytest.skip("pytest temporary directory is outside /private/var")
    target = Path("/var") / relative_tmp_path / "normalized"

    result = normalize_imported_package(source, target)

    assert result.warnings == ()
    entry = find_script_entry(load_script_catalog(target), "QueryPart")
    assert entry is not None
    assert entry["_script_path"] == "scripts/query_part.py"


def test_normalize_legacy_package_warns_for_unsafe_script_path(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_legacy_package(source)
    (source / "scripts-list.md").write_text(
        "- `QueryPart` -> `../secret.py`\n",
        encoding="utf-8",
    )

    result = normalize_imported_package(source, target)

    assert result.warnings == ("Skipped unsafe or missing legacy script: ../secret.py",)
    assert not (target / "scripts" / "query_part.md").exists()
    assert (target / "scripts" / "index.md").is_file()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_normalize_rejects_symbolic_links(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    (source / "linked.py").symlink_to(source / "scripts" / "query_part.py")
    with pytest.raises(ValueError, match="symbolic links"):
        normalize_imported_package(source, target)
