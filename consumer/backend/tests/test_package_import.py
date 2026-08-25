"""Tests for the v0.1-only ontology import boundary."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from ontology.ontology_catalog import (
    ObjectDataMappingStatus,
    find_field_alias_matches,
    find_exact_object_data_mapping_result,
    find_script_entry,
    load_script_catalog,
    resolve_field_reference,
    resolve_object_reference,
    split_field_aliases,
)
from ontology.package_import import (
    inspect_importable_package,
    normalize_imported_package,
    validate_imported_field_aliases,
    validate_imported_object_aliases,
)


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


def _write_object_definition(
    root,
    filename,
    *,
    key,
    title="",
    canonical_key="",
    aliases=(),
):
    objects_dir = root / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "resource": "object",
        "key": key,
        "title": title or key,
    }
    if canonical_key:
        frontmatter["canonical_key"] = canonical_key
    if aliases:
        frontmatter["aliases"] = list(aliases)
    path = objects_dir / filename
    path.write_text(
        "---\n{}---\n\n# {}\n".format(
            yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False),
            title or key,
        ),
        encoding="utf-8",
    )
    return path


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


@pytest.mark.parametrize("missing", ("index.md", "objects/index.md", "operations/index.md"))
def test_inspect_importable_package_rejects_missing_required_index(tmp_path, missing):
    _write_v01_package(tmp_path)
    (tmp_path / missing).unlink()
    assert inspect_importable_package(tmp_path) is None


def test_script_catalog_keeps_parameter_when_description_contains_union_pipe(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "monthly.py").write_text("print('ok')\n", encoding="utf-8")
    (scripts / "monthly.md").write_text(
        """---
resource: script
key: monthly
title: 月度排产
bound_operation: 月度排产
entrypoint: monthly.py
---

# 输入参数

| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| batchPlans | --batchPlans | string | 是 |  | JSON 数组，去向为 '辅助生产线A'|'辅助生产线B' |
| productYieldData | --productYieldData | string | 是 |  | JSON 收率数据 |
""",
        encoding="utf-8",
    )

    entry = find_script_entry(load_script_catalog(tmp_path), "月度排产")

    assert entry is not None
    assert [item["name"] for item in entry["parameters"]] == [
        "batchPlans",
        "productYieldData",
    ]
    assert entry["parameters"][0]["description"].endswith("'辅助生产线A' | '辅助生产线B'")


def test_normalize_v01_preserves_legacy_artifacts_and_completes_required_indexes(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    (source / "objects-list.md").write_text("must disappear\n", encoding="utf-8")

    result = normalize_imported_package(source, target)

    assert result.warnings == ()
    # Verify source files were copied
    assert (target / "manifest.yaml").is_file()
    assert (target / "index.md").is_file()
    assert (target / "objects" / "index.md").is_file()
    assert (target / "operations" / "index.md").is_file()
    # Legacy artifacts are preserved by default (no conversion)
    assert (target / "objects-list.md").is_file()

    entry = find_script_entry(load_script_catalog(target), "QueryPart")
    assert entry is not None
    assert entry["_script_path"] == "scripts/query_part.py"
    assert entry["parameters"][0]["name"] == "part_code"


def test_normalize_rejects_legacy_package(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_legacy_package(source)

    with pytest.raises(ValueError, match="Unsupported or invalid ontology package"):
        normalize_imported_package(source, target)


@pytest.mark.parametrize("key_header", ("object key", "object_key", "key", "中文名称"))
def test_object_alias_resolution_supports_catalog_key_headers(tmp_path, key_header):
    (tmp_path / "objects").mkdir()
    (tmp_path / "objects" / "index.md").write_text(
        "| {} | title | aliases |\n"
        "| --- | --- | --- |\n"
        "| 正式对象 | Catalog title | RouteAlias/SourceItem/TargetItem |\n".format(key_header),
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "formal_object.md",
        key="正式对象",
        title="详情标题",
        aliases=("RouteAlias", "SourceItem", "TargetItem"),
    )

    resolution, error = resolve_object_reference(tmp_path, "routealias")

    assert error is None
    assert resolution is not None
    assert resolution.object_key == "正式对象"
    assert resolution.matched_by == "alias"
    assert resolution.path.name == "formal_object.md"

    for slash_delimited_alias in ("SourceItem", "TargetItem"):
        slash_resolution, slash_error = resolve_object_reference(
            tmp_path,
            slash_delimited_alias,
        )
        assert slash_error is None
        assert slash_resolution is not None
        assert slash_resolution.object_key == "正式对象"


def test_object_reference_uses_fixed_priority_and_reports_ambiguity(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | title | alias |\n"
        "| --- | --- | --- |\n"
        "| Primary | Primary | Shared |\n"
        "| Shared | Shared object | Other |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "primary_object.md",
        key="Primary",
        canonical_key="primary_object",
        aliases=("Shared", "Duplicated", "OtherCanonical", "OtherTitle"),
    )
    _write_object_definition(
        tmp_path,
        "shared_object.md",
        key="Shared",
        canonical_key="othercanonical",
        title="OtherTitle",
        aliases=("Other", "Duplicated"),
    )

    priority_resolution, priority_error = resolve_object_reference(tmp_path, "shared")
    assert priority_error is None
    assert priority_resolution is not None
    assert priority_resolution.object_key == "Shared"
    assert priority_resolution.matched_by == "key"

    ambiguous_resolution, ambiguous_error = resolve_object_reference(tmp_path, "Duplicated")
    assert ambiguous_resolution is None
    assert ambiguous_error is not None
    assert "ambiguous" in ambiguous_error
    assert "Primary" in ambiguous_error
    assert "Shared" in ambiguous_error

    validation = validate_imported_object_aliases(tmp_path)
    assert any("conflicts with key of object 'Shared'" in error for error in validation.errors)
    assert any("conflicts with canonical_key of object 'Shared'" in error for error in validation.errors)
    assert any("conflicts with title of object 'Shared'" in error for error in validation.errors)
    assert any("points to multiple objects: Primary, Shared" in error for error in validation.errors)


def test_object_alias_normalizes_unicode_and_keeps_optional_canonical_key(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    decomposed_alias = "Cafe\u0301"
    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| --- | --- |\n"
        "| 咖啡 | {} |\n".format(decomposed_alias),
        encoding="utf-8",
    )
    _write_object_definition(tmp_path, "coffee.md", key="咖啡", aliases=(decomposed_alias,))

    resolution, error = resolve_object_reference(tmp_path, "Café")

    assert error is None
    assert resolution is not None
    assert resolution.object_key == "咖啡"
    assert resolution.matched_by == "alias"


def test_object_alias_validation_compares_normalized_sets_and_aggregates_errors(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| --- | --- |\n"
        "| A | SECOND；First |\n"
        "| B | Shared |\n",
        encoding="utf-8",
    )
    _write_object_definition(tmp_path, "a.md", key="A", aliases=("first", "second"))
    _write_object_definition(tmp_path, "b.md", key="B", aliases=("Shared",))

    consistent = validate_imported_object_aliases(tmp_path)
    assert consistent.errors == ()
    assert consistent.warnings == ()

    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| --- | --- |\n"
        "| A | Other、Shared |\n"
        "| B | Shared |\n"
        "| Missing | Ghost |\n",
        encoding="utf-8",
    )

    invalid = validate_imported_object_aliases(tmp_path)
    assert any("Object catalog key 'Missing' has no matching detail" in error for error in invalid.errors)
    assert any("Object alias 'shared' points to multiple objects: A, B" in error for error in invalid.errors)
    assert any(
        "对象 key='A'、title='A' 别名不一致：objects/index.md = [Other, Shared]；"
        "a.md = [first, second]" == error
        for error in invalid.errors
    )
    assert invalid.warnings == ()


def test_object_alias_validation_splits_slashes_and_warns_for_empty_catalog_values(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| --- | --- |\n"
        "| WithAliases | First//Second |\n"
        "| BlankAliases | |\n"
        "| NoAliases | - |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "with_aliases.md",
        key="WithAliases",
        aliases=("First", "Second"),
    )
    _write_object_definition(tmp_path, "blank_aliases.md", key="BlankAliases")
    _write_object_definition(tmp_path, "no_aliases.md", key="NoAliases")

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == (
        "Object 'BlankAliases' catalog aliases contain an empty value.",
        "Object 'WithAliases' catalog aliases contain an empty value.",
    )


def test_object_alias_validation_splits_delimiters_inside_yaml_list_items(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| :--- | ---: |\n"
        "| WithAliases | SourceItem/TargetItem |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "with_aliases.md",
        key="WithAliases",
        aliases=("SourceItem/TargetItem",),
    )

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    for alias in ("SourceItem", "TargetItem"):
        resolution, error = resolve_object_reference(tmp_path, alias)
        assert error is None
        assert resolution is not None
        assert resolution.object_key == "WithAliases"


def test_object_alias_validation_reports_normalized_duplicates_once(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | 别名 |\n"
        "| --- | --- |\n"
        "| WithDuplicates | Alpha/ALPHA/Beta/alpha |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "with_duplicates.md",
        key="WithDuplicates",
        aliases=("Alpha", "ALPHA", "Beta", "alpha"),
    )

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == (
        "Object 'WithDuplicates' catalog aliases contain duplicate 'alpha'.",
        "Object 'WithDuplicates' detail aliases contain duplicate 'alpha'.",
    )


def test_object_alias_validation_allows_legacy_catalog_without_alias_column(tmp_path):
    _write_v01_package(tmp_path)
    _write_object_definition(tmp_path, "Part.md", key="Part", title="Part")

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()


def test_object_alias_unknown_key_header_warns_and_uses_detail_fallback(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| unsupported | alias |\n"
        "| --- | --- |\n"
        "| WrongCatalogValue | DetailAlias |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "actual.md",
        key="ActualKey",
        aliases=("DetailAlias",),
    )

    resolution, error = resolve_object_reference(tmp_path, "DetailAlias")
    validation = validate_imported_object_aliases(tmp_path)

    assert error is None
    assert resolution is not None
    assert resolution.object_key == "ActualKey"
    assert validation.errors == ()
    assert any("no supported key column" in warning for warning in validation.warnings)


def test_object_catalog_invalid_utf8_returns_controlled_errors(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_bytes(b"\xff\xfe")
    _write_object_definition(tmp_path, "actual.md", key="ActualKey")

    validation = validate_imported_object_aliases(tmp_path)
    resolution, resolution_error = resolve_object_reference(tmp_path, "ActualKey")

    assert validation.errors == ("Failed to decode objects/index.md as UTF-8.",)
    assert validation.warnings == ()
    assert resolution is None
    assert resolution_error == "Error: Failed to decode objects/index.md as UTF-8."


def test_invalid_utf8_object_mapping_returns_structured_error_and_fails_import(
    tmp_path,
):
    _write_v01_package(tmp_path)
    _write_object_definition(tmp_path, "Part.md", key="Part", title="Part")
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir()
    missing_result = find_exact_object_data_mapping_result(tmp_path, "Part")
    (mappings_dir / "object-data-mappings.json").write_text(
        '{"schema_version":"0.1","objects":{}}',
        encoding="utf-8",
    )
    not_found_result = find_exact_object_data_mapping_result(tmp_path, "Part")
    (mappings_dir / "object-data-mappings.json").write_bytes(b"\xff\xfe")

    mapping_result = find_exact_object_data_mapping_result(tmp_path, "Part")
    validation = validate_imported_object_aliases(tmp_path)

    assert missing_result.status == ObjectDataMappingStatus.FILE_MISSING
    assert not_found_result.status == ObjectDataMappingStatus.NOT_FOUND
    assert mapping_result.status == ObjectDataMappingStatus.INVALID
    assert mapping_result.mapping is None
    assert mapping_result.error == (
        "Error: Failed to decode mappings/object-data-mappings.json as UTF-8."
    )
    assert validation.errors == (
        "Failed to decode mappings/object-data-mappings.json as UTF-8.",
    )


def test_object_alias_validation_rejects_catalog_key_detail_key_mismatch(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | alias |\n"
        "| --- | --- |\n"
        "| ExpectedKey | Alias |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "ExpectedKey.md",
        key="ActualKey",
        aliases=("Alias",),
    )

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == (
        "Object catalog key 'ExpectedKey' does not exactly match detail key 'ActualKey'.",
    )


def test_object_alias_validation_rejects_invalid_canonical_key(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key |\n| --- |\n| ActualKey |\n",
        encoding="utf-8",
    )
    _write_object_definition(
        tmp_path,
        "actual.md",
        key="ActualKey",
        canonical_key="非ASCII key",
    )

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == (
        "Object 'ActualKey' canonical_key '非ASCII key' must contain only ASCII letters, "
        "digits, underscores, or hyphens.",
    )


def test_object_reference_uses_versioned_in_memory_index(tmp_path, monkeypatch):
    from ontology import ontology_catalog as catalog_module

    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | alias |\n| --- | --- |\n| ObjectA | AliasA |\n",
        encoding="utf-8",
    )
    detail_path = _write_object_definition(
        tmp_path,
        "object_a.md",
        key="ObjectA",
        aliases=("AliasA",),
    )
    original_loader = catalog_module._load_object_definitions
    load_count = 0

    def counting_loader(path):
        nonlocal load_count
        load_count += 1
        return original_loader(path)

    monkeypatch.setattr(catalog_module, "_load_object_definitions", counting_loader)

    first, first_error = resolve_object_reference(tmp_path, "AliasA")
    second, second_error = resolve_object_reference(tmp_path, "AliasA")

    assert first_error is None
    assert second_error is None
    assert first is not None and second is not None
    assert load_count == 1

    detail_path.write_text(
        detail_path.read_text(encoding="utf-8") + "\nchanged\n",
        encoding="utf-8",
    )
    refreshed, refreshed_error = resolve_object_reference(tmp_path, "AliasA")
    assert refreshed_error is None
    assert refreshed is not None
    assert load_count == 2


def test_object_reference_does_not_cache_transient_detail_read_error(
    tmp_path,
    monkeypatch,
):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key | alias |\n| --- | --- |\n| ObjectA | AliasA |\n",
        encoding="utf-8",
    )
    detail_path = _write_object_definition(
        tmp_path,
        "object_a.md",
        key="ObjectA",
        aliases=("AliasA",),
    )
    original_read_text = Path.read_text
    failed_once = False

    def flaky_read_text(path, *args, **kwargs):
        nonlocal failed_once
        if path == detail_path and not failed_once:
            failed_once = True
            raise OSError("temporarily unavailable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    first, first_error = resolve_object_reference(tmp_path, "AliasA")
    second, second_error = resolve_object_reference(tmp_path, "AliasA")

    assert first is None
    assert first_error == "Error: Failed to read object detail 'object_a.md'."
    assert second_error is None
    assert second is not None
    assert second.object_key == "ObjectA"
    assert second.matched_by == "alias"


def test_object_alias_validation_groups_shared_canonical_conflicts(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_text(
        "| object key |\n| --- |\n| A |\n| B |\n| SharedCanonical |\n",
        encoding="utf-8",
    )
    _write_object_definition(tmp_path, "a.md", key="A", canonical_key="SharedCanonical")
    _write_object_definition(tmp_path, "b.md", key="B", canonical_key="SharedCanonical")
    _write_object_definition(tmp_path, "c.md", key="SharedCanonical")

    validation = validate_imported_object_aliases(tmp_path)

    assert validation.errors == (
        "Canonical object key 'sharedcanonical' is shared by: A, B.",
        "Canonical object key 'sharedcanonical' shared by A, B conflicts with key of object 'SharedCanonical'.",
    )


def test_field_alias_resolution_supports_standard_and_legacy_headers(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    standard_path = objects_dir / "affected.md"
    standard_path.write_text(
        "---\nresource: object\nkey: Affected\ntitle: 受影响对象\n---\n\n"
        "# 关系\n\n"
        "| 关系 | 目标对象 |\n| --- | --- |\n| belongs_to | CHANGE_ORDER |\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 类型 | 别名 |\n"
        "| --- | --- | --- | --- |\n"
        "| CHANGE_ORDER_ID | CHANGE_ORDER编码 | string | 变更单号；CHANGE_ORDER编号 |\n"
        "| MaterialType | 物料变更类型 | string | 物料类型/SourceItem |\n",
        encoding="utf-8",
    )
    legacy_path = objects_dir / "legacy.md"
    legacy_path.write_text(
        "# 实体属性清单\n\n"
        "| 中文名称 | 英文名称 | 描述 | 属性别名 |\n"
        "| --- | --- | --- | --- |\n"
        "| 受影响编码 | AffectedObjectCode | 编码 | 影响编码 |\n",
        encoding="utf-8",
    )

    title_resolution, title_error = resolve_field_reference(
        standard_path,
        "CHANGE_ORDER编码",
    )
    key_resolution, key_error = resolve_field_reference(
        standard_path,
        "change_order_id",
    )
    alias_resolution, alias_error = resolve_field_reference(
        standard_path,
        "变更单号",
    )
    slash_resolution, slash_error = resolve_field_reference(
        standard_path,
        "物料类型/SourceItem",
    )
    legacy_resolution, legacy_error = resolve_field_reference(
        legacy_path,
        "影响编码",
    )

    assert title_error is None
    assert title_resolution is not None
    assert title_resolution.field_key == "CHANGE_ORDER_ID"
    assert title_resolution.matched_by == "title"
    assert key_error is None
    assert key_resolution is not None
    assert key_resolution.field_key == "CHANGE_ORDER_ID"
    assert key_resolution.matched_by == "key"
    assert alias_error is None
    assert alias_resolution is not None
    assert alias_resolution.field_key == "CHANGE_ORDER_ID"
    assert slash_error is None
    assert slash_resolution is not None
    assert slash_resolution.field_key == "MaterialType"
    assert legacy_error is None
    assert legacy_resolution is not None
    assert legacy_resolution.field_key == "AffectedObjectCode"


def test_field_alias_parser_prefers_explicit_field_table_over_generic_table(tmp_path):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "| key | name |\n"
        "| --- | --- |\n"
        "| metadata | 示例元数据 |\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ActualCode | 正式编码 | 业务编号 |\n",
        encoding="utf-8",
    )

    resolution, error = resolve_field_reference(object_path, "业务编号")

    assert error is None
    assert resolution is not None
    assert resolution.field_key == "ActualCode"
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )


def test_field_alias_validation_uses_explicit_field_table_after_generic_table(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "object.md").write_text(
        "| key | name |\n"
        "| --- | --- |\n"
        "| metadata | 示例元数据 |\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| FieldA | 字段甲 | 编号 |\n"
        "| FieldB | 字段乙 | 编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == (
        "对象文件“objects/object.md”中的对象“object”的字段名称“编号”同时指向 "
        "[FieldA, FieldB]；原始声明：[字段“FieldA”的别名原值“编号”，字段“FieldB”的别名原值“编号”]",
    )


def test_field_alias_parser_reads_only_recognized_field_section(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "# 示例\n\n"
        "说明：`这里的内容不需要按 Markdown 语法解释。\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ExampleA | 示例字段甲 | 示例编号 |\n"
        "\n# 已废弃字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| OldCode | 旧字段 | 旧编号 |\n\n"
        "# 字段\n\n"
        "    | 字段 key | 中文名 | 别名 |\n"
        "    | --- | --- | --- |\n"
        "    | IndentedCode | 缩进示例 | 缩进编号 |\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ActualCode | 正式编码 | 业务编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )
    assert find_field_alias_matches(
        object_path,
        "查询示例编号、旧编号和缩进编号",
    ) == ()


def test_field_alias_parser_ignores_fenced_field_heading(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "```markdown\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ExampleCode | 示例编码 | 示例编号 |\n"
        "```\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ActualCode | 正式编码 | 业务编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )
    assert find_field_alias_matches(object_path, "查询示例编号") == ()


def test_field_alias_parser_ignores_standalone_html_comment_blocks(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "<!--\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| OldCodeA | 旧编码甲 | 编号 |\n"
        "| OldCodeB | 旧编码乙 | 编号 |\n"
        "-->\n\n"
        "说明：`<!--` 是正文中的字面量。\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ActualCode | 正式编码 | 业务编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )
    assert find_field_alias_matches(object_path, "查询编号") == ()


def test_field_alias_parser_requires_recognized_field_section(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "# 示例\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ExampleCode | 示例编码 | 示例编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询示例编号") == ()


def test_field_alias_parser_supports_display_name_english_name_table(tmp_path):
    object_path = tmp_path / "product.md"
    object_path.write_text(
        "# 字段定义\n\n"
        "| 显示名称 | 英文名称 | 描述 |\n"
        "| --- | --- | --- |\n"
        "| 产品编号 | productid | 产品唯一编号 |\n"
        "| 产品名称 | productname | 产品名称 |\n",
        encoding="utf-8",
    )

    resolution, error = resolve_field_reference(object_path, "产品编号")

    assert error is None
    assert resolution is not None
    assert resolution.field_key == "productid"
    assert find_field_alias_matches(object_path, "查询产品编号") == (
        ("产品编号", "productid"),
    )


def test_field_alias_validation_checks_display_name_english_name_table(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "product.md").write_text(
        "# 字段定义\n\n"
        "| 显示名称 | 英文名称 | 描述 |\n"
        "| --- | --- | --- |\n"
        "| 产品编号 | productid | 产品唯一编号 |\n"
        "| 备用编号 | productid | 备用编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == (
        "对象“product”的字段 key“productid”重复",
    )


def test_field_alias_query_matches_only_declared_names_with_safe_ascii_boundaries(
    tmp_path,
):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "---\nresource: object\nkey: Affected\ntitle: 受影响对象\n---\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| CHANGE_ORDER_ID | CHANGE_ORDER编码 | 变更单号、id |\n"
        "| MaterialType | 物料变更类型 | 物料类型 |\n"
        "| CafeName | 咖啡名称 | Cafe\u0301 |\n",
        encoding="utf-8",
    )

    matches = find_field_alias_matches(
        object_path,
        "查询变更单号为CHANGE_ORDER-001的物料变更类型，忽略order_id",
    )

    assert matches == (
        ("变更单号", "CHANGE_ORDER_ID"),
        ("物料变更类型", "MaterialType"),
    )
    assert find_field_alias_matches(object_path, "查询 CHANGE_ORDER_ID") == ()
    assert find_field_alias_matches(object_path, "查询 Café") == (
        ("Cafe\u0301", "CafeName"),
    )


def test_field_alias_query_prefers_longest_overlapping_name(tmp_path):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| WorkHour | 工时 | 作业时长 |\n"
        "| WorkHourUnit | 工时单位 | 作业时长单位 |\n",
        encoding="utf-8",
    )

    assert find_field_alias_matches(object_path, "查询工时单位") == (
        ("工时单位", "WorkHourUnit"),
    )
    assert find_field_alias_matches(object_path, "查询工时以及工时单位") == (
        ("工时", "WorkHour"),
        ("工时单位", "WorkHourUnit"),
    )
    assert find_field_alias_matches(object_path, "查询作业时长单位") == (
        ("作业时长单位", "WorkHourUnit"),
    )


def test_field_alias_parser_supports_escaped_markdown_pipe(tmp_path):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "| 字段 key | 中文名 | 描述 | 别名 |\n"
        "| --- | --- | --- | --- |\n"
        "| PipeCode | 管道编码 | 业务\\|技术编码 | A\\|B |\n",
        encoding="utf-8",
    )

    resolution, error = resolve_field_reference(object_path, "A|B")

    assert error is None
    assert resolution is not None
    assert resolution.field_key == "PipeCode"
    assert find_field_alias_matches(object_path, "查询 A|B") == (
        ("A|B", "PipeCode"),
    )


@pytest.mark.parametrize(
    ("header", "separator", "row"),
    (
        (
            "| 字段 key | 中文名 | 别名 |",
            "| --- | --- | --- |",
            "| ActualCode | 业务编号 |",
        ),
        (
            "字段 key | 中文名 | 别名 |",
            "--- | --- | --- |",
            "ActualCode | 业务编号 |",
        ),
        (
            "| 字段 key | 中文名 | 别名",
            "| --- | --- | ---",
            "| ActualCode | 业务编号",
        ),
        (
            "字段 key | 中文名 | 别名",
            "--- | --- | ---",
            "ActualCode | 业务编号",
        ),
    ),
)
def test_field_alias_parser_allows_optional_outer_pipes_and_short_rows(
    tmp_path,
    header,
    separator,
    row,
):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "# 字段\n\n" + "\n".join((header, separator, row)),
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )


def test_field_alias_parser_stops_at_markdown_block_after_field_table(
    tmp_path,
):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| ActualCode | 正式编码 | 业务编号 |\n"
        "## 说明 | 不是字段 | 伪别名\n"
        "--- | --- | ---\n"
        "FakeCode | 示例编码 | 伪别名\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询业务编号") == (
        ("业务编号", "ActualCode"),
    )
    assert find_field_alias_matches(object_path, "查询伪别名") == ()


def test_field_alias_query_ignores_definition_without_field_key(tmp_path):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "|  | 编号 | 业务编号 |\n"
        "| OtherField | 编号 | 其他编号 |\n",
        encoding="utf-8",
    )

    assert find_field_alias_matches(object_path, "查询编号和业务编号") == (
        ("编号", "OtherField"),
    )


def test_field_alias_query_skips_case_insensitive_field_key_ambiguity(tmp_path):
    object_path = tmp_path / "object.md"
    object_path.write_text(
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| FieldA | 字段甲 | 编号 |\n"
        "| fielda | 字段乙 | 编号 |\n",
        encoding="utf-8",
    )

    resolution, error = resolve_field_reference(object_path, "编号")

    assert resolution is None
    assert error == (
        "Error: field reference '编号' is ambiguous. "
        "Candidate field keys: FieldA, fielda."
    )
    assert find_field_alias_matches(object_path, "查询编号") == ()


def test_field_alias_validation_reports_conflicts_and_duplicate_warnings(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "conflict.md").write_text(
        "---\nresource: object\nkey: Conflict\ntitle: 冲突对象\n---\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| FieldA | 字段甲 | 编号、重复、重复 |\n"
        "| FieldB | 字段乙 | 编号 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == (
        "对象文件“objects/conflict.md”中的对象“冲突对象”的字段名称“编号”同时指向 "
        "[FieldA, FieldB]；原始声明：[字段“FieldA”的别名原值“编号”，字段“FieldB”的别名原值“编号”]",
    )
    assert validation.warnings == (
        "对象“冲突对象”的字段“FieldA”存在重复别名“重复”",
    )


@pytest.mark.parametrize("marker", [None, "", "-", "—", "–", "无", "N/A", " n/a "])
def test_field_alias_parser_ignores_empty_alias_markers(marker):
    assert split_field_aliases(marker) == []


def test_field_alias_validation_ignores_multiple_empty_markers_in_one_object(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "synthetic.md"
    object_path.write_text(
        "---\nresource: object\nkey: Synthetic\ntitle: Synthetic Object\n---\n\n"
        "# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| FieldEmpty | Empty | |\n"
        "| FieldAscii | ASCII | - |\n"
        "| FieldEm | Em | — |\n"
        "| FieldEn | En | – |\n"
        "| FieldNone | None | 无 |\n"
        "| FieldNA | NA | N/A |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "- — – 无 N/A") == ()


def test_field_alias_validation_allows_object_without_alias_column(tmp_path):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    object_path = objects_dir / "legacy.md"
    object_path.write_text(
        "# 实体属性清单\n\n"
        "| 中文名称 | 英文名称 | 描述 |\n"
        "| --- | --- | --- |\n"
        "| 受影响编码 | AffectedObjectCode | 编码 |\n",
        encoding="utf-8",
    )

    validation = validate_imported_field_aliases(tmp_path)

    assert validation.errors == ()
    assert validation.warnings == ()
    assert find_field_alias_matches(object_path, "查询受影响编码") == (
        ("受影响编码", "AffectedObjectCode"),
    )


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_normalize_rejects_symbolic_links(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "normalized"
    _write_v01_package(source)
    probe = tmp_path / "link_probe"
    try:
        probe.symlink_to(source / "scripts" / "query_part.py")
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic link creation not permitted on this platform")
    probe.unlink()
    (source / "linked.py").symlink_to(source / "scripts" / "query_part.py")
    with pytest.raises(ValueError, match="symbolic links"):
        normalize_imported_package(source, target)
