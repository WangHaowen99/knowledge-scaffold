"""Strict frozen-snapshot resolution and durable validation evidence storage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from workspace.workspaces import get_workspace_paths
from utils.atomic_files import staged_bytes

from .canonical import sha256_digest
from .run_ledger import RunLedgerError, SolverRunLedger


class ValidationArtifactError(ValueError):
    """Raised when immutable validation artifacts cannot be trusted."""


class ValidationArtifactIntegrityError(ValidationArtifactError):
    """Raised when a registered artifact does not match its content."""


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationArtifactError("{} is required".format(field_name))
    return value.strip()


@dataclass(frozen=True)
class ResolvedFactSnapshot:
    workspace_id: str
    owner_id: str
    source_run_id: str
    knowledge_package_id: str
    knowledge_package_revision: str
    rules_revision: str
    fact_snapshot_ref: str
    fact_snapshot_digest: str
    fact_snapshot_path: Path
    fact_payload: Mapping[str, Any]


class FactSnapshotArtifactResolver:
    _FIELDS = frozenset(
        {
            "schema_version",
            "artifact_type",
            "source_run_id",
            "knowledge_package_id",
            "knowledge_package_revision",
            "rules_revision",
            "fact_payload",
            "fact_snapshot_digest",
        }
    )

    def __init__(
        self,
        workspace_id: str,
        *,
        ledger: SolverRunLedger | None = None,
    ) -> None:
        self.workspace_id = _required_text(workspace_id, "workspace_id")
        self.ledger = ledger or SolverRunLedger(self.workspace_id)
        if self.ledger.workspace_id != self.workspace_id:
            raise ValidationArtifactError("workspace ledger does not match")
        self.runtime_root = get_workspace_paths(self.workspace_id).runtime_dir.resolve()

    def resolve(
        self,
        *,
        owner_id: str,
        source_run_id: str,
        knowledge_package_id: str,
        knowledge_package_revision: str,
    ) -> ResolvedFactSnapshot:
        owner = _required_text(owner_id, "owner_id")
        source = _required_text(source_run_id, "source_run_id")
        package_id = _required_text(knowledge_package_id, "knowledge_package_id")
        revision = _required_text(
            knowledge_package_revision, "knowledge_package_revision"
        )
        rows = self.ledger.find_artifacts(
            owner_id=owner,
            source_run_id=source,
            artifact_type="fact_snapshot",
        )
        if len(rows) != 1:
            if not rows:
                raise ValidationArtifactError(
                    "fact snapshot source run was not found"
                )
            raise ValidationArtifactError("exactly one fact snapshot is required")
        row = rows[0]
        target = self._resolve_artifact_path(row.get("artifact_path"))
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationArtifactIntegrityError("fact snapshot cannot be read") from exc
        if not isinstance(payload, dict) or set(payload) != self._FIELDS:
            raise ValidationArtifactIntegrityError("fact snapshot fields are invalid")
        if (
            payload.get("schema_version")
            != "solver_coordination.fact_snapshot.v1"
            or payload.get("artifact_type") != "fact_snapshot"
        ):
            raise ValidationArtifactIntegrityError("fact snapshot schema is invalid")
        identities = {
            "source_run_id": source,
            "knowledge_package_id": package_id,
            "knowledge_package_revision": revision,
        }
        for field_name, expected in identities.items():
            if payload.get(field_name) != expected:
                raise ValidationArtifactIntegrityError(
                    "fact snapshot {} identity does not match".format(field_name)
                )
        fact_payload = payload.get("fact_payload")
        if not isinstance(fact_payload, Mapping):
            raise ValidationArtifactIntegrityError("fact snapshot payload is invalid")
        digest = sha256_digest({"fact_payload": fact_payload})
        if (
            payload.get("fact_snapshot_digest") != digest
            or row.get("artifact_digest") != digest
        ):
            raise ValidationArtifactIntegrityError("fact snapshot digest does not match")
        return ResolvedFactSnapshot(
            workspace_id=self.workspace_id,
            owner_id=owner,
            source_run_id=source,
            knowledge_package_id=package_id,
            knowledge_package_revision=revision,
            rules_revision=_required_text(payload.get("rules_revision"), "rules_revision"),
            fact_snapshot_ref=_required_text(row.get("artifact_ref"), "fact_snapshot_ref"),
            fact_snapshot_digest=digest,
            fact_snapshot_path=target,
            fact_payload=dict(fact_payload),
        )

    def _resolve_artifact_path(self, artifact_path: object) -> Path:
        raw = _required_text(artifact_path, "artifact_path")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationArtifactIntegrityError("fact snapshot path is invalid")
        target = (self.runtime_root / relative).resolve()
        try:
            target.relative_to(self.runtime_root)
        except ValueError as exc:
            raise ValidationArtifactIntegrityError("fact snapshot path is invalid") from exc
        if not target.is_file() or target.is_symlink():
            raise ValidationArtifactIntegrityError("fact snapshot path is invalid")
        return target


class ValidationEvidenceStore:
    """Persist evidence before exposing its content-addressed reference."""

    def __init__(
        self,
        workspace_id: str,
        *,
        ledger: SolverRunLedger | None = None,
        root: str | Path | None = None,
    ) -> None:
        self.workspace_id = _required_text(workspace_id, "workspace_id")
        self.ledger = ledger or SolverRunLedger(self.workspace_id)
        if self.ledger.workspace_id != self.workspace_id:
            raise ValidationArtifactError("workspace ledger does not match")
        self.runtime_root = get_workspace_paths(self.workspace_id).runtime_dir.resolve()
        self.root = (
            Path(root).resolve()
            if root is not None
            else self.runtime_root / "solver_coordination" / "validation_evidence"
        )
        try:
            self.root.relative_to(self.runtime_root)
        except ValueError as exc:
            raise ValidationArtifactError("evidence root is outside workspace") from exc
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: Any, *, owner_id: str, source_run_id: str) -> str:
        owner = _required_text(owner_id, "owner_id")
        source = _required_text(source_run_id, "source_run_id")
        digest = sha256_digest(
            {"source_run_id": source, "evidence_payload": payload}
        )
        suffix = digest.rsplit(":", 1)[-1]
        ref = "validation_evidence_{}".format(suffix)
        owner_root = self.root / sha256_digest({"owner_id": owner}).rsplit(":", 1)[-1]
        owner_root.mkdir(parents=True, exist_ok=True)
        target = owner_root / (ref + ".json")
        envelope = {
            "schema_version": "solver_coordination.validation_evidence.v1",
            "artifact_type": "validation_evidence",
            "source_run_id": source,
            "evidence_payload": payload,
            "validation_evidence_digest": digest,
        }
        body = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with staged_bytes(
                directory=owner_root,
                prefix=".validation-evidence-",
                suffix=".tmp",
                payload=body,
            ) as temporary:
                if target.exists():
                    if target.read_bytes() != body:
                        raise ValidationArtifactIntegrityError(
                            "validation evidence digest collision"
                        )
                else:
                    os.replace(temporary, target)
                relative_path = target.relative_to(self.runtime_root).as_posix()
                self.ledger.register_artifact(
                    artifact_digest=digest,
                    artifact_type="validation_evidence",
                    artifact_ref=ref,
                    artifact_path=relative_path,
                    owner_id=owner,
                    source_run_id=source,
                )
        except (OSError, RunLedgerError, ValidationArtifactError) as exc:
            # Do not leave an unindexed artifact that could later be mistaken
            # for durable evidence.
            try:
                if self.ledger.get_artifact(digest, owner_id=owner) is None:
                    target.unlink(missing_ok=True)
            except Exception:
                target.unlink(missing_ok=True)
            if isinstance(exc, ValidationArtifactError):
                raise
            raise ValidationArtifactError("failed to register validation evidence") from exc
        return ref

    def get(
        self,
        evidence_ref: str,
        *,
        owner_id: str,
        source_run_id: str,
    ) -> Any:
        ref = _required_text(evidence_ref, "evidence_ref")
        rows = self.ledger.find_artifacts(
            owner_id=owner_id,
            source_run_id=source_run_id,
            artifact_type="validation_evidence",
        )
        row = next((item for item in rows if item.get("artifact_ref") == ref), None)
        if row is None:
            raise ValidationArtifactError("validation evidence not found")
        target = (self.runtime_root / str(row["artifact_path"])).resolve()
        try:
            target.relative_to(self.runtime_root)
            envelope = json.loads(target.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ValidationArtifactIntegrityError("validation evidence is invalid") from exc
        return envelope.get("evidence_payload")
