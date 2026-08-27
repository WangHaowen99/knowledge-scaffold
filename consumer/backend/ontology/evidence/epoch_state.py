"""Recoverable digest snapshots for ontology evidence epoch reconciliation."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ontology.change_events import DIGEST_BASIS, new_ulid
from ontology.evidence.identity import build_file_set_digest, build_ontology_epoch
from ontology.hash_contract import field_h, field_i, field_s, hash_frames, normalize_relative_path


CONTRACT = "OntologyEvidenceEpochState/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_FIELDS = {
    "contract",
    "workspace_id",
    "ontology_id",
    "active_version",
    "mutation_revision",
    "last_event_id",
    "digest_basis",
    "semantic_manifest_digest",
    "file_set_digest",
    "file_digests",
    "ontology_epoch",
    "epoch_state_digest",
    "updated_at",
}


class EpochStateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_epoch_state(
    *,
    workspace_id: str,
    ontology_id: str,
    active_version: str,
    mutation_revision: int,
    semantic_manifest_digest: str,
    file_digests: Mapping[str, str],
    file_sizes: Mapping[str, int],
    last_event_id: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for raw_path in sorted(file_digests, key=lambda value: str(value).encode("utf-8")):
        path = normalize_relative_path(str(raw_path))
        if path in entries:
            raise EpochStateError("DUPLICATE_PATH", "duplicate epoch-state path")
        entries[path] = {
            "sha256": str(file_digests[raw_path]),
            "size_bytes": int(file_sizes[raw_path]),
        }
    file_set_digest = build_file_set_digest(
        {path: str(entry["sha256"]) for path, entry in entries.items()}
    )
    ontology_epoch = build_ontology_epoch(
        ontology_id=ontology_id,
        active_version=active_version,
        semantic_manifest_digest=semantic_manifest_digest,
        file_set_digest=file_set_digest,
    )
    event_id = str(last_event_id or new_ulid())
    payload: dict[str, Any] = {
        "contract": CONTRACT,
        "workspace_id": workspace_id,
        "ontology_id": ontology_id,
        "active_version": active_version,
        "mutation_revision": int(mutation_revision),
        "last_event_id": event_id,
        "digest_basis": DIGEST_BASIS,
        "semantic_manifest_digest": semantic_manifest_digest,
        "file_set_digest": file_set_digest,
        "file_digests": entries,
        "ontology_epoch": ontology_epoch,
        "epoch_state_digest": _state_digest(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            active_version=active_version,
            mutation_revision=int(mutation_revision),
            last_event_id=event_id,
            semantic_manifest_digest=semantic_manifest_digest,
            file_set_digest=file_set_digest,
            ontology_epoch=ontology_epoch,
        ),
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
    }
    validate_epoch_state(payload)
    return payload


def validate_epoch_state(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        raise EpochStateError("INVALID_EPOCH_STATE", "epoch state has invalid fields")
    if payload.get("contract") != CONTRACT:
        raise EpochStateError("INVALID_CONTRACT", "invalid epoch-state contract")
    for field in ("workspace_id", "ontology_id", "active_version"):
        value = payload.get(field)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise EpochStateError("INVALID_EPOCH_STATE", f"invalid {field}")
    revision = payload.get("mutation_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise EpochStateError("INVALID_EPOCH_STATE", "invalid mutation_revision")
    event_id = payload.get("last_event_id")
    if not isinstance(event_id, str) or not _ULID_RE.fullmatch(event_id):
        raise EpochStateError("INVALID_EPOCH_STATE", "invalid last_event_id")
    if payload.get("digest_basis") != DIGEST_BASIS:
        raise EpochStateError("INVALID_DIGEST_BASIS", "unsupported epoch-state digest basis")
    for field in (
        "semantic_manifest_digest",
        "file_set_digest",
        "ontology_epoch",
        "epoch_state_digest",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise EpochStateError("INVALID_SHA256", f"invalid {field}")
    try:
        parsed_at = datetime.fromisoformat(str(payload.get("updated_at") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise EpochStateError("INVALID_EPOCH_STATE", "invalid updated_at") from exc
    if parsed_at.tzinfo is None:
        raise EpochStateError("INVALID_EPOCH_STATE", "updated_at must include timezone")

    raw_entries = payload.get("file_digests")
    if not isinstance(raw_entries, Mapping) or len(raw_entries) > 5_000:
        raise EpochStateError("INVALID_EPOCH_STATE", "invalid file_digests")
    normalized: dict[str, str] = {}
    paths: list[str] = []
    for raw_path, raw_entry in raw_entries.items():
        path = normalize_relative_path(str(raw_path))
        paths.append(path)
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != {"sha256", "size_bytes"}:
            raise EpochStateError("INVALID_EPOCH_STATE", "invalid file digest entry")
        digest = raw_entry.get("sha256")
        size = raw_entry.get("size_bytes")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise EpochStateError("INVALID_SHA256", "invalid file digest")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise EpochStateError("INVALID_EPOCH_STATE", "invalid file size")
        normalized[path] = digest
    if paths != sorted(paths, key=lambda value: value.encode("utf-8")) or len(paths) != len(set(paths)):
        raise EpochStateError("INVALID_EPOCH_STATE", "file_digests are not canonically ordered")
    actual_file_set = build_file_set_digest(normalized)
    if actual_file_set != payload["file_set_digest"]:
        raise EpochStateError("EPOCH_STATE_INTEGRITY_FAILED", "file-set digest mismatch")
    actual_epoch = build_ontology_epoch(
        ontology_id=str(payload["ontology_id"]),
        active_version=str(payload["active_version"]),
        semantic_manifest_digest=str(payload["semantic_manifest_digest"]),
        file_set_digest=actual_file_set,
    )
    if actual_epoch != payload["ontology_epoch"]:
        raise EpochStateError("EPOCH_STATE_INTEGRITY_FAILED", "ontology epoch mismatch")
    actual_state_digest = _state_digest(
        workspace_id=str(payload["workspace_id"]),
        ontology_id=str(payload["ontology_id"]),
        active_version=str(payload["active_version"]),
        mutation_revision=int(revision),
        last_event_id=str(event_id),
        semantic_manifest_digest=str(payload["semantic_manifest_digest"]),
        file_set_digest=actual_file_set,
        ontology_epoch=actual_epoch,
    )
    if actual_state_digest != payload["epoch_state_digest"]:
        raise EpochStateError("EPOCH_STATE_INTEGRITY_FAILED", "epoch-state digest mismatch")


def read_epoch_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EpochStateError("INVALID_EPOCH_STATE", "epoch state is unreadable") from exc
    if not isinstance(payload, dict):
        raise EpochStateError("INVALID_EPOCH_STATE", "epoch state must be an object")
    validate_epoch_state(payload)
    return payload


def write_epoch_state(path: Path, payload: Mapping[str, Any]) -> None:
    validate_epoch_state(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _state_digest(
    *,
    workspace_id: str,
    ontology_id: str,
    active_version: str,
    mutation_revision: int,
    last_event_id: str,
    semantic_manifest_digest: str,
    file_set_digest: str,
    ontology_epoch: str,
) -> str:
    return hash_frames(
        "epoch_state/v1",
        [
            field_s("workspace_id", workspace_id),
            field_s("ontology_id", ontology_id),
            field_s("active_version", active_version),
            field_i("mutation_revision", mutation_revision),
            field_s("last_event_id", last_event_id),
            field_s("digest_basis", DIGEST_BASIS),
            field_h("semantic_manifest_digest", semantic_manifest_digest),
            field_h("file_set_digest", file_set_digest),
            field_h("ontology_epoch", ontology_epoch),
        ],
    )
