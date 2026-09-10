"""Unit tests for validate_package and convert_package.

Run with:
    pytest test_import_pipeline.py -v
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

# Ensure the module under test is importable.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.append(str(SCRIPT_DIR))

from legacy_package_converter import _decode_zip_member_name  # noqa: E402
from legacy_package_converter import convert_package  # noqa: E402
from legacy_package_converter import import_package  # noqa: E402
from legacy_package_converter import import_package_from_zip  # noqa: E402
from legacy_package_converter import validate_package  # noqa: E402

# --------------------------------------------------------------------------- #
# Tests: validate_package error handling
# --------------------------------------------------------------------------- #


class TestValidateErrorHandling:
    """Tests for validator error handling."""

    @staticmethod
    def test_raises_for_nonexistent_directory(tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a directory"):
            validate_package(tmp_path / "nonexistent")

    @staticmethod
    def test_raises_for_file_not_directory(tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError, match="not a directory"):
            validate_package(f)

    @staticmethod
    def test_missing_manifest_reports_error(tmp_path: Path) -> None:
        result = validate_package(tmp_path)
        assert result.is_conformant is False
        assert any(e.rule == "manifest.exists" for e in result.errors)


# --------------------------------------------------------------------------- #
# Tests: synthetic edge cases
# --------------------------------------------------------------------------- #


class TestSyntheticPackages:
    """Tests with minimal synthetic package structures."""

    @staticmethod
    def make_standard_package(root: Path) -> None:
        """Create a minimal conformant package."""
        (root / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: '0.1'\n"
            "id: test\n"
            "name: test\n"
            "description: test\n"
            "version: 0.1.0\n"
            "language: zh-CN\n"
            "status: draft\n",
            encoding="utf-8",
        )
        (root / "index.md").write_text(
            "---\nresource: root\nkey: root\ntitle: test\n---\n# Test\n",
            encoding="utf-8",
        )
        objects = root / "objects"
        objects.mkdir()
        (objects / "index.md").write_text(
            "---\nresource: object_index\nkey: objects\ntitle: objects\n---\n" "# Objects\n",
            encoding="utf-8",
        )
        (objects / "Foo.md").write_text(
            "---\nresource: object\nkey: Foo\ntitle: Foo\n---\n# Foo\n",
            encoding="utf-8",
        )
        ops = root / "operations"
        ops.mkdir()
        (ops / "index.md").write_text(
            "---\nresource: operation_index\nkey: operations\n" "title: operations\n---\n# Operations\n",
            encoding="utf-8",
        )
        (ops / "Bar.md").write_text(
            "---\nresource: operation\nkey: Bar\ntitle: Bar\n" "uses:\n  objects:\n    - Foo\n---\n# Bar\n",
            encoding="utf-8",
        )

    @staticmethod
    def test_standard_package_imports_directly(tmp_path: Path) -> None:
        pkg = tmp_path / "standard_pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)

        output = tmp_path / "output"
        result = import_package(pkg, output)
        assert result.imported is True
        assert result.required_conversion is False

    @staticmethod
    def test_old_format_imports_after_conversion(tmp_path: Path) -> None:
        """Simulate a legacy old-format package."""
        pkg = tmp_path / "old_pkg"
        pkg.mkdir()
        (pkg / "manifest.yaml").write_text(
            "kind: ontology\n" "id: old-test\n" "name: old-test\n" "description: test\n" "version: 1.0.0\n",
            encoding="utf-8",
        )
        (pkg / "ONTOLOGY.md").write_text(
            "# Old Package\n\nNo frontmatter here.",
            encoding="utf-8",
        )
        (pkg / "objects-list.md").write_text("# Objects\n\nNo frontmatter.", encoding="utf-8")
        objects = pkg / "objects"
        objects.mkdir()
        (objects / "Widget.md").write_text(
            "# Widget\n\n| 属性名称 | 来源表 |\n| --- | --- |\n",
            encoding="utf-8",
        )
        (pkg / "operations-list.md").write_text("# Operations\n", encoding="utf-8")
        ops = pkg / "operations"
        ops.mkdir()
        (ops / "DoStuff.md").write_text(
            "---\nresource: operation\nkey: DoStuff\ntitle: DoStuff\n"
            "uses:\n  objects:\n    - Widget\n---\n# DoStuff\n",
            encoding="utf-8",
        )

        output = tmp_path / "converted"
        result = import_package(pkg, output)
        assert result.imported is True
        assert result.required_conversion is True
        assert (output / "index.md").exists()
        assert (output / "objects" / "index.md").exists()
        assert (output / "operations" / "index.md").exists()


# --------------------------------------------------------------------------- #
# Tests: mapping conversion and field-level validation
# --------------------------------------------------------------------------- #


def _write_mapping(root: Path, payload: dict) -> None:
    mappings = root / "mappings"
    mappings.mkdir(exist_ok=True)
    (mappings / "object-data-mappings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _valid_mapping_payload() -> dict:
    return {
        "schema_version": "0.1",
        "default_provider_alias": "plm",
        "objects": {
            "Foo": {
                "provider_alias": "plm",
                "resource": "FOO_TABLE",
                "primary_key": ["编号"],
                "fields": {
                    "编号": {"source": "fd_id", "type": "string"},
                    "名称": {"source": "fd_name", "type": "string"},
                },
                "policy": {"read_only": True, "max_rows": 200},
            }
        },
    }


class TestManifestIdFormat:
    """The ontology id must be [A-Za-z0-9._-], starting with alphanumeric."""

    @staticmethod
    def test_valid_id_passes(tmp_path: Path) -> None:
        TestSyntheticPackages.make_standard_package(tmp_path)
        result = validate_package(tmp_path)
        assert result.is_conformant is True
        assert not any(e.rule == "manifest.id_format" for e in result.errors)

    @staticmethod
    def test_id_with_dots_accepted(tmp_path: Path) -> None:
        """Dots are allowed in ontology id (aligned with version contract)."""
        TestSyntheticPackages.make_standard_package(tmp_path)
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "kind: ontology-package\n"
            "schema_version: '0.1'\n"
            "id: my.package.1.2\n"
            "name: test\n"
            "description: test\n"
            "version: 0.1.0\n",
            encoding="utf-8",
        )
        result = validate_package(tmp_path)
        assert result.is_conformant is True
        assert not any(e.rule == "manifest.id_format" for e in result.errors)

    @staticmethod
    def test_id_starting_with_underscore_rejected(tmp_path: Path) -> None:
        TestSyntheticPackages.make_standard_package(tmp_path)
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(
            "kind: ontology-package\n"
            "schema_version: '0.1'\n"
            "id: _bad_start\n"
            "name: test\n"
            "description: test\n"
            "version: 0.1.0\n",
            encoding="utf-8",
        )
        result = validate_package(tmp_path)
        assert result.is_conformant is False
        assert any(e.rule == "manifest.id_format" for e in result.errors)
        issue = next(e for e in result.errors if e.rule == "manifest.id_format")
        assert "bad_start" in issue.message


class TestManifestFirstClassFields:
    """enabled/scope are first-class manifest fields and must survive conversion.

    They must not be rewritten to the x_ prefixed form.
    """

    @staticmethod
    def test_convert_preserves_enabled_and_scope(tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)
        (pkg / "manifest.yaml").write_text(
            "kind: ontology-package\n"
            "schema_version: '0.1'\n"
            "id: test\n"
            "name: test\n"
            "description: test\n"
            "version: 0.1.0\n"
            "language: zh-CN\n"
            "status: draft\n"
            "enabled: false\n"
            "scope:\n"
            "  - finance\n",
            encoding="utf-8",
        )

        output = tmp_path / "out"
        result = convert_package(pkg, output)
        assert result.success is True

        converted = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))
        # Semantics survive conversion untouched (no silent default rebuild).
        assert converted["enabled"] is False
        assert converted["scope"] == ["finance"]
        assert "x_enabled" not in converted
        assert "x_scope" not in converted
        assert not any("x_enabled" in action or "x_scope" in action for action in result.actions_applied)


class TestMappingValidation:
    """Tests for validate_package mapping field-level rules."""

    @staticmethod
    def _make_package(tmp_path: Path, payload: dict) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)
        _write_mapping(pkg, payload)
        return pkg

    @staticmethod
    def test_valid_mapping_passes(tmp_path: Path) -> None:
        pkg = TestMappingValidation._make_package(tmp_path, _valid_mapping_payload())
        result = validate_package(pkg)
        assert result.is_conformant is True
        assert len(result.errors) == 0

    @staticmethod
    def test_primary_key_missing_reference(tmp_path: Path) -> None:
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["primary_key"] = ["missing_field"]
        pkg = TestMappingValidation._make_package(tmp_path, payload)
        result = validate_package(pkg)
        assert any(e.rule == "mappings.primary_key_ref" for e in result.errors)

    @staticmethod
    def test_metadata_field_key_rejected(tmp_path: Path) -> None:
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["fields"]["source_table"] = {
            "source": "FOO_TABLE",
            "type": "string",
        }
        pkg = TestMappingValidation._make_package(tmp_path, payload)
        result = validate_package(pkg)
        assert any(e.rule == "mappings.field_metadata" for e in result.errors)

    @staticmethod
    def test_boolean_max_rows_rejected(tmp_path: Path) -> None:
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["policy"]["max_rows"] = True
        pkg = TestMappingValidation._make_package(tmp_path, payload)
        result = validate_package(pkg)
        assert any(e.rule == "mappings.policy_max_rows" for e in result.errors)

    @staticmethod
    def test_require_user_filter_missing_reference(tmp_path: Path) -> None:
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["policy"]["require_user_filter"] = ["missing_field"]
        pkg = TestMappingValidation._make_package(tmp_path, payload)
        result = validate_package(pkg)
        assert any(e.rule == "mappings.require_user_filter_ref" for e in result.errors)


class TestMappingConversion:
    """Tests for convert_package mapping conversion."""

    @staticmethod
    def _make_mapping_package(root: Path) -> None:
        """Standard package whose mapping uses physical primary_key names."""
        TestSyntheticPackages.make_standard_package(root)
        payload = {
            "schema_version": "0.1",
            "default_provider_alias": "plm",
            "objects": {
                "Foo": {
                    "provider_alias": "plm",
                    "resource": "FOO_TABLE",
                    "primary_key": ["fd_id", "fd_name"],
                    "fields": {
                        "编号": {"source": "fd_id", "type": "string"},
                        "名称": {"source": "fd_name", "type": "string"},
                        "source_table": {
                            "source": "FOO_TABLE",
                            "type": "string",
                        },
                    },
                }
            },
        }
        _write_mapping(root, payload)

    @staticmethod
    def test_primary_key_physical_names_rewritten(tmp_path: Path) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestMappingConversion._make_mapping_package(pkg)
        output = tmp_path / "map_out"
        convert_package(pkg, output)
        mapping = json.loads((output / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8"))
        assert mapping["objects"]["Foo"]["primary_key"] == ["编号", "名称"]

    @staticmethod
    def test_metadata_field_removed_with_action(tmp_path: Path) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestMappingConversion._make_mapping_package(pkg)
        output = tmp_path / "map_out"
        result = convert_package(pkg, output)
        # source_table is removed from the converted fields.
        mapping = json.loads((output / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8"))
        assert "source_table" not in mapping["objects"]["Foo"]["fields"]
        # ... and the removal is recorded as an action.
        assert any("source_table" in action and "removed" in action for action in result.actions_applied)

    @staticmethod
    def test_metadata_field_references_cleaned(tmp_path: Path) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestMappingConversion._make_mapping_package(pkg)
        mapping_path = pkg / "mappings" / "object-data-mappings.json"
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        entry = payload["objects"]["Foo"]
        entry["primary_key"] = ["编号", "source_table"]
        entry["policy"] = {"require_user_filter": ["source_table", "编号"]}
        mapping_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        output = tmp_path / "map_out"
        result = convert_package(pkg, output)
        assert result.success is True
        mapping = json.loads((output / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8"))
        entry = mapping["objects"]["Foo"]
        assert "source_table" not in entry["fields"]
        assert entry["primary_key"] == ["编号"]
        assert entry["policy"]["require_user_filter"] == ["编号"]
        # The dangling-reference cleanup is recorded too.
        assert any("source_table" in action and "removed" in action for action in result.actions_applied)

    @staticmethod
    def test_unmatched_primary_key_kept_with_error(tmp_path: Path) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestMappingConversion._make_mapping_package(pkg)
        mapping_path = pkg / "mappings" / "object-data-mappings.json"
        payload = json.loads(mapping_path.read_text(encoding="utf-8"))
        payload["objects"]["Foo"]["primary_key"] = ["ghost_col", "fd_name"]
        mapping_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        output = tmp_path / "map_out"
        result = convert_package(pkg, output)
        assert result.success is False
        assert any("ghost_col" in e for e in result.errors)
        # The unknown entry is kept as-is; the matched one is rewritten.
        mapping = json.loads((output / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8"))
        assert mapping["objects"]["Foo"]["primary_key"] == ["ghost_col", "名称"]

    @staticmethod
    def test_no_mapping_file_skipped(tmp_path: Path) -> None:
        pkg = tmp_path / "map_pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)
        output = tmp_path / "map_out"
        result = convert_package(pkg, output)
        assert result.success is True
        assert not (output / "mappings").exists()
        assert not any(action.startswith("mapping:") for action in result.actions_applied)


class TestFieldTypeNormalization:
    """Numeric type aliases are accepted by validation and normalized to number.

    The alias is replaced by the canonical ``number`` type during conversion.
    """

    @staticmethod
    def _make_package(tmp_path: Path, field_type: str) -> Path:
        pkg = tmp_path / "type_pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["fields"]["编号"]["type"] = field_type
        _write_mapping(pkg, payload)
        return pkg

    @staticmethod
    @pytest.mark.parametrize("alias", ["float", "int", "integer", "double", "decimal"])
    def test_validate_accepts_numeric_alias(tmp_path: Path, alias: str) -> None:
        pkg = TestFieldTypeNormalization._make_package(tmp_path, alias)
        result = validate_package(pkg)
        assert result.is_conformant is True
        assert not any(e.rule.startswith("mappings") for e in result.errors)

    @staticmethod
    @pytest.mark.parametrize("alias", ["float", "int", "integer", "double", "decimal"])
    def test_convert_normalizes_field_type(tmp_path: Path, alias: str) -> None:
        pkg = TestFieldTypeNormalization._make_package(tmp_path, alias)
        output = tmp_path / "type_out"
        result = convert_package(pkg, output)
        assert result.success is True
        mapping = json.loads((output / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8"))
        assert mapping["objects"]["Foo"]["fields"]["编号"]["type"] == "number"

    @staticmethod
    def test_validate_rejects_unknown_type(tmp_path: Path) -> None:
        pkg = TestFieldTypeNormalization._make_package(tmp_path, "blob")
        result = validate_package(pkg)
        assert any(e.rule == "mappings.field_type" for e in result.errors)


class TestSkillSourcePackages:
    """Tests for legacy SKILL.md/object/logic source packages."""

    @staticmethod
    def make_source_package(root: Path) -> None:
        """Create a minimal synthetic source-format package."""
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            "---\n"
            "id: source-demo\n"
            "name: source-demo\n"
            "version: 1.0.0\n"
            "description: Synthetic source package for conversion tests.\n"
            "---\n",
            encoding="utf-8",
        )
        (root / "object" / "物料.md").parent.mkdir(parents=True)
        (root / "object" / "物料.md").write_text(
            "# 对象描述\n"
            "合成对象描述。\n\n"
            "# 对象别名\n"
            "物料号, 零件\n\n"
            "# 英文名称\n"
            "Material\n\n"
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 是 | 字符串 | 料号 | 必填 |\n"
            "| 物料名称 | material_name | 物料名称 | 否 | 字符串 | 无 | 无 |\n",
            encoding="utf-8",
        )
        (root / "logic").mkdir(parents=True, exist_ok=True)
        (root / "logic" / "物料查询.md").write_text(
            "# 业务描述\n"
            "根据物料编号查询物料。\n\n"
            "# 与logic的关联\n"
            "- 被logic调用 > 无\n\n"
            "# 触发条件\n"
            "用户输入物料编号。\n\n"
            "# 输入\n"
            "| 参数名称 | 描述 | 输入源 |\n"
            "| --- | --- | --- |\n"
            "| 物料编号 | 需要查询的编号 | 用户输入 |\n\n"
            "# 输出\n"
            "| 参数名称 | 描述 |\n"
            "| --- | --- |\n"
            "| 物料名称 | 查询到的名称 |\n\n"
            "# 逻辑描述\n"
            "按物料编号查询并返回物料信息。\n",
            encoding="utf-8",
        )

    @staticmethod
    def test_directory_source_package_converts_to_v01(tmp_path: Path) -> None:
        source = tmp_path / "source"
        output = tmp_path / "output_converted"
        TestSkillSourcePackages.make_source_package(source)

        result = import_package(source, output_dir=output)

        assert result.imported is True
        assert result.required_conversion is True
        assert result.errors == []
        assert validate_package(output).is_conformant is True
        assert (output / "manifest.yaml").is_file()
        assert (output / "index.md").is_file()
        assert (output / "mappings" / "object-data-mappings.json").is_file()

        manifest = yaml.safe_load((output / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["kind"] == "ontology-package"
        assert manifest["schema_version"] == "0.1"
        assert manifest["id"] == "source-demo"

        object_doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "resource: object" in object_doc
        assert "| 中文名称 | 英文名称 | 描述 | 属性别名 |" in object_doc
        assert "material_code" in object_doc
        assert "属性规则" not in object_doc

        operation_doc = (output / "operations" / "物料查询.md").read_text(encoding="utf-8")
        assert "resource: operation" in operation_doc
        assert "objects: []" in operation_doc
        assert "# 管理标签\n#主流程" in operation_doc

    @staticmethod
    def test_source_field_aliases_sanitized(tmp_path: Path) -> None:
        source = tmp_path / "alias_source"
        output = tmp_path / "alias_output_converted"
        TestSkillSourcePackages.make_source_package(source)
        object_path = source / "object" / "物料.md"
        object_path.write_text(
            "# 对象描述\n"
            "合成对象描述。\n\n"
            "# 对象别名\n"
            "物料号\n\n"
            "# 英文名称\n"
            "Material\n\n"
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 原因代码 | cause_code | 原因代码 | 否 | 字符串 | 原因 | 无 |\n"
            "| 原因分析 | cause_analysis | 原因分析 | 否 | 字符串 | 原因 | 无 |\n"
            "| 数量 | quantity | 数量 | 否 | 数字 | 无 | 无 |\n"
            "| 单位 | unit | 单位 | 否 | 字符串 | 无 | 无 |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "| 原因代码 | cause_code | 原因代码 | 原因 |" in doc
        assert "| 原因分析 | cause_analysis | 原因分析 |  |" in doc
        assert "| 数量 | quantity | 数量 |  |" in doc
        assert "| 单位 | unit | 单位 |  |" in doc

    @staticmethod
    def test_zip_source_package_with_wrapper_directory(tmp_path: Path) -> None:
        source = tmp_path / "source"
        zip_path = tmp_path / "source.zip"
        zip_output = tmp_path / "source_converted.zip"
        TestSkillSourcePackages.make_source_package(source)

        with zipfile.ZipFile(zip_path, "w") as zf:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                arcname = "wrapped/" + path.relative_to(source).as_posix()
                zf.write(path, arcname)

        result = import_package_from_zip(zip_path, zip_output=zip_output)

        assert result.imported is True
        assert result.errors == []
        assert result.zip_output == str(zip_output)
        with zipfile.ZipFile(zip_output) as zf:
            names = set(zf.namelist())
        assert "manifest.yaml" in names
        assert "objects/物料.md" in names
        assert "operations/物料查询.md" in names

    @staticmethod
    def test_object_table_missing_alias_column_keeps_rows(tmp_path: Path) -> None:
        source = tmp_path / "missing_alias_source"
        output = tmp_path / "missing_alias_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "object" / "物料.md").write_text(
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 是 | 字符串 |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "| 物料编号 | material_code | 物料唯一编号 |  |" in doc

    @staticmethod
    def test_source_field_rows_missing_key_are_reported(tmp_path: Path) -> None:
        source = tmp_path / "empty_key_source"
        output = tmp_path / "empty_key_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "object" / "物料.md").write_text(
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 是 | 字符串 | 无 | 必填 |\n"
            "| 备注 | | 备注文本 | 否 | 字符串 | 无 | 无 |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is False
        assert any("备注" in error for error in result.errors)
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "| 备注" in doc
        assert "material_code" in doc

    @staticmethod
    def test_source_table_garbage_rows_are_dropped(tmp_path: Path) -> None:
        source = tmp_path / "garbage_source"
        output = tmp_path / "garbage_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "object" / "物料.md").write_text(
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 属性别名 |\n"
            "| --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 无 |\n"
            "|  |  | 仅描述文本 |  |\n"
            "|  |  |  |  |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "material_code" in doc
        assert "仅描述文本" not in doc

    @staticmethod
    def test_source_no_space_heading_parsed(tmp_path: Path) -> None:
        source = tmp_path / "no_space_source"
        output = tmp_path / "no_space_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "object" / "物料.md").write_text(
            "#实体属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 属性别名 |\n"
            "| --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 无 |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "material_code" in doc

    @staticmethod
    def test_source_copy_skips_cache_and_junk_files(tmp_path: Path) -> None:
        source = tmp_path / "junk_source"
        output = tmp_path / "junk_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "action" / "__pycache__").mkdir(parents=True, exist_ok=True)
        (source / "action" / "__pycache__" / "helper.pyc").write_bytes(b"x")
        (source / "action" / ".DS_Store").write_bytes(b"junk")
        (source / "action" / "查询物料.py").write_text(
            "# 工具流程: 物料查询\n" "print(1)\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        assert (output / "scripts" / "查询物料.py").is_file()
        assert not (output / "scripts" / "__pycache__").exists()
        assert not (output / "scripts" / ".DS_Store").exists()

    @staticmethod
    def test_source_operation_tag_supports_flow_use(tmp_path: Path) -> None:
        source = tmp_path / "flow_use_source"
        output = tmp_path / "flow_use_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "logic" / "物料查询.md").write_text(
            "# 与logic的关联\n" "- 被流程使用 > 查询流程\n\n" "# 逻辑描述\n" "查询物料。\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "operations" / "物料查询.md").read_text(encoding="utf-8")
        assert "# 管理标签\n#子流程" in doc

    @staticmethod
    def test_source_multi_line_object_aliases_preserved(tmp_path: Path) -> None:
        source = tmp_path / "multi_alias_source"
        output = tmp_path / "multi_alias_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "object" / "物料.md").write_text(
            "# 对象别名\n"
            "物料号\n"
            "零件\n\n"
            "# 对象属性清单\n"
            "| 中文名称 | 英文名称 | 描述 | 属性别名 |\n"
            "| --- | --- | --- | --- |\n"
            "| 物料编号 | material_code | 物料唯一编号 | 无 |\n",
            encoding="utf-8",
        )

        result = convert_package(source, output)

        assert result.success is True
        doc = (output / "objects" / "物料.md").read_text(encoding="utf-8")
        assert "- 物料号" in doc
        assert "- 零件" in doc

    @staticmethod
    def test_source_action_and_asset_converted(tmp_path: Path) -> None:
        source = tmp_path / "action_asset_source"
        output = tmp_path / "action_asset_output"
        TestSkillSourcePackages.make_source_package(source)
        (source / "action").mkdir(parents=True, exist_ok=True)
        (source / "action" / "查询物料.py").write_text(
            "# 工具流程: 物料查询\n" "print(1)\n",
            encoding="utf-8",
        )
        (source / "asset").mkdir(parents=True, exist_ok=True)
        (source / "asset" / "common.conf").write_text("threshold=3\n", encoding="utf-8")

        result = convert_package(source, output)

        assert result.success is True
        assert (output / "scripts" / "查询物料.py").is_file()
        assert (output / "scripts" / "查询物料.md").is_file()
        script_doc = (output / "scripts" / "查询物料.md").read_text(encoding="utf-8")
        assert "bound_operation: 物料查询" in script_doc
        assert "entrypoint: 查询物料.py" in script_doc
        assert (output / "scripts" / "index.md").is_file()
        assert (output / "assets" / "common.conf").is_file()

    @staticmethod
    def test_zip_source_package_with_asset_skill_md(tmp_path: Path) -> None:
        source = tmp_path / "asset_skill_source"
        zip_path = tmp_path / "asset_skill_source.zip"
        zip_output = tmp_path / "asset_skill_source_converted.zip"
        TestSkillSourcePackages.make_source_package(source)
        (source / "asset").mkdir(parents=True, exist_ok=True)
        (source / "asset" / "SKILL.md").write_text("# Shared asset\n", encoding="utf-8")

        with zipfile.ZipFile(zip_path, "w") as zf:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(source).as_posix()
                zf.write(path, arcname)

        result = import_package_from_zip(zip_path, zip_output=zip_output)

        assert result.imported is True
        assert result.errors == []


# --------------------------------------------------------------------------- #
# Tests: zip import
# --------------------------------------------------------------------------- #


def _make_standard_zip(root: Path, zip_path: Path) -> None:
    TestSyntheticPackages.make_standard_package(root)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for file_path in sorted(root.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(root).as_posix())


class TestZipImport:
    """Zip input: pipeline semantics, hardening, and clean errors."""

    @staticmethod
    def test_corrupt_zip_raises_clean_error(tmp_path: Path) -> None:
        bad_zip = tmp_path / "bad.zip"
        bad_zip.write_bytes(b"this is not a zip file")
        with pytest.raises(zipfile.BadZipFile):
            import_package_from_zip(bad_zip)

    @staticmethod
    def test_decode_zip_member_name_handles_legacy_gbk() -> None:
        # 无 UTF-8 flag 的旧式 zip（Java/老工具产物）文件名是 cp437 码位，
        # 解码后应恢复为 GBK 中文（与 UTF-8 flag 与纯 ASCII 共同回归）。
        utf8_info = zipfile.ZipInfo("operations/查询流程.md")
        utf8_info.flag_bits |= 0x800
        assert _decode_zip_member_name(utf8_info) == "operations/查询流程.md"

        gbk_info = zipfile.ZipInfo("operations/查询流程.md".encode("gbk").decode("cp437"))
        gbk_info.flag_bits &= ~0x800
        assert _decode_zip_member_name(gbk_info) == "operations/查询流程.md"

        ascii_info = zipfile.ZipInfo("objects/Widget.md")
        ascii_info.flag_bits &= ~0x800
        assert _decode_zip_member_name(ascii_info) == "objects/Widget.md"

    @staticmethod
    def test_traversal_member_rejected(tmp_path: Path) -> None:
        evil_zip = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil_zip, "w") as zf:
            zf.writestr("../evil.md", "boom")
        with pytest.raises(ValueError, match="Unsafe zip member path"):
            import_package_from_zip(evil_zip)

    @staticmethod
    def test_zip_with_directory_entries_extracts_cleanly(
        tmp_path: Path,
    ) -> None:
        """Zips produced by standard tools include directory-entry members.

        Paths ending with ``/`` must not be treated as unsafe.
        """
        src = tmp_path / "src"
        src.mkdir()
        zip_path = tmp_path / "pkg.zip"
        TestSyntheticPackages.make_standard_package(src)

        # Build a zip WITH explicit directory members, mirroring what
        # ``zip -r`` / Windows Explorer produce.
        with zipfile.ZipFile(zip_path, "w") as zf:
            for entry in sorted(src.rglob("*")):
                arcname = entry.relative_to(src).as_posix()
                if entry.is_dir():
                    zf.writestr(arcname + "/", "")
                else:
                    zf.write(entry, arcname)

        result = import_package_from_zip(zip_path)
        assert result.imported is True
        assert result.zip_output != ""

    @staticmethod
    def test_zip_import_produces_zip_and_clears_import_path(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        zip_path = tmp_path / "pkg.zip"
        _make_standard_zip(src, zip_path)

        result = import_package_from_zip(zip_path)
        assert result.imported is True
        assert result.import_path == ""
        assert result.zip_output != ""
        assert Path(result.zip_output).is_file()
        assert Path(result.zip_output).suffix == ".zip"

    @staticmethod
    def test_zip_import_preserves_dir_with_output_dir(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        zip_path = tmp_path / "pkg.zip"
        _make_standard_zip(src, zip_path)
        out_dir = tmp_path / "pkg_out_converted"

        result = import_package_from_zip(zip_path, output_dir=out_dir)
        assert result.imported is True
        assert result.import_path == str(out_dir)
        assert out_dir.is_dir()
        assert (out_dir / "manifest.yaml").is_file()

    @staticmethod
    def test_raw_cli_with_output_dir(tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        TestSyntheticPackages.make_standard_package(src)
        # Force a conversion by using a mapping that declares a provider
        # metadata field key (strip_metadata_fields is a conversion action).
        payload = _valid_mapping_payload()
        payload["objects"]["Foo"]["fields"]["source_table"] = {
            "source": "FOO_TABLE",
            "type": "string",
        }
        _write_mapping(src, payload)
        out_dir = tmp_path / "out_converted"

        from legacy_package_converter import main

        exit_code = main(["legacy_package_converter.py", str(src), "--output-dir", str(out_dir)])
        assert exit_code == 0
        assert out_dir.is_dir()


class TestStripEmptyKeyRows:
    """Placeholder field-table rows are removed without touching real rows."""

    @staticmethod
    def test_strips_placeholder_rows_only() -> None:
        from legacy_package_converter import _strip_empty_key_field_rows

        text = (
            "| 显示名 | key | 类型 | 必填 |\n"
            "| --- | --- | --- | --- |\n"
            "| 继承自MPart的各个属性 |  |  |  |\n"
            "| 占位说明 |  |  |  |\n"
            "| 有类型但无key |  | string | 是 |\n"
            "| 真实字段 | fd_x | string | 否 |\n"
        )
        cleaned, did_strip = _strip_empty_key_field_rows(text)
        assert did_strip is True
        assert "继承自MPart的各个属性" not in cleaned
        assert "占位说明" not in cleaned
        assert "有类型但无key" in cleaned
        assert "真实字段" in cleaned


class TestPartialFrontmatterRepair:
    """Details starting with ``---`` but lacking a complete frontmatter are repaired.

    Such details are repaired instead of skipped.
    """

    @staticmethod
    def test_unclosed_frontmatter_repaired(tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        TestSyntheticPackages.make_standard_package(pkg)
        (pkg / "objects" / "实体X.md").write_text(
            "---\nresource: object\nkey: 实体X\ntitle: 残块\n"  # no closing ---
            "#### 对象描述\n实体X说明\n",
            encoding="utf-8",
        )
        output = tmp_path / "out"
        result = convert_package(pkg, output)
        assert result.success is True
        repaired = (output / "objects" / "实体X.md").read_text(encoding="utf-8")
        assert "resource: object" in repaired
        assert "key: 实体X" in repaired
        assert "#### 对象描述" in repaired
