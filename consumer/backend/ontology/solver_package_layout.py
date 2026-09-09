"""Validation and migration helpers for solver assets in ontology packages.

The solver contract is deliberately kept operation-centric.  A package which
opts into ``operation-centric.v1`` stores the model, schemas and presentation
sidecar for an operation next to the operation Markdown file, under the
existing ``operations/`` directory.  Older package revisions continue to use
the three historical top-level directories and are read in compatibility mode.

This module is intentionally independent from the solver runtime/catalog.  It
only deals with package files, manifest declarations and indexes, which makes
it safe to use at the import boundary and from the standalone migration CLI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import yaml


OPERATION_CENTRIC_LAYOUT = "operation-centric.v1"
LEGACY_LAYOUT = "legacy"
FORBIDDEN_SOLVER_DIRECTORIES = frozenset(
    {"decision-models", "schemas", "presentations"}
)


class SolverPackageLayoutError(ValueError):
    """Raised when a solver package layout is unsafe or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class SolverOperationLayout:
    """Resolved package paths for one solver operation.

    Paths are relative to the package root.  ``fact_snapshot_schema`` and
    ``presentation`` are optional because early v0.2 packages did not always
    declare them; a new operation-centric package validates them when present.
    """

    operation_key: str
    operation_path: Path
    decision_model: Path
    input_schema: Path
    result_schema: Path
    fact_snapshot_schema: Path | None = None
    presentation: Path | None = None
    decision_model_id: str = ""


@dataclass(frozen=True, slots=True)
class SolverPackageLayoutResult:
    """Deterministic result returned by :func:`validate_solver_package_layout`."""

    layout: str
    legacy: bool
    operations: tuple[SolverOperationLayout, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SolverPackageMigrationReport:
    """JSON-friendly summary of a dry-run or applied layout migration."""

    source: Path
    target: Path
    dry_run: bool
    planned_files: tuple[str, ...] = ()
    copied_files: tuple[str, ...] = ()
    removed_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_posix(),
            "target": self.target.as_posix(),
            "dry_run": self.dry_run,
            "planned_files": list(self.planned_files),
            "copied_files": list(self.copied_files),
            "removed_paths": list(self.removed_paths),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class _Operation:
    key: str
    path: Path
    decision_model_id: str
    enabled: bool
    frontmatter: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ModelDeclaration:
    model_id: str
    operation_key: str
    values: Mapping[str, Any]


_MARKER_KEYS = (
    "solver_layout",
    "solver_layout_version",
    "layout_version",
    "layout",
)
_MARKER_VALUES = frozenset(
    {
        OPERATION_CENTRIC_LAYOUT,
        "operation-centric",
        "operation_centric.v1",
        "operation_centric",
    }
)
_RELEASED_VALUES = frozenset(
    {"released", "published", "immutable", "locked"}
)
_OPERATION_KEY_HEADERS = frozenset(
    {
        "operation key",
        "operation_key",
        "operation",
        "solver operation",
        "solver_operation",
        "操作 key",
        "操作key",
        "求解 operation",
        "求解operation",
        "key",
    }
)
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?P<body>.*?)^---[ \t]*(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)
_SOLVER_OPERATION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def is_operation_centric_layout(manifest: Mapping[str, Any] | None) -> bool:
    """Return whether *manifest* explicitly opts into the new layout marker."""

    return _layout_marker(manifest) == OPERATION_CENTRIC_LAYOUT


def solver_layout_marker(manifest: Mapping[str, Any] | None) -> str:
    """Return the normalized layout marker (``legacy`` when absent).

    Unknown non-empty markers are rejected by the validator; this helper is
    intentionally side-effect free and reports them as normalized text so a
    caller can include the value in a diagnostic.
    """

    return _layout_marker(manifest) or LEGACY_LAYOUT


def resolve_solver_sidecar_path(
    package_root: Path,
    relative_path: str | os.PathLike[str],
    label: str = "solver sidecar",
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a declared package path without allowing traversal or links.

    The returned path is absolute.  ``..``, absolute paths, symlinked path
    components and paths that resolve outside *package_root* are rejected.
    ``must_exist=False`` is useful while a migration is planning destinations.
    """

    root = Path(package_root)
    if not root.is_dir() or root.is_symlink():
        raise SolverPackageLayoutError("Solver package root is not a regular directory")
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise SolverPackageLayoutError(f"{label.capitalize()} path is missing")
    relative = Path(raw)
    if (
        relative.is_absolute()
        or raw.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SolverPackageLayoutError(f"{label.capitalize()} path must be contained in the package")

    root_resolved = root.resolve(strict=True)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SolverPackageLayoutError(f"{label.capitalize()} path must be contained in the package")
    try:
        resolved = cursor.resolve(strict=must_exist)
    except OSError as exc:
        if must_exist:
            raise SolverPackageLayoutError(f"{label.capitalize()} file is missing") from exc
        resolved = cursor.resolve(strict=False)
    try:
        contained = resolved.is_relative_to(root_resolved)
    except AttributeError:  # pragma: no cover - Python 3.10 compatibility guard
        contained = str(resolved).startswith(str(root_resolved) + os.sep)
    if not contained:
        raise SolverPackageLayoutError(f"{label.capitalize()} path must be contained in the package")
    if must_exist and not resolved.is_file():
        raise SolverPackageLayoutError(f"{label.capitalize()} file is missing")
    return resolved


def validate_solver_package_layout(
    package_root: Path,
    manifest: Mapping[str, Any] | None = None,
    *,
    require_operation_index: bool = True,
) -> SolverPackageLayoutResult:
    """Validate solver sidecars and package indexes.

    A package without the explicit marker is treated as legacy and remains
    readable.  Once ``solver_layout: operation-centric.v1`` is present, the
    three historical solver directories are forbidden and the root index,
    ``operations/index.md`` and manifest declarations must contain exactly the
    same enabled solver operation keys.
    """

    root = Path(package_root)
    if not root.is_dir() or root.is_symlink():
        raise SolverPackageLayoutError("Solver package root is not a regular directory")
    if manifest is None:
        manifest = _read_manifest(root)
    if not isinstance(manifest, Mapping):
        raise SolverPackageLayoutError("Solver package manifest must be a mapping")

    marker = _layout_marker(manifest)
    if marker and marker not in _MARKER_VALUES:
        raise SolverPackageLayoutError(f"Unsupported solver package layout marker: {marker}")
    operation_centric = marker == OPERATION_CENTRIC_LAYOUT
    if operation_centric:
        for directory_name in sorted(FORBIDDEN_SOLVER_DIRECTORIES):
            directory = root / directory_name
            if directory.exists():
                raise SolverPackageLayoutError(
                    f"Operation-centric solver layout forbids directory: {directory_name}"
                )
        operations_dir = root / "operations"
        nested_operation_docs = [
            path
            for path in operations_dir.rglob("*.md")
            if path.parent != operations_dir
        ] if operations_dir.is_dir() else []
        if nested_operation_docs:
            raise SolverPackageLayoutError(
                "Operation Markdown files must use a flat operations directory"
            )

    operations = _discover_operations(root)
    declarations = _discover_model_declarations(manifest, operations)
    declaration_by_operation: dict[str, _ModelDeclaration] = {}
    for declaration in declarations:
        if not declaration.operation_key:
            continue
        if declaration.operation_key in declaration_by_operation:
            raise SolverPackageLayoutError(
                f"Manifest declares solver operation more than once: {declaration.operation_key}"
            )
        declaration_by_operation[declaration.operation_key] = declaration

    if operation_centric:
        if not operations and declaration_by_operation:
            raise SolverPackageLayoutError("Manifest solver operations are missing from operations/")
        if not (root / "index.md").is_file():
            raise SolverPackageLayoutError("Root index.md is required for operation-centric solver layout")
        if require_operation_index and not (root / "operations" / "index.md").is_file():
            raise SolverPackageLayoutError(
                "operations/index.md is required for operation-centric solver layout"
            )

    result_operations: list[SolverOperationLayout] = []
    referenced_sidecars: dict[Path, str] = {}
    for operation in operations:
        declaration = declaration_by_operation.get(operation.key)
        if declaration is None:
            if operation_centric:
                raise SolverPackageLayoutError(
                    f"Solver operation is missing from manifest: {operation.key}"
                )
            # Legacy packages occasionally omitted operation_key.  Infer the
            # declaration by the operation's decision model id.
            declaration = _find_declaration_for_model(
                declarations, operation.decision_model_id
            )
        if declaration is None:
            if operation_centric:
                raise SolverPackageLayoutError(
                    f"Solver operation has no decision model declaration: {operation.key}"
                )
            continue

        values = declaration.values
        model_id = declaration.model_id or operation.decision_model_id
        decision_model = _declared_value(
            values,
            "entry",
            "decision_model_entry",
            "decision_model_path",
        )
        input_schema = _declared_value(values, "input_schema", "input")
        result_schema = _declared_value(values, "result_schema", "output_schema", "output")
        fact_schema = _declared_value(
            values,
            "fact_snapshot_schema",
            "fact_schema",
            "fact_snapshot",
        )
        presentation = _declared_value(values, "presentation", "presentation_path")
        if not decision_model or not input_schema or not result_schema:
            if operation_centric:
                raise SolverPackageLayoutError(
                    f"Manifest solver operation is missing required sidecar paths: {operation.key}"
                )
            continue
        if operation_centric and (not fact_schema or not presentation):
            missing = []
            if not fact_schema:
                missing.append("fact_snapshot_schema")
            if not presentation:
                missing.append("presentation")
            raise SolverPackageLayoutError(
                "Manifest solver operation is missing required sidecar paths: {} ({})".format(
                    operation.key,
                    ", ".join(missing),
                )
            )

        resolved_paths: dict[str, Path] = {}
        for field, value in (
            ("decision model", decision_model),
            ("input schema", input_schema),
            ("result schema", result_schema),
            ("fact snapshot schema", fact_schema),
            ("presentation", presentation),
        ):
            if not value:
                continue
            resolved = resolve_solver_sidecar_path(root, value, field)
            relative = resolved.relative_to(root.resolve(strict=True))
            if operation_centric and relative.parts[0].casefold() != "operations":
                raise SolverPackageLayoutError(
                    f"Operation-centric {field} must be under operations/: {relative.as_posix()}"
                )
            previous = referenced_sidecars.get(relative)
            if previous and previous != operation.key:
                raise SolverPackageLayoutError(
                    f"Solver sidecar is shared by multiple operations: {relative.as_posix()}"
                )
            referenced_sidecars[relative] = operation.key
            resolved_paths[field] = relative

        result_operations.append(
            SolverOperationLayout(
                operation_key=operation.key,
                operation_path=operation.path.relative_to(root.resolve(strict=True)),
                decision_model=resolved_paths["decision model"],
                input_schema=resolved_paths["input schema"],
                result_schema=resolved_paths["result schema"],
                fact_snapshot_schema=resolved_paths.get("fact snapshot schema"),
                presentation=resolved_paths.get("presentation"),
                decision_model_id=model_id,
            )
        )

    if operation_centric:
        expected = {operation.key for operation in operations}
        declared = set(declaration_by_operation)
        if declared != expected:
            missing = sorted(expected - declared)
            extra = sorted(declared - expected)
            detail = []
            if missing:
                detail.append("missing=" + ",".join(missing))
            if extra:
                detail.append("extra=" + ",".join(extra))
            raise SolverPackageLayoutError(
                "Manifest and Operation solver keys are inconsistent ({} )".format(
                    "; ".join(detail)
                ).replace("( )", "()")
            )
        _validate_indexes(root, expected, require_operation_index=require_operation_index)

    result_operations.sort(key=lambda item: item.operation_key)
    return SolverPackageLayoutResult(
        layout=OPERATION_CENTRIC_LAYOUT if operation_centric else LEGACY_LAYOUT,
        legacy=not operation_centric,
        operations=tuple(result_operations),
    )


def migrate_solver_package_layout(
    source_root: Path,
    target_root: Path | None = None,
    *,
    dry_run: bool = True,
    allow_in_place: bool = True,
    require_operation_index: bool = True,
    target_version: str | None = None,
) -> SolverPackageMigrationReport:
    """Plan or apply migration from the historical three-directory layout.

    Distinct ``target_root`` paths represent a new package revision and leave
    the source untouched.  In-place migration is supported for drafts only;
    revisions marked released/published/immutable are always rejected before
    any write.  Dry-run mode performs all reads and validation but writes
    neither the target nor the source.
    """

    source = Path(source_root)
    target = Path(target_root) if target_root is not None else source
    _ensure_regular_root(source, "Source")
    manifest = _read_manifest(source)
    if not isinstance(manifest, Mapping):
        raise SolverPackageLayoutError("Source package manifest must be a mapping")

    source_marker = _layout_marker(manifest)
    if source_marker == OPERATION_CENTRIC_LAYOUT:
        # Idempotent operation: no legacy directories to move.  Still validate
        # the complete package so callers get a useful consistency error.
        validate_solver_package_layout(
            source, manifest, require_operation_index=require_operation_index
        )
        return SolverPackageMigrationReport(
            source=source.resolve(),
            target=target.resolve(),
            dry_run=dry_run,
            warnings=("Package already uses operation-centric.v1",),
        )
    if source_marker and source_marker not in _MARKER_VALUES:
        raise SolverPackageLayoutError(f"Unsupported solver package layout marker: {source_marker}")

    in_place = source.resolve() == target.resolve()
    if in_place and not allow_in_place:
        raise SolverPackageLayoutError("In-place solver package migration is disabled")
    if in_place and _is_released_manifest(manifest):
            raise SolverPackageLayoutError(
                "released solver package revisions cannot be rewritten in place; provide a new target revision"
            )
    if not in_place and target.exists():
        raise SolverPackageLayoutError("Migration target already exists")

    operations = _discover_operations(source)
    declarations = _discover_model_declarations(manifest, operations)
    declaration_by_operation: dict[str, _ModelDeclaration] = {}
    for declaration in declarations:
        if not declaration.operation_key:
            continue
        if declaration.operation_key in declaration_by_operation:
            raise SolverPackageLayoutError(
                f"Manifest declares solver operation more than once: {declaration.operation_key}"
            )
        declaration_by_operation[declaration.operation_key] = declaration
    if not operations:
        raise SolverPackageLayoutError("No solver_workflow operations found to migrate")

    moves: list[tuple[Path, Path, str, str]] = []
    updated_manifest = _deep_copy_mapping(manifest)
    updated_manifest["solver_layout"] = OPERATION_CENTRIC_LAYOUT
    if target_version is not None:
        normalized_target_version = str(target_version).strip()
        if not normalized_target_version:
            raise SolverPackageLayoutError("Migration target version must be a non-empty string")
        updated_manifest["version"] = normalized_target_version
        if "active_version" in updated_manifest:
            updated_manifest["active_version"] = normalized_target_version
    updated_declarations = _manifest_model_lists(updated_manifest)
    for operation in operations:
        declaration = declaration_by_operation.get(operation.key)
        if declaration is None:
            declaration = _find_declaration_for_model(
                declarations, operation.decision_model_id
            )
        if declaration is None:
            raise SolverPackageLayoutError(
                f"Solver operation has no manifest decision model: {operation.key}"
            )
        values = declaration.values
        source_paths = {
            "entry": _declared_value(values, "entry", "decision_model_entry", "decision_model_path"),
            "input_schema": _declared_value(values, "input_schema", "input"),
            "fact_snapshot_schema": _declared_value(
                values, "fact_snapshot_schema", "fact_schema", "fact_snapshot"
            ),
            "result_schema": _declared_value(values, "result_schema", "output_schema", "output"),
            "presentation": _declared_value(values, "presentation", "presentation_path"),
        }
        required = ("entry", "input_schema", "result_schema")
        if any(not source_paths[field] for field in required):
            raise SolverPackageLayoutError(
                f"Manifest solver operation is missing required sidecar paths: {operation.key}"
            )
        destination_names = {
            "entry": f"operations/{operation.key}.model.yaml",
            "input_schema": f"operations/{operation.key}.input.schema.json",
            "fact_snapshot_schema": f"operations/{operation.key}.fact-snapshot.schema.json",
            "result_schema": f"operations/{operation.key}.result.schema.json",
            "presentation": f"operations/{operation.key}.presentation.yaml",
        }
        model_values = _find_mutable_model_values(
            updated_declarations,
            model_id=declaration.model_id,
            operation_key=operation.key,
        )
        if model_values is None:
            raise SolverPackageLayoutError(
                f"Manifest model declaration cannot be updated: {operation.key}"
            )
        model_values["operation_key"] = operation.key
        for field, raw_source in source_paths.items():
            if not raw_source:
                continue
            source_path = resolve_solver_sidecar_path(source, raw_source, field)
            destination = Path(destination_names[field])
            moves.append((source_path, destination, field, operation.key))
            model_values[field if field != "entry" else "entry"] = destination.as_posix()

    planned = sorted(
        {
            destination.as_posix()
            for _source_path, destination, _field, _operation_key in moves
        }
        | {"manifest.yaml", "index.md", "operations/index.md"}
    )
    removed = sorted(
        {
            f"{directory}/"
            for directory in FORBIDDEN_SOLVER_DIRECTORIES
            if (source / directory).exists()
        }
    )
    warnings: list[str] = []
    if not removed:
        warnings.append("No legacy solver directories were present; manifest paths will still be normalized")

    if dry_run:
        return SolverPackageMigrationReport(
            source=source.resolve(),
            target=target.resolve(),
            dry_run=True,
            planned_files=tuple(planned),
            removed_paths=tuple(removed),
            warnings=tuple(warnings),
        )

    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="solver-layout-", dir=str(source.parent)))
        # ``mkdtemp`` creates the directory.  The copy helper intentionally
        # requires a non-existent destination, so remove only this freshly
        # allocated staging directory before copying into it.
        stage.rmdir()
        _copy_tree_without_links(source, stage)
        working = stage
        _apply_moves(working, moves, source_root=source)
        _write_solver_indexes(working, [operation.key for operation in operations])
        _write_manifest(working, updated_manifest)
        # Remove only the exact legacy solver directories after all referenced
        # files have been copied.  Refuse to discard unrelated files.
        _remove_legacy_directories_safely(working, moves, source_root=source)
        validate_solver_package_layout(
            working,
            _read_manifest(working),
            require_operation_index=require_operation_index,
        )
        copied_files = sorted(
            {
                destination.as_posix()
                for _source_path, destination, _field, _operation_key in moves
            }
            | {"manifest.yaml", "index.md", "operations/index.md"}
        )
        if in_place:
            _replace_directory_contents(source, working)
        else:
            # The target was deliberately not touched until the staged copy
            # passed the complete layout validator.
            os.replace(str(working), str(target))
            stage = None
        return SolverPackageMigrationReport(
            source=source.resolve(),
            target=target.resolve(),
            dry_run=False,
            planned_files=tuple(planned),
            copied_files=tuple(copied_files),
            removed_paths=tuple(removed),
            warnings=tuple(warnings),
        )
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


# Backwards/forward-compatible aliases used by import-boundary callers.
validate_solver_layout = validate_solver_package_layout
migrate_solver_layout = migrate_solver_package_layout


def _layout_marker(manifest: Mapping[str, Any] | None) -> str:
    if not isinstance(manifest, Mapping):
        return ""
    candidates: list[Any] = []
    for key in _MARKER_KEYS:
        if key in manifest:
            candidates.append(manifest.get(key))
    for container_key in ("capabilities", "solver", "solver_capabilities"):
        container = manifest.get(container_key)
        if isinstance(container, Mapping):
            for key in _MARKER_KEYS:
                if key in container:
                    candidates.append(container.get(key))
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            normalized = text.casefold().replace("_", "-")
            if normalized in {value.casefold().replace("_", "-") for value in _MARKER_VALUES}:
                return OPERATION_CENTRIC_LAYOUT
            return text
    return ""


def _read_manifest(root: Path) -> Mapping[str, Any]:
    path = root / "manifest.yaml"
    if not path.is_file() or path.is_symlink():
        raise SolverPackageLayoutError("Solver package manifest is missing or unsafe")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SolverPackageLayoutError("Solver package manifest is malformed") from exc
    if not isinstance(value, Mapping):
        raise SolverPackageLayoutError("Solver package manifest must be a mapping")
    return value


def _discover_operations(root: Path) -> tuple[_Operation, ...]:
    operations_dir = root / "operations"
    if not operations_dir.is_dir() or operations_dir.is_symlink():
        return ()
    found: list[_Operation] = []
    seen: set[str] = set()
    for path in sorted(operations_dir.glob("*.md"), key=lambda item: item.name.casefold()):
        if path.name.casefold() == "index.md":
            continue
        frontmatter = _read_frontmatter(path)
        if not isinstance(frontmatter, Mapping):
            continue
        if str(frontmatter.get("resource") or "").strip() != "operation":
            continue
        if not _enabled(frontmatter.get("enabled", True)):
            continue
        execution = frontmatter.get("execution")
        if not isinstance(execution, Mapping):
            continue
        if str(execution.get("mode") or "").strip() != "solver_workflow":
            continue
        if not _enabled(execution.get("enabled", True)):
            continue
        key = str(frontmatter.get("key") or "").strip()
        if not key:
            raise SolverPackageLayoutError(f"Solver operation has no key: {path.name}")
        if not _SOLVER_OPERATION_KEY_RE.fullmatch(key):
            raise SolverPackageLayoutError(
                f"Solver operation key is invalid: {key!r}; expected [a-z][a-z0-9_]{{0,31}}"
            )
        if key in seen:
            raise SolverPackageLayoutError(f"Duplicate solver operation key: {key}")
        model_id = str(
            execution.get("decision_model")
            or execution.get("decision_model_id")
            or frontmatter.get("decision_model")
            or frontmatter.get("decision_model_id")
            or ""
        ).strip()
        seen.add(key)
        found.append(
            _Operation(
                key=key,
                path=path,
                decision_model_id=model_id,
                enabled=True,
                frontmatter=frontmatter,
            )
        )
    return tuple(found)


def _discover_model_declarations(
    manifest: Mapping[str, Any], operations: Iterable[_Operation]
) -> tuple[_ModelDeclaration, ...]:
    capabilities = manifest.get("capabilities")
    raw_models: list[Any] = []
    raw_solver_operations: list[Any] = []
    if isinstance(capabilities, Mapping):
        if isinstance(capabilities.get("decision_models"), list):
            raw_models.extend(capabilities["decision_models"])
        if isinstance(capabilities.get("solver_operations"), list):
            raw_solver_operations.extend(capabilities["solver_operations"])
    if isinstance(manifest.get("decision_models"), list):
        raw_models.extend(manifest["decision_models"])
    if isinstance(manifest.get("solver_operations"), list):
        raw_solver_operations.extend(manifest["solver_operations"])

    operation_model_ids = {operation.key: operation.decision_model_id for operation in operations}
    # Keep the two manifest vocabularies as independent inputs first.  A v0.2
    # package may legitimately declare a model in ``decision_models`` and its
    # operation metadata in ``solver_operations``; treating those as duplicate
    # declarations would reject the normal package shape.  We merge matching
    # entries by operation key or model id below.
    model_entries: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, Mapping) or not _enabled(raw.get("enabled", True)):
            continue
        model_id = str(raw.get("id") or raw.get("model_id") or "").strip()
        explicit_operation = str(
            raw.get("operation_key") or raw.get("operation") or raw.get("bound_operation") or ""
        ).strip()
        operation_key = explicit_operation
        if not operation_key and model_id:
            matches = [key for key, candidate in operation_model_ids.items() if candidate == model_id]
            if len(matches) == 1:
                operation_key = matches[0]
        model_entries.append(
            {
                "model_id": model_id,
                "operation_key": operation_key,
                "values": dict(raw),
                "kind": "model",
            }
        )
    operation_entries: list[dict[str, Any]] = []
    for raw in raw_solver_operations:
        if not isinstance(raw, Mapping) or not _enabled(raw.get("enabled", True)):
            continue
        operation_key = str(
            raw.get("operation_key") or raw.get("operation") or raw.get("key") or ""
        ).strip()
        model_id = str(
            raw.get("decision_model") or raw.get("decision_model_id") or raw.get("model_id") or ""
        ).strip()
        operation_entries.append(
            {
                "model_id": model_id,
                "operation_key": operation_key,
                "values": dict(raw),
                "kind": "operation",
            }
        )

    merged: list[dict[str, Any]] = []
    for operation_entry in operation_entries:
        operation_key = str(operation_entry["operation_key"] or "").strip()
        model_id = str(operation_entry["model_id"] or "").strip()
        matches = [
            item
            for item in model_entries
            if (
                operation_key
                and str(item["operation_key"] or "").strip() == operation_key
            )
            or (model_id and str(item["model_id"] or "").strip() == model_id)
        ]
        if len(matches) > 1:
            raise SolverPackageLayoutError(
                "Manifest solver operation maps to multiple decision models: {}".format(
                    operation_key or model_id or "<missing>"
                )
            )
        if matches:
            model_entry = matches[0]
            model_entry["values"].update(operation_entry["values"])
            if operation_key:
                model_entry["operation_key"] = operation_key
            if model_id:
                model_entry["model_id"] = model_id
            continue
        merged.append(operation_entry)

    merged.extend(model_entries)
    declarations: list[_ModelDeclaration] = []
    for entry in merged:
        model_id = str(entry["model_id"] or "").strip()
        operation_key = str(entry["operation_key"] or "").strip()
        if not operation_key and model_id:
            matches = [
                key
                for key, candidate in operation_model_ids.items()
                if candidate == model_id
            ]
            if len(matches) == 1:
                operation_key = matches[0]
        declarations.append(
            _ModelDeclaration(
                model_id=model_id,
                operation_key=operation_key,
                values=entry["values"],
            )
        )
    return tuple(declarations)


def _find_declaration_for_model(
    declarations: Iterable[_ModelDeclaration], model_id: str
) -> _ModelDeclaration | None:
    candidates = [item for item in declarations if item.model_id == model_id and model_id]
    return candidates[0] if len(candidates) == 1 else None


def _declared_value(values: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\\", "/")
    return ""


def _validate_indexes(
    root: Path,
    expected: set[str],
    *,
    require_operation_index: bool,
) -> None:
    root_keys = _index_operation_keys(root / "index.md")
    if root_keys != expected:
        _raise_index_mismatch("Root index", expected, root_keys)
    if require_operation_index:
        operation_keys = _index_operation_keys(root / "operations" / "index.md")
        if operation_keys != expected:
            _raise_index_mismatch("Operations index", expected, operation_keys)


def _raise_index_mismatch(label: str, expected: set[str], actual: set[str]) -> None:
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    detail: list[str] = []
    if missing:
        detail.append("missing=" + ",".join(missing))
    if extra:
        detail.append("extra=" + ",".join(extra))
    raise SolverPackageLayoutError(
        f"{label} is inconsistent with solver operations ({'; '.join(detail) or 'duplicate or malformed rows'})"
    )


def _index_operation_keys(path: Path) -> set[str]:
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    rows = _markdown_tables(text)
    keys: list[str] = []
    for headers, data_rows in rows:
        normalized_headers = [header.strip().casefold() for header in headers]
        operation_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if header in {item.casefold() for item in _OPERATION_KEY_HEADERS}
            or "operation key" in header
            or "操作" in header
        ]
        if not operation_columns:
            continue
        column = operation_columns[0]
        for row in data_rows:
            if column < len(row):
                value = row[column].strip().strip("`")
                if value and value not in {"-", "—"}:
                    keys.append(value)
    if keys:
        # Preserve duplicate detection by returning a sentinel impossible to
        # confuse with a real operation key.  The validator will report an
        # index mismatch rather than silently accepting duplicate rows.
        result = set(keys)
        if len(keys) != len(result):
            result.add("__duplicate_operation_key__")
        return result

    # Compatibility indexes produced by the v0.1 converter were often plain
    # prose.  Extract conservative operation-looking tokens from those files;
    # strict operation-centric packages should use a table and therefore hit
    # the branch above.
    fallback = re.findall(r"\b[a-z][a-z0-9_]{1,63}\b", text)
    return set(fallback)


def _markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    lines = str(text or "").splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        header = _split_table_row(lines[index])
        separator = _split_table_row(lines[index + 1])
        if header is None or separator is None or not _is_separator(separator):
            index += 1
            continue
        rows: list[list[str]] = []
        index += 2
        while index < len(lines):
            row = _split_table_row(lines[index])
            if row is None or len(row) != len(header):
                break
            rows.append(row)
            index += 1
        tables.append((header, rows))
    return tables


def _split_table_row(line: str) -> list[str] | None:
    stripped = str(line or "").strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    cells = re.split(r"(?<!\\)\|", stripped)
    return [cell.strip().replace("\\|", "|") for cell in cells] if len(cells) >= 2 else None


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _read_frontmatter(path: Path) -> Mapping[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        value = yaml.safe_load(match.group("body")) or {}
    except yaml.YAMLError:
        return None
    return value if isinstance(value, Mapping) else None


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1", "yes", "on", "enabled"}


def _ensure_regular_root(root: Path, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise SolverPackageLayoutError(f"{label} package root must be a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SolverPackageLayoutError("Solver package contains symbolic links")


def _deep_copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    # YAML-compatible values are JSON-shaped in the package manifest.  A JSON
    # round trip avoids mutating the caller's dictionary while retaining plain
    # dictionaries/lists for updates.
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise SolverPackageLayoutError("Solver package manifest is not serializable") from exc


def _manifest_model_lists(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    capabilities = manifest.get("capabilities")
    if isinstance(capabilities, dict) and isinstance(capabilities.get("decision_models"), list):
        result.extend(item for item in capabilities["decision_models"] if isinstance(item, dict))
    if isinstance(manifest.get("decision_models"), list):
        result.extend(item for item in manifest["decision_models"] if isinstance(item, dict))
    return result


def _find_mutable_model_values(
    models: Iterable[dict[str, Any]], *, model_id: str, operation_key: str
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in models
        if str(item.get("id") or item.get("model_id") or "").strip() == model_id
        or str(item.get("operation_key") or item.get("operation") or "").strip() == operation_key
    ]
    return candidates[0] if len(candidates) == 1 else None


def _apply_moves(
    working_root: Path,
    moves: Iterable[tuple[Path, Path, str, str]],
    *,
    source_root: Path,
) -> None:
    for source_path, destination, field, operation_key in moves:
        # Re-resolve the source relative path against the working copy; never
        # copy from an arbitrary path supplied by a manifest after staging.
        source_relative = source_path.resolve().relative_to(source_root.resolve())
        staged_source = working_root / source_relative
        if not staged_source.is_file() or staged_source.is_symlink():
            raise SolverPackageLayoutError(
                f"Solver {field} is missing for operation: {operation_key}"
            )
        destination_path = working_root / destination
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            if destination_path.is_symlink() or not destination_path.is_file():
                raise SolverPackageLayoutError(
                    f"Migration destination is not a regular file: {destination.as_posix()}"
                )
            if destination_path.read_bytes() != staged_source.read_bytes():
                raise SolverPackageLayoutError(
                    f"Migration destination already exists with different content: {destination.as_posix()}"
                )
        else:
            shutil.copy2(staged_source, destination_path)


def _remove_legacy_directories_safely(
    working_root: Path,
    moves: Iterable[tuple[Path, Path, str, str]],
    *,
    source_root: Path,
) -> None:
    # Compare package-relative source paths because moves are planned against
    # the source revision while cleanup runs against a staged copy.
    referenced_relative: set[str] = set()
    for path, _destination, _field, _operation_key in moves:
        try:
            relative = path.resolve().relative_to(Path(source_root).resolve())
        except ValueError as exc:
            raise SolverPackageLayoutError("Migration source path escaped source package") from exc
        referenced_relative.add(relative.as_posix().casefold())
    for directory_name in sorted(FORBIDDEN_SOLVER_DIRECTORIES):
        directory = working_root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(working_root).as_posix().casefold()
            if relative not in referenced_relative:
                raise SolverPackageLayoutError(
                    f"Refusing to remove unreferenced legacy solver file: {path.relative_to(working_root).as_posix()}"
                )
        shutil.rmtree(directory)


def _copy_tree_without_links(source: Path, target: Path) -> None:
    if target.exists():
        raise SolverPackageLayoutError("Migration target already exists")
    try:
        shutil.copytree(source, target, symlinks=False)
    except (OSError, shutil.Error) as exc:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise SolverPackageLayoutError("Unable to stage solver package migration") from exc
    _ensure_regular_root(target, "Staged")


def _write_manifest(root: Path, manifest: Mapping[str, Any]) -> None:
    path = root / "manifest.yaml"
    path.write_text(
        yaml.safe_dump(dict(manifest), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_solver_indexes(root: Path, operation_keys: Iterable[str]) -> None:
    """Write deterministic root and operation index projections.

    Migration must not preserve prose from a legacy index when it would make
    the new strict one-to-one contract ambiguous.  The generated projections
    intentionally stay lightweight; full operation documentation remains in
    the individual Markdown files.
    """

    keys = sorted({str(key).strip() for key in operation_keys if str(key).strip()})
    root_path = root / "index.md"
    root_path.write_text(
        "# Solver capabilities\n\n"
        "| capability key | operation key | execution |\n"
        "| --- | --- | --- |\n"
        + "".join(f"| {key} | {key} | solver_workflow |\n" for key in keys),
        encoding="utf-8",
    )
    operations_path = root / "operations" / "index.md"
    operations_path.parent.mkdir(parents=True, exist_ok=True)
    operations_path.write_text(
        "# Operations\n\n"
        "| operation key | type |\n"
        "| --- | --- |\n"
        + "".join(f"| {key} | solver_workflow |\n" for key in keys),
        encoding="utf-8",
    )


def _replace_directory_contents(source: Path, staged: Path) -> None:
    # Keep the source directory itself stable for callers holding its path.
    for child in list(source.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in list(staged.iterdir()):
        destination = source / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, destination, symlinks=False)
        else:
            shutil.copy2(child, destination)


def _is_released_manifest(manifest: Mapping[str, Any]) -> bool:
    values: list[Any] = [
        manifest.get("status"),
        manifest.get("release_status"),
        manifest.get("state"),
        manifest.get("lifecycle"),
    ]
    lifecycle = manifest.get("lifecycle")
    if isinstance(lifecycle, Mapping):
        values.append(lifecycle.get("status"))
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("status")
        if isinstance(value, bool):
            if value:
                return True
            continue
        if str(value or "").strip().casefold() in _RELEASED_VALUES:
            return True
    for key in ("released", "published", "immutable", "locked"):
        value = manifest.get(key)
        if isinstance(value, bool) and value:
            return True
    for marker in ("RELEASED", ".released", "release.marker"):
        if (Path.cwd() / marker).is_file():
            # Never treat a process cwd marker as package state.  This branch
            # is intentionally omitted; package status must be manifest-owned.
            return False
    return False


__all__ = [
    "FORBIDDEN_SOLVER_DIRECTORIES",
    "LEGACY_LAYOUT",
    "OPERATION_CENTRIC_LAYOUT",
    "SolverOperationLayout",
    "SolverPackageLayoutError",
    "SolverPackageLayoutResult",
    "SolverPackageMigrationReport",
    "is_operation_centric_layout",
    "migrate_solver_layout",
    "migrate_solver_package_layout",
    "resolve_solver_sidecar_path",
    "solver_layout_marker",
    "validate_solver_layout",
    "validate_solver_package_layout",
]
