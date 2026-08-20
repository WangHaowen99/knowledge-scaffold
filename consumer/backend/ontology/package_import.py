"""Validate ontology packages and normalize imports to schema v0.1."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from ontology.ontology_catalog import (
    FieldAliasValidationResult,
    ObjectAliasValidationResult,
    validate_field_aliases,
    validate_object_aliases,
    validate_object_data_mappings,
    validate_value_bindings,
)


V01_SCHEMA_VERSION = "0.1"
SUPPORTED_IMPORT_MANIFEST_KINDS = frozenset({"ontology-package"})


class PackageNormalizationError(ValueError):
    """A client-visible invalid package error raised during normalization."""


@dataclass(frozen=True)
class ImportableOntologyPackage:
    """Metadata read from an extracted package before runtime installation."""

    root: Path
    ontology_id: str
    name: str
    description: str
    version: str
    source_format: str


@dataclass(frozen=True)
class PackageNormalizationResult:
    """Result of preparing a package for the v0.1-only runtime."""

    root: Path
    generated_files: tuple[str, ...]
    warnings: tuple[str, ...]


def validate_imported_object_aliases(root: Path) -> ObjectAliasValidationResult:
    """Validate object aliases, mapping readability and value bindings before installation."""
    validation = validate_object_aliases(Path(root))
    mapping_error = validate_object_data_mappings(Path(root))
    binding_validation = validate_value_bindings(Path(root))
    raw_errors = (
        *validation.errors,
        mapping_error,
        *binding_validation.errors,
    )
    error_set: set[str] = set()
    for error in raw_errors:
        if error:
            error_set.add(error)
    errors = sorted(error_set)
    warnings = sorted(set((*validation.warnings, *binding_validation.warnings)))
    if not errors and not warnings:
        return validation
    return ObjectAliasValidationResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def validate_imported_field_aliases(root: Path) -> FieldAliasValidationResult:
    """Validate object field aliases before installation."""
    return validate_field_aliases(Path(root))


def has_importable_package_layout(root: Path) -> bool:
    """Return whether an extracted root has a v0.1 package layout."""
    package_root = Path(root)
    new_indexes = (
        package_root / "index.md",
        package_root / "objects" / "index.md",
        package_root / "operations" / "index.md",
    )
    if any(path.is_file() for path in new_indexes):
        return True
    return False


def inspect_importable_package(root: Path) -> Optional[ImportableOntologyPackage]:
    """Inspect a conformant v0.1 ontology package."""
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
    if str(raw.get("schema_version") or "").strip() != V01_SCHEMA_VERSION:
        return None

    ontology_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    version = str(raw.get("active_version") or raw.get("version") or "").strip()
    if not all((ontology_id, name, description, version)):
        return None

    required_paths = (
        package_root / "index.md",
        package_root / "objects" / "index.md",
        package_root / "operations" / "index.md",
    )
    if any(not path.is_file() or path.is_symlink() for path in required_paths):
        return None

    return ImportableOntologyPackage(
        root=package_root,
        ontology_id=ontology_id,
        name=name,
        description=description,
        version=version,
        source_format="v0.1",
    )


def normalize_imported_package(source_root: Path, target_root: Path) -> PackageNormalizationResult:
    """Copy a conformant v0.1 package to a clean staging directory."""
    source = Path(source_root)
    target = Path(target_root)
    package = inspect_importable_package(source)
    if package is None:
        raise ValueError("Unsupported or invalid ontology package")
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Ontology package root must be a regular directory")
    if target.exists():
        raise ValueError("Ontology normalization target already exists")

    _reject_symbolic_links(source)
    shutil.copytree(source, target)
    return PackageNormalizationResult(
        root=target,
        generated_files=(),
        warnings=(),
    )


def _reject_symbolic_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Ontology package contains symbolic links")
