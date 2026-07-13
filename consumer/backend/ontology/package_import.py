"""Normalize supported ontology package formats at the import boundary."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


SUPPORTED_IMPORT_MANIFEST_KINDS = frozenset({"ontology", "ontology-package"})
V01_SCHEMA_VERSION = "0.1"


@dataclass(frozen=True)
class ImportableOntologyPackage:
    """Metadata read from an extracted package without loading it into runtime."""

    root: Path
    ontology_id: str
    name: str
    description: str
    version: str
    source_format: str


@dataclass(frozen=True)
class PackageNormalizationResult:
    """Result of converting an import package to the current internal layout."""

    root: Path
    generated_files: tuple[str, ...]
    warnings: tuple[str, ...]


def inspect_importable_package(root: Path) -> Optional[ImportableOntologyPackage]:
    """Read current or v0.1 package metadata without relaxing the runtime loader."""
    package_root = Path(root)
    manifest_path = package_root / "manifest.yaml"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None

    kind = str(raw.get("kind") or "").strip()
    if kind not in SUPPORTED_IMPORT_MANIFEST_KINDS:
        return None
    if kind == "ontology-package":
        schema_version = str(raw.get("schema_version") or "").strip()
        if schema_version != V01_SCHEMA_VERSION:
            return None

    ontology_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    version = str(raw.get("active_version") or raw.get("version") or "").strip()
    if not all((ontology_id, name, description, version)):
        return None

    source_format = "v0.1" if kind == "ontology-package" else "current"
    return ImportableOntologyPackage(
        root=package_root,
        ontology_id=ontology_id,
        name=name,
        description=description,
        version=version,
        source_format=source_format,
    )


def normalize_imported_package(
    source_root: Path,
    target_root: Path,
) -> PackageNormalizationResult:
    """Copy a package and generate current catalogs from v0.1 directory indexes."""
    source = Path(source_root)
    target = Path(target_root)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Ontology package root must be a regular directory")
    if target.exists():
        raise ValueError("Ontology normalization target already exists")

    _reject_symbolic_links(source)
    shutil.copytree(source, target)

    generated: list[str] = []
    warnings: list[str] = []
    _generate_catalog_from_index(target, "objects-list.md", "objects/index.md", generated)
    _generate_catalog_from_index(target, "operations-list.md", "operations/index.md", generated)
    _generate_catalog_from_index(target, "rules-list.md", "rules/index.md", generated)
    _generate_catalog_from_index(target, "metrics-list.md", "metrics/index.md", generated)
    _generate_root_document(target, generated)
    _generate_script_catalog(target, generated, warnings)

    return PackageNormalizationResult(
        root=target,
        generated_files=tuple(generated),
        warnings=tuple(warnings),
    )


def _reject_symbolic_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Ontology package contains symbolic links")


def _generate_catalog_from_index(
    root: Path,
    destination_name: str,
    source_name: str,
    generated: list[str],
) -> None:
    destination = root / destination_name
    source = root / source_name
    if destination.is_file() or not source.is_file():
        return
    text = _markdown_body(source.read_text(encoding="utf-8"))
    destination.write_text(text.rstrip() + "\n", encoding="utf-8")
    generated.append(destination_name)


def _generate_root_document(root: Path, generated: list[str]) -> None:
    destination = root / "ONTOLOGY.md"
    source = root / "index.md"
    if destination.is_file() or not source.is_file():
        return
    text = _markdown_body(source.read_text(encoding="utf-8"))
    destination.write_text(text.rstrip() + "\n", encoding="utf-8")
    generated.append("ONTOLOGY.md")


def _generate_script_catalog(
    root: Path,
    generated: list[str],
    warnings: list[str],
) -> None:
    destination = root / "scripts-list.md"
    scripts_dir = root / "scripts"
    if destination.is_file() or not scripts_dir.is_dir():
        return

    entries: list[tuple[str, str, list[dict[str, Any]]]] = []
    for descriptor in sorted(scripts_dir.glob("*.md"), key=lambda path: path.name.lower()):
        if descriptor.name.lower() == "index.md":
            continue
        entry, warning = _script_entry_from_descriptor(root, descriptor)
        if warning:
            warnings.append(warning)
        if entry is not None:
            entries.append(entry)

    if not entries:
        return

    lines = [
        "# Tool flow script catalog",
        "",
        "## Script mappings",
    ]
    for operation_key, script_path, parameters in entries:
        lines.append("- `{}` -> `{}`".format(operation_key, script_path))
        lines.append(
            "  `parameters`: `{}`".format(
                json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
            )
        )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generated.append("scripts-list.md")


def _script_entry_from_descriptor(
    root: Path,
    descriptor: Path,
) -> tuple[Optional[tuple[str, str, list[dict[str, Any]]]], Optional[str]]:
    try:
        frontmatter, body = _split_markdown_frontmatter(descriptor.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None, "Failed to read script descriptor '{}'".format(descriptor.name)

    operation_key = str(
        frontmatter.get("bound_operation") or frontmatter.get("operation_key") or ""
    ).strip()
    entrypoint = str(frontmatter.get("entrypoint") or frontmatter.get("path") or "").strip()
    if not operation_key or not entrypoint:
        return None, None

    normalized_entrypoint = entrypoint.replace("\\", "/")
    entrypoint_path = Path(normalized_entrypoint)
    if (
        entrypoint_path.is_absolute()
        or re.match(r"^[A-Za-z]:", normalized_entrypoint)
        or any(part in {"", ".", ".."} for part in entrypoint_path.parts)
    ):
        return None, "Rejected unsafe script entrypoint in '{}'".format(descriptor.name)
    if not normalized_entrypoint.startswith("scripts/"):
        normalized_entrypoint = "scripts/{}".format(normalized_entrypoint.lstrip("/"))

    script_path = root / Path(normalized_entrypoint)
    try:
        script_path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return None, "Script entrypoint not found for '{}'".format(descriptor.name)
    if script_path.is_symlink() or not script_path.is_file():
        return None, "Script entrypoint not found for '{}'".format(descriptor.name)

    return (
        operation_key,
        normalized_entrypoint,
        _parse_script_parameters(body),
    ), None


def _split_markdown_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    text = str(markdown or "")
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)$", text, re.DOTALL)
    if match is None:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        parsed = {}
    return (parsed if isinstance(parsed, dict) else {}), match.group(2)


def _markdown_body(markdown: str) -> str:
    _frontmatter, body = _split_markdown_frontmatter(markdown)
    return body.strip()


def _parse_script_parameters(markdown: str) -> list[dict[str, Any]]:
    rows = _parse_markdown_table(markdown)
    parameters: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        name = _first_row_value(row, "参数 key", "参数key", "参数名称", "name")
        flag = _first_row_value(row, "CLI 参数", "CLI参数", "flag")
        if not name and not flag:
            continue
        parameter: dict[str, Any] = {
            "name": name or _name_from_flag(flag) or "arg_{}".format(index + 1),
            "flag": flag,
            "required": _as_bool(
                _first_row_value(row, "必填", "required"),
                default=True,
            ),
            "type": _first_row_value(row, "类型", "type") or "string",
            "multiple": _as_bool(
                _first_row_value(row, "是否多值", "multiple"),
                default=False,
            ),
        }
        description = _first_row_value(row, "说明", "描述", "description")
        if description:
            parameter["description"] = description
        aliases = _split_aliases(_first_row_value(row, "别名", "aliases"))
        if aliases:
            parameter["aliases"] = aliases
        parameters.append(parameter)
    return parameters


def _parse_markdown_table(markdown: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    headers: list[str] = []
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and all(set(cell.replace(" ", "")) <= {"-"} for cell in cells):
            continue
        if not headers:
            headers = cells
            continue
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _first_row_value(row: dict[str, str], *keys: str) -> str:
    normalized = {
        str(key).strip().lower().replace(" ", ""): value
        for key, value in row.items()
    }
    for key in keys:
        value = normalized.get(key.strip().lower().replace(" ", ""))
        if value is not None:
            return str(value or "").strip()
    return ""


def _split_aliases(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,，、;/；]+", str(value or ""))
        if item.strip()
    ]


def _name_from_flag(value: str) -> str:
    text = str(value or "").strip().lstrip("-")
    return text.replace("-", "_") if text else ""


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"true", "1", "yes", "y", "是", "必填", "需要", "required"}:
        return True
    if text in {"false", "0", "no", "n", "否", "可选", "不需要", "optional"}:
        return False
    return bool(value)
