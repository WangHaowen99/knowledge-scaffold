#!/usr/bin/env python3
"""Ontology package converter and import orchestrator.

Combines format detection, conformance validation, format conversion and the
import pipeline into a single script:

    1. Validate package conformance (validate_package).
    2. If conformant (zero errors) -> import directly.
    3. If not conformant -> detect format -> convert -> re-validate.
    4. If still not conformant after conversion -> reject.

Accepts either a directory or a ``.zip`` file as input.  For zip input the
converted result is packed into a zip next to the source archive (override
with ``--zip-output``); the converted directory is preserved only when
``--output-dir`` is given.  For directory input no zip is produced; use
``--output-dir`` to choose the output directory (default: sibling
``<name>_converted``).

Usage:
    python legacy_package_converter.py <package_dir_or_zip> [--output-dir <dir>] [--zip-output <zip>]

Exit codes:
    0  package imported successfully (either directly or after conversion)
    1  package rejected (failed validation after conversion)
    2  invalid arguments
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

_logger = logging.getLogger(__name__)

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


# ===========================================================================
#  Data model
# ===========================================================================


@dataclass
class PackageFormatProfile:
    """Records the detected format of an imported ontology package."""

    # Signal values
    manifest_kind: str = ""
    has_schema_version: bool = False
    root_index_file: str = ""  # "index.md" / "ONTOLOGY.md" / ""
    root_index_has_frontmatter: bool = False
    object_index_file: str = ""  # "objects/index.md" / "objects-list.md" / ""
    object_detail_has_frontmatter: bool = False
    operation_index_file: str = ""  # "operations/index.md" / "operations-list.md" / ""
    has_relations_dir: bool = False
    relations_list_file: str = ""  # "relations-list.md" / ""
    ontology_subdir: bool = False
    field_table_header: str = ""  # sample header line for column detection

    # Derived conversion actions
    conversion_actions: list[str] = field(default_factory=list)


@dataclass
class ValidationIssue:
    """A single conformance issue found during validation."""

    level: str  # "error" or "warning"
    rule: str  # short rule identifier
    path: str  # file or directory path relevant to the issue
    message: str  # human-readable description


@dataclass
class ValidationResult:
    """Aggregate result of validating one package."""

    package_root: str
    is_conformant: bool  # True when zero errors
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def total_issues(self) -> int:
        return len(self.errors) + len(self.warnings)


@dataclass
class ConversionResult:
    """Outcome of converting one package."""

    source_root: str
    output_root: str
    actions_applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    success: bool = False


@dataclass
class ImportResult:
    """Outcome of importing one package."""

    package_root: str
    imported: bool = False
    import_path: str = ""
    required_conversion: bool = False
    conversion_actions: list[str] = field(default_factory=list)
    initial_validation: dict = field(default_factory=dict)
    final_validation: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    zip_output: str = ""


# ===========================================================================
#  Shared constants and frontmatter / YAML helpers
# ===========================================================================

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Backend ontology id contract: [A-Za-z0-9._-], must start with alphanumeric.
_MANIFEST_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")

_PROVIDER_META_FIELDS = frozenset(
    {
        "source_table",
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
    }
)

_VALID_FIELD_TYPES = frozenset(
    {
        "string",
        "number",
        "boolean",
        "date",
        "datetime",
    }
)

_NUMERIC_TYPE_ALIASES = frozenset({"float", "int", "integer", "double", "decimal"})


def _has_frontmatter(path: Path) -> bool:
    """Quick check: does *path* start with ``---``?"""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.readline().strip() == "---"
    except (OSError, UnicodeDecodeError):
        return False


def has_frontmatter_for_test(path: Path) -> bool:
    """Check frontmatter through a public test boundary."""
    return _has_frontmatter(path)


def _has_complete_frontmatter(text: str) -> bool:
    """True when *text* starts with a parseable frontmatter declaring a resource.

    A file starting with ``---`` but missing a closing delimiter, or whose
    frontmatter has no ``resource`` field, is treated as incomplete so the
    converter repairs it instead of skipping it.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return False
    if yaml is not None:
        try:
            parsed = yaml.safe_load(match.group(1))
        except Exception:
            return False
        return isinstance(parsed, dict) and bool(parsed.get("resource"))
    return "resource:" in match.group(1)


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a Markdown file.

    Returns an empty dict if the file has no frontmatter or cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}

    raw = match.group(1)
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError:
            return {}

    # Minimal fallback: parse ``key: value`` and ``key: [a, b]`` lines.
    result: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                items = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
                result[key] = items
            else:
                result[key] = value.strip("\"'")
    return result


def _build_frontmatter(fields: dict[str, object]) -> str:
    """Build a YAML frontmatter block from *fields*."""
    if yaml is not None:
        body = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False)
    else:
        lines = []
        for key, value in fields.items():
            if isinstance(value, list):
                if value:
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append(f"  - {item}")
                else:
                    lines.append(f"{key}: []")
            elif isinstance(value, dict):
                lines.append(f"{key}:")
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, list) and not sub_value:
                        lines.append(f"  {sub_key}: []")
                    else:
                        lines.append(f"  {sub_key}: {sub_value}")
            else:
                lines.append(f"{key}: {value}")
        body = "\n".join(lines) + "\n"
    return f"---\n{body}---\n"


def _strip_frontmatter(text: str) -> tuple[str, dict]:
    """Remove frontmatter from *text*, return (body, frontmatter_dict)."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text, {}
    raw = match.group(1)
    body = text[match.end() :]
    fm: dict = {}
    if yaml is not None:
        try:
            data = yaml.safe_load(raw)
            if isinstance(data, dict):
                fm = data
        except yaml.YAMLError:
            pass
    else:
        for line in raw.splitlines():
            if ":" in line and not line.strip().startswith("#"):
                key, _, value = line.partition(":")
                fm[key.strip()] = value.strip()
    return body, fm


def _normalize_field_type(field_type: object) -> Optional[str]:
    """Return the canonical field type, or None when invalid.

    Numeric aliases (float, int, double, ...) are normalized to "number".
    """
    raw = str(field_type or "").strip().lower()
    if not raw:
        return ""
    if raw in _NUMERIC_TYPE_ALIASES:
        return "number"
    if raw in _VALID_FIELD_TYPES:
        return raw
    return None


def _load_yaml_file(path: Path) -> dict:
    """Load a YAML file safely, returning an empty dict on parse failure."""
    if yaml is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
        except (yaml.YAMLError, OSError):
            return {}
    # Minimal fallback: parse ``key: value`` lines.
    result: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def _save_yaml_file(path: Path, data: dict) -> None:
    if yaml is not None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    else:
        lines = []
        for key, value in data.items():
            if isinstance(value, list):
                if value:
                    lines.append(f"{key}:")
                    for item in value:
                        lines.append(f"  - {item}")
                else:
                    lines.append(f"{key}: []")
            else:
                lines.append(f"{key}: {value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ===========================================================================
#  Content extraction helpers
# ===========================================================================


def _extract_title_from_content(text: str) -> str:
    """Extract a title from the first Markdown heading."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _extract_description_from_content(text: str) -> str:
    """Extract a one-line description from the first paragraph after title."""
    lines = text.splitlines()
    found_heading = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            found_heading = True
            continue
        if found_heading and stripped and not stripped.startswith("|"):
            desc = stripped
            for sep in ("\u3002", "\uff1b", ";", ". "):
                if sep in desc:
                    desc = desc.split(sep)[0] + sep
                    break
            return desc[:120]
    return ""


def _extract_first_table_header(path: Path) -> str:
    """Return the first Markdown table header line found in *path*."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("|") and "---" not in stripped:
                # Verify the next line is a table separator row so we do not
                # mistake a data row or a non-table `|` line for a header.
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.startswith("|") and "---" in next_line:
                        return stripped
    except (OSError, UnicodeDecodeError):
        pass
    return ""


# ===========================================================================
#  Format detection
# ===========================================================================

KNOWN_MANIFEST_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "name",
        "description",
        "version",
        "language",
        "status",
        "enabled",
        "scope",
    }
)


def _find_manifest(base: Path) -> Optional[Path]:
    """Locate manifest.yaml under *base*."""
    candidate = base / "manifest.yaml"
    if candidate.exists():
        return candidate
    return None


def detect_package_format(pkg_root: Path) -> PackageFormatProfile:
    """Probe structural signals and build a format profile.

    Args:
        pkg_root: Root directory of the ontology package.

    Returns:
        A :class:`PackageFormatProfile` with signal values and a
        ``conversion_actions`` list describing every transformation the
        importer should apply.

    Raises:
        ValueError: If *pkg_root* is not a directory or manifest.yaml is
            missing.
    """
    if not pkg_root.is_dir():
        raise ValueError(f"Package root is not a directory: {pkg_root}")

    if _is_skill_source_package(pkg_root):
        profile = PackageFormatProfile(manifest_kind="skill-source")
        profile.conversion_actions.append("source: convert SKILL.md/object/logic layout to v0.1")
        return profile

    # The ontology files may live under an ``ontology/`` subdirectory or
    # directly at the package root.
    ontology_dir = pkg_root / "ontology"
    base = ontology_dir if ontology_dir.is_dir() else pkg_root

    profile = PackageFormatProfile()
    profile.ontology_subdir = ontology_dir.is_dir()

    # -- S1 + S2: manifest -------------------------------------------------
    manifest_path = _find_manifest(base)
    if manifest_path is None:
        raise ValueError(f"manifest.yaml not found under {base}")

    manifest = _load_yaml_file(manifest_path)
    profile.manifest_kind = str(manifest.get("kind", ""))
    profile.has_schema_version = "schema_version" in manifest

    if profile.manifest_kind != "ontology-package":
        profile.conversion_actions.append("manifest: fix kind -> ontology-package")
    if not profile.has_schema_version:
        profile.conversion_actions.append("manifest: add schema_version: 0.1")

    # Preserve unknown manifest fields with x_ prefix.
    extra_fields = sorted(set(manifest.keys()) - KNOWN_MANIFEST_FIELDS)
    if extra_fields:
        profile.conversion_actions.append(f"manifest: prefix extra fields with x_: {extra_fields}")

    # -- S3 + S4: root index -----------------------------------------------
    index_md = base / "index.md"
    ontology_md = base / "ONTOLOGY.md"

    if index_md.exists():
        profile.root_index_file = "index.md"
        profile.root_index_has_frontmatter = _has_frontmatter(index_md)
    elif ontology_md.exists():
        profile.root_index_file = "ONTOLOGY.md"
        profile.root_index_has_frontmatter = False
        profile.conversion_actions.append("root: rename ONTOLOGY.md -> index.md")
    else:
        profile.conversion_actions.append("root: create index.md from manifest description")

    if profile.root_index_file == "index.md" and not profile.root_index_has_frontmatter:
        profile.conversion_actions.append("root: add frontmatter (resource: root, key: root, title)")
    if profile.root_index_file == "ONTOLOGY.md":
        profile.conversion_actions.append("root: add frontmatter (resource: root, key: root, title)")

    # -- S5 + S6 + S10: object index, detail, field table ------------------
    obj_index = base / "objects" / "index.md"
    obj_list = base / "objects-list.md"

    if obj_index.exists():
        profile.object_index_file = "objects/index.md"
    elif obj_list.exists():
        profile.object_index_file = "objects-list.md"
        profile.conversion_actions.append("objects: rename objects-list.md -> objects/index.md")
        profile.conversion_actions.append("objects: add frontmatter (resource: object_index, key: objects)")
    else:
        profile.conversion_actions.append("objects: create objects/index.md")

    # Probe the first object detail file.
    objects_dir = base / "objects"
    if objects_dir.is_dir():
        first_obj: Optional[Path] = None
        for p in sorted(objects_dir.glob("*.md")):
            if p.name != "index.md":
                first_obj = p
                break
        if first_obj is not None:
            profile.object_detail_has_frontmatter = _has_frontmatter(first_obj)
            profile.field_table_header = _extract_first_table_header(first_obj)
            if not profile.object_detail_has_frontmatter:
                profile.conversion_actions.append("objects: add frontmatter to all object detail files")

    # -- S7: operation index -----------------------------------------------
    op_index = base / "operations" / "index.md"
    op_list = base / "operations-list.md"

    if op_index.exists():
        profile.operation_index_file = "operations/index.md"
    elif op_list.exists():
        profile.operation_index_file = "operations-list.md"
        profile.conversion_actions.append("operations: rename operations-list.md -> operations/index.md")
        profile.conversion_actions.append("operations: add frontmatter (resource: operation_index, key: operations)")

    # -- S8: relations ------------------------------------------------------
    relations_dir = base / "relations"
    relations_list = base / "relations-list.md"

    if relations_dir.is_dir():
        profile.has_relations_dir = True
        profile.relations_list_file = "relations-list.md" if relations_list.exists() else ""
        profile.conversion_actions.append(
            "relations: remove non-standard relations/ directory (downgrade to object details deferred)"
        )
    if relations_list.exists():
        profile.relations_list_file = "relations-list.md"
        profile.conversion_actions.append("relations: remove non-standard relations-list.md")

    # -- S9: path -----------------------------------------------------------
    if profile.ontology_subdir:
        profile.conversion_actions.append("path: flatten ontology/ subdirectory to package root")

    return profile


# ===========================================================================
#  Conformance validation
# ===========================================================================


def _resolve_base(pkg_root: Path) -> Path:
    """Return the directory that contains manifest.yaml.

    The ontology files may live under an ``ontology/`` subdirectory or
    directly at the package root.
    """
    if (pkg_root / "ontology" / "manifest.yaml").exists():
        return pkg_root / "ontology"
    return pkg_root


def _collect_object_keys(base: Path) -> set[str]:
    """Collect all object keys from objects/*.md frontmatter."""
    keys: set[str] = set()
    objects_dir = base / "objects"
    if not objects_dir.is_dir():
        return keys
    for md_file in objects_dir.glob("*.md"):
        if md_file.name == "index.md":
            continue
        fm = _parse_frontmatter(md_file)
        key = fm.get("key")
        if key:
            keys.add(str(key))
        else:
            # Fall back to filename without extension.
            keys.add(md_file.stem)
    return keys


def _check_manifest(base: Path, result: ValidationResult) -> None:
    """Validate manifest.yaml fields."""
    manifest_path = base / "manifest.yaml"
    if not manifest_path.exists():
        result.errors.append(
            ValidationIssue(
                "error",
                "manifest.exists",
                str(manifest_path),
                "manifest.yaml not found",
            )
        )
        return

    manifest = _load_yaml_file(manifest_path)

    # kind
    kind = manifest.get("kind", "")
    if kind != "ontology-package":
        result.errors.append(
            ValidationIssue(
                "error",
                "manifest.kind",
                str(manifest_path),
                f"kind must be 'ontology-package', got '{kind}'",
            )
        )

    # schema_version
    sv = manifest.get("schema_version")
    if sv is None:
        result.errors.append(
            ValidationIssue(
                "error",
                "manifest.schema_version",
                str(manifest_path),
                "schema_version is missing, expected '0.1'",
            )
        )
    elif str(sv) != "0.1":
        result.errors.append(
            ValidationIssue(
                "error",
                "manifest.schema_version",
                str(manifest_path),
                f"schema_version must be '0.1', got '{sv}'",
            )
        )

    # required non-empty fields
    for field_name in ("id", "name", "description", "version"):
        value = manifest.get(field_name)
        if value is None or str(value).strip() == "":
            result.errors.append(
                ValidationIssue(
                    "error",
                    f"manifest.{field_name}",
                    str(manifest_path),
                    f"{field_name} is missing or empty",
                )
            )

    # id format: only [A-Za-z0-9._-], must start with alphanumeric.
    raw_id = str(manifest.get("id", "")).strip()
    if raw_id and not _MANIFEST_ID_RE.match(raw_id):
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "-", raw_id)
        sanitized = re.sub(r"\A[^A-Za-z0-9]+", "", sanitized) or "package"
        result.errors.append(
            ValidationIssue(
                "error",
                "manifest.id_format",
                str(manifest_path),
                f"id '{raw_id}' 不合法，仅支持字母数字下划线点号短横线，且需字母或数字开头；建议改为 '{sanitized}'",
            )
        )


def _check_root_index(base: Path, result: ValidationResult) -> None:
    """Validate root index.md."""
    index_path = base / "index.md"
    if not index_path.exists():
        result.errors.append(
            ValidationIssue(
                "error",
                "root_index.exists",
                str(index_path),
                "index.md not found at package root",
            )
        )
        return

    if not _has_frontmatter(index_path):
        result.errors.append(
            ValidationIssue(
                "error",
                "root_index.frontmatter",
                str(index_path),
                "index.md must have YAML frontmatter",
            )
        )
        return

    fm = _parse_frontmatter(index_path)
    if fm.get("resource") != "root":
        result.errors.append(
            ValidationIssue(
                "error",
                "root_index.resource",
                str(index_path),
                f"frontmatter resource must be 'root', got '{fm.get('resource')}'",
            )
        )
    if fm.get("key") != "root":
        result.errors.append(
            ValidationIssue(
                "error",
                "root_index.key",
                str(index_path),
                f"frontmatter key must be 'root', got '{fm.get('key')}'",
            )
        )


def _check_object_index(base: Path, result: ValidationResult) -> None:
    """Validate objects/index.md."""
    index_path = base / "objects" / "index.md"
    if not index_path.exists():
        result.errors.append(
            ValidationIssue(
                "error",
                "object_index.exists",
                str(index_path),
                "objects/index.md not found",
            )
        )
        return

    if not _has_frontmatter(index_path):
        result.errors.append(
            ValidationIssue(
                "error",
                "object_index.frontmatter",
                str(index_path),
                "objects/index.md must have YAML frontmatter",
            )
        )
        return

    fm = _parse_frontmatter(index_path)
    if fm.get("resource") != "object_index":
        result.errors.append(
            ValidationIssue(
                "error",
                "object_index.resource",
                str(index_path),
                f"frontmatter resource must be 'object_index', got '{fm.get('resource')}'",
            )
        )


def _check_object_details(base: Path, result: ValidationResult) -> None:
    """Validate each objects/*.md detail file has required frontmatter."""
    objects_dir = base / "objects"
    if not objects_dir.is_dir():
        return

    for md_file in sorted(objects_dir.glob("*.md")):
        if md_file.name == "index.md":
            continue

        if not _has_frontmatter(md_file):
            result.errors.append(
                ValidationIssue(
                    "error",
                    "object_detail.frontmatter",
                    str(md_file),
                    f"{md_file.name} must have YAML frontmatter (resource: object, key, title)",
                )
            )
            continue

        fm = _parse_frontmatter(md_file)
        for required in ("resource", "key", "title"):
            if not fm.get(required):
                result.errors.append(
                    ValidationIssue(
                        "error",
                        f"object_detail.{required}",
                        str(md_file),
                        f"{md_file.name} frontmatter missing required field: {required}",
                    )
                )
        if fm.get("resource") and fm.get("resource") != "object":
            result.errors.append(
                ValidationIssue(
                    "error",
                    "object_detail.resource",
                    str(md_file),
                    f"{md_file.name} resource must be 'object', got '{fm.get('resource')}'",
                )
            )


def _check_operation_index(base: Path, result: ValidationResult) -> None:
    """Validate operations/index.md."""
    index_path = base / "operations" / "index.md"
    if not index_path.exists():
        result.errors.append(
            ValidationIssue(
                "error",
                "operation_index.exists",
                str(index_path),
                "operations/index.md not found",
            )
        )
        return

    if not _has_frontmatter(index_path):
        result.errors.append(
            ValidationIssue(
                "error",
                "operation_index.frontmatter",
                str(index_path),
                "operations/index.md must have YAML frontmatter",
            )
        )
        return

    fm = _parse_frontmatter(index_path)
    if fm.get("resource") != "operation_index":
        result.errors.append(
            ValidationIssue(
                "error",
                "operation_index.resource",
                str(index_path),
                f"frontmatter resource must be 'operation_index', got '{fm.get('resource')}'",
            )
        )


def _check_operation_details(base: Path, result: ValidationResult) -> None:
    """Validate each operations/*.md detail file."""
    ops_dir = base / "operations"
    if not ops_dir.is_dir():
        return

    # Collect all object keys for uses.objects reference checking.
    object_keys = _collect_object_keys(base)

    for md_file in sorted(ops_dir.glob("*.md")):
        if md_file.name == "index.md":
            continue

        if not _has_frontmatter(md_file):
            result.errors.append(
                ValidationIssue(
                    "error",
                    "operation_detail.frontmatter",
                    str(md_file),
                    f"{md_file.name} must have YAML frontmatter (resource: operation, key, title, uses.objects)",
                )
            )
            continue

        fm = _parse_frontmatter(md_file)
        for required in ("resource", "key", "title"):
            if not fm.get(required):
                result.errors.append(
                    ValidationIssue(
                        "error",
                        f"operation_detail.{required}",
                        str(md_file),
                        f"{md_file.name} frontmatter missing required field: {required}",
                    )
                )
        if fm.get("resource") and fm.get("resource") != "operation":
            result.errors.append(
                ValidationIssue(
                    "error",
                    "operation_detail.resource",
                    str(md_file),
                    f"{md_file.name} resource must be 'operation', got '{fm.get('resource')}'",
                )
            )

        # uses.objects presence
        uses = fm.get("uses")
        if not isinstance(uses, dict) or "objects" not in uses:
            result.errors.append(
                ValidationIssue(
                    "error",
                    "operation_detail.uses_objects",
                    str(md_file),
                    f"{md_file.name} must declare uses.objects (use empty array [] if no objects)",
                )
            )
        else:
            uses_objects = uses.get("objects", [])
            if not isinstance(uses_objects, list):
                result.errors.append(
                    ValidationIssue(
                        "error",
                        "operation_detail.uses_objects_type",
                        str(md_file),
                        f"{md_file.name} uses.objects must be a list",
                    )
                )
            else:
                # Warning: referenced object keys should exist.
                for ref_key in uses_objects:
                    if ref_key not in object_keys:
                        result.warnings.append(
                            ValidationIssue(
                                "warning",
                                "operation_detail.uses_objects_ref",
                                str(md_file),
                                f"{md_file.name} uses.objects references '{ref_key}' which is not found in objects/",
                            )
                        )


def _check_relations(base: Path, result: ValidationResult) -> None:
    """Warn about non-standard relations/ content (not part of the design format).

    The ontology-package design format (v0.1) has no ``relations/`` directory.
    If one exists, it should be downgraded to inline object detail tables.
    """
    relations_dir = base / "relations"
    relations_list = base / "relations-list.md"

    if relations_dir.is_dir():
        result.warnings.append(
            ValidationIssue(
                "warning",
                "relations.nonstandard",
                str(relations_dir),
                "relations/ directory is not part of the v0.1 ontology-package "
                "format. Downgrade to object detail tables is deferred.",
            )
        )
    if relations_list.exists():
        result.warnings.append(
            ValidationIssue(
                "warning",
                "relations.nonstandard_list",
                str(relations_list),
                "relations-list.md is not part of the v0.1 ontology-package "
                "format. Downgrade to object detail tables is deferred.",
            )
        )


@dataclass
class MappingCheckContext:
    """Context for validating a single mapping entry."""

    mapping_path: Path
    category: str
    resource_key: str
    default_alias: str


def _check_mapping_entry(
    ctx: MappingCheckContext,
    entry: object,
    result: ValidationResult,
) -> None:
    """Validate one <category>.<resource_key> mapping entry.

    Enforces the v0.1 object-data-mappings field contract so converted packages
    carry valid, consistent mappings.
    """
    if not isinstance(entry, dict):
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.entry_structure",
                str(ctx.mapping_path),
                f"{ctx.category}.{ctx.resource_key} must be a JSON object.",
            )
        )
        return

    fields = entry.get("fields")
    if not isinstance(fields, dict):
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.fields_type",
                str(ctx.mapping_path),
                f"{ctx.category}.{ctx.resource_key}.fields must be a JSON object.",
            )
        )
        return

    provider_alias = str(entry.get("provider_alias") or "").strip()
    if not provider_alias and not ctx.default_alias:
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.provider_alias",
                str(ctx.mapping_path),
                f"{ctx.category}.{ctx.resource_key} requires provider_alias or a default_provider_alias.",
            )
        )

    resource = str(entry.get("resource") or "").strip()
    if not resource:
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.resource",
                str(ctx.mapping_path),
                f"{ctx.category}.{ctx.resource_key}.resource is required.",
            )
        )

    primary_key = entry.get("primary_key")
    if isinstance(primary_key, list):
        for pk_field in primary_key:
            pk = str(pk_field or "").strip()
            if pk and pk not in fields:
                result.errors.append(
                    ValidationIssue(
                        "error",
                        "mappings.primary_key_ref",
                        str(ctx.mapping_path),
                        f"{ctx.category}.{ctx.resource_key}.primary_key references '{pk}' which is not in fields.",
                    )
                )

    for field_key, field_def in fields.items():
        path_prefix = f"{ctx.category}.{ctx.resource_key}.fields.{field_key}"
        if not isinstance(field_def, dict):
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.field_structure",
                    str(ctx.mapping_path),
                    f"{path_prefix} must be a JSON object.",
                )
            )
            continue

        source = str(field_def.get("source") or "").strip()
        if not source:
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.field_source",
                    str(ctx.mapping_path),
                    f"{path_prefix}.source is required.",
                )
            )

        field_type = field_def.get("type")
        normalized_type = _normalize_field_type(field_type)
        if field_type and normalized_type is None:
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.field_type",
                    str(ctx.mapping_path),
                    f"{path_prefix}.type '{field_type}' is not a valid type.",
                )
            )

        for flag in ("readable", "required"):
            value = field_def.get(flag)
            if value is not None and not isinstance(value, bool):
                result.errors.append(
                    ValidationIssue(
                        "error",
                        f"mappings.field_{flag}",
                        str(ctx.mapping_path),
                        f"{path_prefix}.{flag} must be a boolean.",
                    )
                )

        if field_key in _PROVIDER_META_FIELDS:
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.field_metadata",
                    str(ctx.mapping_path),
                    f"{path_prefix} is a provider metadata field and should not be in fields.",
                )
            )

    policy = entry.get("policy")
    if isinstance(policy, dict):
        read_only = policy.get("read_only")
        if read_only is not None and not isinstance(read_only, bool):
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.policy_read_only",
                    str(ctx.mapping_path),
                    f"{ctx.category}.{ctx.resource_key}.policy.read_only must be a boolean.",
                )
            )
        max_rows = policy.get("max_rows")
        max_rows_is_positive_int = isinstance(max_rows, int) and not isinstance(max_rows, bool) and max_rows > 0
        if max_rows is not None and not max_rows_is_positive_int:
            result.errors.append(
                ValidationIssue(
                    "error",
                    "mappings.policy_max_rows",
                    str(ctx.mapping_path),
                    f"{ctx.category}.{ctx.resource_key}.policy.max_rows must be a positive integer.",
                )
            )
        require_user_filter = policy.get("require_user_filter")
        if isinstance(require_user_filter, list):
            for uf_field in require_user_filter:
                uf = str(uf_field or "").strip()
                if uf and uf not in fields:
                    result.errors.append(
                        ValidationIssue(
                            "error",
                            "mappings.require_user_filter_ref",
                            str(ctx.mapping_path),
                            f"{ctx.category}.{ctx.resource_key}.policy.require_user_filter "
                            f"references '{uf}' which is not in fields.",
                        )
                    )


def _check_mappings(base: Path, result: ValidationResult) -> None:
    """Validate mappings/object-data-mappings.json if it exists."""
    mapping_path = base / "mappings" / "object-data-mappings.json"
    if not mapping_path.exists():
        return  # mappings are conditionally required, not an error

    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.json",
                str(mapping_path),
                f"object-data-mappings.json is not valid JSON: {exc}",
            )
        )
        return

    if not isinstance(mapping, dict):
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.structure",
                str(mapping_path),
                "object-data-mappings.json must contain a JSON object",
            )
        )
        return

    sv = mapping.get("schema_version")
    if sv != "0.1":
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.schema_version",
                str(mapping_path),
                f"schema_version must be '0.1', got '{sv}'",
            )
        )

    default_alias = str(mapping.get("default_provider_alias") or "").strip()
    if "default_provider_alias" in mapping and not default_alias:
        result.errors.append(
            ValidationIssue(
                "error",
                "mappings.default_provider_alias",
                str(mapping_path),
                "default_provider_alias must be a non-empty string.",
            )
        )

    for category in ("objects", "metrics", "rules"):
        items = mapping.get(category)
        if not isinstance(items, dict):
            continue
        for resource_key, entry in items.items():
            ctx = MappingCheckContext(
                mapping_path=mapping_path,
                category=category,
                resource_key=resource_key,
                default_alias=default_alias,
            )
            _check_mapping_entry(ctx, entry, result)

    # Warning: mapping object keys should match objects/ keys.
    object_keys = _collect_object_keys(base)
    mapping_objects = mapping.get("objects", {})
    if isinstance(mapping_objects, dict):
        for mkey in mapping_objects:
            if mkey not in object_keys:
                result.warnings.append(
                    ValidationIssue(
                        "warning",
                        "mappings.key_consistency",
                        str(mapping_path),
                        f"mapping object key '{mkey}' has no corresponding object in objects/",
                    )
                )


def _check_index_detail_consistency(base: Path, result: ValidationResult) -> None:
    """Check that index.md files and detail files are consistent.

    - Every detail file declared in an index should exist.
    - Every detail file should be declared in its index.
    """
    for subdir, label in [
        ("objects", "object"),
        ("operations", "operation"),
    ]:
        index_path = base / subdir / "index.md"
        if not index_path.exists():
            continue

        try:
            index_text = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Extract detail filenames mentioned in the index.
        # Look for patterns like `objects/Foo.md` or `| Foo |` table rows.
        # We check file links and table cells that look like keys.
        detail_dir = base / subdir
        actual_files = {f.name for f in detail_dir.glob("*.md") if f.name != "index.md"}

        # Simple heuristic: extract all .md references from index text.
        referenced = set()
        for match in re.finditer(rf"{subdir}/([^\s)|]+\.md)", index_text):
            referenced.add(match.group(1))

        # Also extract keys from table rows (first column after |).
        for line in index_text.splitlines():
            line = line.strip()
            if line.startswith("|") and "---" not in line:
                cells = [c.strip() for c in line.split("|")]
                cells = [c for c in cells if c]  # remove empties
                if cells and cells[0] not in ("object key", "operation key", "key", "---"):
                    # This could be a key; check if a file with that
                    # name exists.
                    potential_name = f"{cells[0]}.md"
                    if potential_name in actual_files:
                        referenced.add(potential_name)

        # Check: detail files not in index -> warning
        for fname in actual_files - referenced:
            result.warnings.append(
                ValidationIssue(
                    "warning",
                    f"{label}_index.consistency",
                    str(detail_dir / fname),
                    f"{fname} exists but is not referenced in {subdir}/index.md",
                )
            )


def validate_package(pkg_root: Path) -> ValidationResult:
    """Validate a package directory against the ontology-package spec.

    Args:
        pkg_root: Root directory of the ontology package.

    Returns:
        A :class:`ValidationResult` with errors and warnings.

    Raises:
        ValueError: If *pkg_root* is not a directory.
    """
    if not pkg_root.is_dir():
        raise ValueError(f"Package root is not a directory: {pkg_root}")

    base = _resolve_base(pkg_root)
    result = ValidationResult(package_root=str(pkg_root), is_conformant=False)

    _check_manifest(base, result)
    _check_root_index(base, result)
    _check_object_index(base, result)
    _check_object_details(base, result)
    _check_operation_index(base, result)
    _check_operation_details(base, result)
    _check_relations(base, result)
    _check_mappings(base, result)
    _check_index_detail_consistency(base, result)

    result.is_conformant = len(result.errors) == 0
    return result


# ===========================================================================
#  Mapping helpers
# ===========================================================================


def _find_field_key_by_source(fields: dict, source: str) -> Optional[str]:
    """Return the field key whose ``source`` equals *source*.

    Returns None when no field or more than one field matches.
    """
    matched: list[str] = []
    for key, field_def in fields.items():
        if not isinstance(field_def, dict):
            continue
        if str(field_def.get("source") or "").strip() == source:
            matched.append(key)
    if len(matched) == 1:
        return matched[0]
    return None


def _strip_empty_key_field_rows(text: str) -> tuple[str, bool]:
    """Remove markdown table rows where the key column is empty.

    Legacy object files may contain descriptive placeholder rows in field
    tables (e.g. '| 继承自MPart的各个属性 |  |  |') where the first column
    (display name) has text but the second column (field key) is empty.
    These rows cause backend alias validation to reject the package.

    Only rows whose third and later columns are all empty are removed, so a
    row with an empty key but a populated type column is preserved.

    Returns (cleaned_text, did_strip).
    """
    lines = text.splitlines()
    result: list[str] = []
    did_strip = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        prev = "" if i == 0 else lines[i - 1].strip()
        # Table data row: starts with |, not a separator, and the previous
        # line is a table row (header separator or a data row) -- this
        # skips header rows and tables embedded in prose.
        is_table_data_row = stripped.startswith("|") and "---" not in stripped and prev.startswith("|")
        if is_table_data_row:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            is_placeholder_row = len(cells) >= 2 and cells[0] and not cells[1] and not any(cells[2:])
            # Key (second column) empty, display name filled, rest empty
            if is_placeholder_row:
                did_strip = True
                i += 1
                continue
        result.append(line)
        i += 1
    return ("\n".join(result), did_strip)


# ===========================================================================
#  SKILL.md source-format conversion
# ===========================================================================

_SOURCE_OBJECT_SECTION_TITLES = ("对象属性清单", "实体属性清单")
_SOURCE_OBJECT_DESC_TITLES = ("对象描述", "实体描述")
_SOURCE_OBJECT_ENGLISH_TITLES = ("英文名称", "实体英文名称")
_SOURCE_OBJECT_ALIAS_TITLES = ("对象别名", "实体别名")
_SOURCE_OPERATION_DESC_TITLE = "业务描述"
_SOURCE_OPERATION_LOGIC_RELATION_TITLE = "与logic的关联"
_SOURCE_OPERATION_TRIGGER_TITLE = "触发条件"
_SOURCE_OPERATION_INPUT_TITLE = "输入"
_SOURCE_OPERATION_OUTPUT_TITLE = "输出"
_SOURCE_OPERATION_LOGIC_TITLE = "逻辑描述"
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]*(.+?)[ \t]*$")
_MARKDOWN_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")


def _is_skill_source_package(pkg_root: Path) -> bool:
    """Return whether *pkg_root* uses the legacy SKILL.md source layout."""
    try:
        if not pkg_root.is_dir():
            return False
        if (pkg_root / "manifest.yaml").is_file():
            return False
        if not (pkg_root / "SKILL.md").is_file():
            return False
        return any((pkg_root / folder).is_dir() for folder in ("object", "logic"))
    except OSError:
        return False


def _extract_markdown_section(
    text: str,
    titles: Sequence[str],
) -> list[str]:
    """Extract the body of the first Markdown section matching *titles*."""
    title_set = {title.strip() for title in titles if title.strip()}
    if not title_set:
        return []

    lines = text.splitlines()
    start = -1
    start_level = 0
    for index, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if not match:
            continue
        if match.group(2).strip() not in title_set:
            continue
        start = index + 1
        start_level = len(match.group(1))
        break
    if start < 0:
        return []

    end = len(lines)
    for index in range(start, len(lines)):
        match = _MARKDOWN_HEADING_RE.match(lines[index])
        if match and len(match.group(1)) <= start_level:
            end = index
            break
    return lines[start:end]


def _section_first_text(text: str, titles: Sequence[str]) -> str:
    """Return the first non-empty line from a named Markdown section."""
    for line in _extract_markdown_section(text, titles):
        stripped = line.strip()
        if stripped and not stripped.startswith("{说明："):
            return stripped
    return ""


def _section_joined_text(text: str, titles: Sequence[str], joiner: str = "，") -> str:
    """Return the non-empty text lines from a named Markdown section."""
    lines = [
        line.strip()
        for line in _extract_markdown_section(text, titles)
        if line.strip() and not line.strip().startswith("{说明：")
    ]
    return joiner.join(lines)


def _extract_markdown_table(section: Sequence[str]) -> list[list[str]]:
    """Return the first Markdown table in *section* as header plus rows."""
    lines = [line.strip() for line in section]
    for index in range(len(lines) - 1):
        header = lines[index]
        separator = lines[index + 1]
        if not header.startswith("|") or not separator.startswith("|"):
            continue
        header_cells = [cell.strip() for cell in header.strip("|").split("|")]
        separator_cells = [cell.strip() for cell in separator.strip("|").split("|")]
        if not header_cells or len(header_cells) != len(separator_cells):
            continue
        if not all(_MARKDOWN_SEPARATOR_CELL_RE.match(cell.replace(" ", "")) for cell in separator_cells):
            continue

        rows = [header_cells]
        for row_line in lines[index + 2 :]:
            if not row_line.startswith("|"):
                break
            rows.append([cell.strip() for cell in row_line.strip("|").split("|")])
        return rows
    return []


def _filter_markdown_table(
    table: Sequence[Sequence[str]],
    columns: Sequence[str],
) -> list[list[str]]:
    """Keep only *columns* from a Markdown table represented as rows."""
    if not table:
        return [list(columns)]
    headers = list(table[0])
    indexes = {column: headers.index(column) for column in columns if column in headers}
    result = [list(columns)]
    for row in table[1:]:
        result.append(
            [row[indexes[column]] if column in indexes and indexes[column] < len(row) else "" for column in columns]
        )
    return result


def _build_markdown_table(rows: Sequence[Sequence[str]]) -> str:
    """Render header-plus-data rows as a Markdown table."""
    if not rows:
        return ""
    lines = ["| " + " | ".join(rows[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
    for row in rows[1:]:
        escaped = [cell.replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines)


def _split_source_alias_values(value: str) -> list[str]:
    """Split source alias text and drop explicit empty markers."""
    result: list[str] = []
    for item in re.split(r"[,，、;/；]", value or ""):
        cleaned = item.strip()
        if cleaned and cleaned != "无" and cleaned not in result:
            result.append(cleaned)
    return result


def _sanitize_field_alias_cell(value: str, claimed: set[str]) -> str:
    """Drop placeholder aliases and cross-field duplicates."""
    kept: list[str] = []
    for alias in _split_source_alias_values(value):
        compare = alias.strip().lower()
        if not compare or compare in claimed:
            continue
        claimed.add(compare)
        kept.append(alias)
    return "，".join(kept)


def _render_source_object(
    path: Path,
    text: Optional[str] = None,
    errors: Optional[list[str]] = None,
) -> str:
    """Render a source object document as a v0.1 object detail file."""
    if text is None:
        text = path.read_text(encoding="utf-8")
    text, _ = _strip_empty_key_field_rows(text)
    table = _extract_markdown_table(_extract_markdown_section(text, _SOURCE_OBJECT_SECTION_TITLES))
    filtered = _filter_markdown_table(table, ("中文名称", "英文名称", "描述", "属性别名"))
    kept_rows: list[list[str]] = []
    missing_key_names: list[str] = []
    for row in filtered[1:]:
        if not (row[0].strip() or row[1].strip() or row[3].strip()):
            continue
        if not row[1].strip() and (row[0].strip() or row[3].strip()):
            missing_key_names.append(row[0].strip() or row[3].strip())
        kept_rows.append(row)
    if missing_key_names and errors is not None:
        errors.append(
            "source: {} contains field rows without an English name: {}".format(path.name, "、".join(missing_key_names))
        )
    filtered = [filtered[0]] + kept_rows
    claimed_aliases: set[str] = set()
    for row in filtered[1:]:
        row[3] = _sanitize_field_alias_cell(row[3], claimed_aliases)
    body = "# 实体属性清单\n\n{}\n".format(_build_markdown_table(filtered))
    aliases = _split_source_alias_values(_section_joined_text(text, _SOURCE_OBJECT_ALIAS_TITLES))
    frontmatter: dict[str, object] = {
        "resource": "object",
        "key": path.stem,
        "title": path.stem,
    }
    if aliases:
        frontmatter["aliases"] = aliases
    return _build_frontmatter(frontmatter) + "\n" + body


def _source_operation_tag(text: str) -> str:
    """Infer the operation tag from the source logic relation section."""
    for line in _extract_markdown_section(text, (_SOURCE_OPERATION_LOGIC_RELATION_TITLE,)):
        if "被logic调用" not in line and "被流程使用" not in line:
            continue
        value = ""
        if ">" in line:
            value = line.split(">", 1)[1].strip()
        elif "：" in line:
            value = line.split("：", 1)[1].strip()
        elif ":" in line:
            value = line.split(":", 1)[1].strip()
        if value and not value.startswith("无"):
            return "子流程"
    return "主流程"


def _render_source_operation(path: Path) -> str:
    """Render a source logic document as a v0.1 operation detail file."""
    text = path.read_text(encoding="utf-8")
    tag = _source_operation_tag(text)
    input_section = _extract_markdown_section(text, (_SOURCE_OPERATION_INPUT_TITLE,))
    output_section = _extract_markdown_section(text, (_SOURCE_OPERATION_OUTPUT_TITLE,))
    logic_section = _extract_markdown_section(text, (_SOURCE_OPERATION_LOGIC_TITLE,))

    def render_section(lines: Sequence[str]) -> str:
        return "\n".join(line.rstrip() for line in lines if line.strip()).strip()

    blocks = [
        "# 管理标签\n#{}".format(tag),
        "# 输入\n\n{}".format(render_section(input_section) or "无"),
        "# 输出\n\n{}".format(render_section(output_section) or "无"),
        "# 逻辑描述\n\n{}".format(render_section(logic_section) or "无"),
    ]
    frontmatter: dict[str, object] = {
        "resource": "operation",
        "key": path.stem,
        "title": path.stem,
        "uses": {"objects": []},
    }
    return _build_frontmatter(frontmatter) + "\n" + "\n\n".join(blocks) + "\n"


def _write_support_index(path: Path, resource: str, key: str, title: str) -> None:
    """Write a minimal v0.1 support directory index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _build_frontmatter(
            {
                "resource": resource,
                "key": key,
                "title": title,
            }
        )
        + "\n# {}\n".format(title),
        encoding="utf-8",
    )


_SOURCE_SCRIPT_BOUND_RE = re.compile(r"^\s*#\s*(?:工具流程|关联logic)\s*[：:]\s*(.+?)\s*$")


def _source_script_dir(pkg_root: Path) -> Optional[Path]:
    """Locate the source script directory, preferring ``scripts/``."""
    for name in ("scripts", "action"):
        candidate = pkg_root / name
        if candidate.is_dir():
            return candidate
    return None


def _source_asset_dir(pkg_root: Path) -> Optional[Path]:
    """Locate the source asset directory, preferring ``asset/``."""
    for name in ("asset", "assets"):
        candidate = pkg_root / name
        if candidate.is_dir():
            return candidate
    return None


def _source_script_operation(source_file: Path, logic_keys: set[str]) -> str:
    """Return the bound logic name from a script header comment."""
    try:
        lines = source_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines[:200]:
        match = _SOURCE_SCRIPT_BOUND_RE.match(line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if candidate in logic_keys:
            return candidate
    return ""


def _source_script_key(candidate: str, used: set[str]) -> str:
    """Return a unique script descriptor key for the payload file."""
    base = candidate.strip() or "script"
    if base.lower() == "index":
        base = "script"
    key = base
    index = 2
    while key in used:
        key = "{}_{}".format(base, index)
        index += 1
    used.add(key)
    return key


_SOURCE_COPY_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
    }
)
_SOURCE_COPY_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", ".gitignore"})


def _copy_source_files(source_dir: Path, target_dir: Path) -> list[Path]:
    """Copy *source_dir* content into *target_dir* preserving structure."""
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for source_file in sorted(
        source_dir.rglob("*"),
        key=lambda item: str(item.relative_to(source_dir)).lower(),
    ):
        if not source_file.is_file() or source_file.is_symlink():
            continue
        relative = source_file.relative_to(source_dir)
        if any(part in _SOURCE_COPY_SKIP_DIRS for part in relative.parts):
            continue
        if source_file.name in _SOURCE_COPY_SKIP_NAMES:
            continue
        if source_file.suffix.lower() == ".pyc":
            continue
        target_file = target_dir / relative
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        copied.append(target_file)
    return copied


def _convert_source_scripts(
    pkg_root: Path,
    output_root: Path,
    logic_keys: set[str],
) -> tuple[int, list[str]]:
    """Convert source action/scripts payloads into v0.1 scripts layout."""
    source_dir = _source_script_dir(pkg_root)
    if source_dir is None:
        return 0, []
    payload_files = _copy_source_files(source_dir, output_root / "scripts")
    script_dir = output_root / "scripts"
    used_keys: set[str] = set()
    rows: list[list[str]] = []
    descriptors: list[Path] = []
    for payload_file in sorted(
        script_dir.rglob("*"),
        key=lambda item: str(item.relative_to(script_dir)).lower(),
    ):
        if not payload_file.is_file() or payload_file.suffix.lower() != ".py":
            continue
        if payload_file.stem.startswith("_"):
            continue
        relative = payload_file.relative_to(script_dir).as_posix()
        operation_key = _source_script_operation(payload_file, logic_keys)
        if not operation_key:
            continue
        key = _source_script_key(payload_file.stem, used_keys)
        descriptor = script_dir / "{}.md".format(key)
        descriptor.write_text(
            _build_frontmatter(
                {
                    "resource": "script",
                    "key": key,
                    "title": key,
                    "bound_operation": operation_key,
                    "runtime": "python",
                    "entrypoint": relative,
                }
            )
            + "\n# 输入参数\n\n{}\n".format(
                _build_markdown_table(
                    [
                        ["参数 key", "CLI 参数", "类型", "必填", "是否多值", "说明", "别名", "Action"],
                    ]
                )
            ),
            encoding="utf-8",
        )
        descriptors.append(descriptor)
        rows.append([key, key, operation_key, relative])
    script_dir.joinpath("index.md").write_text(
        _build_frontmatter(
            {
                "resource": "script_index",
                "key": "scripts",
                "title": "脚本目录",
            }
        )
        + "\n# 脚本目录\n\n{}\n".format(
            _build_markdown_table(
                [
                    ["script key", "标题", "绑定 operation", "入口"],
                    *rows,
                ]
            )
        ),
        encoding="utf-8",
    )
    return len(payload_files), [str(path) for path in descriptors]


def _convert_source_assets(pkg_root: Path, output_root: Path) -> int:
    """Convert source asset files into the v0.1 ``assets/`` layout."""
    source_dir = _source_asset_dir(pkg_root)
    if source_dir is None:
        return 0
    return len(_copy_source_files(source_dir, output_root / "assets"))


def _convert_skill_source_package(
    pkg_root: Path,
    output_root: Path,
) -> ConversionResult:
    """Convert a legacy SKILL.md source package to the v0.1 layout."""
    if not pkg_root.is_dir():
        raise ValueError(f"Source is not a directory: {pkg_root}")
    if output_root.exists():
        raise ValueError(f"Output directory already exists: {output_root}")

    result = ConversionResult(
        source_root=str(pkg_root),
        output_root=str(output_root),
    )
    metadata = _parse_frontmatter(pkg_root / "SKILL.md")
    for field_name in ("id", "name", "description", "version"):
        if not str(metadata.get(field_name) or "").strip():
            result.errors.append(f"source: SKILL.md missing {field_name}")
    ontology_id = str(metadata.get("id") or "").strip()
    if ontology_id and not _MANIFEST_ID_RE.match(ontology_id):
        result.errors.append("source: SKILL.md id must contain only letters, digits, dots, underscores, or hyphens")
    if result.errors:
        return result

    manifest = {
        "kind": "ontology-package",
        "schema_version": "0.1",
        "id": ontology_id,
        "name": str(metadata["name"]),
        "description": str(metadata["description"]),
        "version": str(metadata["version"]),
        "language": str(metadata.get("language") or "zh-CN"),
    }
    output_root.mkdir(parents=True)
    _save_yaml_file(output_root / "manifest.yaml", manifest)
    output_root.joinpath("index.md").write_text(
        _build_frontmatter(
            {
                "resource": "root",
                "key": "root",
                "title": manifest["name"],
            }
        )
        + "\n# {}\n\n{}\n".format(manifest["name"], manifest["description"]),
        encoding="utf-8",
    )

    object_files = sorted((pkg_root / "object").rglob("*.md"))
    objects_dir = output_root / "objects"
    objects_dir.mkdir()
    object_rows: list[list[str]] = []
    for source_file in object_files:
        target_file = objects_dir / source_file.name
        if target_file.exists():
            result.errors.append(f"source: duplicate object filename: {source_file.name}")
            continue
        text = source_file.read_text(encoding="utf-8")
        target_file.write_text(
            _render_source_object(source_file, text=text, errors=result.errors),
            encoding="utf-8",
        )
        object_rows.append(
            [
                source_file.stem,
                _section_first_text(text, _SOURCE_OBJECT_ENGLISH_TITLES),
                _section_joined_text(text, _SOURCE_OBJECT_ALIAS_TITLES),
                _section_first_text(text, _SOURCE_OBJECT_DESC_TITLES),
                "",
            ]
        )

    objects_dir.joinpath("index.md").write_text(
        _build_frontmatter(
            {
                "resource": "object_index",
                "key": "objects",
                "title": "对象目录",
            }
        )
        + "\n# 实体清单\n\n{}\n".format(
            _build_markdown_table(
                [
                    ["中文名称", "英文名称", "别名", "业务定义", "领域分类"],
                    *object_rows,
                ]
            )
        ),
        encoding="utf-8",
    )

    operation_files = sorted((pkg_root / "logic").rglob("*.md"))
    logic_keys = {path.stem for path in operation_files}
    operations_dir = output_root / "operations"
    operations_dir.mkdir()
    operation_rows: list[list[str]] = []
    for source_file in operation_files:
        target_file = operations_dir / source_file.name
        if target_file.exists():
            result.errors.append(f"source: duplicate logic filename: {source_file.name}")
            continue
        target_file.write_text(_render_source_operation(source_file), encoding="utf-8")
        text = source_file.read_text(encoding="utf-8")
        operation_rows.append(
            [
                source_file.stem,
                _source_operation_tag(text),
                _section_first_text(text, (_SOURCE_OPERATION_DESC_TITLE,)),
                _section_first_text(text, (_SOURCE_OPERATION_TRIGGER_TITLE,)),
            ]
        )

    operations_dir.joinpath("index.md").write_text(
        _build_frontmatter(
            {
                "resource": "operation_index",
                "key": "operations",
                "title": "操作目录",
            }
        )
        + "\n# Operations 逻辑清单\n\n{}\n".format(
            _build_markdown_table(
                [
                    ["名称", "标签", "业务描述", "触发条件"],
                    *operation_rows,
                ]
            )
        ),
        encoding="utf-8",
    )

    script_count, script_descriptors = _convert_source_scripts(pkg_root, output_root, logic_keys)
    asset_count = _convert_source_assets(pkg_root, output_root)
    ignored_files = []
    for path in pkg_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.relative_to(pkg_root).parts[0] in {
            "object",
            "logic",
            "scripts",
            "action",
            "asset",
            "assets",
            "SKILL.md",
        }:
            continue
        ignored_files.append(str(path.relative_to(pkg_root)).replace("\\", "/"))
    ignored_files.sort()

    _write_support_index(output_root / "rules" / "index.md", "rule_index", "rules", "规则目录")
    _write_support_index(
        output_root / "metrics" / "index.md",
        "metric_index",
        "metrics",
        "指标目录",
    )
    mappings_dir = output_root / "mappings"
    mappings_dir.mkdir()
    mappings_dir.joinpath("object-data-mappings.json").write_text(
        json.dumps(
            {"schema_version": "0.1", "objects": {}},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result.actions_applied.extend(
        [
            "source: generated manifest.yaml and index.md from SKILL.md",
            f"source: converted {len(object_rows)} object documents",
            f"source: converted {len(operation_rows)} logic documents",
            f"source: copied {script_count} script payload files",
            f"source: generated {len(script_descriptors)} script descriptors",
            f"source: copied {asset_count} asset files",
            "source: uses.objects left empty (logic-object relations are not inferred)",
            "source: generated rules, metrics, and mappings support files",
        ]
    )
    if ignored_files:
        result.actions_applied.append(
            "source: ignored {} file(s) outside converted directories: {}".format(
                len(ignored_files), "、".join(ignored_files[:5])
            )
        )
    result.success = len(result.errors) == 0
    return result


# ===========================================================================
#  Package converter
# ===========================================================================


class PackageConverter:
    """Applies format conversions to a package directory.

    The converter copies the source package to an output directory first,
    then applies all transformations in-place on the output copy.  The
    source directory is never modified.
    """

    def __init__(self, profile: PackageFormatProfile) -> None:
        self.profile = profile
        self.actions: list[str] = []
        self.errors: list[str] = []

    def convert(self, pkg_root: Path, output_root: Path) -> ConversionResult:
        """Convert *pkg_root* into *output_root*."""
        if not pkg_root.is_dir():
            raise ValueError(f"Source is not a directory: {pkg_root}")

        ontology_dir = pkg_root / "ontology"
        src_base = ontology_dir if ontology_dir.is_dir() else pkg_root

        if output_root.exists():
            raise ValueError(f"Output directory already exists: {output_root}")
        shutil.copytree(pkg_root, output_root)

        out_base = output_root
        if self.profile.ontology_subdir:
            out_ontology = output_root / "ontology"
            if out_ontology.is_dir():
                self._flatten_subdir(out_ontology, output_root)
                self.actions.append("path: flattened ontology/ subdirectory to package root")
                out_base = output_root

        self._convert_manifest(out_base)
        self._convert_root_index(out_base)
        self._convert_object_index(out_base)
        self._convert_object_details(out_base)
        self._convert_operation_index(out_base)
        self._convert_operation_details(out_base)
        self._convert_relations(out_base)
        self._convert_mappings(out_base)
        self._convert_scripts(out_base)

        return ConversionResult(
            source_root=str(pkg_root),
            output_root=str(output_root),
            actions_applied=self.actions,
            errors=self.errors,
            success=len(self.errors) == 0,
        )

    # -- manifest ----------------------------------------------------------

    @staticmethod
    def _flatten_subdir(subdir: Path, target: Path) -> None:
        for item in subdir.iterdir():
            dest = target / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(item), str(dest))
        subdir.rmdir()

    def _convert_manifest(self, base: Path) -> None:
        manifest_path = base / "manifest.yaml"
        if not manifest_path.exists():
            self.errors.append("manifest.yaml not found")
            return

        manifest = _load_yaml_file(manifest_path)
        changed = False

        if manifest.get("kind") != "ontology-package":
            manifest["kind"] = "ontology-package"
            changed = True
            self.actions.append("manifest: fixed kind -> ontology-package")

        if "schema_version" not in manifest:
            manifest["schema_version"] = "0.1"
            changed = True
            self.actions.append("manifest: added schema_version: 0.1")
        elif str(manifest["schema_version"]) != "0.1":
            manifest["schema_version"] = "0.1"
            changed = True
            self.actions.append("manifest: fixed schema_version -> 0.1")

        if "status" not in manifest:
            manifest["status"] = "draft"
            changed = True
            self.actions.append("manifest: added status: draft")

        # Keep in sync with KNOWN_MANIFEST_FIELDS (single source of truth).
        known = KNOWN_MANIFEST_FIELDS
        extra_keys = [k for k in manifest if k not in known]
        if extra_keys:
            for key in extra_keys:
                new_key = f"x_{key}"
                manifest[new_key] = manifest.pop(key)
            changed = True
            self.actions.append(f"manifest: prefixed extra fields with x_: {extra_keys}")

        if changed:
            _save_yaml_file(manifest_path, manifest)

    # -- root index --------------------------------------------------------

    def _convert_root_index(self, base: Path) -> None:
        index_path = base / "index.md"
        ontology_path = base / "ONTOLOGY.md"

        if not index_path.exists() and ontology_path.exists():
            ontology_path.rename(index_path)
            self.actions.append("root: renamed ONTOLOGY.md -> index.md")

        if not index_path.exists():
            manifest = _load_yaml_file(base / "manifest.yaml")
            title = manifest.get("name", "Untitled")
            desc = manifest.get("description", "")
            body = f"# {title}\n\n{desc}\n"
            fm = {"resource": "root", "key": "root", "title": title}
            index_path.write_text(_build_frontmatter(fm) + "\n" + body, encoding="utf-8")
            self.actions.append("root: created index.md from manifest")
            return

        text = index_path.read_text(encoding="utf-8")
        if not text.strip().startswith("---"):
            title = _extract_title_from_content(text) or index_path.stem
            fm = {"resource": "root", "key": "root", "title": title}
            index_path.write_text(_build_frontmatter(fm) + "\n" + text, encoding="utf-8")
            self.actions.append("root: added frontmatter to index.md")

    # -- object index ------------------------------------------------------

    def _convert_object_index(self, base: Path) -> None:
        index_path = base / "objects" / "index.md"
        list_path = base / "objects-list.md"

        if not index_path.exists() and list_path.exists():
            objects_dir = base / "objects"
            objects_dir.mkdir(exist_ok=True)
            dest = objects_dir / "index.md"
            list_path.rename(dest)
            self.actions.append("objects: renamed objects-list.md -> objects/index.md")
            index_path = dest

        if not index_path.exists():
            return

        text = index_path.read_text(encoding="utf-8")
        if not text.strip().startswith("---"):
            fm = {"resource": "object_index", "key": "objects", "title": "\u5bf9\u8c61\u76ee\u5f55"}
            index_path.write_text(_build_frontmatter(fm) + "\n" + text, encoding="utf-8")
            self.actions.append("objects: added frontmatter to objects/index.md")

    # -- object details ----------------------------------------------------

    def _convert_object_details(self, base: Path) -> None:
        objects_dir = base / "objects"
        if not objects_dir.is_dir():
            return

        fm_count = 0
        strip_count = 0
        for md_file in sorted(objects_dir.glob("*.md")):
            if md_file.name == "index.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            added_frontmatter = False
            if not _has_complete_frontmatter(text):
                key = md_file.stem
                title = _extract_title_from_content(text) or key
                desc = _extract_description_from_content(text)
                fm_fields: dict[str, object] = {
                    "resource": "object",
                    "key": key,
                    "title": title,
                }
                if desc:
                    fm_fields["description"] = desc
                text = _build_frontmatter(fm_fields) + "\n" + text
                added_frontmatter = True

            # Remove field table rows where the key column (second column)
            # is empty but the name column (first column) has text.  These
            # are descriptive placeholder rows (e.g. "继承自MPart的各个属性")
            # that the old skill_parser pipeline dropped by accident when
            # rebuilding tables.  Keeping them causes alias validation to
            # reject the package at import time.
            stripped_text, did_strip = _strip_empty_key_field_rows(text)
            if did_strip:
                text = stripped_text
                strip_count += 1

            if added_frontmatter or did_strip:
                md_file.write_text(text, encoding="utf-8")
            if added_frontmatter:
                fm_count += 1

        if fm_count > 0:
            self.actions.append(f"objects: added frontmatter to {fm_count} object detail files")
        if strip_count > 0:
            self.actions.append(f"objects: stripped {strip_count} empty-key field rows")

    # -- operation index ---------------------------------------------------

    def _convert_operation_index(self, base: Path) -> None:
        index_path = base / "operations" / "index.md"
        list_path = base / "operations-list.md"

        if not index_path.exists() and list_path.exists():
            ops_dir = base / "operations"
            ops_dir.mkdir(exist_ok=True)
            dest = ops_dir / "index.md"
            list_path.rename(dest)
            self.actions.append("operations: renamed operations-list.md -> operations/index.md")
            index_path = dest
        elif index_path.exists() and list_path.exists():
            list_path.unlink()
            self.actions.append("operations: removed duplicate operations-list.md (operations/index.md already exists)")

        if not index_path.exists():
            return

        text = index_path.read_text(encoding="utf-8")
        if not text.strip().startswith("---"):
            fm = {"resource": "operation_index", "key": "operations", "title": "\u64cd\u4f5c\u76ee\u5f55"}
            index_path.write_text(_build_frontmatter(fm) + "\n" + text, encoding="utf-8")
            self.actions.append("operations: added frontmatter to operations/index.md")

    # -- operation details -------------------------------------------------

    def _convert_operation_details(self, base: Path) -> None:
        ops_dir = base / "operations"
        if not ops_dir.is_dir():
            return

        count = 0
        for md_file in sorted(ops_dir.glob("*.md")):
            if md_file.name == "index.md":
                continue
            text = md_file.read_text(encoding="utf-8")
            if _has_complete_frontmatter(text):
                continue

            key = md_file.stem
            title = _extract_title_from_content(text) or key
            desc = _extract_description_from_content(text)
            fm_fields: dict[str, object] = {
                "resource": "operation",
                "key": key,
                "title": title,
                "uses": {"objects": []},
            }
            if desc:
                fm_fields["description"] = desc

            md_file.write_text(_build_frontmatter(fm_fields) + "\n" + text, encoding="utf-8")
            count += 1

        if count > 0:
            self.actions.append(f"operations: added frontmatter to {count} operation detail files")

    # -- scripts ----------------------------------------------------------

    def _convert_scripts(self, base: Path) -> None:
        list_path = base / "scripts-list.md"
        if not list_path.exists():
            return
        scripts_dir = base / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        dest = scripts_dir / "index.md"
        if dest.exists():
            dest.unlink()
        list_path.rename(dest)
        self.actions.append("scripts: renamed scripts-list.md -> scripts/index.md")
        text = dest.read_text(encoding="utf-8")
        if not text.strip().startswith("---"):
            fm = {"resource": "script_index", "key": "scripts", "title": "\u811a\u672c\u76ee\u5f55"}
            dest.write_text(_build_frontmatter(fm) + "\n" + text, encoding="utf-8")
            self.actions.append("scripts: added frontmatter to scripts/index.md")

    # -- relations ---------------------------------------------------------

    def _convert_relations(self, base: Path) -> None:
        """Remove non-standard relations content from the converted package."""
        relations_dir = base / "relations"
        list_path = base / "relations-list.md"

        if relations_dir.is_dir():
            shutil.rmtree(relations_dir)
            self.actions.append(
                "relations: removed non-standard relations/ directory (downgrade to object details deferred)"
            )
        if list_path.exists():
            list_path.unlink()
            self.actions.append(
                "relations: removed non-standard relations-list.md (downgrade to object details deferred)"
            )

    # -- mappings -----------------------------------------------------------

    def _convert_mappings(self, base: Path) -> None:
        """Align mappings with the v0.1 field-key contract."""
        mapping_path = base / "mappings" / "object-data-mappings.json"
        if not mapping_path.exists():
            return

        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.errors.append(f"mappings: failed to read object-data-mappings.json: {exc}")
            return

        if not isinstance(mapping, dict):
            self.errors.append("mappings: object-data-mappings.json must be a JSON object")
            return

        changed = False
        for category in ("objects", "metrics", "rules"):
            items = mapping.get(category)
            if not isinstance(items, dict):
                continue
            for resource_key, entry in items.items():
                if not isinstance(entry, dict):
                    continue
                fields = entry.get("fields")
                if not isinstance(fields, dict):
                    continue

                if self._rewrite_primary_key(category, resource_key, entry, fields):
                    changed = True

                if self._strip_metadata_fields(category, resource_key, entry, fields):
                    changed = True

                if self._normalize_field_types(category, resource_key, fields):
                    changed = True

        if changed:
            mapping_path.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def _rewrite_primary_key(
        self,
        category: str,
        resource_key: str,
        entry: dict,
        fields: dict,
    ) -> bool:
        """Rewrite physical primary_key names to business field keys."""
        primary_key = entry.get("primary_key")
        if not isinstance(primary_key, list):
            return False

        rewritten = False
        new_key_list: list[object] = []
        for pk_field in primary_key:
            pk = str(pk_field or "").strip()
            if not pk or pk in fields:
                new_key_list.append(pk_field)
                continue
            matched_field = _find_field_key_by_source(fields, pk)
            if matched_field is not None:
                new_key_list.append(matched_field)
                rewritten = True
                self.actions.append(
                    "mapping: {}.{}.primary_key '{}' -> field key '{}'".format(
                        category, resource_key, pk, matched_field
                    )
                )
            else:
                new_key_list.append(pk_field)
                self.errors.append(
                    "mapping: {}.{}.primary_key '{}' not found in fields or its source".format(
                        category, resource_key, pk
                    )
                )
        if rewritten:
            entry["primary_key"] = new_key_list
        return rewritten

    def _strip_metadata_fields(
        self,
        category: str,
        resource_key: str,
        entry: dict,
        fields: dict,
    ) -> bool:
        """Remove provider metadata field keys and dangling references."""
        removed = [fk for fk in fields if fk in _PROVIDER_META_FIELDS]
        if not removed:
            return False

        for field_key in removed:
            del fields[field_key]
            self.actions.append(
                "mapping: {}.{}.{} is a provider metadata field; removed from fields".format(
                    category, resource_key, field_key
                )
            )

        for ref_key in ("primary_key", "label_fields"):
            refs = entry.get(ref_key)
            if isinstance(refs, list):
                dangling = [r for r in refs if r in removed]
                if dangling:
                    entry[ref_key] = [r for r in refs if r not in removed]
                    self.actions.append(
                        "mapping: {}.{}.{} removed from {}".format(category, resource_key, ", ".join(dangling), ref_key)
                    )

        policy = entry.get("policy")
        if isinstance(policy, dict):
            user_filter = policy.get("require_user_filter")
            if isinstance(user_filter, list):
                dangling = [r for r in user_filter if r in removed]
                if dangling:
                    policy["require_user_filter"] = [r for r in user_filter if r not in removed]
                    self.actions.append(
                        "mapping: {}.{}.{} removed from policy.require_user_filter".format(
                            category, resource_key, ", ".join(dangling)
                        )
                    )

        return True

    def _normalize_field_types(
        self,
        category: str,
        resource_key: str,
        fields: dict,
    ) -> bool:
        """Normalize numeric type aliases (float/int/double) to 'number'."""
        changed = False
        for field_key, field_def in fields.items():
            if not isinstance(field_def, dict):
                continue
            raw_type = str(field_def.get("type") or "").strip().lower()
            if raw_type in _NUMERIC_TYPE_ALIASES:
                field_def["type"] = "number"
                changed = True
                self.actions.append(
                    "mapping: {}.{}.fields.{}.type '{}' -> 'number'".format(category, resource_key, field_key, raw_type)
                )
        return changed


# ===========================================================================
#  Public conversion API
# ===========================================================================


def convert_package(
    pkg_root: Path,
    output_root: Path,
    profile: Optional[PackageFormatProfile] = None,
) -> ConversionResult:
    """Convert a package to v0.1 format.

    Args:
        pkg_root: Source package directory.
        output_root: Output directory for the converted package.
        profile: Pre-computed format profile.  If None, detection runs
            automatically.

    Returns:
        A :class:`ConversionResult`.
    """
    if _is_skill_source_package(pkg_root):
        return _convert_skill_source_package(pkg_root, output_root)

    if profile is None:
        profile = detect_package_format(pkg_root)

    converter = PackageConverter(profile)
    return converter.convert(pkg_root, output_root)


# ===========================================================================
#  Zip helpers
# ===========================================================================

_MAX_ZIP_MEMBERS = 1024
_MAX_ZIP_TOTAL_UNCOMPRESSED = 512 * 1024 * 1024
_ZIP_FALLBACK_ENCODINGS = ("utf-8", "gbk", "gb18030", "big5")


def _decode_zip_member_name(member: zipfile.ZipInfo) -> str:
    """Best-effort decode for legacy zip filename encodings (GBK etc.)."""
    raw_name = member.filename or ""
    if not raw_name:
        return ""
    # General purpose bit flag 11 indicates UTF-8 filenames.
    if member.flag_bits & 0x800:
        return raw_name
    try:
        raw_bytes = raw_name.encode("cp437")
    except UnicodeEncodeError:
        return raw_name
    for encoding in _ZIP_FALLBACK_ENCODINGS:
        try:
            decoded = raw_bytes.decode(encoding)
            if decoded:
                return decoded
        except UnicodeDecodeError:
            continue
    return raw_name


def decode_zip_member_name_for_test(member: zipfile.ZipInfo) -> str:
    """Decode a legacy archive name through a public test boundary."""
    return _decode_zip_member_name(member)


def _extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Extract a zip into *dest_dir* and return the package root.

    Member paths are validated against traversal attempts, member count and
    total uncompressed size are capped, and legacy GBK member names are
    decoded.  The package root is the directory that directly contains
    ``manifest.yaml``; a zip containing several manifests is rejected.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest_roots: list[Path] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        if len(members) > _MAX_ZIP_MEMBERS:
            raise ValueError("Zip contains too many entries")
        total_uncompressed = 0
        for member in members:
            total_uncompressed += member.file_size
            if total_uncompressed > _MAX_ZIP_TOTAL_UNCOMPRESSED:
                raise ValueError("Zip is too large to extract")

            # Skip directory entries early: they carry no file content and
            # their trailing-slash form would falsely trigger the empty-part
            # path check below.  File entries under them are still validated.
            if member.is_dir():
                continue

            decoded = _decode_zip_member_name(member)
            normalized = decoded.replace("\\", "/")
            is_unsafe_path = (
                not normalized
                or "\x00" in normalized
                or normalized.startswith("/")
                or re.match(r"^[A-Za-z]:", normalized)
            )
            if is_unsafe_path:
                raise ValueError(f"Unsafe zip member path: {member.filename!r}")
            parts = tuple(normalized.split("/"))
            if any(not part or part in {".", ".."} for part in parts):
                raise ValueError(f"Unsafe zip member path: {member.filename!r}")

            target = dest_dir.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)

    for candidate in dest_dir.rglob("manifest.yaml"):
        if candidate.parent not in manifest_roots:
            manifest_roots.append(candidate.parent)
    if len(manifest_roots) > 1:
        raise ValueError("Zip contains multiple ontology package roots")
    if manifest_roots:
        return manifest_roots[0]

    skill_roots: list[Path] = []
    for candidate in dest_dir.rglob("SKILL.md"):
        parent = candidate.parent
        if parent in skill_roots:
            continue
        if _is_skill_source_package(parent):
            skill_roots.append(parent)
    if len(skill_roots) > 1:
        raise ValueError("Zip contains multiple source package roots")
    if skill_roots:
        return skill_roots[0]
    return dest_dir


def _zip_directory(src_dir: Path, zip_path: Path) -> None:
    """Pack *src_dir* into a zip file at *zip_path*.

    Files are stored relative to *src_dir* (no top-level wrapper folder).
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file() and not file_path.is_symlink():
                arcname = file_path.relative_to(src_dir).as_posix()
                zf.write(file_path, arcname)


# ===========================================================================
#  Import pipeline
# ===========================================================================


def import_package(
    pkg_root: Path,
    output_dir: Optional[Path] = None,
) -> ImportResult:
    """Run the full import pipeline on *pkg_root*.

    Args:
        pkg_root: Source package directory to import.
        output_dir: Where to place the converted package.  If None, a
            sibling directory with ``_converted`` suffix is used.

    Returns:
        An :class:`ImportResult`.
    """
    if not pkg_root.is_dir():
        raise ValueError(f"Package root is not a directory: {pkg_root}")

    result = ImportResult(package_root=str(pkg_root))

    # Step 1: initial validation
    initial = validate_package(pkg_root)
    result.initial_validation = _validation_to_dict(initial)

    if initial.is_conformant:
        result.imported = True
        result.import_path = str(pkg_root)
        result.required_conversion = False
        return result

    # Step 2: detect format
    profile = detect_package_format(pkg_root)

    # Step 3: convert
    if output_dir is None:
        output_dir = pkg_root.parent / f"{pkg_root.name}_converted"

    if output_dir.exists():
        if not output_dir.name.endswith("_converted"):
            raise ValueError(
                "Output directory already exists and is not a converter "
                f"artifact (expected *_converted suffix): {output_dir}"
            )
        shutil.rmtree(output_dir)
    conversion = convert_package(pkg_root, output_dir, profile)
    result.conversion_actions = conversion.actions_applied
    result.required_conversion = True

    if not conversion.success:
        result.errors.extend(conversion.errors)
        result.imported = False
        return result

    # Step 4: re-validate the converted package
    final = validate_package(output_dir)
    result.final_validation = _validation_to_dict(final)

    if final.is_conformant:
        result.imported = True
        result.import_path = str(output_dir)
    else:
        error_msgs = [f"{e.rule}: {e.message}" for e in final.errors]
        result.errors.extend(error_msgs)
        result.imported = False

    return result


def import_package_from_zip(
    zip_path: Path,
    output_dir: Optional[Path] = None,
    zip_output: Optional[Path] = None,
) -> ImportResult:
    """Run the import pipeline on a zip file.

    Extracts the zip to a temporary directory, runs the v0.1 pipeline,
    and packs the converted result into a zip.  When *output_dir* is
    provided the converted directory is also preserved at that path;
    otherwise ``import_path`` is left empty since the staging directory is
    removed on exit.
    """
    if not zip_path.is_file():
        raise ValueError(f"Zip file not found: {zip_path}")

    if zip_output is None:
        zip_output = zip_path.parent / f"{zip_path.stem}_converted.zip"

    with tempfile.TemporaryDirectory(prefix="ontology-zip-import-") as temp_dir:
        extract_dir = Path(temp_dir) / "extracted"
        pkg_root = _extract_zip(zip_path, extract_dir)
        # Convert into a temp staging dir inside the temp directory.
        staging_dir = Path(temp_dir) / "converted"
        result = import_package(pkg_root, staging_dir)

        if result.imported:
            _zip_directory(Path(result.import_path), zip_output)
            result.zip_output = str(zip_output)
            # Optionally copy the converted directory to a permanent location.
            if output_dir is not None:
                if output_dir.exists():
                    if not output_dir.name.endswith("_converted"):
                        raise ValueError(
                            f"Output directory already exists and is not a converter artifact: {output_dir}"
                        )
                    shutil.rmtree(output_dir)
                shutil.copytree(Path(result.import_path), output_dir)
                result.import_path = str(output_dir)
            else:
                result.import_path = ""

    return result


def _validation_to_dict(vr: ValidationResult) -> dict:
    """Serialize a ValidationResult to a plain dict for JSON output."""
    return {
        "is_conformant": vr.is_conformant,
        "error_count": len(vr.errors),
        "warning_count": len(vr.warnings),
        "errors": [asdict(e) for e in vr.errors],
        "warnings": [asdict(w) for w in vr.warnings],
    }


# ===========================================================================
#  CLI
# ===========================================================================


def _print_usage() -> None:
    _logger.error(
        "Usage: python legacy_package_converter.py <package_dir_or_zip> [--output-dir <dir>] [--zip-output <zip>]"
    )


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _print_usage()
        return 2

    input_path = Path(argv[1])
    output_dir: Optional[Path] = None
    zip_output: Optional[Path] = None

    rest = argv[2:]
    while rest:
        flag = rest.pop(0)
        if flag == "--output-dir" and rest:
            output_dir = Path(rest.pop(0))
        elif flag == "--zip-output" and rest:
            zip_output = Path(rest.pop(0))
        else:
            _logger.error("Unknown argument: %s", flag)
            _print_usage()
            return 2

    try:
        if input_path.suffix.lower() == ".zip":
            result = import_package_from_zip(input_path, output_dir, zip_output)
        elif input_path.is_dir():
            if zip_output:
                _logger.error("--zip-output is only valid with zip input")
                return 2
            result = import_package(input_path, output_dir)
        else:
            _logger.error("知识包转换失败: 输入必须是目录或 .zip 文件: %s", input_path)
            return 2
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, zlib.error) as exc:
        _logger.error("知识包转换失败: %s", exc)
        return 1

    # Log a concise success/failure summary instead of the full JSON dump.
    if result.imported:
        name = result.zip_output or result.import_path or str(input_path)
        _logger.info("知识包转换成功: %s", Path(name).name)
    else:
        reason = "; ".join(result.errors[:3]) if result.errors else "未知错误"
        _logger.error("知识包转换失败: %s", reason)
    return 0 if result.imported else 1


if __name__ == "__main__":
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    raise SystemExit(main(sys.argv))
