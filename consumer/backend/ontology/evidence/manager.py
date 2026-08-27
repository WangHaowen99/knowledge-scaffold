"""Build and atomically publish immutable ontology evidence generations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ontology.evidence.chunker import EvidenceChunker
from ontology.evidence.epoch_state import build_epoch_state, write_epoch_state
from ontology.evidence.identity import (
    CHUNKER_VERSION,
    INDEX_SCHEMA_VERSION,
    LEXICAL_ANALYZER_VERSION,
    RELATION_EXTRACTOR_VERSION,
    build_file_set_digest,
    build_index_generation,
    build_ontology_epoch,
    build_semantic_manifest_digest,
)
from ontology.evidence.source_reader import PackageSourceReader
from ontology.evidence.sqlite_index import EvidenceIndexError, SQLiteEvidenceIndex
from ontology.file_lock import exclusive_file_lock
from ontology.resource_parser import PARSER_VERSION, parse_resource_cached
from persistence.evidence_catalog import EvidenceCatalog


logger = logging.getLogger("ontology.evidence.manager")


MAX_SOURCE_FILES = 5_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 50_000

SERVICEABLE_POINTER_STATUSES = frozenset(
    {"lexical_ready", "building_semantic", "ready", "semantic_degraded"}
)

_ALLOWED_BUILD_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"queued", "building_base", "epoch_failed"}),
    "disabled": frozenset(
        {"disabled", "epoch_pending", "manual_required", "queued", "building_base", "epoch_failed"}
    ),
    "stale": frozenset(
        {"stale", "epoch_pending", "manual_required", "queued", "building_base", "epoch_failed"}
    ),
    "epoch_pending": frozenset(
        {"manual_required", "queued", "building_base", "epoch_failed", "disabled"}
    ),
    "epoch_failed": frozenset(
        {"epoch_failed", "epoch_pending", "manual_required", "queued", "building_base", "disabled"}
    ),
    "manual_required": frozenset({"queued", "building_base", "epoch_failed", "disabled"}),
    "queued": frozenset(
        {"building_base", "epoch_failed", "base_failed", "stale", "disabled"}
    ),
    "building_base": frozenset(
        {"lexical_ready", "epoch_failed", "base_failed", "stale", "disabled"}
    ),
    "lexical_ready": frozenset(
        {"ready", "building_semantic", "epoch_failed", "stale", "disabled"}
    ),
    "building_semantic": frozenset(
        {"ready", "semantic_degraded", "epoch_failed", "stale", "disabled"}
    ),
    "ready": frozenset(
        {"queued", "building_base", "epoch_pending", "epoch_failed", "manual_required", "stale", "disabled"}
    ),
    "semantic_degraded": frozenset(
        {"queued", "building_base", "epoch_pending", "epoch_failed", "manual_required", "stale", "disabled"}
    ),
    "base_failed": frozenset({"queued", "building_base", "epoch_failed", "stale", "disabled"}),
}


class EvidenceIndexManagerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceIndexManager:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve() / "ontology-evidence"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.catalog = EvidenceCatalog(self.runtime_root / "evidence-catalog.sqlite3")

    def build(
        self,
        *,
        workspace_id: str,
        ontology_id: str,
        ontology_version: str,
        ontology_dir: Path,
        manifest: Mapping[str, Any],
        mutation_revision: int = 0,
        trigger: str = "manual",
        expected_ontology_epoch: str | None = None,
        expected_index_generation: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        _validate_identifier(ontology_id, "ontology_id")
        if not str(workspace_id or "").strip():
            raise EvidenceIndexManagerError("INVALID_ARGUMENTS", "workspace_id is required")
        ontology_root = self.runtime_root / "ontologies" / ontology_id
        lock_path = ontology_root / "locks" / "build.lock"
        with exclusive_file_lock(lock_path):
            try:
                sources = PackageSourceReader().read(
                    ontology_dir=ontology_dir,
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                )
                _validate_source_capacity(sources)
                plan = _build_plan(
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    manifest=manifest,
                    sources=sources,
                )
            except Exception as exc:
                self._record_epoch_failure(
                    ontology_root,
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    mutation_revision=int(mutation_revision),
                    trigger=trigger,
                    error_code=str(getattr(exc, "code", "EPOCH_COMPUTE_FAILED")),
                )
                raise
            epoch = plan["ontology_epoch"]
            generation = plan["index_generation"]
            if expected_ontology_epoch is not None and epoch != expected_ontology_epoch:
                raise EvidenceIndexManagerError("INDEX_STALE", "ontology content changed after build planning")
            if expected_index_generation is not None and generation != expected_index_generation:
                raise EvidenceIndexManagerError("INDEX_STALE", "index generation changed after build planning")
            final_dir = ontology_root / "generations" / generation
            return self._build_single_flight(
                ontology_root=ontology_root,
                final_dir=final_dir,
                sources=sources,
                plan=plan,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
                mutation_revision=int(mutation_revision),
                trigger=trigger,
                event_id=event_id,
            )

    def _record_epoch_failure(
        self,
        ontology_root: Path,
        *,
        workspace_id: str,
        ontology_id: str,
        ontology_version: str,
        mutation_revision: int,
        trigger: str,
        error_code: str,
    ) -> None:
        """Persist planning failure without hiding the originating error."""

        pointer = {
            "contract": "OntologyEvidenceActivePointer/v1",
            "workspace_id": workspace_id,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "mutation_revision": mutation_revision,
            "enabled": True,
            "epoch_status": "failed",
            "expected_epoch": None,
            "active_generation": None,
            "target_generation": None,
            "retrieval_mode": "lexical",
            "semantic_status": "disabled",
            "trigger": trigger,
        }
        try:
            self._transition_build_state(
                ontology_root,
                pointer,
                status="epoch_failed",
                base_status="pending",
                error_code=error_code,
            )
        except Exception:
            # The planning exception remains authoritative. A newer mutation
            # may have superseded this build while failure state was recorded.
            return

    def _build_single_flight(
        self,
        *,
        ontology_root: Path,
        final_dir: Path,
        sources: Any,
        plan: Mapping[str, Any],
        workspace_id: str,
        ontology_id: str,
        ontology_version: str,
        mutation_revision: int,
        trigger: str,
        event_id: str | None,
    ) -> dict[str, Any]:
        """Build once per workspace/ontology and persist every base transition."""

        epoch = str(plan["ontology_epoch"])
        generation = str(plan["index_generation"])
        current = self.active_pointer(ontology_id)
        _validate_pointer_scope(
            current,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            mutation_revision=mutation_revision,
        )
        generation_valid = final_dir.is_dir() and _generation_is_valid(
            final_dir,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            ontology_epoch=epoch,
            index_generation=generation,
        )
        current_generation = (
            int((current or {}).get("mutation_revision") or 0),
            str((current or {}).get("expected_epoch") or ""),
            str((current or {}).get("active_generation") or ""),
        )
        expected_generation = (mutation_revision, epoch, generation)
        if (
            generation_valid
            and active_pointer_is_serviceable(current)
            and current_generation == expected_generation
        ):
            return dict(current or {})

        job_id = "idxjob_{}".format(uuid.uuid4().hex)
        pointer_base = {
            "contract": "OntologyEvidenceActivePointer/v1",
            "workspace_id": workspace_id,
            "ontology_id": ontology_id,
            "ontology_version": ontology_version,
            "mutation_revision": mutation_revision,
            "enabled": True,
            "epoch_status": "ready",
            "expected_epoch": epoch,
            "active_generation": None,
            "target_generation": generation,
            "retrieval_mode": "lexical",
            "semantic_status": "disabled",
            "build_job_id": job_id,
            "trigger": trigger,
        }
        self._transition_build_state(
            ontology_root,
            pointer_base,
            status="queued",
            base_status="queued",
        )
        self._transition_build_state(
            ontology_root,
            pointer_base,
            status="building_base",
            base_status="building",
        )
        try:
            if final_dir.is_dir() and not generation_valid:
                shutil.rmtree(final_dir, ignore_errors=True)
                if final_dir.exists():
                    raise EvidenceIndexManagerError(
                        "EVIDENCE_STORAGE_INCONSISTENT",
                        "invalid evidence generation could not be quarantined",
                    )
            if not final_dir.is_dir():
                self._build_generation(
                    job_id=job_id,
                    final_dir=final_dir,
                    sources=sources,
                    ontology_id=ontology_id,
                    ontology_version=ontology_version,
                    epoch=epoch,
                    generation=generation,
                    workspace_id=workspace_id,
                    mutation_revision=mutation_revision,
                )
            index = SQLiteEvidenceIndex(final_dir / "index.sqlite3")
            relative_path = (final_dir / "index.sqlite3").relative_to(self.runtime_root).as_posix()
            self.catalog.register_generation(index=index, index_relative_path=relative_path)
            epoch_state = build_epoch_state(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                active_version=ontology_version,
                mutation_revision=mutation_revision,
                semantic_manifest_digest=str(plan["semantic_manifest_digest"]),
                file_digests=sources.file_digests,
                file_sizes=sources.file_sizes,
                last_event_id=event_id,
            )
            write_epoch_state(ontology_root / "epoch-state.json", epoch_state)
            published = {
                **pointer_base,
                "epoch_state_revision": mutation_revision,
                "epoch_state_digest": epoch_state["epoch_state_digest"],
                "active_generation": generation,
                "base_status": "ready",
            }
            self._transition_build_state(
                ontology_root,
                published,
                status="lexical_ready",
                base_status="ready",
            )
            return self._transition_build_state(
                ontology_root,
                published,
                status="ready",
                base_status="ready",
            )
        except Exception as exc:
            try:
                latest = self.active_pointer(ontology_id)
                if (
                    latest
                    and int(latest.get("mutation_revision") or 0) == mutation_revision
                    and str(latest.get("build_job_id") or "") == job_id
                ):
                    self._transition_build_state(
                        ontology_root,
                        pointer_base,
                        status="base_failed",
                        base_status="failed",
                        error_code=str(getattr(exc, "code", "INDEX_BUILD_FAILED")),
                    )
            except Exception:
                logger.exception("failed to persist evidence index build failure state")
            raise

    def _transition_build_state(
        self,
        ontology_root: Path,
        pointer: Mapping[str, Any],
        *,
        status: str,
        base_status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        path = ontology_root / "active.json"
        with exclusive_file_lock(
            _pointer_lock_path(self.runtime_root.parent, str(pointer["ontology_id"]))
        ):
            current = read_active_pointer_snapshot(self.runtime_root.parent, str(pointer["ontology_id"]))
            _validate_pointer_scope(
                current,
                workspace_id=str(pointer["workspace_id"]),
                ontology_id=str(pointer["ontology_id"]),
                mutation_revision=int(pointer["mutation_revision"]),
            )
            current_status = str((current or {}).get("status") or "") or None
            allowed = _ALLOWED_BUILD_TRANSITIONS.get(current_status, frozenset())
            if status not in allowed:
                raise EvidenceIndexManagerError(
                    "INDEX_STATE_CONFLICT",
                    "invalid evidence build state transition: {} -> {}".format(current_status, status),
                )
            payload = {
                **dict(current or {}),
                **dict(pointer),
                "status": status,
                "base_status": base_status,
                "updated_at": _utc_now(),
            }
            if error_code:
                payload["error_code"] = error_code
            else:
                payload.pop("error_code", None)
            _atomic_json_write(path, payload)
            return payload

    def plan(
        self,
        *,
        ontology_id: str,
        ontology_version: str,
        ontology_dir: Path,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compute a target snapshot without creating or publishing a generation."""
        _validate_identifier(ontology_id, "ontology_id")
        sources = PackageSourceReader().read(
            ontology_dir=ontology_dir,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
        )
        _validate_source_capacity(sources)
        return _build_plan(
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            manifest=manifest,
            sources=sources,
        )

    def active_pointer(self, ontology_id: str) -> dict[str, Any] | None:
        return read_active_pointer_snapshot(self.runtime_root.parent, ontology_id)

    def open_active(self, ontology_id: str) -> SQLiteEvidenceIndex:
        pointer = self.active_pointer(ontology_id)
        if not pointer:
            raise EvidenceIndexManagerError("INDEX_NOT_READY", "evidence index is not ready")
        if not active_pointer_is_serviceable(pointer):
            status = str(pointer.get("status") or "")
            if status in {"epoch_failed", "base_failed"}:
                raise EvidenceIndexManagerError("INDEX_BUILD_FAILED", "evidence index build failed")
            if status == "stale":
                raise EvidenceIndexManagerError("INDEX_STALE", "evidence index is stale")
            if status == "disabled":
                raise EvidenceIndexManagerError(
                    "EVIDENCE_FEATURE_DISABLED",
                    "evidence index is disabled",
                )
            raise EvidenceIndexManagerError("INDEX_BUILDING", "evidence index is building")
        generation = str(pointer.get("active_generation") or "")
        _validate_digest(generation, "active_generation")
        index = SQLiteEvidenceIndex(
            self.runtime_root / "ontologies" / ontology_id / "generations" / generation / "index.sqlite3"
        )
        try:
            metadata = index.metadata()
        except (EvidenceIndexError, sqlite3.Error) as exc:
            raise EvidenceIndexManagerError(
                "EVIDENCE_STORAGE_INCONSISTENT",
                "active evidence generation is unreadable",
            ) from exc
        if (
            str(metadata.get("workspace_id") or "") != str(pointer.get("workspace_id") or "")
            or metadata["ontology_epoch"] != pointer.get("expected_epoch")
            or metadata["index_generation"] != generation
        ):
            raise EvidenceIndexManagerError("INDEX_STALE", "pointer and generation do not match")
        return index

    def revoke(self, ontology_id: str, *, mutation_revision: int) -> None:
        ontology_root = self.runtime_root / "ontologies" / ontology_id
        with exclusive_file_lock(_pointer_lock_path(self.runtime_root.parent, ontology_id)):
            pointer = self.active_pointer(ontology_id) or {
                "contract": "OntologyEvidenceActivePointer/v1",
                "ontology_id": ontology_id,
            }
            pointer.update(
                {
                    "mutation_revision": int(mutation_revision),
                    "active_generation": None,
                    "status": "stale",
                    "updated_at": _utc_now(),
                }
            )
            _atomic_json_write(ontology_root / "active.json", pointer)

    def cleanup(
        self,
        *,
        workspace_id: str,
        ontology_id: str | None = None,
        dry_run: bool = True,
        older_than_days: int = 7,
        max_items: int = 1000,
        recent_count: int = 2,
    ) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(older_than_days)))
        limit = max(1, min(int(max_items), 5000))
        ontology_roots = self.runtime_root / "ontologies"
        candidates: list[dict[str, Any]] = []
        if ontology_roots.is_dir():
            roots = [ontology_roots / ontology_id] if ontology_id else sorted(ontology_roots.iterdir())
            for root in roots:
                if not root.is_dir():
                    continue
                current_pointer = self.active_pointer(root.name)
                active_generation = str((current_pointer or {}).get("active_generation") or "")
                generations_dir = root / "generations"
                if not generations_dir.is_dir():
                    continue
                generation_dirs = sorted(
                    (item for item in generations_dir.iterdir() if item.is_dir()),
                    key=lambda item: (item.stat().st_mtime, item.name),
                    reverse=True,
                )
                recent_generations = {
                    item.name for item in generation_dirs[: max(0, int(recent_count))]
                }
                for generation_dir in sorted(generation_dirs, key=lambda item: item.name):
                    if not generation_dir.is_dir():
                        continue
                    generation = generation_dir.name
                    try:
                        _validate_digest(generation, "index_generation")
                    except EvidenceIndexManagerError:
                        continue
                    size_bytes = sum(
                        path.stat().st_size
                        for path in generation_dir.rglob("*")
                        if path.is_file()
                    )
                    modified_at = datetime.fromtimestamp(generation_dir.stat().st_mtime, tz=timezone.utc)
                    reasons: list[str] = []
                    eligible = True
                    if generation == active_generation:
                        eligible = False
                        reasons.append("ACTIVE_GENERATION")
                    if generation in recent_generations:
                        eligible = False
                        reasons.append("WITHIN_RECENT_COUNT")
                    if modified_at > cutoff:
                        eligible = False
                        reasons.append("WITHIN_MIN_RETENTION")
                    holds = self.catalog.generation_hold_count(generation)
                    if holds:
                        eligible = False
                        reasons.append("REFERENCE_HOLD")
                    if eligible:
                        reasons.extend(["OUTSIDE_RECENT_COUNT", "OLDER_THAN_MIN_DAYS"])
                    candidates.append(
                        {
                            "ontology_id": root.name,
                            "ontology_epoch": _read_generation_epoch(generation_dir),
                            "index_generation": generation,
                            "bytes": size_bytes,
                            "eligible": eligible,
                            "reasons": reasons,
                            "modified_at": modified_at.isoformat(),
                        }
                    )
                    if len(candidates) >= limit:
                        break
                if len(candidates) >= limit:
                    break

        deleted = 0
        deleted_bytes = 0
        if not dry_run:
            for item in candidates:
                if not item["eligible"]:
                    continue
                item_ontology_id = str(item["ontology_id"])
                generation = str(item["index_generation"])
                ontology_root = self.runtime_root / "ontologies" / item_ontology_id
                generation_dir = (
                    ontology_root
                    / "generations"
                    / generation
                )
                with exclusive_file_lock(ontology_root / "locks" / "build.lock"):
                    # Runtime Fact persistence and release use the same
                    # workspace-local lock. Holding it across the final hold
                    # check, route removal and directory deletion closes the
                    # reference-registration/cleanup race.
                    with exclusive_file_lock(self.runtime_root / "retention.lock"):
                        with exclusive_file_lock(
                            _pointer_lock_path(self.runtime_root.parent, item_ontology_id)
                        ):
                            latest_pointer = read_active_pointer_snapshot(
                                self.runtime_root.parent,
                                item_ontology_id,
                            )
                        if str((latest_pointer or {}).get("active_generation") or "") == generation:
                            item["eligible"] = False
                            item["reasons"] = ["ACTIVE_GENERATION"]
                            continue
                        if self.catalog.generation_hold_count(generation):
                            item["eligible"] = False
                            item["reasons"] = ["REFERENCE_HOLD"]
                            continue
                        if not generation_dir.is_dir():
                            self.catalog.remove_generation(generation)
                            item["eligible"] = False
                            item["reasons"] = ["GENERATION_ALREADY_REMOVED"]
                            continue

                        # Remove the route first. A crash after this point leaves
                        # a harmless, retriable orphan directory instead of a
                        # catalog route that points at missing storage.
                        self.catalog.remove_generation(generation)
                        shutil.rmtree(generation_dir)
                        deleted += 1
                        deleted_bytes += int(item["bytes"])

        eligible_items = [item for item in candidates if item["eligible"]]
        return {
            "contract": "EvidenceCleanup/v1",
            "dry_run": bool(dry_run),
            "workspace_id": workspace_id,
            "summary": {
                "candidate_generations": len(eligible_items),
                "candidate_epochs": len({item["ontology_epoch"] for item in eligible_items if item["ontology_epoch"]}),
                "candidate_bytes": sum(int(item["bytes"]) for item in eligible_items),
                "protected_by_active": sum("ACTIVE_GENERATION" in item["reasons"] for item in candidates),
                "protected_by_retention": sum(
                    "WITHIN_MIN_RETENTION" in item["reasons"]
                    or "WITHIN_RECENT_COUNT" in item["reasons"]
                    for item in candidates
                ),
                "protected_by_reference": sum("REFERENCE_HOLD" in item["reasons"] for item in candidates),
                "deleted_generations": deleted,
                "deleted_bytes": deleted_bytes,
            },
            "items": candidates,
        }

    def _build_generation(
        self,
        *,
        job_id: str,
        final_dir: Path,
        sources: Any,
        ontology_id: str,
        ontology_version: str,
        epoch: str,
        generation: str,
        workspace_id: str,
        mutation_revision: int,
    ) -> None:
        staging = final_dir.parent.parent / "tmp" / job_id
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            parsed = [
                parse_resource_cached(
                    source,
                    workspace_id=workspace_id,
                    mutation_revision=mutation_revision,
                )
                for source in sources.sources
            ]
            chunker = EvidenceChunker()
            chunks = [
                chunk
                for resource in parsed
                for chunk in chunker.chunk(resource, ontology_epoch=epoch, index_generation=generation)
            ]
            if len(chunks) > MAX_CHUNKS:
                raise EvidenceIndexManagerError(
                    "INDEX_CAPACITY_EXCEEDED",
                    "ontology evidence chunk count exceeds the supported limit",
                )
            built_at = _utc_now()
            stats = SQLiteEvidenceIndex.build(
                staging / "index.sqlite3",
                metadata={
                    "workspace_id": workspace_id,
                    "ontology_id": ontology_id,
                    "ontology_version": ontology_version,
                    "ontology_epoch": epoch,
                    "index_generation": generation,
                    "parser_version": PARSER_VERSION,
                    "chunker_version": CHUNKER_VERSION,
                    "lexical_analyzer_version": LEXICAL_ANALYZER_VERSION,
                    "relation_extractor_version": RELATION_EXTRACTOR_VERSION,
                    "file_count": len(sources.sources),
                    "built_at": built_at,
                    "build_id": job_id,
                },
                chunks=chunks,
            )
            artifact_hash = hashlib.sha256((staging / "index.sqlite3").read_bytes()).hexdigest()
            manifest = {
                "contract": "OntologyEvidenceIndexManifest/v1",
                "index_schema_version": INDEX_SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "ontology_id": ontology_id,
                "ontology_version": ontology_version,
                "ontology_epoch": epoch,
                "index_generation": generation,
                "generation_kind": "lexical",
                "components": {
                    "parser": PARSER_VERSION,
                    "chunker": CHUNKER_VERSION,
                    "lexical_analyzer": LEXICAL_ANALYZER_VERSION,
                    "relation_extractor": RELATION_EXTRACTOR_VERSION,
                },
                "counts": {"files": len(sources.sources), **stats, "vectors": 0},
                "artifacts": {"index.sqlite3": "sha256:" + artifact_hash},
                "built_at": built_at,
                "warnings": [warning.__dict__ for warning in sources.warnings],
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_dir)
            (final_dir / "index.sqlite3").chmod(0o444)
            (final_dir / "manifest.json").chmod(0o444)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_active_pointer_snapshot(runtime_root: Path, ontology_id: str) -> dict[str, Any] | None:
    """Read an active pointer without opening the catalog or an index.

    ``runtime_root`` is the workspace runtime directory.  This helper is used
    on schema/preload paths where the master switch must avoid SQLite work.
    """
    _validate_identifier(ontology_id, "ontology_id")
    path = runtime_root.resolve() / "ontology-evidence" / "ontologies" / ontology_id / "active.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceIndexManagerError("INDEX_POINTER_INVALID", "active evidence pointer is invalid") from exc
    if not isinstance(payload, dict):
        raise EvidenceIndexManagerError("INDEX_POINTER_INVALID", "active evidence pointer is invalid")
    return payload


def active_pointer_is_serviceable(pointer: Mapping[str, Any] | None) -> bool:
    """Return whether the pointer may serve its immutable lexical generation."""

    return bool(
        pointer
        and str(pointer.get("status") or "") in SERVICEABLE_POINTER_STATUSES
        and pointer.get("active_generation")
        and pointer.get("expected_epoch")
    )


def revoke_active_pointer_snapshot(
    runtime_root: Path,
    *,
    workspace_id: str,
    ontology_id: str,
    mutation_revision: int,
    ontology_version: str | None,
    enabled: bool,
) -> bool:
    """Fail closed an existing pointer without opening catalog/index SQLite.

    A workspace that has never enabled evidence retrieval has no pointer and
    remains free of evidence-index files.  Mutation coordination still records
    its lightweight revision/outbox transaction separately.
    """
    _validate_identifier(ontology_id, "ontology_id")
    path = runtime_root.resolve() / "ontology-evidence" / "ontologies" / ontology_id / "active.json"
    with exclusive_file_lock(_pointer_lock_path(runtime_root, ontology_id)):
        if not path.is_file():
            return False
        pointer = read_active_pointer_snapshot(runtime_root, ontology_id)
        if pointer is None:
            return False
        pointer.update(
            {
                "workspace_id": workspace_id,
                "ontology_id": ontology_id,
                "ontology_version": ontology_version,
                "mutation_revision": int(mutation_revision),
                "enabled": bool(enabled),
                "active_generation": None,
                "target_generation": None,
                "epoch_status": "pending" if enabled and ontology_version else "disabled",
                "status": "epoch_pending" if enabled and ontology_version else "disabled",
                "base_status": "pending" if enabled and ontology_version else "disabled",
                "updated_at": _utc_now(),
            }
        )
        _atomic_json_write(path, pointer)
        return True


def mark_active_pointer_state(
    runtime_root: Path,
    *,
    workspace_id: str,
    ontology_id: str,
    mutation_revision: int,
    status: str,
) -> bool:
    """Persist a non-building state for the current authoritative revision."""

    if status not in {"manual_required", "disabled"}:
        raise EvidenceIndexManagerError("INVALID_ARGUMENTS", "unsupported pointer state")
    evidence_root = runtime_root.resolve() / "ontology-evidence"
    ontology_root = evidence_root / "ontologies" / ontology_id
    with exclusive_file_lock(_pointer_lock_path(runtime_root, ontology_id)):
        pointer = read_active_pointer_snapshot(runtime_root, ontology_id)
        if pointer is None:
            return False
        if (
            str(pointer.get("workspace_id") or "") != workspace_id
            or int(pointer.get("mutation_revision") or -1) != int(mutation_revision)
        ):
            return False
        current_status = str(pointer.get("status") or "") or None
        if status not in _ALLOWED_BUILD_TRANSITIONS.get(current_status, frozenset()):
            return False
        pointer.update(
            {
                "status": status,
                "base_status": "manual_required" if status == "manual_required" else "disabled",
                "active_generation": None,
                "updated_at": _utc_now(),
            }
        )
        _atomic_json_write(ontology_root / "active.json", pointer)
        return True


def _validate_identifier(value: str, field_name: str) -> None:
    if not value or Path(value).name != value or any(char in value for char in ("/", "\\", "\x00")):
        raise EvidenceIndexManagerError("INVALID_ARGUMENTS", f"invalid {field_name}")


def _pointer_lock_path(runtime_root: Path, ontology_id: str) -> Path:
    digest = hashlib.sha256(str(ontology_id).encode("utf-8")).hexdigest()
    return runtime_root.resolve() / "ontology-pointer-locks" / f"{digest}.lock"


def _validate_pointer_scope(
    pointer: Mapping[str, Any] | None,
    *,
    workspace_id: str,
    ontology_id: str,
    mutation_revision: int,
) -> None:
    if not pointer:
        return
    if str(pointer.get("workspace_id") or workspace_id) != workspace_id:
        raise EvidenceIndexManagerError(
            "EVIDENCE_SCOPE_MISMATCH",
            "evidence pointer does not belong to workspace",
        )
    if str(pointer.get("ontology_id") or "") != ontology_id:
        raise EvidenceIndexManagerError(
            "EVIDENCE_SCOPE_MISMATCH",
            "evidence pointer does not belong to ontology",
        )
    pointer_revision = int(
        pointer.get("mutation_revision")
        if pointer.get("mutation_revision") is not None
        else -1
    )
    if pointer_revision > int(mutation_revision):
        raise EvidenceIndexManagerError(
            "INDEX_STALE",
            "a newer ontology mutation superseded this evidence build",
        )


def _validate_digest(value: str, field_name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise EvidenceIndexManagerError("INDEX_POINTER_INVALID", f"invalid {field_name}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_source_capacity(sources: Any) -> None:
    if len(sources.sources) > MAX_SOURCE_FILES:
        raise EvidenceIndexManagerError(
            "INDEX_CAPACITY_EXCEEDED",
            "ontology evidence source file count exceeds the supported limit",
        )
    total_bytes = sum(len(source.source_text.encode("utf-8")) for source in sources.sources)
    if total_bytes > MAX_SOURCE_BYTES:
        raise EvidenceIndexManagerError(
            "INDEX_CAPACITY_EXCEEDED",
            "ontology evidence source bytes exceed the supported limit",
        )


def _build_plan(
    *,
    ontology_id: str,
    ontology_version: str,
    manifest: Mapping[str, Any],
    sources: Any,
) -> dict[str, Any]:
    manifest_digest = build_semantic_manifest_digest(manifest)
    file_set_digest = build_file_set_digest(sources.file_digests)
    epoch = build_ontology_epoch(
        ontology_id=ontology_id,
        active_version=ontology_version,
        semantic_manifest_digest=manifest_digest,
        file_set_digest=file_set_digest,
    )
    generation = build_index_generation(ontology_epoch=epoch, parser_version=PARSER_VERSION)
    return {
        "ontology_id": ontology_id,
        "ontology_version": ontology_version,
        "ontology_epoch": epoch,
        "index_generation": generation,
        "semantic_manifest_digest": manifest_digest,
        "file_set_digest": file_set_digest,
        "file_count": len(sources.sources),
        "source_bytes": sum(len(source.source_text.encode("utf-8")) for source in sources.sources),
        "warnings": [warning.__dict__ for warning in sources.warnings],
    }


def _read_generation_epoch(generation_dir: Path) -> str | None:
    manifest_path = generation_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(payload.get("ontology_epoch") or "") if isinstance(payload, dict) else ""
    return value or None


def _generation_is_valid(
    generation_dir: Path,
    *,
    workspace_id: str,
    ontology_id: str,
    ontology_epoch: str,
    index_generation: str,
) -> bool:
    manifest_path = generation_dir / "manifest.json"
    index_path = generation_dir / "index.sqlite3"
    if not manifest_path.is_file() or not index_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata = SQLiteEvidenceIndex(index_path).metadata()
    except (OSError, json.JSONDecodeError, EvidenceIndexError, sqlite3.Error):
        return False
    return bool(
        isinstance(manifest, dict)
        and manifest.get("contract") == "OntologyEvidenceIndexManifest/v1"
        and str(manifest.get("index_schema_version") or "") == INDEX_SCHEMA_VERSION
        and str(manifest.get("workspace_id") or "") == workspace_id
        and str(manifest.get("ontology_id") or "") == ontology_id
        and str(manifest.get("ontology_epoch") or "") == ontology_epoch
        and str(manifest.get("index_generation") or "") == index_generation
        and str(metadata.get("index_schema_version") or "") == INDEX_SCHEMA_VERSION
        and str(metadata.get("workspace_id") or "") == workspace_id
        and str(metadata.get("ontology_id") or "") == ontology_id
        and str(metadata.get("ontology_epoch") or "") == ontology_epoch
        and str(metadata.get("index_generation") or "") == index_generation
    )


def recover_interrupted_builds(
    runtime_root: Path,
    *,
    grace_seconds: int = 600,
) -> dict[str, int]:
    """Remove old staging directories; durable jobs are reclaimed separately."""
    evidence_root = runtime_root.resolve() / "ontology-evidence"
    ontologies_root = evidence_root / "ontologies"
    result = {"scanned": 0, "removed": 0}
    if not ontologies_root.is_dir():
        return result
    cutoff = datetime.now(timezone.utc).timestamp() - max(60, int(grace_seconds))
    for ontology_root in ontologies_root.iterdir():
        tmp_root = ontology_root / "tmp"
        if not tmp_root.is_dir():
            continue
        for staging in tmp_root.iterdir():
            if not staging.is_dir():
                continue
            result["scanned"] += 1
            try:
                modified_at = staging.stat().st_mtime
            except OSError:
                continue
            if modified_at > cutoff:
                continue
            shutil.rmtree(staging, ignore_errors=True)
            if not staging.exists():
                result["removed"] += 1
        try:
            tmp_root.rmdir()
        except OSError:
            pass
    return result
