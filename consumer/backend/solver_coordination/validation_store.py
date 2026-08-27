"""Workspace/owner-scoped persistence and idempotency for validation runs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from utils.atomic_files import staged_bytes
from utils.identifiers import validate_session_id
from workspace.workspaces import get_workspace_paths

from .canonical import sha256_digest


logger = logging.getLogger(__name__)


class ValidationRunStoreError(ValueError):
    """Raised when a validation artifact cannot be read or written."""


class ValidationRunConflict(ValidationRunStoreError):
    """Raised when an idempotency key is reused with different input."""


class _LockEntry:
    """A re-entrant lock plus active-context count for registry reclamation."""

    __slots__ = ("lock", "references")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.references = 0


class ValidationRunStore:
    """Persist immutable validation responses and idempotency records.

    Idempotency is scoped by ``(workspace, owner, key)``.  The route normally
    holds the per-key lock across read, validation, and write; ``put`` also
    acquires the same re-entrant lock so direct callers retain atomic
    read/check/write semantics.  The registry is process-local by design, but
    entries are reference counted and removed after the final waiter exits.
    Durable result records remain available after process restart.
    """

    _registry_guard = threading.Lock()
    _idempotency_locks: dict[tuple[str, str, str], _LockEntry] = {}
    _DIGEST_RE = re.compile(r"^sha256:solver-coordination\.v1:[0-9a-f]{64}$")
    _RUN_FIELDS = {
        "validation_run_id",
        "source_run_id",
        "candidate_set_digest",
        "candidate_set_ref",
        "problem_digest",
        "validator_revision",
        "result_count",
        "checked_candidate_count",
        "completed_count",
        "error_count",
        "batch_status",
        "selection_status",
        "feasible_candidate_ids",
        "published_candidate_ids",
        "all_candidate_results",
    }
    _RESULT_FIELDS = {
        "candidate_id",
        "candidate_digest",
        "status",
        "knowledge_package_id",
        "knowledge_package_revision",
        "fact_snapshot_digest",
        "rules_revision",
        "evidence_ref",
        "error_code",
        "error_stage",
    }

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = str(workspace_id or "").strip()
        if not self.workspace_id:
            raise ValidationRunStoreError("workspace_id is required")
        root = get_workspace_paths(self.workspace_id).runtime_dir / "solver_coordination"
        self._runs_root = root / "validation_runs"
        self._idempotency_root = root / "validation_idempotency"
        self._runs_root.mkdir(parents=True, exist_ok=True)
        self._idempotency_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key_digest(idempotency_key: str) -> str:
        return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _owner_digest(owner_id: str) -> str:
        # Keep externally controlled usernames out of filenames while keeping
        # namespaces collision-resistant.
        return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()

    @classmethod
    @contextmanager
    def idempotency_lock(
        cls,
        *,
        workspace_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> Iterator[None]:
        """Serialize one ``workspace + owner + key`` request in this process."""

        try:
            normalized_workspace = validate_session_id(workspace_id)
            normalized_owner = str(owner_id or "").strip()
            if not normalized_owner:
                raise ValueError("owner_id is required")
            normalized_key = validate_session_id(idempotency_key)
        except ValueError as exc:
            raise ValidationRunStoreError("idempotency scope is invalid") from exc
        lock_key = (normalized_workspace, normalized_owner, normalized_key)
        with cls._registry_guard:
            entry = cls._idempotency_locks.get(lock_key)
            if entry is None:
                entry = _LockEntry()
                cls._idempotency_locks[lock_key] = entry
            entry.references += 1
        try:
            with entry.lock:
                yield
        finally:
            with cls._registry_guard:
                entry.references -= 1
                if entry.references <= 0 and cls._idempotency_locks.get(lock_key) is entry:
                    del cls._idempotency_locks[lock_key]

    @property
    def idempotency_lock_count(self) -> int:
        """Return the current process registry size (useful for health tests)."""

        with self._registry_guard:
            return len(self._idempotency_locks)

    def _run_path(self, run_id: str, *, owner_id: str) -> Path:
        try:
            normalized = validate_session_id(run_id)
        except ValueError as exc:
            raise ValidationRunStoreError("validation_run_id is invalid") from exc
        if not normalized.startswith("validation_run_"):
            raise ValidationRunStoreError("validation_run_id is invalid")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValidationRunStoreError("owner_id is required")
        owner_root = self._runs_root / self._owner_digest(owner)
        target = (owner_root / (normalized + ".json")).resolve()
        try:
            target.relative_to(self._runs_root.resolve())
        except ValueError as exc:
            raise ValidationRunStoreError("validation_run_id resolves outside storage") from exc
        return target

    def _idempotency_path(self, key: str, *, owner_id: str) -> Path:
        try:
            normalized = validate_session_id(key)
        except ValueError as exc:
            raise ValidationRunStoreError("idempotency key is invalid") from exc
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValidationRunStoreError("owner_id is required")
        owner_root = self._idempotency_root / self._owner_digest(owner)
        owner_root.mkdir(parents=True, exist_ok=True)
        target = (owner_root / (self._key_digest(normalized) + ".json")).resolve()
        try:
            target.relative_to(self._idempotency_root.resolve())
        except ValueError as exc:
            raise ValidationRunStoreError("idempotency key resolves outside storage") from exc
        return target

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationRunStoreError("validation artifact is unreadable") from exc
        if not isinstance(value, dict):
            raise ValidationRunStoreError("validation artifact is invalid")
        return value

    @classmethod
    def _require_digest(cls, value: object, field_name: str) -> str:
        if not isinstance(value, str) or not cls._DIGEST_RE.fullmatch(value):
            raise ValidationRunStoreError(
                "validation artifact {} is not a versioned digest".format(field_name)
            )
        return value

    @staticmethod
    def _require_non_negative_count(value: object, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationRunStoreError(
                "validation result {} is invalid".format(field_name)
            )
        return value

    @classmethod
    def _validate_result_payload(cls, payload: dict[str, Any]) -> None:
        """Validate the immutable, typed response before it is replayable."""

        if set(payload) - cls._RUN_FIELDS:
            raise ValidationRunStoreError("validation result schema contains unsupported fields")
        required = {
            "validation_run_id",
            "source_run_id",
            "candidate_set_digest",
            "candidate_set_ref",
            "problem_digest",
            "validator_revision",
            "result_count",
            "checked_candidate_count",
            "completed_count",
            "error_count",
            "batch_status",
            "selection_status",
            "feasible_candidate_ids",
            "published_candidate_ids",
            "all_candidate_results",
        }
        if not required.issubset(payload):
            raise ValidationRunStoreError("validation result schema is incomplete")
        try:
            validate_session_id(payload["validation_run_id"])
            validate_session_id(payload["source_run_id"])
            validate_session_id(payload["candidate_set_ref"])
        except (TypeError, ValueError) as exc:
            raise ValidationRunStoreError("validation result identifier is invalid") from exc
        for field_name in (
            "validation_run_id",
            "source_run_id",
            "candidate_set_ref",
            "problem_digest",
            "validator_revision",
            "batch_status",
            "selection_status",
        ):
            value = payload[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ValidationRunStoreError(
                    "validation result {} is invalid".format(field_name)
                )
        cls._require_digest(payload["candidate_set_digest"], "candidate_set_digest")
        cls._require_digest(payload["problem_digest"], "problem_digest")
        if not str(payload["candidate_set_ref"]).startswith("candidate_set_"):
            raise ValidationRunStoreError("validation result candidate_set_ref is invalid")
        result_count = cls._require_non_negative_count(
            payload["result_count"], "result_count"
        )
        checked_candidate_count = cls._require_non_negative_count(
            payload["checked_candidate_count"], "checked_candidate_count"
        )
        completed_count = cls._require_non_negative_count(
            payload["completed_count"], "completed_count"
        )
        expected_error_count = cls._require_non_negative_count(
            payload["error_count"], "error_count"
        )
        if result_count < 1:
            raise ValidationRunStoreError("validation result must contain at least one result")
        results = payload["all_candidate_results"]
        if not isinstance(results, list) or result_count != len(results):
            raise ValidationRunStoreError("validation result count does not match results")
        if checked_candidate_count != result_count:
            raise ValidationRunStoreError("validation result checked count is inconsistent")
        if completed_count + expected_error_count != result_count:
            raise ValidationRunStoreError("validation result completion counts are inconsistent")
        result_ids: list[str] = []
        actual_error_count = 0
        feasible_ids: list[str] = []
        for result in results:
            if not isinstance(result, dict) or set(result) - cls._RESULT_FIELDS:
                raise ValidationRunStoreError("validation result item schema is invalid")
            for field_name in (
                "candidate_id",
                "knowledge_package_id",
                "knowledge_package_revision",
                "rules_revision",
                "evidence_ref",
            ):
                if not isinstance(result.get(field_name), str) or not result[field_name].strip():
                    raise ValidationRunStoreError("validation result item is incomplete")
            cls._require_digest(result.get("candidate_digest"), "candidate_digest")
            cls._require_digest(result.get("fact_snapshot_digest"), "fact_snapshot_digest")
            if result.get("status") not in {"feasible", "infeasible", "validation_error"}:
                raise ValidationRunStoreError("validation result item status is invalid")
            result_ids.append(result["candidate_id"])
            if result["status"] == "validation_error":
                actual_error_count += 1
                if not result.get("error_code") or not result.get("error_stage"):
                    raise ValidationRunStoreError("validation error item lacks diagnostics")
            elif result.get("error_code") or result.get("error_stage"):
                raise ValidationRunStoreError("completed result contains error diagnostics")
            if result["status"] == "feasible":
                feasible_ids.append(result["candidate_id"])
        if actual_error_count != expected_error_count:
            raise ValidationRunStoreError("validation result error count is inconsistent")
        feasible = payload["feasible_candidate_ids"]
        published = payload["published_candidate_ids"]
        if not isinstance(feasible, list) or not isinstance(published, list):
            raise ValidationRunStoreError("validation result candidate ID lists are invalid")
        if feasible != feasible_ids:
            raise ValidationRunStoreError("validation result feasible IDs are inconsistent")
        if actual_error_count:
            if payload["batch_status"] != "validation_failed" or payload["selection_status"] != "not_publishable" or published:
                raise ValidationRunStoreError("validation result publication state is inconsistent")
        else:
            if payload["batch_status"] != "validation_completed" or payload["selection_status"] != "published" or published != feasible:
                raise ValidationRunStoreError("validation result publication state is inconsistent")
        if len(set(result_ids)) != len(result_ids):
            raise ValidationRunStoreError("validation result candidate IDs are duplicated")

    @classmethod
    def _content_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        metadata_fields = {
            "schema_version",
            "artifact_type",
            "workspace_id",
            "owner_id",
            "created_at",
            "updated_at",
            "payload_digest",
            "request_fingerprint",
        }
        content: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in metadata_fields:
                content[key] = value
        return content

    @classmethod
    def _payload_digest(cls, payload: dict[str, Any]) -> str:
        return sha256_digest(cls._content_payload(payload))

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            logger.warning("Failed to fsync a validation artifact directory")
        finally:
            os.close(fd)

    @classmethod
    def _validate_run_envelope(
        cls,
        payload: dict[str, Any],
        *,
        workspace_id: str,
        owner_id: str,
        expected_run_id: str | None = None,
        expected_fingerprint: str | None = None,
        expected_payload_digest: str | None = None,
    ) -> None:
        if payload.get("schema_version") != "solver_coordination.v1" or payload.get("artifact_type") != "validation_run":
            raise ValidationRunStoreError("validation result schema is invalid")
        if payload.get("workspace_id") != workspace_id or payload.get("owner_id") != owner_id:
            raise ValidationRunStoreError("validation result scope is invalid")
        run_id = payload.get("validation_run_id")
        if expected_run_id is not None and run_id != expected_run_id:
            raise ValidationRunStoreError("validation result run identity is invalid")
        if expected_fingerprint is not None and payload.get("request_fingerprint") != expected_fingerprint:
            raise ValidationRunStoreError("validation result request identity is invalid")
        stored_digest = payload.get("payload_digest")
        if not isinstance(stored_digest, str) or stored_digest != cls._payload_digest(payload):
            raise ValidationRunStoreError("validation result integrity digest does not match")
        if expected_payload_digest is not None and stored_digest != expected_payload_digest:
            raise ValidationRunStoreError("validation result pointer digest does not match")
        content = cls._content_payload(payload)
        cls._validate_result_payload(content)

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            with staged_bytes(
                directory=path.parent,
                prefix=".validation-",
                suffix=".tmp",
                payload=encoded,
            ) as temporary:
                temporary.replace(path)
                ValidationRunStore._fsync_directory(path.parent)
        except Exception as exc:
            raise ValidationRunStoreError("failed to persist validation artifact") from exc

    def get_idempotent(
        self,
        *,
        idempotency_key: str,
        owner_id: str,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValidationRunStoreError("owner_id is required")
        path = self._idempotency_path(idempotency_key, owner_id=owner)
        record = self._read(path)
        if record is None:
            return None
        # The namespace is already owner-specific; retain payload checks to
        # detect hand-edited or migrated records without leaking details.
        if record.get("workspace_id") != self.workspace_id or record.get("owner_id") != owner:
            raise ValidationRunStoreError("validation request is not available")
        if (
            record.get("schema_version") != "solver_coordination.v1"
            or record.get("artifact_type") != "validation_idempotency"
        ):
            raise ValidationRunStoreError("validation request pointer schema is invalid")
        if record.get("request_fingerprint") != request_fingerprint:
            raise ValidationRunConflict("idempotency key is already bound to different input")
        if not isinstance(record.get("validation_run_id"), str):
            raise ValidationRunStoreError("validation request pointer is invalid")
        if not isinstance(record.get("payload_digest"), str):
            raise ValidationRunStoreError("validation request pointer is invalid")
        run_payload = self._read(
            self._run_path(record.get("validation_run_id", ""), owner_id=owner)
        )
        if run_payload is None:
            raise ValidationRunStoreError("validation result is unavailable")
        if run_payload.get("workspace_id") != self.workspace_id or run_payload.get("owner_id") != owner:
            raise ValidationRunStoreError("validation result is not available")
        self._validate_run_envelope(
            run_payload,
            workspace_id=self.workspace_id,
            owner_id=owner,
            expected_run_id=record["validation_run_id"],
            expected_fingerprint=request_fingerprint,
            expected_payload_digest=record["payload_digest"],
        )
        return run_payload

    def put(
        self,
        *,
        idempotency_key: str,
        owner_id: str,
        request_fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValidationRunStoreError("owner_id is required")
        # RLock permits this method to be called from a route that already
        # holds idempotency_lock for the same scope.
        with self.idempotency_lock(
            workspace_id=self.workspace_id,
            owner_id=owner,
            idempotency_key=idempotency_key,
        ):
            existing = self.get_idempotent(
                idempotency_key=idempotency_key,
                owner_id=owner,
                request_fingerprint=request_fingerprint,
            )
            if existing is not None:
                return existing
            run_id = str(payload.get("validation_run_id") or "").strip()
            run_path = self._run_path(run_id, owner_id=owner)
            run_payload = dict(payload)
            run_payload.update(
                {
                    "schema_version": "solver_coordination.v1",
                    "artifact_type": "validation_run",
                    "workspace_id": self.workspace_id,
                    "owner_id": owner,
                    "request_fingerprint": request_fingerprint,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            self._validate_result_payload(self._content_payload(run_payload))
            run_payload["payload_digest"] = self._payload_digest(run_payload)
            self._atomic_write(run_path, run_payload)
            idem_payload = {
                "workspace_id": self.workspace_id,
                "owner_id": owner,
                "request_fingerprint": request_fingerprint,
                "validation_run_id": run_id,
                "payload_digest": run_payload["payload_digest"],
                "schema_version": "solver_coordination.v1",
                "artifact_type": "validation_idempotency",
            }
            self._atomic_write(
                self._idempotency_path(idempotency_key, owner_id=owner),
                idem_payload,
            )
            return run_payload
