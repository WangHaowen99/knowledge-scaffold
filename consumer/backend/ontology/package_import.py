"""Validate ontology packages and normalize supported package schemas."""

from __future__ import annotations

import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from config import LimitsConfig
from ontology.ontology_catalog import (
    FieldAliasValidationResult,
    ObjectAliasValidationResult,
    validate_field_aliases,
    validate_object_aliases,
    validate_object_data_mappings,
    validate_value_bindings,
)
from ontology.legacy_package_converter import (
    LEGACY_FILE_NAMES,
    V01_SCHEMA_VERSION,
    convert_legacy_package_in_place,
    finalize_v01_package_in_place,
)
from ontology.operation_execution import (
    DeterministicPackageError,
    validate_and_lock_deterministic_operations,
)
from ontology.ontologies import (
    SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS,
    validate_no_runtime_binding_data,
)


SUPPORTED_IMPORT_MANIFEST_KINDS = frozenset({"ontology", "ontology-package"})
ONTOLOGY_INSTALL_MANIFEST_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "name",
        "description",
        "version",
        "active_version",
        "versions",
        "language",
        "enabled",
        "scope",
    }
)


class PackageNormalizationError(ValueError):
    """A client-visible invalid package error raised during normalization."""

    def __init__(self, message: str, *, code: str = "invalid_ontology_package") -> None:
        super().__init__(message)
        self.code = str(code or "invalid_ontology_package")


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
    """Result of preparing a package for the versioned runtime."""

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
    """Return whether an extracted root has a supported or temporary legacy layout."""
    package_root = Path(root)
    manifest_path = package_root / "manifest.yaml"
    if manifest_path.is_file() and not manifest_path.is_symlink():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            manifest = {}
        if isinstance(manifest, dict):
            kind = str(manifest.get("kind") or "").strip()
            schema_version = str(manifest.get("schema_version") or "").strip()
            if kind == "ontology-package" and schema_version == "0.2":
                if any((package_root / "operations").glob("*.md")):
                    return True
    new_indexes = (
        package_root / "index.md",
        package_root / "objects" / "index.md",
        package_root / "operations" / "index.md",
    )
    if any(path.is_file() for path in new_indexes):
        return True
    if any((package_root / name).is_file() for name in LEGACY_FILE_NAMES):
        return True
    return any(path.is_file() for path in package_root.glob("*-list.md"))


def inspect_importable_package(root: Path) -> Optional[ImportableOntologyPackage]:
    """Inspect supported packages and legacy packages accepted by the converter."""
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
    try:
        validate_no_runtime_binding_data(raw)
    except ValueError:
        return None

    kind = str(raw.get("kind") or "").strip()
    if kind not in SUPPORTED_IMPORT_MANIFEST_KINDS:
        return None
    schema_version = str(raw.get("schema_version") or "").strip()
    if (
        kind == "ontology"
        and schema_version
        and schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS
    ):
        return None
    if kind == "ontology-package" and schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        return None

    ontology_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    description = str(raw.get("description") or "").strip()
    version = str(raw.get("active_version") or raw.get("version") or "").strip()
    if not all((ontology_id, name, description, version)):
        return None
    return ImportableOntologyPackage(
        root=package_root,
        ontology_id=ontology_id,
        name=name,
        description=description,
        version=version,
        source_format="v{}".format(schema_version) if kind == "ontology-package" else "legacy",
    )


def normalize_imported_package(source_root: Path, target_root: Path) -> PackageNormalizationResult:
    """Copy an import package and normalize only formats that require conversion."""
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
    if package.source_format != "legacy":
        validate_ontology_revision(source)
    shutil.copytree(source, target)

    generated: list[str] = []
    warnings: list[str] = []
    contains_legacy_artifacts = any((target / name).is_file() for name in LEGACY_FILE_NAMES) or any(
        target.glob("*-list.md")
    )
    if package.source_format == "legacy" or contains_legacy_artifacts:
        try:
            converted, conversion_warnings = convert_legacy_package_in_place(target)
        except (OSError, ValueError) as exc:
            message = str(exc).strip() or "Invalid ontology package"
            raise PackageNormalizationError(message) from exc
        generated.extend(converted)
        warnings.extend(conversion_warnings)

    if package.source_format in {"legacy", "v0.1"}:
        finalized, finalization_warnings = finalize_v01_package_in_place(target)
        generated.extend(finalized)
        warnings.extend(finalization_warnings)

    normalized_manifest = validate_ontology_revision(target)
    if str(normalized_manifest.get("schema_version") or "").strip() != "0.2":
        try:
            deterministic_specs = validate_and_lock_deterministic_operations(target)
        except DeterministicPackageError as exc:
            raise PackageNormalizationError(str(exc), code=exc.code) from exc
        if deterministic_specs:
            generated.append(".deterministic-lock.json")
    return PackageNormalizationResult(
        root=target,
        generated_files=tuple(dict.fromkeys(generated)),
        warnings=tuple(warnings),
    )


def validate_ontology_revision(root: Path) -> dict[str, Any]:
    """Validate a complete package revision before it can affect runtime state."""

    package_root = Path(root)
    if not package_root.is_dir() or package_root.is_symlink():
        raise ValueError("Ontology revision must be a regular directory")
    _reject_symbolic_links(package_root)

    manifest_path = package_root / "manifest.yaml"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Ontology revision manifest is missing or unsafe")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("Ontology revision manifest cannot be read") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Ontology revision manifest must be a mapping")
    validate_no_runtime_binding_data(manifest)

    schema_version = str(manifest.get("schema_version") or "").strip()
    kind = str(manifest.get("kind") or "").strip()
    if schema_version and schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        raise ValueError("Unsupported ontology schema version")
    if kind == "ontology-package" and schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        raise ValueError("Unsupported ontology schema version")
    if schema_version != "0.2":
        return manifest

    operations_dir = package_root / "operations"
    if operations_dir.exists() and not operations_dir.is_dir():
        raise ValueError("Ontology operations path must be a directory")
    if operations_dir.is_dir():
        nested_operations = [
            path
            for path in operations_dir.rglob("*.md")
            if path.parent != operations_dir
        ]
        if nested_operations:
            raise ValueError(
                "Operation Markdown files must use a flat operations directory"
            )

        operation_paths = sorted(operations_dir.glob("*.md"))
        operation_count = sum(
            1 for path in operation_paths if path.name.lower() != "index.md"
        )
        limits = LimitsConfig.from_env()
        if operation_count > limits.ontology_operation_max_count:
            raise ValueError("Ontology operation count exceeds the configured limit")
        for operation_path in operation_paths:
            try:
                content = operation_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError("Operation Markdown must be readable UTF-8") from exc
            if len(content) > limits.operation_max_chars:
                raise ValueError("Operation Markdown exceeds the configured size limit")
            frontmatter = _read_markdown_frontmatter(content)
            if frontmatter is not None:
                validate_no_runtime_binding_data(frontmatter)

    from solver_coordination.capability_catalog import (
        SolverCapabilityCatalogError,
        load_solver_capability_catalog,
    )

    try:
        load_solver_capability_catalog(package_root, manifest)
    except SolverCapabilityCatalogError as exc:
        raise ValueError(str(exc)) from exc
    return manifest


def build_versioned_install_manifest(
    *,
    source_manifest: Mapping[str, Any],
    existing_manifest: Optional[Mapping[str, Any]],
    ontology_id: str,
    ontology_name: str,
    ontology_description: str,
    target_version: str,
) -> dict[str, Any]:
    """Merge one supported package revision into the shared install manifest."""

    validate_no_runtime_binding_data(source_manifest)
    if existing_manifest is not None:
        validate_no_runtime_binding_data(existing_manifest)

    raw_schema_version = (
        source_manifest.get("schema_version")
        or (existing_manifest or {}).get("schema_version")
        or V01_SCHEMA_VERSION
    )
    schema_version = str(raw_schema_version).strip()
    if schema_version not in SUPPORTED_ONTOLOGY_SCHEMA_VERSIONS:
        raise ValueError("Unsupported ontology schema version")

    resolved_name = ontology_name or str(
        (existing_manifest or {}).get("name") or ""
    ).strip()
    resolved_description = ontology_description or str(
        (existing_manifest or {}).get("description") or ""
    ).strip()

    manifest = {
        key: deepcopy(value)
        for key, value in (existing_manifest or {}).items()
        if key in ONTOLOGY_INSTALL_MANIFEST_FIELDS
    }
    protected_fields = {
            "id",
            "version",
            "active_version",
            "versions",
    }
    if existing_manifest is not None:
        protected_fields.add("enabled")
    for key, value in source_manifest.items():
        if key in ONTOLOGY_INSTALL_MANIFEST_FIELDS and key not in protected_fields:
            manifest[key] = deepcopy(value)

    manifest.update(
        {
            "kind": "ontology-package",
            "schema_version": deepcopy(raw_schema_version),
            "id": ontology_id,
            "name": resolved_name,
            "description": resolved_description,
        }
    )
    manifest.setdefault("language", "zh-CN")
    manifest.setdefault("enabled", True)
    manifest.setdefault("scope", [])
    manifest.pop("capabilities", None)

    versions: list[str] = []
    for item in (existing_manifest or {}).get("versions") or []:
        version = str(item or "").strip()
        if version and version not in versions:
            versions.append(version)
    if target_version not in versions:
        versions.append(target_version)
    manifest["version"] = target_version
    manifest["active_version"] = target_version
    manifest["versions"] = versions
    return manifest


def _reject_symbolic_links(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Ontology package contains symbolic links")


def _read_markdown_frontmatter(content: str) -> Optional[Mapping[str, Any]]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(
            index
            for index in range(1, len(lines))
            if lines[index].strip() == "---"
        )
        parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except (StopIteration, yaml.YAMLError):
        return None
    return parsed if isinstance(parsed, Mapping) else None
