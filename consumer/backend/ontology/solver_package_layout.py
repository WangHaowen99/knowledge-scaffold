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
from jsonschema import Draft202012Validator


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
        LEGACY_LAYOUT,
    }
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
    }
)
_SOLVER_MARKER_VALUES = frozenset(
    {
        "solver_workflow",
        "solver-workflow",
        "solver",
        "solver operation",
        "solver_operation",
        "solver capability",
        "solver_capability",
    }
)
_SOLVER_INDEX_START = "<!-- solver-capabilities:v1:start -->"
_SOLVER_INDEX_END = "<!-- solver-capabilities:v1:end -->"
_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\r?\n(?P<body>.*?)^---[ \t]*(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)
_SOLVER_OPERATION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_OPERATION_KEY_HEADERS_CASEFOLDED = frozenset(
    item.casefold() for item in _OPERATION_KEY_HEADERS
)
_SOLVER_MARKER_VALUES_CASEFOLDED = frozenset(
    item.casefold() for item in _SOLVER_MARKER_VALUES
)
_EXECUTION_MODE_HEADERS = frozenset(
    {"execution", "execution_mode", "execution mode", "type", "kind"}
)
_SIDECAR_SUFFIX_RE = re.compile(
    r"(?:\.model\.ya?ml"
    r"|\.input\.schema\.json"
    r"|\.result\.schema\.json"
    r"|\.fact(?:[-_]snapshot)?\.schema\.json"
    r"|\.presentation\.ya?ml)\b",
    re.IGNORECASE,
)


def _is_operation_key_header(header: str) -> bool:
    """Return whether a casefolded table header names the operation column.

    The Chinese variants are accepted here too, or a hand-written index would
    parse on read but be rejected on write.
    """
    if header in _OPERATION_KEY_HEADERS_CASEFOLDED:
        return True
    return "operation key" in header or "操作" in header


def _is_solver_marker_header(header: str) -> bool:
    """Return whether a casefolded table header can carry a solver marker."""
    if header in _SOLVER_MARKER_VALUES_CASEFOLDED:
        return True
    return header in _EXECUTION_MODE_HEADERS or "solver" in header


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


def _path_escapes_package(raw: str, relative: Path) -> bool:
    """Return whether a declared relative path can point outside the package."""

    if relative.is_absolute() or raw.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:", raw):
        return True
    return any(part in {"", ".", ".."} for part in relative.parts)


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
    _ensure_regular_root(root, "Solver")
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw:
        raise SolverPackageLayoutError(f"{label.capitalize()} path is missing")
    relative = Path(raw)
    if _path_escapes_package(raw, relative):
        raise SolverPackageLayoutError(f"{label.capitalize()} path must be contained in the package")

    root_resolved = root.resolve(strict=True)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if _is_link_like(cursor):
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
    _ensure_regular_root(root, "Solver")
    if manifest is None:
        manifest = _read_manifest(root)
    if not isinstance(manifest, Mapping):
        raise SolverPackageLayoutError("Solver package manifest must be a mapping")

    # Reuse the package-wide declarative safety policy for all sidecar
    # manifests/index metadata.  Import lazily to avoid an ontology ↔ layout
    # module import cycle during package discovery.
    try:
        from ontology.ontologies import validate_no_runtime_binding_data

        validate_no_runtime_binding_data(manifest)
    except ValueError as exc:
        raise SolverPackageLayoutError(str(exc)) from exc

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
        if operation_centric and operation.decision_model_id != model_id:
            raise SolverPackageLayoutError(
                "Operation decision model does not match manifest declaration: {}".format(
                    operation.key
                )
            )
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
            if operation_centric and relative.parent != Path("operations"):
                raise SolverPackageLayoutError(
                    f"Operation-centric {field} must be under operations/: {relative.as_posix()}"
                )
            previous = referenced_sidecars.get(relative)
            if previous and previous != operation.key:
                raise SolverPackageLayoutError(
                    f"Solver sidecar is shared by multiple operations: {relative.as_posix()}"
                )
            referenced_sidecars[relative] = operation.key
            _validate_sidecar_document(resolved, field)
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
    allow_in_place: bool = False,
    require_operation_index: bool = True,
    target_version: str | None = None,
) -> SolverPackageMigrationReport:
    """Run one migration under the same lock used by package authoring."""

    source = Path(source_root)
    if not source.is_dir() or _is_link_like(source):
        raise SolverPackageLayoutError("Source package root must be a regular directory")
    owning_root = (
        source.parent.parent
        if source.parent.name.casefold() == "versions"
        else source
    )
    from ontology.mutation_lock import ontology_mutation_lock

    with ontology_mutation_lock(owning_root):
        return _migrate_solver_package_layout_locked(
            source,
            target_root,
            dry_run=dry_run,
            allow_in_place=allow_in_place,
            require_operation_index=require_operation_index,
            target_version=target_version,
        )


def _migrate_solver_package_layout_locked(
    source_root: Path,
    target_root: Path | None = None,
    *,
    dry_run: bool = True,
    allow_in_place: bool = False,
    require_operation_index: bool = True,
    target_version: str | None = None,
) -> SolverPackageMigrationReport:
    """Plan or apply migration from the historical three-directory layout.

    Applied migrations create a distinct package revision and leave the
    source untouched.  An omitted/in-place target is supported only for a
    dry-run.  Before publishing, the source content is compared with its
    initial revision so concurrent edits fail closed.
    """

    source = Path(source_root)
    target = Path(target_root) if target_root is not None else source
    _ensure_regular_root(source, "Source")
    source_resolved = source.resolve()
    target_resolved = target.resolve(strict=False)
    in_place = source_resolved == target_resolved
    if not in_place and target_resolved.is_relative_to(source_resolved):
        raise SolverPackageLayoutError(
            "Migration target must be outside the source package"
        )
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
        if not in_place:
            raise SolverPackageLayoutError(
                "Package already uses operation-centric.v1; use a versioning copy workflow for a distinct target"
            )
        return SolverPackageMigrationReport(
            source=source.resolve(),
            target=target.resolve(),
            dry_run=dry_run,
            warnings=("Package already uses operation-centric.v1",),
        )
    if source_marker and source_marker not in _MARKER_VALUES:
        raise SolverPackageLayoutError(f"Unsupported solver package layout marker: {source_marker}")

    if in_place and not dry_run:
        raise SolverPackageLayoutError(
            "Applied solver package migration requires a new target revision"
        )
    if in_place and not allow_in_place and target_root is not None:
        raise SolverPackageLayoutError("In-place solver package migration is disabled")
    if not in_place and (target.exists() or target.is_symlink()):
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
    source_version = str(manifest.get("version") or "").strip()
    if not in_place and target_version is None:
        raise SolverPackageLayoutError(
            "A distinct migration target requires target_version"
        )
    if target_version is not None:
        normalized_target_version = str(target_version).strip()
        if not normalized_target_version or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*",
            normalized_target_version,
        ) is None:
            raise SolverPackageLayoutError("Migration target version is invalid")
        if not in_place and normalized_target_version == source_version:
            raise SolverPackageLayoutError(
                "A distinct migration target requires a new target_version"
            )
        updated_manifest["version"] = normalized_target_version
        if "active_version" in updated_manifest:
            updated_manifest["active_version"] = normalized_target_version
        if "versions" in updated_manifest:
            updated_manifest["versions"] = [normalized_target_version]
    updated_declarations = _manifest_model_lists(updated_manifest)
    migrated_model_ids: set[str] = set()
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
        if declaration.model_id in migrated_model_ids:
            raise SolverPackageLayoutError(
                "Legacy solver operations sharing one decision model must be split before migration"
            )
        migrated_model_ids.add(declaration.model_id)
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
        if not source_paths["fact_snapshot_schema"]:
            source_paths["fact_snapshot_schema"] = _fact_snapshot_path_from_model(
                source,
                source_paths["entry"],
            )
        if not source_paths["fact_snapshot_schema"] and len(operations) == 1:
            source_paths["fact_snapshot_schema"] = _infer_fact_snapshot_path(
                source,
                operation_key=operation.key,
                decision_model_id=declaration.model_id,
            )
        required = ("entry", "input_schema", "result_schema")
        if any(not source_paths.get(field) for field in required):
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
            destination_name = destination_names.get(field, "")
            if not destination_name:
                raise SolverPackageLayoutError(
                    f"Migration has no destination for sidecar field: {field}"
                )
            destination = Path(destination_name)
            moves.append((source_path, destination, field, operation.key))
            model_values[field if field != "entry" else "entry"] = destination.as_posix()
        _update_manifest_solver_references(
            updated_manifest,
            operation_key=operation.key,
            decision_model_id=declaration.model_id,
            destination_names=destination_names,
            source_paths=source_paths,
        )

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

    from ontology.view_cache import build_package_content_revision

    source_content_revision = build_package_content_revision(source)

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
        _rewrite_solver_sidecars(
            working,
            source_root=source,
            moves=moves,
        )
        _rewrite_operation_documents(
            working,
            operations=operations,
            source_root=source,
            moves=moves,
        )
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
        if build_package_content_revision(source) != source_content_revision:
            raise SolverPackageLayoutError(
                "Source solver package changed during migration"
            )
        copied_files = sorted(
            {
                destination.as_posix()
                for _source_path, destination, _field, _operation_key in moves
            }
            | {"manifest.yaml", "index.md", "operations/index.md"}
        )
        if dry_run:
            # The staged tree has gone through the same copy, rewrite,
            # cleanup, and final validation path as an applied migration.  It
            # is discarded below, so dry-run never mutates source or target.
            return SolverPackageMigrationReport(
                source=source.resolve(),
                target=target.resolve(),
                dry_run=True,
                planned_files=tuple(planned),
                removed_paths=tuple(removed),
                warnings=tuple(warnings),
            )
        # The target was deliberately not touched until the staged copy
        # passed validation and the source CAS check.
        moved_stage = False
        try:
            if build_package_content_revision(source) != source_content_revision:
                raise SolverPackageLayoutError(
                    "Source solver package changed during migration"
                )
            os.replace(str(working), str(target))
            moved_stage = True
        except OSError:
            target.parent.mkdir(parents=True, exist_ok=True)
            target_stage = Path(
                tempfile.mkdtemp(prefix="solver-layout-target-", dir=str(target.parent))
            )
            shutil.rmtree(target_stage)
            try:
                _copy_tree_without_links(working, target_stage)
                if build_package_content_revision(source) != source_content_revision:
                    raise SolverPackageLayoutError(
                        "Source solver package changed during migration"
                    )
                os.replace(str(target_stage), str(target))
            except (OSError, SolverPackageLayoutError):
                if target_stage.exists():
                    shutil.rmtree(target_stage, ignore_errors=True)
                raise
        if moved_stage:
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
            if normalized == LEGACY_LAYOUT:
                return LEGACY_LAYOUT
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
    if not operations_dir.is_dir() or _is_link_like(operations_dir):
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


def _model_entry_matches(entry: Mapping[str, Any], operation_key: str, model_id: str) -> bool:
    """Return whether a merged manifest entry names the same model declaration."""
    if operation_key and str(entry["operation_key"] or "").strip() == operation_key:
        return True
    return bool(model_id and str(entry["model_id"] or "").strip() == model_id)


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
            if _model_entry_matches(item, operation_key, model_id)
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
    root_keys = _index_operation_keys(root / "index.md", expected=expected)
    if root_keys != expected:
        _raise_index_mismatch("Root index", expected, root_keys)
    if require_operation_index:
        operation_keys = _index_operation_keys(
            root / "operations" / "index.md", expected=expected
        )
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


def _index_operation_keys(path: Path, *, expected: set[str] | None = None) -> set[str]:
    """Extract solver rows without mistaking ordinary package tables for them.

    A knowledge-package index is a shared navigation document: its root index
    commonly contains object and operation tables, and ``operations/index.md``
    contains non-solver operations too.  Solver rows therefore need an
    explicit marker (the generated block), a solver execution/type column, or
    a sidecar reference.  A generic ``key`` column is deliberately *not*
    sufficient evidence.
    """
    if not path.is_file() or path.is_symlink():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    start_count = text.count(_SOLVER_INDEX_START)
    end_count = text.count(_SOLVER_INDEX_END)
    marked_projection = start_count or end_count
    if marked_projection:
        if start_count != 1 or end_count != 1:
            return {"__invalid_solver_index_markers__"}
        marker_start = text.find(_SOLVER_INDEX_START)
        marker_end = text.find(_SOLVER_INDEX_END)
        if marker_end < marker_start:
            return {"__invalid_solver_index_markers__"}
        # Generated solver projection is authoritative for this index.  This
        # lets ordinary rows remain in the document without entering the
        # solver consistency check.
        text = text[marker_start + len(_SOLVER_INDEX_START) : marker_end]
    rows = _markdown_tables(text)
    keys: list[str] = []
    for headers, data_rows in rows:
        normalized_headers = [header.strip().casefold() for header in headers]
        operation_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if _is_operation_key_header(header)
        ]
        if not operation_columns:
            continue
        solver_columns = {
            index
            for index, header in enumerate(normalized_headers)
            if _is_solver_marker_header(header)
        }
        column = operation_columns[0]
        for row in data_rows:
            if column < len(row):
                values = [str(cell or "").strip().strip("`") for cell in row]
                value = values[column]
                if not value or value in {"-", "—"}:
                    continue
                marker_values = {
                    values[index].casefold()
                    for index in solver_columns
                    if index < len(values)
                }
                sidecar_row = any(
                    _SIDECAR_SUFFIX_RE.search(cell) for cell in values
                )
                explicit_solver = bool(marker_values & _SOLVER_MARKER_VALUES)
                # If an expected set is available, a row that names an
                # expected operation and carries sidecar columns is also an
                # unambiguous solver projection (used by hand-authored v1
                # indexes before migration adds the marker block).
                expected_sidecar = bool(expected and value in expected and sidecar_row)
                if explicit_solver or sidecar_row or expected_sidecar:
                    keys.append(value)
    if keys:
        # Preserve duplicate detection by returning a sentinel impossible to
        # confuse with a real operation key.  The validator will report an
        # index mismatch rather than silently accepting duplicate rows.
        result = set(keys)
        if len(keys) != len(result):
            result.add("__duplicate_operation_key__")
        return result

    return set()


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


def _starts_markdown_table(lines: list[str], index: int) -> bool:
    """Return whether ``lines[index]`` is a header row followed by a separator."""
    if index + 1 >= len(lines):
        return False
    if _split_table_row(lines[index]) is None:
        return False
    return _is_separator(_split_table_row(lines[index + 1]) or [])


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


def _validate_sidecar_document(path: Path, label: str) -> None:
    """Check sidecar syntax and declarative safety at the package boundary."""

    try:
        if path.suffix.casefold() == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # ValueError also covers the decode and JSON failures raised above.
        raise SolverPackageLayoutError(f"{label.capitalize()} sidecar is malformed") from exc
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise SolverPackageLayoutError(f"{label.capitalize()} sidecar must be a mapping")
    if path.suffix.casefold() == ".json":
        try:
            _reject_remote_schema_references(value)
            Draft202012Validator.check_schema(dict(value))
        except Exception as exc:
            raise SolverPackageLayoutError(
                f"{label.capitalize()} sidecar is not a valid local JSON Schema"
            ) from exc
    try:
        from ontology.ontologies import validate_no_runtime_binding_data

        validate_no_runtime_binding_data(value)
    except ValueError as exc:
        raise SolverPackageLayoutError(str(exc)) from exc


def _reject_remote_schema_references(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in {"$ref", "$dynamicRef"}:
                if not isinstance(item, str) or not item.startswith("#/"):
                    raise ValueError("Only local JSON Schema references are allowed")
            if str(key) == "$id" and isinstance(item, str):
                normalized = item.strip().casefold()
                if normalized.startswith(("http:", "https:", "//")):
                    raise ValueError("remote JSON Schema identifiers are not allowed")
            _reject_remote_schema_references(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_remote_schema_references(item)


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return str(value or "").strip().casefold() in {"true", "1", "yes", "on", "enabled"}


def _ensure_regular_root(root: Path, label: str) -> None:
    if not root.is_dir() or _is_link_like(root):
        raise SolverPackageLayoutError(f"{label} package root must be a regular directory")
    for path in root.rglob("*"):
        if _is_link_like(path):
            raise SolverPackageLayoutError(
                "Solver package contains symbolic links or junctions"
            )


def _is_link_like(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


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


def _mutable_model_matches(item: Mapping[str, Any], model_id: str, operation_key: str) -> bool:
    """Return whether a raw manifest model maps to the given model or operation."""
    if str(item.get("id") or item.get("model_id") or "").strip() == model_id:
        return True
    return str(item.get("operation_key") or item.get("operation") or "").strip() == operation_key


def _find_mutable_model_values(
    models: Iterable[dict[str, Any]], *, model_id: str, operation_key: str
) -> dict[str, Any] | None:
    candidates = [
        item
        for item in models
        if _mutable_model_matches(item, model_id, operation_key)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _stem_carries_preferred_token(path: Path, preferred_tokens: set[str]) -> bool:
    """Return whether a candidate file name contains one of the expected tokens."""
    normalized_stem = re.sub(r"[^a-z0-9]+", "-", path.stem.casefold()).strip("-")
    return any(token in normalized_stem for token in preferred_tokens)


def _infer_fact_snapshot_path(
    root: Path,
    *,
    operation_key: str,
    decision_model_id: str,
) -> str:
    """Infer a legacy fact-snapshot sidecar from deterministic file names.

    Older v0.2 packages (including early released revisions) stored the fact
    snapshot schema in ``schemas/`` but omitted the field from the model
    declaration.  Only an exact, unambiguous filename match is accepted; no
    arbitrary file is selected.
    """

    candidates: list[Path] = []
    for directory in (root / "schemas", root / "operations"):
        if not directory.is_dir() or _is_link_like(directory):
            continue
        for path in directory.iterdir():
            if not path.is_file() or _is_link_like(path) or path.suffix.casefold() != ".json":
                continue
            stem = path.stem.casefold()
            if "fact" not in stem or "snapshot" not in stem:
                continue
            candidates.append(path)
    preferred_tokens = set()
    for raw_token in (operation_key, decision_model_id):
        normalized = re.sub(r"[^a-z0-9]+", "-", str(raw_token or "").casefold()).strip("-")
        if normalized:
            preferred_tokens.add(normalized)
    preferred = [
        path
        for path in candidates
        if _stem_carries_preferred_token(path, preferred_tokens)
    ]
    if len(preferred) == 1:
        return preferred[0].relative_to(root).as_posix()
    return ""


def _fact_snapshot_path_from_model(root: Path, model_path: str) -> str:
    if not model_path:
        return ""
    resolved = resolve_solver_sidecar_path(root, model_path, "decision model")
    model = _read_yaml_mapping_file(resolved, "decision model")
    return _declared_value(
        model,
        "fact_snapshot_schema",
        "fact_schema",
        "fact_snapshot",
    )


def _read_yaml_mapping_file(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SolverPackageLayoutError(f"{label.capitalize()} is malformed") from exc
    if not isinstance(value, Mapping):
        raise SolverPackageLayoutError(f"{label.capitalize()} must be a mapping")
    return value


def _update_manifest_solver_references(
    manifest: dict[str, Any],
    *,
    operation_key: str,
    decision_model_id: str,
    destination_names: Mapping[str, str],
    source_paths: Mapping[str, str],
) -> None:
    """Normalize all known manifest-level solver path references in place."""

    def matches(item: Mapping[str, Any]) -> bool:
        item_operation = str(
            item.get("operation_key") or item.get("operation") or ""
        ).strip()
        item_model = str(
            item.get("id") or item.get("model_id") or item.get("decision_model")
            or item.get("decision_model_id") or ""
        ).strip()
        return item_operation == operation_key or (
            bool(decision_model_id) and item_model == decision_model_id
        )

    def rewrite(item: dict[str, Any]) -> None:
        if not matches(item):
            return
        item["operation_key"] = operation_key
        for field, destination in destination_names.items():
            if field == "entry":
                item["entry"] = destination
            elif field == "fact_snapshot_schema" and not source_paths.get(field):
                continue
            elif source_paths.get(field):
                item[field] = destination

    containers: list[Any] = [manifest]
    capabilities = manifest.get("capabilities")
    if isinstance(capabilities, Mapping):
        containers.append(capabilities)
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("decision_models", "solver_operations"):
            values = container.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    rewrite(item)

    # The package-level presentation declaration is another path-bearing
    # reference used by old packages.  Normalize only when it points at this
    # operation's legacy presentation (or when it is a mapping with an
    # explicit operation/model discriminator).
    presentation = manifest.get("presentation")
    if isinstance(presentation, dict):
        if matches(presentation) or not any(
            key in presentation for key in ("operation_key", "operation", "model_id", "decision_model")
        ):
            if source_paths.get("presentation"):
                presentation["contract"] = destination_names["presentation"]
    elif isinstance(presentation, str) and source_paths.get("presentation"):
        if presentation.replace("\\", "/") == source_paths["presentation"]:
            manifest["presentation"] = destination_names["presentation"]


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
        if not staged_source.is_file() or _is_link_like(staged_source):
            raise SolverPackageLayoutError(
                f"Solver {field} is missing for operation: {operation_key}"
            )
        destination_path = working_root / destination
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            if _is_link_like(destination_path) or not destination_path.is_file():
                raise SolverPackageLayoutError(
                    f"Migration destination is not a regular file: {destination.as_posix()}"
                )
            if destination_path.read_bytes() != staged_source.read_bytes():
                raise SolverPackageLayoutError(
                    f"Migration destination already exists with different content: {destination.as_posix()}"
                )
        else:
            shutil.copy2(staged_source, destination_path)


def _rewrite_operation_documents(
    working_root: Path,
    *,
    operations: Iterable[_Operation],
    source_root: Path,
    moves: Iterable[tuple[Path, Path, str, str]],
) -> None:
    """Rewrite operation frontmatter/contract tables to the new sidecars."""

    moves_by_operation: dict[str, list[tuple[Path, Path, str, str]]] = {}
    for move in moves:
        moves_by_operation.setdefault(move[3], []).append(move)

    replacements_by_operation: dict[str, dict[str, str]] = {}
    for operation in operations:
        destinations = {
            field: destination.as_posix()
            for _source_path, destination, field, move_operation_key in moves_by_operation.get(operation.key, [])
            if move_operation_key == operation.key
        }
        replacements: dict[str, str] = {}
        for field, destination in destinations.items():
            # ``execution.decision_model`` is the stable model *identity*,
            # not a filesystem path; update its path only in the contract
            # table below.  Rewriting it here would make the catalog unable to
            # resolve the declared model.
            if field == "input_schema":
                replacements["input_schema"] = destination
            elif field == "fact_snapshot_schema":
                replacements["fact_snapshot_schema"] = destination
            elif field == "result_schema":
                replacements["result_schema"] = destination
            elif field == "presentation":
                replacements["presentation"] = destination
        replacements_by_operation[operation.key] = replacements

    for operation in operations:
        replacements = replacements_by_operation.get(operation.key) or {}
        if not replacements:
            continue
        relative = operation.path.resolve().relative_to(Path(source_root).resolve())
        path = working_root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SolverPackageLayoutError(
                f"Operation document cannot be read: {operation.key}"
            ) from exc
        frontmatter_match = _FRONTMATTER_RE.match(text)
        if frontmatter_match is None:
            raise SolverPackageLayoutError(
                f"Operation document frontmatter is malformed: {operation.key}"
            )
        frontmatter_text = frontmatter_match.group("body")
        body_text = text[frontmatter_match.end() :]
        for field, destination in replacements.items():
            pattern = re.compile(
                rf"(?m)^(?P<prefix>[ \t]*"
                rf"(?:{re.escape(field)}|{re.escape(field.replace('_', '-'))})"
                rf"[ \t]*:[ \t]*)(?P<value>[^\r\n#]+)"
            )
            frontmatter_text = pattern.sub(
                lambda match: match.group("prefix") + destination,
                frontmatter_text,
            )
        text = "---\n" + frontmatter_text.rstrip() + "\n---\n" + body_text.lstrip("\r\n")
        # In prose, update only exact inline-code tokens.  Broad substring
        # replacement would corrupt archive/example paths that merely contain
        # the legacy resource name.
        for move_source, destination, _field, move_operation_key in moves_by_operation.get(
            operation.key, []
        ):
            old = move_source.relative_to(source_root.resolve()).as_posix()
            destination_text = destination.as_posix()
            text = text.replace(f"`{old}`", f"`{destination_text}`")
        path.write_text(text, encoding="utf-8")


def _rewrite_solver_sidecars(
    working_root: Path,
    *,
    source_root: Path,
    moves: Iterable[tuple[Path, Path, str, str]],
) -> None:
    """Rewrite exact legacy path values inside copied YAML sidecars.

    Decision models and presentation contracts often reference the input,
    fact-snapshot, or result schemas themselves.  After the legacy directories
    are removed those references must point to the copied ``operations/``
    sidecars too.  Recursive mapping/list traversal avoids broad text
    substitution while preserving ordinary strings and comments is not a
    requirement for machine-owned YAML sidecars.
    """

    moves_by_operation: dict[str, list[tuple[Path, Path, str, str]]] = {}
    for move in moves:
        moves_by_operation.setdefault(move[3], []).append(move)
    source_root_resolved = source_root.resolve()
    for operation_moves in moves_by_operation.values():
        replacements = {
            source_path.relative_to(source_root_resolved).as_posix(): destination.as_posix()
            for source_path, destination, _field, _operation_key in operation_moves
        }
        yaml_destinations = {
            destination
            for _source_path, destination, field, _operation_key in operation_moves
            if field in {"entry", "presentation"}
        }
        for relative in yaml_destinations:
            path = working_root / relative
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise SolverPackageLayoutError(
                    f"Solver sidecar cannot be rewritten: {relative.as_posix()}"
                ) from exc
            rewritten = _replace_exact_path_values(document, replacements)
            if not isinstance(rewritten, Mapping):
                raise SolverPackageLayoutError(
                    f"Solver sidecar must be a mapping: {relative.as_posix()}"
                )
            path.write_text(
                yaml.safe_dump(dict(rewritten), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )


def _replace_exact_path_values(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _replace_exact_path_values(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_exact_path_values(item, replacements) for item in value]
    if isinstance(value, str):
        normalized = value.strip().replace("\\", "/")
        return replacements.get(normalized, value)
    return value


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
        referenced_relative.add(relative.as_posix())
    for directory_name in sorted(FORBIDDEN_SOLVER_DIRECTORIES):
        directory = working_root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(working_root).as_posix()
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
    except OSError as exc:
        # shutil.Error, raised for per-file copytree failures, is an OSError.
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
    """Upsert a marked solver projection while preserving shared indexes.

    ``index.md`` and ``operations/index.md`` are package-wide navigation
    documents, not solver-only files.  Replacing either document would erase
    object and ordinary-operation rows.  A bounded marker block gives the
    layout validator a precise projection to check and makes a second
    migration deterministic/idempotent.
    """

    keys = sorted({str(key).strip() for key in operation_keys if str(key).strip()})
    root_path = root / "index.md"
    _upsert_solver_index_block(
        root_path,
        "# Solver capabilities",
        "| capability key | operation key | execution |\n"
        "| --- | --- | --- |\n"
        + "".join(f"| {key} | {key} | solver_workflow |\n" for key in keys),
    )
    operations_path = root / "operations" / "index.md"
    operations_path.parent.mkdir(parents=True, exist_ok=True)
    _mark_solver_rows_in_operation_index(operations_path, keys)


def _mark_solver_rows_in_operation_index(path: Path, operation_keys: list[str]) -> None:
    """Mark existing operation rows so the first shared table stays canonical."""

    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise SolverPackageLayoutError("Solver operation index cannot be read") from exc
    lines = existing.splitlines()
    table_start = next(
        (index for index in range(len(lines)) if _starts_markdown_table(lines, index)),
        None,
    )
    if table_start is None:
        generated = (
            (existing.rstrip() + "\n\n" if existing.strip() else "")
            + "| operation key | execution |\n| --- | --- |\n"
            + "".join(f"| {key} | solver_workflow |\n" for key in operation_keys)
        )
        path.write_text(generated, encoding="utf-8")
        return
    headers = _split_table_row(lines[table_start]) or []
    normalized = [header.strip().casefold() for header in headers]
    operation_column = next(
        (
            index
            for index, header in enumerate(normalized)
            if _is_operation_key_header(header)
        ),
        None,
    )
    if operation_column is None:
        raise SolverPackageLayoutError(
            "operations/index.md has no operation key column"
        )
    type_column = next(
        (index for index, header in enumerate(normalized) if header in {"type", "kind", "execution"}),
        None,
    )
    if type_column is None:
        headers.append("execution")
        type_column = len(headers) - 1
        lines[table_start] = "| " + " | ".join(headers) + " |"
        separator = _split_table_row(lines[table_start + 1]) or []
        separator.append("---")
        lines[table_start + 1] = "| " + " | ".join(separator) + " |"
    found: set[str] = set()
    row_index = table_start + 2
    while row_index < len(lines):
        row = _split_table_row(lines[row_index])
        if row is None:
            break
        while len(row) < len(headers):
            row.append("")
        key = row[operation_column].strip().strip("`")
        if key in operation_keys:
            row[type_column] = "solver_workflow"
            found.add(key)
        lines[row_index] = "| " + " | ".join(row[: len(headers)]) + " |"
        row_index += 1
    missing = [key for key in operation_keys if key not in found]
    if missing:
        for key in missing:
            row = [""] * len(headers)
            row[operation_column] = key
            row[type_column] = "solver_workflow"
            lines.insert(row_index, "| " + " | ".join(row) + " |")
            row_index += 1
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _upsert_solver_index_block(path: Path, heading: str, table: str) -> None:
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise SolverPackageLayoutError("Solver index cannot be read") from exc
    block = "\n".join(
        (
            _SOLVER_INDEX_START,
            heading,
            "",
            table.rstrip(),
            _SOLVER_INDEX_END,
        )
    )
    start = existing.find(_SOLVER_INDEX_START)
    end = existing.find(_SOLVER_INDEX_END)
    if start >= 0 and end >= start:
        end += len(_SOLVER_INDEX_END)
        updated = existing[:start].rstrip() + "\n\n" + block + existing[end:]
    elif start >= 0:
        raise SolverPackageLayoutError(
            f"Solver index marker is incomplete: {path.name}"
        )
    else:
        prefix = existing.rstrip()
        updated = (prefix + "\n\n" if prefix else "") + block
    path.write_text(updated.rstrip() + "\n", encoding="utf-8")


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
