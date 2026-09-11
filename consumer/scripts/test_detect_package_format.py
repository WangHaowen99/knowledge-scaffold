"""Unit tests for the format-detection logic in legacy_package_converter.py.

Run with:
    pytest test_detect_package_format.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Ensure the module under test is importable.
SCRIPT_DIR = Path(__file__).resolve().parent
_CONVERTER_SPEC = importlib.util.spec_from_file_location(
    "legacy_package_converter",
    SCRIPT_DIR / "legacy_package_converter.py",
)
if _CONVERTER_SPEC is None or _CONVERTER_SPEC.loader is None:
    raise ImportError("legacy_package_converter cannot be loaded")
_CONVERTER = importlib.util.module_from_spec(_CONVERTER_SPEC)
sys.modules[_CONVERTER_SPEC.name] = _CONVERTER
_CONVERTER_SPEC.loader.exec_module(_CONVERTER)
PackageFormatProfile = _CONVERTER.PackageFormatProfile
_has_frontmatter = _CONVERTER._has_frontmatter
detect_package_format = _CONVERTER.detect_package_format


# --------------------------------------------------------------------------- #
# Tests: _has_frontmatter helper
# --------------------------------------------------------------------------- #


class TestHasFrontmatter:
    """Tests for the _has_frontmatter helper."""

    @staticmethod
    def test_returns_false_for_nonexistent_file(tmp_path: Path) -> None:
        assert _has_frontmatter(tmp_path / "nonexistent.md") is False

    @staticmethod
    def test_returns_true_for_frontmatter_file(tmp_path: Path) -> None:
        f = tmp_path / "test.md"
        f.write_text("---\nkey: value\n---\nbody", encoding="utf-8")
        assert _has_frontmatter(f) is True

    @staticmethod
    def test_returns_false_for_plain_markdown(tmp_path: Path) -> None:
        f = tmp_path / "test.md"
        f.write_text("# Title\n\nbody", encoding="utf-8")
        assert _has_frontmatter(f) is False

    @staticmethod
    def test_returns_false_for_empty_file(tmp_path: Path) -> None:
        f = tmp_path / "empty.md"
        f.write_text("", encoding="utf-8")
        assert _has_frontmatter(f) is False


# --------------------------------------------------------------------------- #
# Tests: error handling
# --------------------------------------------------------------------------- #


class TestErrorHandling:
    """Tests for error and edge cases."""

    @staticmethod
    def test_raises_for_nonexistent_directory(tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a directory"):
            detect_package_format(tmp_path / "nonexistent")

    @staticmethod
    def test_raises_for_file_not_directory(tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hello", encoding="utf-8")
        with pytest.raises(ValueError, match="not a directory"):
            detect_package_format(f)

    @staticmethod
    def test_raises_when_manifest_missing(tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="manifest.yaml not found"):
            detect_package_format(tmp_path)

    @staticmethod
    def test_empty_manifest_detected(tmp_path: Path) -> None:
        """A package with an empty manifest.yaml should still be detected."""
        (tmp_path / "manifest.yaml").write_text("", encoding="utf-8")
        profile = detect_package_format(tmp_path)
        assert profile.manifest_kind == ""
        assert profile.has_schema_version is False
        # Should suggest creating index.md since neither exists.
        assert any("create index.md" in a for a in profile.conversion_actions)


# --------------------------------------------------------------------------- #
# Tests: synthetic packages
# --------------------------------------------------------------------------- #


class TestSyntheticPackages:
    """Tests with minimal synthetic package structures."""

    @staticmethod
    def _write_manifest(path: Path, **kwargs: object) -> None:
        lines = []
        for key, value in kwargs.items():
            lines.append(f"{key}: {value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def test_standard_package_no_conversion(tmp_path: Path) -> None:
        """A fully standard package should produce minimal actions."""
        TestSyntheticPackages._write_manifest(
            tmp_path / "manifest.yaml",
            kind="ontology-package",
            schema_version="0.1",
            id="test",
            name="test",
            description="test",
            version="0.1.0",
        )
        index = tmp_path / "index.md"
        index.write_text(
            "---\nresource: root\nkey: root\ntitle: test\n---\n# Test",
            encoding="utf-8",
        )
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "index.md").write_text(
            "---\nresource: object_index\nkey: objects\n---\n",
            encoding="utf-8",
        )
        (objects_dir / "Foo.md").write_text(
            "---\nresource: object\nkey: Foo\ntitle: Foo\n---\n"
            "# Foo\n\n| 字段 key | 中文名 | 类型 | 必填 | 业务说明 |\n"
            "| --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
        ops_dir = tmp_path / "operations"
        ops_dir.mkdir()
        (ops_dir / "index.md").write_text(
            "---\nresource: operation_index\nkey: operations\n---\n",
            encoding="utf-8",
        )

        profile = detect_package_format(tmp_path)
        assert profile.manifest_kind == "ontology-package"
        assert profile.has_schema_version is True
        assert profile.root_index_file == "index.md"
        assert profile.root_index_has_frontmatter is True
        assert profile.object_detail_has_frontmatter is True
        # No conversion actions expected for a fully standard package.
        assert profile.conversion_actions == []

    @staticmethod
    def test_old_format_package_detected(tmp_path: Path) -> None:
        """A legacy old-format package should be detected."""
        TestSyntheticPackages._write_manifest(
            tmp_path / "manifest.yaml",
            kind="ontology",
            id="test-old",
            name="test-old",
            description="test",
            version="1.0.0",
        )
        (tmp_path / "ONTOLOGY.md").write_text(
            "# Old ontology\n\nNo frontmatter here.",
            encoding="utf-8",
        )
        (tmp_path / "objects-list.md").write_text(
            "# Objects\n\nNo frontmatter.",
            encoding="utf-8",
        )
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "Foo.md").write_text(
            "# Foo\n\n| 属性名称 | 来源表 | 来源字段 | 数据类型 |\n| --- | --- | --- | --- |\n",
            encoding="utf-8",
        )

        profile = detect_package_format(tmp_path)
        assert profile.manifest_kind == "ontology"
        assert profile.root_index_file == "ONTOLOGY.md"
        assert profile.object_index_file == "objects-list.md"
        assert profile.object_detail_has_frontmatter is False
        assert any("fix kind" in a for a in profile.conversion_actions)
        assert any("ONTOLOGY.md" in a for a in profile.conversion_actions)
        assert any("objects-list.md" in a for a in profile.conversion_actions)
