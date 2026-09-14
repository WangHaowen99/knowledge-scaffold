# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Deterministic operation package contracts and integrity helpers.

The v0.1 package format remains the only runtime format.  Operations opt in by
adding an ``execution`` mapping; packages without it do not pass through these
strict validators.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml
from integration.sandbox.runtime_contract import (
    allowed_external_import_roots,
    allowed_runtime_imports,
    allowed_standard_library_imports,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

DETERMINISTIC_LOCK_NAME = ".deterministic-lock.json"
DETERMINISTIC_LOCK_VERSION = 2
_SUPPORTED_DETERMINISTIC_LOCK_VERSIONS = frozenset({1, DETERMINISTIC_LOCK_VERSION})
MAX_SCHEMA_BYTES = 256 * 1024
_EXECUTION_FIELDS = frozenset({"type", "parser", "input_schema", "output_schema"})
_DETERMINISTIC_EXECUTION_OWNER = "deterministic"
_SOLVER_EXECUTION_OWNER = "solver_workflow"
_VALIDATION_EXECUTION_OWNER = "validation_workflow"
_PROTECTED_EXECUTION_OWNERS = frozenset({_DETERMINISTIC_EXECUTION_OWNER, _VALIDATION_EXECUTION_OWNER})
_DATA_ACCESS_VALUES = frozenset({"none", "workspace_provider"})
_FRONTMATTER_END = re.compile(r"\r?\n---(?:\r?\n|$)")
_NONDETERMINISTIC_CALL_PREFIXES = (
    "random.",
    "secrets.",
    "tempfile.",
    "uuid.",
)
_NONDETERMINISTIC_CALLS = frozenset(
    {
        "__import__",
        "argparse.FileType",
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
        "importlib.import_module",
        "io.FileIO",
        "io.open",
        "io.open_code",
        "os.getrandom",
        "os.urandom",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.fork",
        "os.forkpty",
        "os.popen",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.setpgid",
        "os.setsid",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.system",
        "operator.attrgetter",
        "time.monotonic",
        "time.monotonic_ns",
        "time.perf_counter",
        "time.perf_counter_ns",
        "time.process_time",
        "time.process_time_ns",
        "time.time",
        "time.time_ns",
    }
)
_FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS = frozenset(
    {
        "asyncio",
        "concurrent",
        "ctypes",
        "datetime",
        "glob",
        "importlib",
        "inspect",
        "marshal",
        "multiprocessing",
        "os",
        "pathlib",
        "pickle",
        "platform",
        "random",
        "resource",
        "secrets",
        "signal",
        "socket",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "time",
        "uuid",
    }
)
_FORBIDDEN_DETERMINISTIC_BUILTIN_CALLS = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "id",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
_FORBIDDEN_DETERMINISTIC_ATTRIBUTES = frozenset(
    {
        "__base__",
        "__bases__",
        "__builtins__",
        "__class__",
        "__closure__",
        "__code__",
        "__dict__",
        "__getattr__",
        "__getattribute__",
        "__globals__",
        "__mro__",
        "__reduce__",
        "__reduce_ex__",
        "__subclasses__",
    }
)


class DeterministicPackageError(ValueError):
    """A stable, client-visible deterministic package validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeterministicHandlerSpec:
    script_key: str
    operation_key: str
    descriptor_path: str
    entrypoint: str
    data_access: str
    sha256: str


@dataclass(frozen=True)
class DeterministicOperationSpec:
    operation_key: str
    aliases: tuple[str, ...]
    operation_path: str
    parser_path: Optional[str]
    input_schema_path: str
    output_schema_path: str
    handler: DeterministicHandlerSpec
    package_revision: str = ""
    routing_all_terms: tuple[str, ...] = ()
    routing_any_terms: tuple[str, ...] = ()


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one JSON encoding used by package and execution hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def normalize_operation_token(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.strip().casefold().split())


def protected_operation_keys(ontology_dir: Path) -> frozenset[str]:
    """Discover opt-in operation keys without requiring a valid installed lock.

    This deliberately uses a lenient read.  An invalid deterministic package
    must still be hidden from Agent tools instead of becoming executable through
    a legacy fallback path.
    """
    result: set[str] = set()
    all_operation_keys: set[str] = set()
    root = Path(ontology_dir)
    package_schema_version = _execution_package_schema_version(root)
    operations_dir = root / "operations"
    if operations_dir.is_dir():
        for path in _markdown_files(operations_dir):
            try:
                frontmatter = _read_frontmatter(path)
            except DeterministicPackageError:
                all_operation_keys.add(path.stem)
                if _has_suspected_execution_declaration(path):
                    result.add(path.stem)
                continue
            key = str(frontmatter.get("key") or path.stem).strip()
            aliases = _aliases(frontmatter.get("aliases"), strict=False)
            if key:
                all_operation_keys.add(key)
            all_operation_keys.update(aliases)
            try:
                execution_owner = _execution_declaration_owner(
                    path,
                    frontmatter,
                    package_schema_version=package_schema_version,
                )
            except DeterministicPackageError:
                # Invalid declarations remain protected until strict validation
                # can report the stable package error.
                execution_owner = _DETERMINISTIC_EXECUTION_OWNER
            if execution_owner not in _PROTECTED_EXECUTION_OWNERS:
                continue
            if key:
                result.add(key)
            result.update(aliases)
    lock_present = is_deterministic_package_locked(root)
    try:
        locked_operations = read_deterministic_lock(root).get("operations")
    except DeterministicPackageError:
        locked_operations = None
    if isinstance(locked_operations, Mapping):
        result.update(str(key).strip() for key in locked_operations if str(key).strip())
        if lock_present and not locked_operations:
            result.update(all_operation_keys)
    elif lock_present:
        # An unreadable or semantically incomplete lock must never reopen a
        # generic execution path. Conservatively reserve the whole catalog.
        result.update(all_operation_keys)
    return frozenset(result)


def is_deterministic_operation(ontology_dir: Path, operation_key: str) -> bool:
    normalized = normalize_operation_token(operation_key)
    protected = {normalize_operation_token(item) for item in protected_operation_keys(ontology_dir)}
    return bool(normalized and normalized in protected)


def is_protected_operation_descriptor(
    ontology_dir: Path,
    descriptor_path: Path,
) -> bool:
    """Classify the resolved operation descriptor instead of its lookup token."""
    root = Path(ontology_dir).resolve()
    path = Path(descriptor_path).resolve()
    package_schema_version = _execution_package_schema_version(root)
    try:
        path.relative_to((root / "operations").resolve())
    except ValueError:
        return False

    try:
        frontmatter = _read_frontmatter(path)
    except DeterministicPackageError:
        if _has_suspected_execution_declaration(path):
            return True
        frontmatter = {}
    try:
        execution_owner = _execution_declaration_owner(
            path,
            frontmatter,
            package_schema_version=package_schema_version,
        )
    except DeterministicPackageError:
        return True
    if execution_owner in _PROTECTED_EXECUTION_OWNERS:
        return True
    tokens = {
        str(frontmatter.get("key") or path.stem).strip(),
        *_aliases(frontmatter.get("aliases"), strict=False),
    }
    protected = {
        normalize_operation_token(item) for item in protected_operation_keys(root) if normalize_operation_token(item)
    }
    if any(normalize_operation_token(token) in protected for token in tokens if token):
        return True

    try:
        locked_operations = read_deterministic_lock(root).get("operations")
    except DeterministicPackageError:
        locked_operations = None
    if not isinstance(locked_operations, Mapping):
        return False
    for raw_operation in locked_operations.values():
        if not isinstance(raw_operation, Mapping):
            continue
        raw_path = str(raw_operation.get("operation_path") or "").strip()
        if not raw_path:
            continue
        try:
            if _resolve_locked_path(root, raw_path) == path:
                return True
        except DeterministicPackageError:
            continue
    return False


def is_protected_handler_descriptor(
    ontology_dir: Path,
    descriptor_path: Path,
) -> bool:
    """Classify a resolved script descriptor by its bound operation/handler."""
    root = Path(ontology_dir).resolve()
    path = Path(descriptor_path).resolve()
    try:
        path.relative_to((root / "scripts").resolve())
    except ValueError:
        return False

    frontmatter = _read_frontmatter_lenient(path)
    operation_key = str(frontmatter.get("bound_operation") or frontmatter.get("operation_key") or "").strip()
    if operation_key and is_deterministic_operation(root, operation_key):
        return True

    entrypoint = str(frontmatter.get("entrypoint") or frontmatter.get("path") or "").strip()
    if not entrypoint:
        return False
    try:
        resolved_entrypoint = _resolve_handler_entrypoint(root, entrypoint).resolve()
    except DeterministicPackageError:
        return False
    return resolved_entrypoint in protected_handler_paths(root)


def is_deterministic_package_locked(ontology_dir: Path) -> bool:
    """Return whether an installed version must be treated as immutable.

    A symlinked lock is invalid for execution, but it still means the version
    opted into deterministic execution and must fail closed at write gates.
    """
    lock_path = Path(ontology_dir) / DETERMINISTIC_LOCK_NAME
    return lock_path.is_file() or lock_path.is_symlink()


def protected_handler_paths(ontology_dir: Path) -> frozenset[Path]:
    """Return handler paths protected by either live declarations or the lock."""
    root = Path(ontology_dir).resolve()
    result: set[Path] = set()
    try:
        result.update(
            (root / spec.handler.entrypoint).resolve()
            for spec in load_deterministic_operation_specs(root, require_lock=False)
        )
    except DeterministicPackageError:
        pass
    # A malformed declaration must not make its bound handler available to a
    # legacy caller. Recover the binding leniently even when strict package
    # validation (for example Schema validation) has failed.
    protected_keys = {
        normalize_operation_token(item) for item in protected_operation_keys(root) if normalize_operation_token(item)
    }
    scripts_dir = root / "scripts"
    if protected_keys and scripts_dir.is_dir():
        for descriptor_path in _markdown_files(scripts_dir):
            frontmatter = _read_frontmatter_lenient(descriptor_path)
            operation_key = str(frontmatter.get("bound_operation") or frontmatter.get("operation_key") or "").strip()
            if normalize_operation_token(operation_key) not in protected_keys:
                continue
            entrypoint = str(frontmatter.get("entrypoint") or frontmatter.get("path") or "").strip()
            try:
                result.add(_resolve_handler_entrypoint(root, entrypoint).resolve())
            except DeterministicPackageError:
                continue
    try:
        locked_operations = read_deterministic_lock(root).get("operations")
    except DeterministicPackageError:
        locked_operations = None
    if isinstance(locked_operations, Mapping):
        for raw_operation in locked_operations.values():
            if not isinstance(raw_operation, Mapping):
                continue
            raw_handler = str(raw_operation.get("handler") or "").strip()
            if not raw_handler:
                continue
            try:
                result.add(_resolve_locked_path(root, raw_handler))
            except DeterministicPackageError:
                continue
    return frozenset(result)


def load_deterministic_operation_specs(
    ontology_dir: Path,
    *,
    require_lock: bool = True,
    verify_integrity: bool = False,
) -> tuple[DeterministicOperationSpec, ...]:
    """Load deterministic specs from an installed ontology version directory."""
    root = Path(ontology_dir)
    specs = _validate_deterministic_operations(root)
    if not specs:
        if require_lock and (root / DETERMINISTIC_LOCK_NAME).exists():
            lock = read_deterministic_lock(root)
            locked_operations = lock.get("operations")
            if isinstance(locked_operations, Mapping) and locked_operations:
                raise DeterministicPackageError(
                    "package_integrity_mismatch",
                    "Deterministic declarations do not match the installed lock.",
                )
        return ()
    package_revision = ""
    if require_lock:
        lock = read_deterministic_lock(root)
        package_revision = str(lock.get("package_revision") or "")
        _validate_locked_operation_index(root, lock=lock, specs=specs)
        if verify_integrity:
            verify_deterministic_package_lock(root, lock=lock)
    if require_lock and specs and not package_revision:
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock is missing package_revision.",
        )
    return tuple(
        DeterministicOperationSpec(
            operation_key=spec.operation_key,
            aliases=spec.aliases,
            operation_path=spec.operation_path,
            parser_path=spec.parser_path,
            input_schema_path=spec.input_schema_path,
            output_schema_path=spec.output_schema_path,
            handler=spec.handler,
            package_revision=package_revision,
            routing_all_terms=spec.routing_all_terms,
            routing_any_terms=spec.routing_any_terms,
        )
        for spec in specs
    )


def validate_and_lock_deterministic_operations(
    ontology_dir: Path,
) -> tuple[DeterministicOperationSpec, ...]:
    """Validate opt-in operations and atomically generate their package lock."""
    root = Path(ontology_dir)
    lock_path = root / DETERMINISTIC_LOCK_NAME
    specs = _validate_deterministic_operations(root)
    if not specs:
        if lock_path.exists():
            lock_path.unlink()
        return ()

    entries = _package_file_entries(root)
    package_revision = sha256_bytes(canonical_json_bytes(entries))
    payload = {
        "version": DETERMINISTIC_LOCK_VERSION,
        "package_revision": package_revision,
        "files": entries,
        "operations": {
            spec.operation_key: {
                "operation_path": spec.operation_path,
                "routing_tokens": sorted(
                    {
                        normalize_operation_token(token)
                        for token in (spec.operation_key, *spec.aliases)
                        if normalize_operation_token(token)
                    },
                    key=lambda item: item.encode("utf-8"),
                ),
                "handler": spec.handler.entrypoint,
                "parser": spec.parser_path,
                "input_schema": spec.input_schema_path,
                "output_schema": spec.output_schema_path,
            }
            for spec in sorted(specs, key=lambda item: item.operation_key.encode("utf-8"))
        },
    }
    temporary_path = lock_path.with_name(lock_path.name + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, lock_path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise DeterministicPackageError(
            "deterministic_lock_failed",
            "Failed to write deterministic package lock.",
        ) from exc

    return tuple(
        DeterministicOperationSpec(
            operation_key=spec.operation_key,
            aliases=spec.aliases,
            operation_path=spec.operation_path,
            parser_path=spec.parser_path,
            input_schema_path=spec.input_schema_path,
            output_schema_path=spec.output_schema_path,
            handler=spec.handler,
            package_revision=package_revision,
            routing_all_terms=spec.routing_all_terms,
            routing_any_terms=spec.routing_any_terms,
        )
        for spec in specs
    )


def read_deterministic_lock(ontology_dir: Path) -> dict[str, Any]:
    path = Path(ontology_dir) / DETERMINISTIC_LOCK_NAME
    if not path.is_file() or path.is_symlink():
        raise DeterministicPackageError(
            "deterministic_lock_missing",
            "Deterministic package lock is missing.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock is invalid.",
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") not in _SUPPORTED_DETERMINISTIC_LOCK_VERSIONS:
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock version is invalid.",
        )
    return payload


def _validate_locked_operation_index(
    root: Path,
    *,
    lock: Mapping[str, Any],
    specs: tuple[DeterministicOperationSpec, ...],
) -> None:
    """Keep pre-routing metadata bound to the installed package lock.

    Lock v2 carries the normalized routing index directly.  Lock v1 packages
    remain readable, but their Operation declarations are hash-checked before
    routing because v1 did not persist aliases or descriptor paths.
    """
    locked_operations = lock.get("operations")
    if not isinstance(locked_operations, Mapping):
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock operations are invalid.",
        )
    current_by_key = {spec.operation_key: spec for spec in specs}
    locked_keys = {str(key) for key in locked_operations}
    if locked_keys != set(current_by_key):
        raise DeterministicPackageError(
            "package_integrity_mismatch",
            "Deterministic declarations do not match the installed lock.",
        )

    lock_version = int(lock.get("version") or 0)
    if lock_version == 1:
        raw_entries = lock.get("files")
        if not isinstance(raw_entries, list):
            raise DeterministicPackageError(
                "deterministic_lock_invalid",
                "Deterministic package lock files are invalid.",
            )
        operation_paths = set()
        for entry in raw_entries:
            if not isinstance(entry, Mapping):
                continue
            entry_path = str(entry.get("path") or "")
            if not entry_path.startswith("operations/"):
                continue
            if entry_path.casefold().endswith(".md"):
                operation_paths.add(entry_path)
        verify_deterministic_package_lock(
            root,
            lock=lock,
            critical_paths=operation_paths,
        )

    for operation_key, spec in current_by_key.items():
        raw_operation = locked_operations.get(operation_key)
        if not isinstance(raw_operation, Mapping):
            raise DeterministicPackageError(
                "deterministic_lock_invalid",
                "Deterministic package lock operation is invalid.",
            )
        expected = {
            "handler": spec.handler.entrypoint,
            "parser": spec.parser_path,
            "input_schema": spec.input_schema_path,
            "output_schema": spec.output_schema_path,
        }
        if any(raw_operation.get(key) != value for key, value in expected.items()):
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Deterministic declarations do not match the installed lock.",
            )
        if lock_version < 2:
            continue
        expected_tokens = sorted(
            {
                normalize_operation_token(token)
                for token in (spec.operation_key, *spec.aliases)
                if normalize_operation_token(token)
            },
            key=lambda item: item.encode("utf-8"),
        )
        if (
            raw_operation.get("operation_path") != spec.operation_path
            or raw_operation.get("routing_tokens") != expected_tokens
        ):
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Deterministic routing declarations do not match the installed lock.",
            )


def verify_deterministic_package_lock(
    ontology_dir: Path,
    *,
    lock: Optional[Mapping[str, Any]] = None,
    critical_paths: Optional[Iterable[str]] = None,
    cancel_event: Any = None,
) -> str:
    """Verify lock contents and return the package revision."""
    root = Path(ontology_dir).resolve()
    payload = dict(lock or read_deterministic_lock(root))
    raw_entries = payload.get("files")
    if not isinstance(raw_entries, list):
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock files are invalid.",
        )
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise DeterministicPackageError(
                "deterministic_lock_invalid",
                "Deterministic package lock entry is invalid.",
            )
        path = str(raw.get("path") or "")
        size = raw.get("size")
        sha256 = str(raw.get("sha256") or "")
        if not path or not isinstance(size, int) or not sha256.startswith("sha256:"):
            raise DeterministicPackageError(
                "deterministic_lock_invalid",
                "Deterministic package lock entry is incomplete.",
            )
        entries.append({"path": path, "size": size, "sha256": sha256})

    expected_revision = sha256_bytes(canonical_json_bytes(entries))
    package_revision = str(payload.get("package_revision") or "")
    if package_revision != expected_revision:
        raise DeterministicPackageError(
            "package_integrity_mismatch",
            "Deterministic package revision does not match its lock.",
        )

    # Verify the complete version payload, not only paths that were present
    # when the lock was generated.  An added Python module can change import
    # resolution just as surely as editing the handler itself.
    if critical_paths is None:
        try:
            actual_entries = _package_file_entries(root, cancel_event=cancel_event)
        except DeterministicPackageError as exc:
            if exc.code == "execution_cancelled":
                raise
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Deterministic package contents could not be verified.",
            ) from exc
        except OSError as exc:
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Deterministic package contents could not be verified.",
            ) from exc
        if actual_entries != entries:
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Deterministic package contents do not match its lock.",
            )
        # ``_package_file_entries`` already read and hashed every file. Avoid a
        # second full pass through the same package in one verification call.
        return package_revision

    selected = {str(path) for path in critical_paths}
    known_paths = {entry["path"] for entry in entries}
    missing_selected = selected - known_paths
    if missing_selected:
        raise DeterministicPackageError(
            "package_integrity_mismatch",
            "Locked package file is missing: {}.".format(
                sorted(missing_selected, key=lambda item: item.encode("utf-8"))[0]
            ),
        )
    for entry in entries:
        if entry["path"] not in selected:
            continue
        path = _resolve_locked_path(root, entry["path"])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Locked package file is missing: {}.".format(entry["path"]),
            ) from exc
        if len(data) != entry["size"] or sha256_bytes(data) != entry["sha256"]:
            raise DeterministicPackageError(
                "package_integrity_mismatch",
                "Locked package file changed: {}.".format(entry["path"]),
            )
    return package_revision


def _validate_deterministic_operations(
    ontology_dir: Path,
) -> tuple[DeterministicOperationSpec, ...]:
    root = Path(ontology_dir).resolve()
    package_schema_version = _execution_package_schema_version(root)
    operations_dir = root / "operations"
    scripts_dir = root / "scripts"
    if not operations_dir.is_dir():
        return ()

    operation_records: list[tuple[Path, dict[str, Any], Optional[str]]] = []
    all_tokens: dict[str, list[str]] = {}
    operation_key_sources: dict[str, list[Path]] = {}
    deterministic_declared_keys: set[str] = set()
    for operation_path in _markdown_files(operations_dir):
        frontmatter = _read_optional_execution_frontmatter(operation_path)
        operation_key = str(frontmatter.get("key") or operation_path.stem).strip()
        if not operation_key:
            continue
        execution_owner = _execution_declaration_owner(
            operation_path,
            frontmatter,
            package_schema_version=package_schema_version,
        )
        operation_records.append((operation_path, frontmatter, execution_owner))
        operation_key_sources.setdefault(operation_key, []).append(operation_path)
        if execution_owner == _DETERMINISTIC_EXECUTION_OWNER:
            deterministic_declared_keys.add(operation_key)
        for token in (operation_key, *_aliases(frontmatter.get("aliases"), strict=False)):
            normalized = normalize_operation_token(token)
            if normalized:
                all_tokens.setdefault(normalized, []).append(operation_key)

    duplicate_keys = {
        key: paths
        for key, paths in operation_key_sources.items()
        if len(paths) > 1 and key in deterministic_declared_keys
    }
    if duplicate_keys:
        key = sorted(duplicate_keys, key=lambda item: item.encode("utf-8"))[0]
        sources = ", ".join(
            sorted(
                (_relative_path(root, path) for path in duplicate_keys[key]),
                key=lambda item: item.encode("utf-8"),
            )
        )
        raise DeterministicPackageError(
            "deterministic_alias_conflict",
            "Deterministic operation key '{}' is declared more than once: {}.".format(
                key,
                sources,
            ),
        )

    script_records: list[tuple[Path, dict[str, Any]]] = []
    if scripts_dir.is_dir():
        for descriptor_path in _markdown_files(scripts_dir):
            script_records.append((descriptor_path, _read_frontmatter_lenient(descriptor_path)))

    specs: list[DeterministicOperationSpec] = []
    for operation_path, frontmatter, execution_owner in operation_records:
        if execution_owner != _DETERMINISTIC_EXECUTION_OWNER:
            continue
        execution = frontmatter.get("execution")
        if not isinstance(execution, Mapping):
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Operation '{}' execution must be a mapping.".format(operation_path.name),
            )
        unknown = sorted(set(execution) - _EXECUTION_FIELDS)
        if unknown:
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Operation '{}' execution has unsupported fields: {}.".format(
                    operation_path.name,
                    ", ".join(unknown),
                ),
            )
        execution_type = str(execution.get("type") or "").strip().casefold()
        if execution_type != "deterministic":
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Operation '{}' execution.type must be deterministic.".format(operation_path.name),
            )

        operation_key = str(frontmatter.get("key") or operation_path.stem).strip()
        if not operation_key:
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic operation key is required.",
            )
        aliases = _aliases(frontmatter.get("aliases"), strict=True)
        routing_all_terms, routing_any_terms = _deterministic_routing_terms(frontmatter.get("execution_policy"))
        for token in (operation_key, *aliases):
            collisions = {
                candidate
                for candidate in all_tokens.get(normalize_operation_token(token), [])
                if candidate != operation_key
            }
            if collisions:
                raise DeterministicPackageError(
                    "deterministic_alias_conflict",
                    "Deterministic operation '{}' token '{}' conflicts with {}.".format(
                        operation_key,
                        token,
                        ", ".join(sorted(collisions)),
                    ),
                )
        normalized_self = [normalize_operation_token(item) for item in (operation_key, *aliases)]
        if len(normalized_self) != len(set(normalized_self)):
            raise DeterministicPackageError(
                "deterministic_alias_conflict",
                "Deterministic operation '{}' has duplicate key or aliases.".format(operation_key),
            )

        parser_path = _optional_execution_path(root, execution.get("parser"), "parser")
        if parser_path is not None and parser_path.suffix.casefold() != ".py":
            raise DeterministicPackageError(
                "execution_path_invalid",
                "Deterministic parser must be a Python file.",
            )
        if parser_path is not None:
            try:
                parser_path.relative_to((root / "scripts").resolve())
            except ValueError as exc:
                raise DeterministicPackageError(
                    "execution_path_invalid",
                    "Deterministic parser must be within scripts/.",
                ) from exc
        input_schema_path = _required_execution_path(root, execution.get("input_schema"), "input_schema")
        output_schema_path = _required_execution_path(root, execution.get("output_schema"), "output_schema")
        _validate_json_schema(input_schema_path, require_object_root=True)
        _validate_json_schema(output_schema_path, require_object_root=True)

        matching_scripts = []
        normalized_operation_key = normalize_operation_token(operation_key)
        for path, raw in script_records:
            bound_operation = raw.get("bound_operation") or raw.get("operation_key")
            if normalize_operation_token(bound_operation or "") == normalized_operation_key:
                matching_scripts.append((path, raw))
        if not matching_scripts:
            raise DeterministicPackageError(
                "deterministic_handler_missing",
                "Deterministic operation '{}' has no bound handler.".format(operation_key),
            )
        if len(matching_scripts) != 1:
            raise DeterministicPackageError(
                "deterministic_handler_ambiguous",
                "Deterministic operation '{}' must have exactly one bound handler.".format(operation_key),
            )
        descriptor_path, script_frontmatter = matching_scripts[0]
        script_key = str(script_frontmatter.get("key") or descriptor_path.stem).strip()
        entrypoint_value = str(script_frontmatter.get("entrypoint") or script_frontmatter.get("path") or "").strip()
        entrypoint_path = _resolve_handler_entrypoint(root, entrypoint_value)
        if entrypoint_path.suffix.casefold() != ".py":
            raise DeterministicPackageError(
                "execution_path_invalid",
                "Deterministic handler must be a Python file.",
            )
        if parser_path is not None and parser_path == entrypoint_path:
            raise DeterministicPackageError(
                "execution_path_invalid",
                "Deterministic parser and handler must use different files.",
            )
        data_access = str(script_frontmatter.get("data_access") or "none").strip().casefold()
        if data_access not in _DATA_ACCESS_VALUES:
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic handler '{}' data_access must be none or workspace_provider.".format(script_key),
            )
        deterministic_python_roots = [entrypoint_path]
        if parser_path is not None:
            deterministic_python_roots.append(parser_path)
        _validate_deterministic_python_closure(root, deterministic_python_roots)
        handler = DeterministicHandlerSpec(
            script_key=script_key,
            operation_key=operation_key,
            descriptor_path=_relative_path(root, descriptor_path),
            entrypoint=_relative_path(root, entrypoint_path),
            data_access=data_access,
            sha256=sha256_bytes(entrypoint_path.read_bytes()),
        )
        specs.append(
            DeterministicOperationSpec(
                operation_key=operation_key,
                aliases=aliases,
                operation_path=_relative_path(root, operation_path),
                parser_path=(_relative_path(root, parser_path) if parser_path is not None else None),
                input_schema_path=_relative_path(root, input_schema_path),
                output_schema_path=_relative_path(root, output_schema_path),
                handler=handler,
                routing_all_terms=routing_all_terms,
                routing_any_terms=routing_any_terms,
            )
        )
    return tuple(sorted(specs, key=lambda item: item.operation_key.encode("utf-8")))


def _validate_deterministic_python_closure(root: Path, entrypoints: Iterable[Path]) -> None:
    """Reject obvious nondeterministic APIs in Handler/Parser local dependencies.

    Deterministic packages are trusted code, so this is a qualification gate,
    not a hostile-code sandbox.  Following normal local imports catches common
    accidental clock/random/temp-path dependencies without imposing a new DSL.
    """
    root = Path(root).resolve()
    pending = [
        (
            Path(path).resolve(),
            tuple(dict.fromkeys((Path(path).resolve().parent, root))),
        )
        for path in entrypoints
    ]
    visited: set[tuple[Path, tuple[Path, ...]]] = set()
    while pending:
        path, absolute_search_roots = pending.pop()
        visit_key = (path, absolute_search_roots)
        if visit_key in visited:
            continue
        visited.add(visit_key)
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            raise DeterministicPackageError(
                "deterministic_python_invalid",
                "Deterministic Python file '{}' is invalid.".format(_relative_path(root, path)),
            ) from exc

        aliases = _python_import_aliases(tree)
        for node in ast.walk(tree):
            imported_module = ""
            if isinstance(node, ast.Import):
                for item in node.names:
                    imported_module = item.name
                    if imported_module.split(".", 1)[0] in _FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS:
                        break
                else:
                    imported_module = ""
            elif isinstance(node, ast.ImportFrom):
                imported_module = str(node.module or "")
                if any(item.name == "*" for item in node.names):
                    raise DeterministicPackageError(
                        "determinism_contract_violation",
                        "Deterministic Python file '{}' uses wildcard import '{}.*'.".format(
                            _relative_path(root, path),
                            imported_module or "relative",
                        ),
                    )
                forbidden_export = ""
                for item in node.names:
                    export_root = item.name.split(".", 1)[0]
                    if export_root in _FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS:
                        forbidden_export = item.name
                        break
                if node.level == 0 and forbidden_export:
                    raise DeterministicPackageError(
                        "determinism_contract_violation",
                        "Deterministic Python file '{}' imports unsupported module export '{}.{}'.".format(
                            _relative_path(root, path),
                            imported_module,
                            forbidden_export,
                        ),
                    )
            if imported_module and imported_module.split(".", 1)[0] in _FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS:
                raise DeterministicPackageError(
                    "determinism_contract_violation",
                    "Deterministic Python file '{}' imports unsupported module '{}'.".format(
                        _relative_path(root, path),
                        imported_module,
                    ),
                )
            if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_DETERMINISTIC_ATTRIBUTES:
                raise DeterministicPackageError(
                    "determinism_contract_violation",
                    "Deterministic Python file '{}' uses unsupported attribute '{}'.".format(
                        _relative_path(root, path),
                        node.attr,
                    ),
                )
            if isinstance(node, ast.Name) and node.id == "__builtins__":
                raise DeterministicPackageError(
                    "determinism_contract_violation",
                    "Deterministic Python file '{}' uses unsupported runtime introspection.".format(
                        _relative_path(root, path),
                    ),
                )
            if not isinstance(node, ast.Call):
                continue
            call_name = _python_call_name(node.func, aliases)
            if call_name in _FORBIDDEN_DETERMINISTIC_BUILTIN_CALLS or _is_nondeterministic_call_name(call_name):
                raise DeterministicPackageError(
                    "determinism_contract_violation",
                    "Deterministic Python file '{}' uses unsupported API '{}'.".format(
                        _relative_path(root, path),
                        call_name,
                    ),
                )

        _validate_deterministic_import_contract(
            root,
            path,
            tree,
            absolute_search_roots=absolute_search_roots,
        )
        for dependency in _local_python_imports(
            root,
            path,
            tree,
            absolute_search_roots=absolute_search_roots,
        ):
            dependency_key = (dependency, absolute_search_roots)
            if dependency_key not in visited:
                pending.append(dependency_key)


def _is_nondeterministic_call_name(call_name: str) -> bool:
    normalized = str(call_name or "").strip()
    if not normalized:
        return False
    if any(normalized == forbidden or normalized.endswith("." + forbidden) for forbidden in _NONDETERMINISTIC_CALLS):
        return True
    qualified = "." + normalized
    return any("." + prefix in qualified for prefix in _NONDETERMINISTIC_CALL_PREFIXES)


def _validate_deterministic_import_contract(
    root: Path,
    source_path: Path,
    tree: ast.AST,
    *,
    absolute_search_roots: tuple[Path, ...],
) -> None:
    allowed_external = allowed_external_import_roots()
    allowed_standard_library = allowed_standard_library_imports()
    allowed_runtime = allowed_runtime_imports()

    def standard_library_allowed(module_name: str) -> bool:
        return module_name in allowed_standard_library

    def reject(module_name: str, level: int = 0) -> None:
        raise DeterministicPackageError(
            "deterministic_dependency_unavailable",
            "Deterministic Python file '{}' imports module '{}' outside the sandbox runtime contract.".format(
                _relative_path(root, source_path),
                "." * level + module_name,
            ),
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                module_name = item.name
                import_root = module_name.split(".", 1)[0]
                if import_root in _FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS:
                    continue
                if (
                    _resolve_local_python_module(
                        root,
                        source_path,
                        module_name,
                        0,
                        absolute_search_roots=absolute_search_roots,
                    )
                    is not None
                ):
                    continue
                if standard_library_allowed(module_name) or import_root in allowed_external:
                    continue
                # Runtime capabilities are intentionally exposed only through
                # named public exports. A module import would also expose
                # implementation submodules attached to the package object.
                reject(module_name)
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        base_module = str(node.module or "")
        level = int(node.level or 0)
        imported_names = [item.name for item in node.names if item.name != "*"]
        if _resolve_local_python_import_paths(
            root,
            source_path,
            base_module,
            level,
            absolute_search_roots=absolute_search_roots,
        ):
            continue

        # PEP 420 namespace packages do not have __init__.py. In that case each
        # imported name must resolve as a concrete local submodule.
        if imported_names and all(
            _resolve_local_python_module(
                root,
                source_path,
                ".".join(part for part in (base_module, name) if part),
                level,
                absolute_search_roots=absolute_search_roots,
            )
            is not None
            for name in imported_names
        ):
            continue

        if level == 0 and base_module in allowed_runtime:
            unavailable_exports = sorted(
                set(imported_names) - set(allowed_runtime[base_module]),
                key=lambda item: item.encode("utf-8"),
            )
            if not unavailable_exports:
                continue
            reject("{}.{}".format(base_module, unavailable_exports[0]))

        import_root = base_module.split(".", 1)[0] if base_module else ""
        if import_root in _FORBIDDEN_DETERMINISTIC_IMPORT_ROOTS:
            continue
        if level == 0 and (standard_library_allowed(base_module) or import_root in allowed_external):
            continue
        if not base_module and imported_names:
            reject(imported_names[0], level)
        reject(base_module, level)


def _python_import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                local_name = item.asname or item.name.split(".", 1)[0]
                aliases[local_name] = item.name if item.asname else local_name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                if item.name == "*":
                    continue
                aliases[item.asname or item.name] = "{}.{}".format(
                    node.module,
                    item.name,
                )
    # Track simple callable rebinding such as ``clock = datetime.datetime.now``.
    # Dynamic construction remains prohibited by the trusted package contract.
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        resolved = _python_call_name(value, aliases) if value is not None else ""
        if not resolved:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = resolved
    return aliases


def _python_call_name(node: ast.AST, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Subscript):
        container = node.value
        key = node.slice
        if isinstance(container, ast.Name) and container.id == "__builtins__":
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value
    if isinstance(node, ast.Attribute):
        parent = _python_call_name(node.value, aliases)
        return "{}.{}".format(parent, node.attr) if parent else node.attr
    return ""


def _local_python_imports(
    root: Path,
    source_path: Path,
    tree: ast.AST,
    *,
    absolute_search_roots: tuple[Path, ...],
) -> set[Path]:
    dependencies: set[Path] = set()
    for node in ast.walk(tree):
        module_names: list[tuple[str, int]] = []
        if isinstance(node, ast.Import):
            module_names.extend((item.name, 0) for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            base_module = str(node.module or "")
            level = int(node.level or 0)
            module_names.append((base_module, level))
            module_names.extend(
                (
                    ".".join(part for part in (base_module, item.name) if part),
                    level,
                )
                for item in node.names
                if item.name != "*"
            )
        else:
            continue
        for module_name, level in module_names:
            dependencies.update(
                _resolve_local_python_import_paths(
                    root,
                    source_path,
                    module_name,
                    level,
                    absolute_search_roots=absolute_search_roots,
                )
            )
    return dependencies


def _resolve_local_python_import_paths(
    root: Path,
    source_path: Path,
    module_name: str,
    level: int,
    *,
    absolute_search_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    """Resolve every local file Python executes for one import.

    Importing ``package.module`` executes each existing package
    ``__init__.py`` before the leaf module.  The qualification closure must
    inspect that complete chain, not only the final ``module.py``.
    """
    root = Path(root).resolve()
    module_parts = [part for part in module_name.split(".") if part]
    if level:
        relative_root = source_path.parent
        for _index in range(max(0, level - 1)):
            relative_root = relative_root.parent
        search_roots = [relative_root]
    else:
        # Python keeps the entrypoint directory as ``sys.path[0]`` while it
        # executes nested imports.  It does not replace that path with each
        # dependency's parent directory.  Use the same fixed roots for the
        # complete closure so qualification inspects the file runtime imports.
        search_roots = list(absolute_search_roots)

    for search_root in search_roots:
        if not module_parts:
            candidates = (search_root / "__init__.py",) if level else ()
        else:
            module_root = search_root.joinpath(*module_parts)
            candidates = (
                module_root.with_suffix(".py"),
                module_root / "__init__.py",
            )
        leaf = next(
            (candidate.resolve() for candidate in candidates if candidate.is_file() and not candidate.is_symlink()),
            None,
        )
        if leaf is None:
            continue
        try:
            leaf.relative_to(root)
        except ValueError:
            continue

        resolved_paths: list[Path] = []
        if level:
            relative_init = search_root / "__init__.py"
            if relative_init.is_file() and not relative_init.is_symlink():
                resolved_paths.append(relative_init.resolve())
        current = search_root
        for part in module_parts[:-1]:
            current /= part
            package_init = current / "__init__.py"
            if package_init.is_file() and not package_init.is_symlink():
                resolved_paths.append(package_init.resolve())
        resolved_paths.append(leaf)
        return tuple(dict.fromkeys(resolved_paths))
    return ()


def _resolve_local_python_module(
    root: Path,
    source_path: Path,
    module_name: str,
    level: int,
    *,
    absolute_search_roots: tuple[Path, ...],
) -> Optional[Path]:
    resolved = _resolve_local_python_import_paths(
        root,
        source_path,
        module_name,
        level,
        absolute_search_roots=absolute_search_roots,
    )
    return resolved[-1] if resolved else None


def _read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Failed to read descriptor '{}'.".format(path.name),
        ) from exc
    if not text.startswith("---"):
        return {}
    match = _FRONTMATTER_END.search(text, 3)
    if match is None:
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Descriptor '{}' has unclosed YAML frontmatter.".format(path.name),
        )
    try:
        parsed = yaml.safe_load(text[3 : match.start()].strip()) or {}
    except yaml.YAMLError as exc:
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Descriptor '{}' has invalid YAML frontmatter.".format(path.name),
        ) from exc
    if not isinstance(parsed, dict):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Descriptor '{}' frontmatter must be a mapping.".format(path.name),
        )
    return parsed


def _read_optional_execution_frontmatter(path: Path) -> dict[str, Any]:
    """Keep legacy descriptors tolerant unless they opt in to execution."""
    try:
        return _read_frontmatter(path)
    except DeterministicPackageError:
        if _has_suspected_execution_declaration(path):
            raise
        return {}


def _execution_declaration_owner(
    path: Path,
    frontmatter: Mapping[str, Any],
    *,
    package_schema_version: Optional[str],
) -> Optional[str]:
    if "execution" not in frontmatter:
        return None
    execution = frontmatter.get("execution")
    if not isinstance(execution, Mapping):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Operation '{}' execution must be a mapping.".format(path.name),
        )

    has_type = "type" in execution
    has_mode = "mode" in execution
    if has_type and has_mode:
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Operation '{}' execution has conflicting type and mode discriminators.".format(path.name),
        )
    if has_type and str(execution.get("type") or "").strip().casefold() == "deterministic":
        return _DETERMINISTIC_EXECUTION_OWNER
    execution_mode = str(execution.get("mode") or "").strip() if has_mode else ""
    if execution_mode == "solver_workflow":
        if package_schema_version != "0.2":
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Operation '{}' solver_workflow requires package schema_version 0.2.".format(path.name),
            )
        return _SOLVER_EXECUTION_OWNER
    if execution_mode == "validation_workflow":
        return _VALIDATION_EXECUTION_OWNER
    raise DeterministicPackageError(
        "execution_declaration_invalid",
        "Operation '{}' execution discriminator is unsupported.".format(path.name),
    )


def _execution_package_schema_version(root: Path) -> Optional[str]:
    manifest_path = Path(root) / "manifest.yaml"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(manifest, Mapping):
        return None
    return str(manifest.get("schema_version") or "").strip() or None


def _has_suspected_execution_declaration(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return has_execution_declaration(text)


def has_execution_declaration(markdown: Any) -> bool:
    """Return whether Markdown opts into the fail-closed execution boundary."""
    text = str(markdown or "")
    if not text.startswith("---"):
        return False
    match = _FRONTMATTER_END.search(text, 3)
    frontmatter_text = text[3 : match.start()] if match is not None else text[3:]
    try:
        parsed = yaml.safe_load(frontmatter_text.strip()) or {}
    except yaml.YAMLError:
        parsed = {}
    if isinstance(parsed, Mapping) and "execution" in parsed:
        return True
    return bool(re.search(r"(?m)^[ \t]*execution[ \t]*:", frontmatter_text))


def _read_frontmatter_lenient(path: Path) -> dict[str, Any]:
    try:
        return _read_frontmatter(path)
    except DeterministicPackageError:
        return {}


def _markdown_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.rglob("*.md") if path.is_file() and path.name.casefold() != "index.md"),
        key=lambda item: item.relative_to(directory).as_posix().encode("utf-8"),
    )


def _aliases(value: Any, *, strict: bool) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        if strict:
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic operation aliases must be a list.",
            )
        return ()
    aliases = tuple(str(item or "").strip() for item in value)
    if strict and any(not item for item in aliases):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Deterministic operation aliases must not be empty.",
        )
    return tuple(item for item in aliases if item)


def _deterministic_routing_terms(
    execution_policy: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load the optional deterministic natural-language routing declaration."""
    if execution_policy in (None, ""):
        return (), ()
    if not isinstance(execution_policy, Mapping):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Deterministic operation execution_policy must be a mapping.",
        )
    routing = execution_policy.get("routing")
    if routing in (None, ""):
        return (), ()
    if not isinstance(routing, Mapping):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Deterministic operation execution_policy.routing must be a mapping.",
        )

    def read_terms(field: str) -> tuple[str, ...]:
        raw_terms = routing.get(field)
        if raw_terms in (None, ""):
            return ()
        if not isinstance(raw_terms, list):
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic routing.{} must be a list.".format(field),
            )
        terms = tuple(str(item or "").strip() for item in raw_terms)
        if any(not item for item in terms):
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic routing.{} must not contain empty terms.".format(field),
            )
        normalized = tuple(normalize_operation_token(item) for item in terms)
        if len(normalized) != len(set(normalized)):
            raise DeterministicPackageError(
                "execution_declaration_invalid",
                "Deterministic routing.{} must not contain duplicate terms.".format(field),
            )
        return terms

    return read_terms("all_terms"), read_terms("any_terms")


def _optional_execution_path(root: Path, value: Any, field: str) -> Optional[Path]:
    if value in (None, ""):
        return None
    return _resolve_package_file(root, value, field)


def _required_execution_path(root: Path, value: Any, field: str) -> Path:
    if value in (None, ""):
        raise DeterministicPackageError(
            "execution_declaration_invalid",
            "Deterministic execution.{} is required.".format(field),
        )
    return _resolve_package_file(root, value, field)


def _resolve_package_file(root: Path, value: Any, field: str) -> Path:
    raw = str(value or "").strip().replace("\\", "/")
    absolute_path = raw.startswith("/") or bool(re.match(r"^[A-Za-z]:", raw))
    invalid_parts = any(part in {"", ".", ".."} for part in raw.split("/"))
    if not raw or absolute_path or invalid_parts:
        raise DeterministicPackageError(
            "execution_path_invalid",
            "Deterministic execution.{} path is invalid.".format(field),
        )
    candidate = root / Path(*raw.split("/"))
    if candidate.is_symlink():
        raise DeterministicPackageError(
            "execution_path_invalid",
            "Deterministic execution.{} must not be a symbolic link.".format(field),
        )
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise DeterministicPackageError(
            "execution_path_invalid",
            "Deterministic execution.{} escapes the package.".format(field),
        ) from exc
    if not resolved.is_file():
        raise DeterministicPackageError(
            "execution_path_invalid",
            "Deterministic execution.{} file is missing.".format(field),
        )
    return resolved


def _resolve_handler_entrypoint(root: Path, value: Any) -> Path:
    raw = str(value or "").strip().replace("\\", "/")
    if raw and not raw.startswith("scripts/"):
        raw = "scripts/" + raw
    return _resolve_package_file(root, raw, "handler")


def _validate_json_schema(path: Path, *, require_object_root: bool) -> None:
    try:
        if path.stat().st_size > MAX_SCHEMA_BYTES:
            raise DeterministicPackageError(
                "execution_schema_invalid",
                "Schema '{}' exceeds {} bytes.".format(path.name, MAX_SCHEMA_BYTES),
            )
        schema = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_non_json_constant,
            object_pairs_hook=_reject_duplicate_object_keys,
        )
    except DeterministicPackageError:
        raise
    except (OSError, ValueError) as exc:
        raise DeterministicPackageError(
            "execution_schema_invalid",
            "Schema '{}' is not valid UTF-8 JSON.".format(path.name),
        ) from exc
    if not isinstance(schema, dict):
        raise DeterministicPackageError(
            "execution_schema_invalid",
            "Schema '{}' root must be an object.".format(path.name),
        )
    if require_object_root and schema.get("type") != "object":
        raise DeterministicPackageError(
            "execution_schema_invalid",
            "Schema '{}' must declare an object root.".format(path.name),
        )
    for ref in _iter_refs(schema):
        if not ref.startswith("#/"):
            raise DeterministicPackageError(
                "execution_schema_invalid",
                "Schema '{}' contains a non-local schema reference.".format(path.name),
            )
        if not _local_json_pointer_exists(schema, ref):
            raise DeterministicPackageError(
                "execution_schema_invalid",
                "Schema '{}' contains an unresolved local $ref/$dynamicRef: {}.".format(
                    path.name,
                    ref,
                ),
            )
    if any(True for _value in _iter_nested_schema_ids(schema)):
        raise DeterministicPackageError(
            "execution_schema_invalid",
            "Schema '{}' contains a nested $id, which is not supported.".format(path.name),
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise DeterministicPackageError(
            "execution_schema_invalid",
            "Schema '{}' is invalid for Draft 2020-12.".format(path.name),
        ) from exc


def _local_json_pointer_exists(document: Any, ref: str) -> bool:
    """Return whether a local JSON Pointer resolves inside ``document``.

    ``Draft202012Validator.check_schema`` validates the shape of ``$ref`` but
    deliberately does not dereference it.  Publish-time validation must do so
    explicitly; otherwise a package can pass the gate and crash later during
    instance validation.
    """
    current = document
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if part not in current:
                return False
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except (TypeError, ValueError):
                return False
            if index < 0 or index >= len(current):
                return False
            current = current[index]
            continue
        return False
    return True


def _iter_refs(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(item, str):
                yield item
            else:
                yield from _iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_refs(item)


def _iter_nested_schema_ids(value: Any, *, root: bool = True) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$id" and not root and isinstance(item, str):
                yield item
            yield from _iter_nested_schema_ids(item, root=False)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_schema_ids(item, root=False)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("Non-standard JSON constant: {}".format(value))


def _reject_duplicate_object_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key: {}".format(key))
        result[key] = value
    return result


def _package_file_entries(
    root: Path,
    *,
    cancel_event: Any = None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    package_paths: list[Path] = []
    for path in root.rglob("*"):
        _raise_if_package_verification_cancelled(cancel_event)
        package_paths.append(path)
    package_paths.sort(key=lambda item: item.relative_to(root).as_posix().encode("utf-8"))
    for path in package_paths:
        _raise_if_package_verification_cancelled(cancel_event)
        relative = path.relative_to(root).as_posix()
        if relative == DETERMINISTIC_LOCK_NAME or relative == "manifest.yaml":
            continue
        if path.is_symlink():
            raise DeterministicPackageError(
                "execution_path_invalid",
                "Ontology package contains symbolic link '{}'.".format(relative),
            )
        if not path.is_file():
            continue
        size, digest = _hash_package_file(path, cancel_event=cancel_event)
        entries.append({"path": relative, "size": size, "sha256": digest})
    return entries


def _raise_if_package_verification_cancelled(cancel_event: Any) -> None:
    is_set = getattr(cancel_event, "is_set", None)
    if callable(is_set) and is_set():
        raise DeterministicPackageError(
            "execution_cancelled",
            "Deterministic package verification was cancelled.",
        )


def _hash_package_file(
    path: Path,
    *,
    cancel_event: Any = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            _raise_if_package_verification_cancelled(cancel_event)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


def _resolve_locked_path(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or ".." in Path(relative).parts:
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock contains an unsafe path.",
        )
    path = root / Path(*relative.split("/"))
    if path.is_symlink():
        raise DeterministicPackageError(
            "package_integrity_mismatch",
            "Locked package file became a symbolic link.",
        )
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DeterministicPackageError(
            "deterministic_lock_invalid",
            "Deterministic package lock path escapes the package.",
        ) from exc
    return resolved


def _relative_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
