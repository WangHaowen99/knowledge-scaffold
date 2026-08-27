"""Validated ``OntologyChanged/v1`` event envelopes.

The event is an invalidation hint, not an authoritative ontology snapshot.  The
helpers in this module deliberately keep the schema validation independent of
the evidence index so mutation commits can remain lightweight when the feature
is disabled.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ontology.hash_contract import normalize_relative_path


CONTRACT = "OntologyChanged/v1"
MAX_EVENT_BYTES = 512 * 1024
MAX_DIGEST_ENTRIES = 5_000
DIGEST_ALGORITHM = "sha256"
DIGEST_BASIS = "raw_file_bytes_v1"

MUTATION_TYPES = frozenset(
    {
        "ACTIVE_VERSION_CHANGED",
        "ONTOLOGY_VISIBILITY_CHANGED",
        "ONTOLOGY_FILE_CHANGED",
        "ONTOLOGY_IMPORTED",
        "ONTOLOGY_VERSION_DELETED",
        "ONTOLOGY_DELETED",
        "EXTERNAL_DRIFT_DETECTED",
    }
)
TRIGGERS = frozenset({"api", "upload", "import", "startup_recovery", "sweeper", "admin"})
OPERATOR_TYPES = frozenset({"user", "service", "system"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_CROCKFORD = (string.digits + string.ascii_uppercase).translate(
    str.maketrans("", "", "ILOU")
)
_TOP_LEVEL_FIELDS = {
    "contract",
    "event_id",
    "occurred_at",
    "workspace_id",
    "ontology_id",
    "mutation_type",
    "mutation_revision",
    "before_version",
    "after_version",
    "known_file_digests",
    "deleted_paths",
    "operator",
    "request_id",
    "trigger",
}
_REQUIRED_FIELDS = {
    "contract",
    "event_id",
    "occurred_at",
    "workspace_id",
    "ontology_id",
    "mutation_type",
    "mutation_revision",
    "before_version",
    "after_version",
    "known_file_digests",
    "deleted_paths",
}
_DIGEST_FIELDS = {
    "algorithm",
    "digest_basis",
    "scope_version",
    "completeness",
    "base_mutation_revision",
    "semantic_manifest_digest",
    "entries",
}


class OntologyChangedValidationError(ValueError):
    """Raised when an internal mutation event violates its contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def new_ulid(*, timestamp_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Return a monotonic-sortable ULID string without a third-party dependency."""
    instant = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if instant < 0 or instant >= 2**48:
        raise ValueError("ULID timestamp is out of range")
    random_bytes = secrets.token_bytes(10) if randomness is None else bytes(randomness)
    if len(random_bytes) != 10:
        raise ValueError("ULID randomness must contain exactly 10 bytes")
    value = (instant << 80) | int.from_bytes(random_bytes, "big")
    chars = ["0"] * 26
    for index in range(25, -1, -1):
        chars[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(chars)


def empty_digest_hints(*, scope_version: str | None) -> dict[str, Any]:
    return {
        "algorithm": DIGEST_ALGORITHM,
        "digest_basis": DIGEST_BASIS,
        "scope_version": scope_version,
        "completeness": "none",
        "base_mutation_revision": None,
        "semantic_manifest_digest": None,
        "entries": {},
    }


def build_digest_hints(
    *,
    scope_version: str | None,
    completeness: str,
    mutation_revision: int,
    entries: Mapping[str, Mapping[str, Any]] | None = None,
    semantic_manifest_digest: str | None = None,
) -> dict[str, Any]:
    normalized_entries: dict[str, dict[str, Any]] = {}
    for raw_path, raw_digest in (entries or {}).items():
        path = normalize_relative_path(str(raw_path))
        if path in normalized_entries:
            raise OntologyChangedValidationError("DUPLICATE_PATH", "duplicate normalized digest path")
        if not isinstance(raw_digest, Mapping):
            raise OntologyChangedValidationError("INVALID_DIGEST_ENTRY", "digest entry must be an object")
        normalized_entries[path] = {
            "sha256": str(raw_digest.get("sha256") or ""),
            "size_bytes": raw_digest.get("size_bytes"),
        }
    ordered_entries = {
        path: normalized_entries[path]
        for path in sorted(normalized_entries, key=lambda item: item.encode("utf-8"))
    }
    return {
        "algorithm": DIGEST_ALGORITHM,
        "digest_basis": DIGEST_BASIS,
        "scope_version": scope_version,
        "completeness": completeness,
        "base_mutation_revision": mutation_revision - 1 if completeness == "partial" else None,
        "semantic_manifest_digest": semantic_manifest_digest,
        "entries": ordered_entries,
    }


def build_ontology_changed_event(
    *,
    workspace_id: str,
    ontology_id: str,
    mutation_type: str,
    mutation_revision: int,
    before_version: str | None,
    after_version: str | None,
    known_file_digests: Mapping[str, Any] | None = None,
    deleted_paths: Sequence[str] = (),
    operator: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    trigger: str | None = None,
    event_id: str | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    normalized_deleted = _normalize_deleted_paths(deleted_paths)
    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "event_id": event_id or new_ulid(),
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
        "workspace_id": str(workspace_id),
        "ontology_id": str(ontology_id),
        "mutation_type": str(mutation_type),
        "mutation_revision": int(mutation_revision),
        "before_version": before_version,
        "after_version": after_version,
        "known_file_digests": dict(known_file_digests or empty_digest_hints(scope_version=after_version)),
        "deleted_paths": normalized_deleted,
    }
    if operator is not None:
        payload["operator"] = dict(operator)
    if request_id is not None:
        payload["request_id"] = request_id
    if trigger is not None:
        payload["trigger"] = trigger

    try:
        validate_ontology_changed_event(payload)
    except OntologyChangedValidationError as exc:
        hints = payload.get("known_file_digests") or {}
        if exc.code not in {"PAYLOAD_TOO_LARGE", "TOO_MANY_DIGESTS"} or hints.get("completeness") == "none":
            raise
        payload["known_file_digests"] = empty_digest_hints(scope_version=after_version)
        payload["deleted_paths"] = []
        validate_ontology_changed_event(payload)
    return payload


def canonical_event_json(payload: Mapping[str, Any]) -> str:
    validate_ontology_changed_event(payload)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def load_event_json(payload_json: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OntologyChangedValidationError("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(payload_json, object_pairs_hook=reject_duplicate_keys)
    except (TypeError, json.JSONDecodeError) as exc:
        raise OntologyChangedValidationError("INVALID_JSON", "event payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise OntologyChangedValidationError("INVALID_EVENT", "event payload must be an object")
    validate_ontology_changed_event(payload)
    return payload


def validate_ontology_changed_event(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping):
        raise OntologyChangedValidationError("INVALID_EVENT", "event payload must be an object")
    keys = set(payload)
    missing = _REQUIRED_FIELDS - keys
    extra = keys - _TOP_LEVEL_FIELDS
    if missing:
        raise OntologyChangedValidationError("MISSING_FIELDS", "missing required event fields")
    if extra:
        raise OntologyChangedValidationError("UNKNOWN_FIELDS", "event contains unsupported fields")
    if payload.get("contract") != CONTRACT:
        raise OntologyChangedValidationError("INVALID_CONTRACT", "invalid event contract")
    if not isinstance(payload.get("event_id"), str) or not _ULID_RE.fullmatch(payload["event_id"]):
        raise OntologyChangedValidationError("INVALID_EVENT_ID", "event_id must be a ULID")
    _validate_datetime(payload.get("occurred_at"))
    _validate_identifier(payload.get("workspace_id"), "workspace_id")
    _validate_identifier(payload.get("ontology_id"), "ontology_id")
    mutation_type = payload.get("mutation_type")
    if mutation_type not in MUTATION_TYPES:
        raise OntologyChangedValidationError("INVALID_MUTATION_TYPE", "unsupported mutation_type")
    revision = payload.get("mutation_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise OntologyChangedValidationError("INVALID_REVISION", "mutation_revision must be a positive integer")
    before = _validate_version(payload.get("before_version"), "before_version")
    after = _validate_version(payload.get("after_version"), "after_version")
    _validate_operator(payload.get("operator"), present="operator" in payload)
    _validate_optional_string(payload.get("request_id"), "request_id", present="request_id" in payload, limit=128)
    trigger = payload.get("trigger")
    if "trigger" in payload and trigger is not None and trigger not in TRIGGERS:
        raise OntologyChangedValidationError("INVALID_TRIGGER", "unsupported trigger")

    hints = payload.get("known_file_digests")
    _validate_digest_hints(hints, revision=revision, after_version=after)
    deleted = payload.get("deleted_paths")
    _validate_deleted_paths(deleted, entries=(hints or {}).get("entries") or {})
    if (hints or {}).get("completeness") == "none" and deleted:
        raise OntologyChangedValidationError("INVALID_NONE_HINTS", "none digest hints require empty deleted_paths")
    _validate_version_combination(mutation_type, before, after, str((hints or {}).get("completeness")))

    if len((hints or {}).get("entries") or {}) > MAX_DIGEST_ENTRIES:
        raise OntologyChangedValidationError("TOO_MANY_DIGESTS", "event contains too many digest entries")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OntologyChangedValidationError("INVALID_EVENT", "event is not JSON serializable") from exc
    if len(encoded) > MAX_EVENT_BYTES:
        raise OntologyChangedValidationError("PAYLOAD_TOO_LARGE", "event payload exceeds 512 KiB")


def _validate_digest_hints(hints: Any, *, revision: int, after_version: str | None) -> None:
    if not isinstance(hints, Mapping) or set(hints) != _DIGEST_FIELDS:
        raise OntologyChangedValidationError("INVALID_DIGEST_HINTS", "known_file_digests has invalid fields")
    if hints.get("algorithm") != DIGEST_ALGORITHM or hints.get("digest_basis") != DIGEST_BASIS:
        raise OntologyChangedValidationError("INVALID_DIGEST_BASIS", "unsupported digest basis")
    scope = _validate_version(hints.get("scope_version"), "scope_version")
    completeness = hints.get("completeness")
    if completeness not in {"none", "partial", "complete"}:
        raise OntologyChangedValidationError("INVALID_COMPLETENESS", "invalid digest completeness")
    base = hints.get("base_mutation_revision")
    semantic = hints.get("semantic_manifest_digest")
    entries = hints.get("entries")
    if not isinstance(entries, Mapping):
        raise OntologyChangedValidationError("INVALID_DIGEST_ENTRIES", "digest entries must be an object")
    normalized_paths: list[str] = []
    for path, digest in entries.items():
        try:
            normalized_path = normalize_relative_path(path)
        except (TypeError, ValueError) as exc:
            raise OntologyChangedValidationError("INVALID_PATH", "invalid digest path") from exc
        normalized_paths.append(normalized_path)
        if not isinstance(digest, Mapping) or set(digest) != {"sha256", "size_bytes"}:
            raise OntologyChangedValidationError("INVALID_DIGEST_ENTRY", "digest entry has invalid fields")
        sha256 = digest.get("sha256")
        size_bytes = digest.get("size_bytes")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise OntologyChangedValidationError("INVALID_SHA256", "digest must be lowercase SHA-256")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise OntologyChangedValidationError("INVALID_SIZE", "size_bytes must be a non-negative integer")
    if normalized_paths != sorted(normalized_paths, key=lambda item: item.encode("utf-8")):
        raise OntologyChangedValidationError("UNSORTED_PATHS", "digest entries must use canonical path order")
    if completeness == "none":
        if not _none_digest_hints_are_valid(scope, after_version, base, semantic, entries):
            raise OntologyChangedValidationError("INVALID_NONE_HINTS", "none digest hints have invalid values")
    elif completeness == "partial":
        if scope is None or scope != after_version or base != revision - 1:
            raise OntologyChangedValidationError("INVALID_PARTIAL_HINTS", "partial digest hints have invalid values")
        if semantic is not None and (not isinstance(semantic, str) or not _SHA256_RE.fullmatch(semantic)):
            raise OntologyChangedValidationError("INVALID_MANIFEST_DIGEST", "invalid semantic manifest digest")
    else:
        if scope is None or scope != after_version or base is not None:
            raise OntologyChangedValidationError("INVALID_COMPLETE_HINTS", "complete digest hints have invalid values")
        if not isinstance(semantic, str) or not _SHA256_RE.fullmatch(semantic):
            raise OntologyChangedValidationError("INVALID_MANIFEST_DIGEST", "complete hints require manifest digest")


def _none_digest_hints_are_valid(
    scope: str | None,
    after_version: str | None,
    base: Any,
    semantic: Any,
    entries: Mapping[str, Any],
) -> bool:
    if scope != after_version:
        return False
    if base is not None:
        return False
    if semantic is not None:
        return False
    return not entries


def _validate_deleted_paths(deleted: Any, *, entries: Mapping[str, Any]) -> None:
    if not isinstance(deleted, list) or not all(isinstance(item, str) for item in deleted):
        raise OntologyChangedValidationError("INVALID_DELETED_PATHS", "deleted_paths must be a string array")
    try:
        normalized = [normalize_relative_path(item) for item in deleted]
    except ValueError as exc:
        raise OntologyChangedValidationError("INVALID_PATH", "invalid deleted path") from exc
    if len(set(normalized)) != len(normalized):
        raise OntologyChangedValidationError("DUPLICATE_PATH", "deleted_paths must be unique")
    if normalized != sorted(normalized, key=lambda item: item.encode("utf-8")):
        raise OntologyChangedValidationError("UNSORTED_PATHS", "deleted_paths must use canonical path order")
    if set(normalized).intersection(entries):
        raise OntologyChangedValidationError("OVERLAPPING_PATHS", "deleted_paths and entries overlap")


def _validate_version_combination(mutation_type: str, before: str | None, after: str | None, completeness: str) -> None:
    if mutation_type == "ACTIVE_VERSION_CHANGED" and (after is None or before == after):
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "active version change requires a new after_version")
    if mutation_type == "ONTOLOGY_VISIBILITY_CHANGED" and before != after:
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "visibility change cannot change active version")
    if mutation_type == "ONTOLOGY_FILE_CHANGED" and (before is None or before != after):
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "file change requires one active version")
    if mutation_type == "ONTOLOGY_IMPORTED" and after is None:
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "import requires an active version")
    if mutation_type == "ONTOLOGY_VERSION_DELETED" and before != after:
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "version deletion cannot change active version")
    if mutation_type == "ONTOLOGY_DELETED" and (after is not None or completeness != "none"):
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "ontology deletion requires after_version null and no hints")
    if mutation_type == "EXTERNAL_DRIFT_DETECTED" and not _external_drift_versions_are_valid(
        before,
        after,
        completeness,
    ):
        raise OntologyChangedValidationError("INVALID_VERSION_COMBINATION", "external drift requires one active version and no hints")


def _external_drift_versions_are_valid(
    before: str | None,
    after: str | None,
    completeness: str,
) -> bool:
    if before is None:
        return False
    if before != after:
        return False
    return completeness == "none"


def _validate_datetime(value: Any) -> None:
    if not isinstance(value, str):
        raise OntologyChangedValidationError("INVALID_OCCURRED_AT", "occurred_at must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OntologyChangedValidationError("INVALID_OCCURRED_AT", "occurred_at must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OntologyChangedValidationError("INVALID_OCCURRED_AT", "occurred_at must include a timezone")


def _validate_identifier(value: Any, field_name: str) -> None:
    if not _valid_identifier_component(value):
        raise OntologyChangedValidationError("INVALID_IDENTIFIER", f"invalid {field_name}")


def _validate_version(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not _valid_identifier_component(value):
        raise OntologyChangedValidationError("INVALID_VERSION", f"invalid {field_name}")
    return value


def _valid_identifier_component(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if not value:
        return False
    if len(value) > 128:
        return False
    return not any(char in value for char in ("/", "\\", "\x00"))


def _validate_operator(value: Any, *, present: bool) -> None:
    if not present or value is None:
        return
    if not isinstance(value, Mapping) or set(value) != {"type", "id"}:
        raise OntologyChangedValidationError("INVALID_OPERATOR", "operator must contain type and id")
    if value.get("type") not in OPERATOR_TYPES:
        raise OntologyChangedValidationError("INVALID_OPERATOR", "unsupported operator type")
    identifier = value.get("id")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
        raise OntologyChangedValidationError("INVALID_OPERATOR", "operator id must contain 1-128 characters")


def _validate_optional_string(value: Any, field_name: str, *, present: bool, limit: int) -> None:
    if not present or value is None:
        return
    if not isinstance(value, str) or len(value) > limit:
        raise OntologyChangedValidationError("INVALID_OPTIONAL_FIELD", f"invalid {field_name}")


def _normalize_deleted_paths(paths: Sequence[str]) -> list[str]:
    normalized = {normalize_relative_path(path) for path in paths}
    return sorted(normalized, key=lambda item: item.encode("utf-8"))
