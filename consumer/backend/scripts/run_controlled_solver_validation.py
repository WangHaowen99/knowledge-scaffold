"""Opt-in validation of an approved solver bundle outside the repository.

The runner intentionally avoids the durable application service.  It compiles
the approved input in memory and submits the resulting neutral ProblemIR
through Consumer's OR-Tools Connector to an explicitly configured Solver
Service.  No solver engine or local worker is loaded by this process.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import ntpath
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any
from urllib.parse import urlsplit


ROOT_ENV = "SOLVER_CONTROLLED_VALIDATION_ROOT"
TRUST_ROOT_ENV = "SOLVER_CONTROLLED_VALIDATION_TRUST_ROOT"
SERVICE_ENDPOINT_ENV = "SOLVER_CONTROLLED_VALIDATION_SERVICE_ENDPOINT"
SERVICE_CREDENTIAL_REF_ENV = "SOLVER_CONTROLLED_VALIDATION_SERVICE_CREDENTIAL_REF"
SERVICE_TOKEN_ENV = "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN"
SERVICE_TOKEN_FILE_ENV = "SOLVER_CONTROLLED_VALIDATION_SERVICE_TOKEN_FILE"
CONNECTOR_ALLOW_HTTP_ENV = "SOLVER_CONTROLLED_VALIDATION_ALLOW_HTTP"
ALLOWED_FILES = frozenset({"manifest.json", "input.json", "expected.json"})
MAX_FILE_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)
MAX_GITDIR_BYTES = 32 * 1024
MANIFEST_SCHEMA_VERSION = "solver.controlled-validation-manifest.v1"
INPUT_SCHEMA_VERSION = "solver.controlled-validation-input.v1"
EXPECTED_SCHEMA_VERSION = "solver.controlled-validation-expected.v1"
PROFILE_ID = "ordered_binary_route.v1"
LOCKED_OBJECTIVES = (
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
)


def _write_stdout_line(value: str) -> None:
    sys.stdout.write(value)
    sys.stdout.write("\n")
    sys.stdout.flush()

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPERATION_KEY = "solve_controlled_ordered_route"
_PACKAGE_ID = "controlled-validation-package"
_PACKAGE_REVISION = "controlled-validation-revision.v1"
_BINDING_REVISION = "controlled-validation-connector.v1"
_TERMINAL_CONNECTOR_STATES = frozenset(
    {"completed", "failed", "cancelled", "attempt_lost"}
)


class ControlledValidationError(RuntimeError):
    """A generic, caller-safe controlled validation failure."""


@dataclass(frozen=True)
class ControlledValidationBundle:
    root: Path
    protected_repository_roots: tuple[Path, ...]
    input_payload: Mapping[str, Any]
    expected_payload: Mapping[str, Any]
    input_digest: str
    expected_digest: str
    trust_root_digest: str
    profile_id: str


@dataclass(frozen=True)
class ControlledConnectorConfiguration:
    endpoint: str
    credential_ref: str
    token: str


def _connector_configuration_from_env(
    *,
    protected_repository_roots: tuple[Path, ...] = (),
    bundle_root: Path | None = None,
) -> ControlledConnectorConfiguration:
    """Read the explicitly injected Connector endpoint and service secret.

    Controlled validation must never infer a local backend.  The endpoint and
    credential reference are deployment configuration; the token is held only
    in a transport credential-resolver closure and is never placed in the
    binding, ProblemIR, output, or error text.
    """

    endpoint = str(os.environ.get(SERVICE_ENDPOINT_ENV) or "").strip()
    credential_ref = str(os.environ.get(SERVICE_CREDENTIAL_REF_ENV) or "").strip()
    token = os.environ.get(SERVICE_TOKEN_ENV)
    token_file = os.environ.get(SERVICE_TOKEN_FILE_ENV)
    if token is not None and token_file is not None:
        raise ControlledValidationError("Connector credential sources are mutually exclusive")
    if token_file is not None:
        token_path = Path(str(token_file).strip())
        if not token_path.is_absolute() or _unsafe_link(token_path):
            raise ControlledValidationError("Connector credential file is invalid")
        try:
            resolved_token_path = token_path.resolve(strict=True)
        except OSError as exc:
            raise ControlledValidationError("Connector credential file is invalid") from exc
        if any(
            _is_contained(resolved_token_path, protected)
            for protected in protected_repository_roots
        ) or (
            bundle_root is not None and _is_contained(resolved_token_path, bundle_root)
        ):
            raise ControlledValidationError("Connector credential file is invalid")
        try:
            raw = _read_bounded_file(resolved_token_path)
            if raw.startswith(b"\xef\xbb\xbf"):
                raise ValueError
            token = raw.decode("utf-8", errors="strict")
            if token.endswith("\n"):
                token = token[:-1]
            if token.endswith("\r"):
                token = token[:-1]
        except (ControlledValidationError, ValueError) as exc:
            raise ControlledValidationError("Connector credential file is invalid") from exc
    if not endpoint or not credential_ref:
        raise ControlledValidationError("Connector configuration is required")
    if not isinstance(token, str) or not token:
        raise ControlledValidationError("Connector configuration is required")
    parsed = urlsplit(endpoint)
    allow_http = str(os.environ.get(CONNECTOR_ALLOW_HTTP_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ControlledValidationError("Connector endpoint is invalid")
    if parsed.scheme == "http" and not allow_http:
        raise ControlledValidationError("Connector endpoint is invalid")
    if parsed.username is not None or parsed.password is not None or parsed.query:
        raise ControlledValidationError("Connector endpoint is invalid")
    if parsed.fragment:
        raise ControlledValidationError("Connector endpoint is invalid")
    if endpoint != endpoint.rstrip("/"):
        endpoint = endpoint.rstrip("/")
    if token != token.strip() or "\r" in token or "\n" in token:
        raise ControlledValidationError("Connector credential is invalid")
    if len(token) < 16:
        raise ControlledValidationError("Connector credential is invalid")
    return ControlledConnectorConfiguration(
        endpoint=endpoint,
        credential_ref=credential_ref,
        token=token,
    )


def _connector(configuration: ControlledConnectorConfiguration) -> Any:
    def resolve(reference: str) -> str | None:
        if reference == configuration.credential_ref:
            return configuration.token
        return None

    try:
        from solver_coordination.connectors.endpoint_policy import EndpointPolicy
        from solver_coordination.connectors.http_transport import HttpSolverTransport
        from solver_coordination.connectors.ortools import OrToolsConnector

        parsed = urlsplit(configuration.endpoint)
        allow_http = parsed.scheme == "http"
        policy = EndpointPolicy(
            allowed_hosts=(parsed.hostname or "",),
            allowed_ports=(parsed.port or (80 if allow_http else 443),),
            allow_http=allow_http,
            allow_localhost=False,
        )
        return OrToolsConnector(
            transport=HttpSolverTransport(
                credential_resolver=resolve,
                max_retries=2,
                endpoint_policy=policy,
            )
        )
    except Exception as exc:
        raise ControlledValidationError("OR-Tools Connector is unavailable") from exc


def _connector_binding(configuration: ControlledConnectorConfiguration) -> dict[str, Any]:
    return {
        "connector_type": "ortools",
        "service_api_version": "ortools.solver.v1",
        "endpoint": configuration.endpoint,
        "service_credential_ref": configuration.credential_ref,
        "allowed_families": {"binary_ordered_route": ["1.0"]},
        "neutral_limits": {"max_solve_time_ms": 60000, "max_workers": 1},
    }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ControlledValidationError("controlled value is not canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _required_digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ControlledValidationError("{} is not a canonical digest".format(field_name))
    return value


def _strict_json(value: bytes, field_name: str) -> dict[str, Any]:
    if value.startswith(b"\xef\xbb\xbf"):
        raise ControlledValidationError("{} must be BOM-free UTF-8 JSON".format(field_name))
    try:
        text = value.decode("utf-8", errors="strict")

        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ControlledValidationError(
                        "{} contains a duplicate key".format(field_name)
                    )
                result[key] = item
            return result

        def reject_constant(_value: str) -> None:
            raise ControlledValidationError(
                "{} contains a non-finite number".format(field_name)
            )

        def reject_float(_value: str) -> None:
            raise ControlledValidationError(
                "{} contains a floating-point number".format(field_name)
            )

        parsed = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ControlledValidationError("{} is not strict JSON".format(field_name)) from exc
    if not isinstance(parsed, dict):
        raise ControlledValidationError("{} must be a JSON object".format(field_name))
    return parsed


def _unsafe_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except OSError:
        return True


def _reject_unsafe_components(path: Path) -> None:
    if not path.is_absolute():
        raise ControlledValidationError("controlled root must be an absolute path")
    cursor = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        cursor = cursor / part
        if os.path.lexists(cursor) and _unsafe_link(cursor):
            raise ControlledValidationError("controlled path contains a link")


def _is_contained(child: Path, parent: Path) -> bool:
    try:
        return child == parent or child.is_relative_to(parent)
    except (OSError, ValueError):
        return False


def _external_directory(root: Path, repository_roots: tuple[Path, ...]) -> Path:
    _reject_unsafe_components(root)
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ControlledValidationError("controlled root is unavailable") from exc
    if not resolved.is_dir():
        raise ControlledValidationError("controlled root is not a directory")
    if any(
        _is_contained(resolved, repository)
        or _is_contained(repository, resolved)
        for repository in repository_roots
    ):
        raise ControlledValidationError("controlled root must be outside the repository")
    return resolved


def _directory_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ControlledValidationError("controlled root identity is unavailable") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise ControlledValidationError("controlled root is not a directory")
    return _file_identity(value)


def _bundle_files(root: Path) -> tuple[dict[str, Path], tuple[int, int, int, int]]:
    root_identity = _directory_identity(root)
    try:
        entries = tuple(os.scandir(root))
    except OSError as exc:
        raise ControlledValidationError("controlled root cannot be enumerated") from exc
    names = {entry.name for entry in entries}
    if names != ALLOWED_FILES:
        raise ControlledValidationError("controlled root violates the file allowlist")
    files: dict[str, Path] = {}
    for entry in entries:
        path = root / entry.name
        if _unsafe_link(path) or not entry.is_file(follow_symlinks=False):
            raise ControlledValidationError("controlled bundle contains a link")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ControlledValidationError("controlled bundle file is unavailable") from exc
        if resolved.parent != root:
            raise ControlledValidationError("controlled bundle file escapes its root")
        files[entry.name] = resolved
    if _directory_identity(root) != root_identity:
        raise ControlledValidationError("controlled root changed while enumerating")
    return files, root_identity


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
    )


def _windows_path_key(value: str | Path) -> str:
    rendered = str(value)
    if rendered.startswith("\\\\?\\UNC\\"):
        rendered = ntpath.join("\\\\", rendered[8:])
    elif rendered.startswith("\\\\?\\"):
        rendered = rendered[4:]
    return os.path.normcase(os.path.normpath(os.path.abspath(rendered)))


def _native_handle_value(handle: Any) -> int:
    """Return a validated integer for a ctypes native handle."""

    import ctypes

    value = getattr(handle, "value", handle)
    invalid = ctypes.c_void_p(-1).value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControlledValidationError("controlled native handle is invalid")
    if value <= 0 or value == invalid:
        raise ControlledValidationError("controlled native handle is invalid")
    return value


def _open_windows_no_follow(path: Path) -> int:
    """Open a regular file without following a final Windows reparse point."""

    if os.name != "nt":
        raise ControlledValidationError("Windows no-follow open is unavailable")
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("last_access_time", wintypes.FILETIME),
                ("last_write_time", wintypes.FILETIME),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ByHandleFileInformation),
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateFileW(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,  # OPEN_EXISTING
            0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
            None,
        )
        try:
            raw_handle = _native_handle_value(handle)
        except ControlledValidationError as exc:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed") from exc
        owns_handle = True
        try:
            information = ByHandleFileInformation()
            if not kernel32.GetFileInformationByHandle(
                handle, ctypes.byref(information)
            ):
                raise OSError(
                    ctypes.get_last_error(), "GetFileInformationByHandle failed"
                )
            if information.file_attributes & 0x00000400:
                raise ControlledValidationError(
                    "controlled bundle contains a reparse point"
                )
            if information.file_attributes & 0x00000010:
                raise ControlledValidationError(
                    "controlled bundle entry is not a file"
                )
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetFinalPathNameByHandleW(
                handle, buffer, len(buffer), 0
            )
            if length == 0 or length >= len(buffer):
                raise OSError(
                    ctypes.get_last_error(), "GetFinalPathNameByHandleW failed"
                )
            if _windows_path_key(buffer.value) != _windows_path_key(path):
                raise ControlledValidationError(
                    "controlled bundle file escaped its approved path"
                )
            descriptor = msvcrt.open_osfhandle(
                raw_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
            owns_handle = False
            return descriptor
        finally:
            if owns_handle:
                kernel32.CloseHandle(handle)
    except (OSError, TypeError, ValueError) as exc:
        raise ControlledValidationError(
            "controlled bundle file cannot be opened safely"
        ) from exc


def _read_bounded_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        if os.name == "nt":
            descriptor = _open_windows_no_follow(path)
        else:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = None
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ControlledValidationError("controlled bundle entry is not a file")
            if before.st_size > MAX_FILE_BYTES:
                raise ControlledValidationError("controlled bundle file exceeds the size limit")
            payload = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ControlledValidationError("controlled bundle file cannot be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                logger.debug(
                    "Controlled bundle descriptor cleanup failed",
                )
    if len(payload) > MAX_FILE_BYTES or len(payload) != before.st_size:
        raise ControlledValidationError("controlled bundle file exceeds the size limit")
    if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
        raise ControlledValidationError("controlled bundle file changed while reading")
    if _unsafe_link(path):
        raise ControlledValidationError("controlled bundle contains a link")
    return payload


def _validate_manifest(
    manifest: Mapping[str, Any], *, approved_trust_root: str
) -> tuple[str, dict[str, str], str]:
    if set(manifest) != {
        "schema_version",
        "trust_root_digest",
        "profile_id",
        "file_digests",
    }:
        raise ControlledValidationError("controlled manifest fields are invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ControlledValidationError("controlled manifest schema is unsupported")
    if manifest.get("profile_id") != PROFILE_ID:
        raise ControlledValidationError("controlled manifest profile is unsupported")
    digests = manifest.get("file_digests")
    if not isinstance(digests, Mapping) or set(digests) != ALLOWED_FILES:
        raise ControlledValidationError("controlled manifest file digests are invalid")
    normalized_digests: dict[str, str] = {}
    for name in sorted(ALLOWED_FILES):
        normalized_digests[name] = _required_digest(
            digests.get(name), "{} digest".format(name)
        )
    declared_trust = _required_digest(
        manifest.get("trust_root_digest"), "trust_root_digest"
    )
    approved = _required_digest(approved_trust_root, "approved trust root")
    projection = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "profile_id": PROFILE_ID,
        "file_digests": {
            "input.json": normalized_digests["input.json"],
            "expected.json": normalized_digests["expected.json"],
        },
    }
    projection_digest = _sha256_bytes(_canonical_bytes(projection))
    for trusted_digest in (
        declared_trust,
        normalized_digests["manifest.json"],
        approved,
    ):
        if not hmac.compare_digest(projection_digest, trusted_digest):
            raise ControlledValidationError(
                "controlled manifest trust root is not approved"
            )
    return PROFILE_ID, normalized_digests, projection_digest


def _validate_input(value: Mapping[str, Any], profile_id: str) -> None:
    if set(value) != {"schema_version", "values", "decision_model"}:
        raise ControlledValidationError("controlled input fields are invalid")
    if value.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise ControlledValidationError("controlled input schema is unsupported")
    values = value.get("values")
    if not isinstance(values, Mapping) or set(values) != {"items"}:
        raise ControlledValidationError("controlled input values are invalid")
    items = values.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise ControlledValidationError("controlled input items are invalid")
    seen: set[str] = set()
    for order, item in enumerate(items, start=1):
        if not isinstance(item, Mapping) or set(item) != {
            "item_order",
            "item_id",
            "quantity",
            "duration",
        }:
            raise ControlledValidationError("controlled input item fields are invalid")
        item_id = item.get("item_id")
        item_order = item.get("item_order")
        if item_order != order or isinstance(item_order, bool):
            raise ControlledValidationError("controlled input item identity is invalid")
        if not isinstance(item_id, str) or not item_id:
            raise ControlledValidationError("controlled input item identity is invalid")
        if item_id != item_id.strip() or item_id in seen:
            raise ControlledValidationError("controlled input item identity is invalid")
        for field_name in ("quantity", "duration"):
            field_value = item.get(field_name)
            if not isinstance(field_value, str) or not field_value or field_value != field_value.strip():
                raise ControlledValidationError("controlled input item value is invalid")
        seen.add(item_id)
    model = value.get("decision_model")
    expected_model_fields = {
        "profile_id",
        "required_facts",
        "fact_source",
        "materialized_facts",
        "display_mapping",
        "objectives",
    }
    if not isinstance(model, Mapping) or set(model) != expected_model_fields:
        raise ControlledValidationError("controlled decision model fields are invalid")
    if model.get("profile_id") != profile_id:
        raise ControlledValidationError("controlled decision model profile diverged")
    if model.get("required_facts") != ["route_metrics"]:
        raise ControlledValidationError("controlled decision model facts are invalid")
    if model.get("fact_source") != {"kind": "locked_model"}:
        raise ControlledValidationError("controlled decision model fact source is invalid")
    facts = model.get("materialized_facts")
    if not isinstance(facts, list) or len(facts) != 1:
        raise ControlledValidationError("controlled materialized facts are invalid")
    materialized_fact = facts[0]
    if not isinstance(materialized_fact, Mapping):
        raise ControlledValidationError("controlled materialized facts are invalid")
    if set(materialized_fact) != {"name", "value"}:
        raise ControlledValidationError("controlled materialized facts are invalid")
    if materialized_fact.get("name") != "route_metrics":
        raise ControlledValidationError("controlled materialized facts are invalid")
    if not isinstance(materialized_fact.get("value"), Mapping):
        raise ControlledValidationError("controlled materialized facts are invalid")
    mapping = model.get("display_mapping")
    if not isinstance(mapping, Mapping) or set(mapping) != {"OPTION_0", "OPTION_1"}:
        raise ControlledValidationError("controlled display mapping is invalid")
    if any(
        not isinstance(label, str) or not label or label != label.strip()
        for label in mapping.values()
    ):
        raise ControlledValidationError("controlled display mapping is invalid")
    objectives = model.get("objectives")
    if not isinstance(objectives, list) or objectives != [
        dict(objective) for objective in LOCKED_OBJECTIVES
    ]:
        raise ControlledValidationError("controlled objectives are invalid")


def _validate_expected(value: Mapping[str, Any]) -> None:
    if set(value) != {
        "schema_version",
        "status",
        "feasibility",
        "optimality",
        "proof_kind",
        "summary",
    }:
        raise ControlledValidationError("controlled expected fields are invalid")
    if value.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        raise ControlledValidationError("controlled expected schema is unsupported")
    if value.get("status") != "completed" or value.get("feasibility") != "feasible":
        raise ControlledValidationError("controlled expected authority is invalid")
    if value.get("optimality") != "proven_optimal":
        raise ControlledValidationError("controlled expected optimality is invalid")
    if value.get("proof_kind") != "verifier_exhaustive_check":
        raise ControlledValidationError("controlled expected proof is invalid")
    summary = value.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "candidate_id",
        "combination_no",
        "assignment",
        "items",
        "objective_values",
    }:
        raise ControlledValidationError("controlled expected summary is invalid")
    candidate_id = summary.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ControlledValidationError("controlled expected candidate is invalid")
    if candidate_id != candidate_id.strip():
        raise ControlledValidationError("controlled expected candidate is invalid")
    combination_no = summary.get("combination_no")
    if (
        isinstance(combination_no, bool)
        or not isinstance(combination_no, int)
        or combination_no < 1
    ):
        raise ControlledValidationError("controlled expected combination is invalid")
    assignment = summary.get("assignment")
    if not isinstance(assignment, Mapping) or not assignment:
        raise ControlledValidationError("controlled expected assignment is invalid")
    if any(
        not isinstance(item_id, str)
        or not item_id
        or item_id != item_id.strip()
        or option not in {"OPTION_0", "OPTION_1"}
        for item_id, option in assignment.items()
    ):
        raise ControlledValidationError("controlled expected assignment is invalid")
    items = summary.get("items")
    if not isinstance(items, list) or len(items) != len(assignment):
        raise ControlledValidationError("controlled expected items are invalid")
    for item in items:
        if not isinstance(item, Mapping):
            raise ControlledValidationError("controlled expected items are invalid")
        if set(item) != {"item_id", "route"}:
            raise ControlledValidationError("controlled expected items are invalid")
        if item.get("item_id") not in assignment:
            raise ControlledValidationError("controlled expected items are invalid")
        route = item.get("route")
        if not isinstance(route, str) or not route:
            raise ControlledValidationError("controlled expected items are invalid")
        if route != route.strip():
            raise ControlledValidationError("controlled expected items are invalid")
    objective_values = summary.get("objective_values")
    expected_objectives = {objective["metric"] for objective in LOCKED_OBJECTIVES}
    if not isinstance(objective_values, Mapping) or set(objective_values) != expected_objectives:
        raise ControlledValidationError("controlled expected objectives are invalid")
    if any(
        isinstance(metric_value, bool) or not isinstance(metric_value, int)
        for metric_value in objective_values.values()
    ):
        raise ControlledValidationError("controlled expected objectives are invalid")


def _repository_boundary(start: Path) -> Path:
    """Resolve a linked-worktree path to the containing main Git repository."""

    worktree = start.resolve(strict=True)
    marker = worktree / ".git"
    if marker.is_file() and not _unsafe_link(marker):
        try:
            text = _read_bounded_file(marker).decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise ControlledValidationError("repository boundary is unavailable") from exc
        prefix = "gitdir:"
        if text.strip().lower().startswith(prefix):
            value = text.strip()[len(prefix) :].strip()
            git_dir = Path(value)
            if not git_dir.is_absolute():
                git_dir = worktree / git_dir
            try:
                git_dir = git_dir.resolve(strict=True)
            except OSError as exc:
                raise ControlledValidationError("repository boundary is unavailable") from exc
            if git_dir.parent.name == "worktrees" and git_dir.parent.parent.name == ".git":
                main_root = git_dir.parent.parent.parent
                if main_root.is_dir():
                    return main_root.resolve(strict=True)
    for candidate in (worktree, *worktree.parents):
        git_marker = candidate / ".git"
        if git_marker.is_dir() and not _unsafe_link(git_marker):
            return candidate.resolve(strict=True)
    return worktree


def _protected_repository_roots(start: Path) -> tuple[Path, ...]:
    """Return the main repository and every worktree registered by Git."""

    try:
        selected = start.resolve(strict=True)
    except OSError as exc:
        raise ControlledValidationError("repository boundary is unavailable") from exc
    main_root = _repository_boundary(selected)
    roots = {selected, main_root}
    worktrees_root = main_root / ".git" / "worktrees"
    if not os.path.lexists(worktrees_root):
        return tuple(sorted(roots, key=lambda path: _windows_path_key(path)))
    _reject_unsafe_components(worktrees_root)
    try:
        worktrees_root = worktrees_root.resolve(strict=True)
    except OSError as exc:
        raise ControlledValidationError("repository worktree registry is unavailable") from exc
    if not worktrees_root.is_dir():
        raise ControlledValidationError("repository worktree registry is invalid")
    registry_identity = _directory_identity(worktrees_root)
    try:
        entries = tuple(os.scandir(worktrees_root))
    except OSError as exc:
        raise ControlledValidationError(
            "repository worktree registry is unavailable"
        ) from exc
    for entry in entries:
        entry_path = worktrees_root / entry.name
        if _unsafe_link(entry_path) or not entry.is_dir(follow_symlinks=False):
            raise ControlledValidationError("repository worktree registry is unsafe")
        gitdir_path = entry_path / "gitdir"
        if _unsafe_link(gitdir_path) or not gitdir_path.is_file():
            raise ControlledValidationError("repository worktree registry is invalid")
        payload = _read_bounded_file(gitdir_path)
        if len(payload) > MAX_GITDIR_BYTES:
            raise ControlledValidationError("repository worktree registry is invalid")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ControlledValidationError(
                "repository worktree registry is invalid"
            ) from exc
        lines = text.splitlines()
        if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
            raise ControlledValidationError("repository worktree registry is invalid")
        worktree_gitdir = Path(lines[0])
        if not worktree_gitdir.is_absolute():
            worktree_gitdir = entry_path / worktree_gitdir
        if worktree_gitdir.name != ".git":
            raise ControlledValidationError("repository worktree registry is invalid")
        worktree_root = worktree_gitdir.parent
        _reject_unsafe_components(worktree_root)
        try:
            roots.add(worktree_root.resolve(strict=False))
        except OSError as exc:
            raise ControlledValidationError(
                "repository worktree registry is invalid"
            ) from exc
    if _directory_identity(worktrees_root) != registry_identity:
        raise ControlledValidationError("repository worktree registry changed while reading")
    return tuple(sorted(roots, key=lambda path: _windows_path_key(path)))


def _default_repository_root() -> Path:
    return _repository_boundary(Path(__file__).resolve(strict=True).parents[3])


def load_bundle(
    *,
    root: str | Path | None = None,
    repository_root: str | Path | None = None,
    approved_trust_root: str | None = None,
) -> ControlledValidationBundle:
    raw_root = root if root is not None else os.environ.get(ROOT_ENV)
    trust_root = (
        approved_trust_root
        if approved_trust_root is not None
        else os.environ.get(TRUST_ROOT_ENV)
    )
    if raw_root is None or trust_root is None:
        raise ControlledValidationError("controlled validation is not opted in")
    selected_root = Path(raw_root)
    selected_repository = (
        Path(repository_root)
        if repository_root is not None
        else _default_repository_root()
    )
    protected_roots = _protected_repository_roots(selected_repository)
    external_root = _external_directory(selected_root, protected_roots)
    files, root_identity = _bundle_files(external_root)
    payloads = {name: _read_bounded_file(path) for name, path in files.items()}
    second_files, second_identity = _bundle_files(external_root)
    if set(second_files) != ALLOWED_FILES or second_identity != root_identity:
        raise ControlledValidationError("controlled root changed while reading")
    manifest = _strict_json(payloads["manifest.json"], "manifest.json")
    profile_id, digests, trust_digest = _validate_manifest(
        manifest, approved_trust_root=trust_root
    )
    for name in ("input.json", "expected.json"):
        if not hmac.compare_digest(_sha256_bytes(payloads[name]), digests[name]):
            raise ControlledValidationError("controlled bundle digest mismatch")
    input_payload = _strict_json(payloads["input.json"], "input.json")
    expected_payload = _strict_json(payloads["expected.json"], "expected.json")
    _validate_input(input_payload, profile_id)
    _validate_expected(expected_payload)
    return ControlledValidationBundle(
        root=external_root,
        protected_repository_roots=protected_roots,
        input_payload=input_payload,
        expected_payload=expected_payload,
        input_digest=digests["input.json"],
        expected_digest=digests["expected.json"],
        trust_root_digest=trust_digest,
        profile_id=profile_id,
    )


def _solve(bundle: ControlledValidationBundle) -> dict[str, Any]:
    # Imports are deliberately deferred until after both opt-in environment
    # variables, Connector configuration, and every bundle trust check succeed.
    configuration = _connector_configuration_from_env(
        protected_repository_roots=bundle.protected_repository_roots,
        bundle_root=bundle.root,
    )
    from solver_coordination.compiler import ProblemIRCompiler
    from solver_coordination.connectors.helpers import (
        capability_fingerprint,
        idempotency_key,
    )
    from solver_coordination.connectors.ortools import API_VERSION
    from solver_coordination.contracts import NormalizedInput
    from solver_coordination.fact_snapshot import FactSnapshotBuilder
    from solver_coordination.presentation import ProfilePresentationService
    from solver_coordination.problem_families.candidate_selection import (
        CandidateSelectionAdapter,
    )
    from solver_coordination.problem_families.registry import ProblemFamilyRegistry
    from solver_coordination.profiles import (
        ProfileFactResolver,
        build_default_solver_profile_registry,
    )
    from solver_coordination.result_projection import (
        validate_authoritative_verification,
    )
    from solver_coordination.verification.profile_dispatch import (
        ProfileDispatchVerifier,
    )
    from solver_coordination.verification.service import IndependentVerificationService

    profiles = build_default_solver_profile_registry()
    normalized_input = NormalizedInput(
        operation_key=_OPERATION_KEY,
        values=bundle.input_payload["values"],
    )
    capability = {
        "package_id": _PACKAGE_ID,
        "package_revision": _PACKAGE_REVISION,
        "operation_key": _OPERATION_KEY,
        "problem_family": "candidate_selection.v1",
        "decision_model": bundle.input_payload["decision_model"],
    }
    model = capability["decision_model"]
    required_facts = tuple(sorted(str(name) for name in model["required_facts"]))
    fact_payload = ProfileFactResolver(
        profile_registry=profiles,
        binding_revision_id=_BINDING_REVISION,
    ).resolve(
        capability=capability,
        normalized_input=normalized_input,
        required_facts=required_facts,
    )
    fact_snapshot = FactSnapshotBuilder().build(
        workspace_id="controlled-validation-workspace",
        owner_id="controlled-validation-owner",
        package_id=_PACKAGE_ID,
        package_revision=_PACKAGE_REVISION,
        normalized_input=normalized_input,
        facts=fact_payload["facts"],
        bindings=fact_payload["bindings"],
        as_of=fact_payload["as_of"],
        source_evidence=fact_payload["source_evidence"],
        required_facts=required_facts,
    )
    problem = ProblemIRCompiler(
        registry=ProblemFamilyRegistry([CandidateSelectionAdapter(profiles)])
    ).compile(
        capability=capability,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )

    binding = _connector_binding(configuration)
    connector = _connector(configuration)
    try:
        capability_payload = connector.capabilities(binding=binding)
    except Exception as exc:
        raise ControlledValidationError("solver Connector capability handshake failed") from exc
    try:
        if (
            capability_payload.get("protocol_versions") != [API_VERSION]
            or not any(
                isinstance(family, Mapping)
                and family.get("name") == "binary_ordered_route"
                and family.get("versions") == ["1.0"]
                for family in capability_payload.get("problem_families", ())
            )
        ):
            raise ControlledValidationError("solver Connector capability is unsupported")
        binding["capability_fingerprint"] = capability_fingerprint(capability_payload)
    except Exception as exc:
        if isinstance(exc, ControlledValidationError):
            raise
        raise ControlledValidationError("solver Connector capability is invalid") from exc

    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    request_idempotency_key = idempotency_key(
        secret=secrets.token_bytes(32),
        binding_revision_id=_BINDING_REVISION,
        run_id="controlled-validation",
        attempt_id="attempt-1",
        problem_digest=problem.problem_id,
    )
    try:
        submitted = connector.submit(
            binding=binding,
            problem=problem,
            result_quality="optimal",
            idempotency_key=request_idempotency_key,
            deadline=deadline,
        )
    except Exception as exc:
        raise ControlledValidationError("controlled solver Connector submission failed") from exc
    backend_job_id = str(
        submitted.get("job_id", "") if isinstance(submitted, Mapping) else ""
    )
    if not backend_job_id:
        raise ControlledValidationError("controlled solver Connector returned no job")
    terminal_status = ""
    while True:
        try:
            state = connector.poll(binding=binding, job_id=backend_job_id)
        except Exception as exc:
            try:
                connector.cancel(binding=binding, job_id=backend_job_id)
            except Exception:
                logger.warning(
                    "Controlled solver cancellation after polling failure failed",
                )
            raise ControlledValidationError("controlled solver Connector polling failed") from exc
        terminal_status = str(
            state.get("status", "") if isinstance(state, Mapping) else ""
        )
        if terminal_status in _TERMINAL_CONNECTOR_STATES:
            break
        if datetime.now(timezone.utc) >= deadline:
            try:
                connector.cancel(binding=binding, job_id=backend_job_id)
            except Exception:
                logger.warning(
                    "Controlled solver cancellation after deadline failed",
                )
            raise ControlledValidationError("controlled solver deadline expired")
        time.sleep(0.02)
    if terminal_status != "completed":
        raise ControlledValidationError("controlled solver Connector did not complete")
    try:
        solver_result = connector.result(
            binding=binding,
            job_id=backend_job_id,
        )
    except Exception as exc:
        raise ControlledValidationError("controlled solver Connector result is invalid") from exc

    verification = IndependentVerificationService(
        verifier=ProfileDispatchVerifier(profiles)
    ).verify(
        problem=problem,
        capability=capability,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        solver_result=solver_result,
    )
    validate_authoritative_verification(
        fact_snapshot=fact_snapshot,
        problem=problem,
        solver_result=solver_result,
        verification=verification,
    )
    summary = ProfilePresentationService(profiles).present(
        capability=capability,
        normalized_input=normalized_input,
        solver_result=solver_result,
        verification=verification,
    )
    return {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "status": "completed",
        "feasibility": verification.feasibility,
        "optimality": verification.optimality,
        "proof_kind": verification.proof_kind,
        "summary": summary,
    }


def run_bundle(
    *,
    root: str | Path,
    repository_root: str | Path,
    approved_trust_root: str,
) -> int:
    repository = Path(repository_root).resolve(strict=True)
    bundle = load_bundle(
        root=root,
        repository_root=repository,
        approved_trust_root=approved_trust_root,
    )
    actual = _solve(bundle)
    if actual != dict(bundle.expected_payload):
        raise ControlledValidationError("controlled validation result mismatch")
    output = {
        "status": "passed",
        "input_digest": bundle.input_digest,
        "result_digest": _sha256_bytes(_canonical_bytes(actual)),
    }
    _write_stdout_line(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0


def main() -> int:
    raw_root = os.environ.get(ROOT_ENV)
    approved_trust_root = os.environ.get(TRUST_ROOT_ENV)
    connector_endpoint = os.environ.get(SERVICE_ENDPOINT_ENV)
    connector_ref = os.environ.get(SERVICE_CREDENTIAL_REF_ENV)
    connector_token = os.environ.get(SERVICE_TOKEN_ENV)
    connector_token_file = os.environ.get(SERVICE_TOKEN_FILE_ENV)
    if not raw_root or not approved_trust_root or not connector_endpoint:
        return 2
    if not connector_ref:
        return 2
    if connector_token is None and connector_token_file is None:
        return 2
    if connector_token is not None and connector_token_file is not None:
        return 2
    try:
        return run_bundle(
            root=Path(raw_root),
            repository_root=_default_repository_root(),
            approved_trust_root=approved_trust_root,
        )
    except Exception:
        _write_stdout_line(
            '{"error_code":"controlled_validation_failed","status":"failed"}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
