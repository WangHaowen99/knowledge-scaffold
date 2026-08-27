"""Workspace-scoped lookup for static ontology and runtime evidence IDs."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from ontology.evidence.manager import (
    EvidenceIndexManagerError,
    active_pointer_is_serviceable,
    read_active_pointer_snapshot,
)
from ontology.evidence.sqlite_index import EvidenceIndexError, SQLiteEvidenceIndex
from persistence.evidence_catalog import EvidenceCatalog
from persistence.runtime_facts import RuntimeFactsStore


_STATIC_ID_RE = re.compile(r"^ont_ev_[A-Za-z0-9_]{16,120}$")
_RUNTIME_ID_RE = re.compile(r"^ev_[A-Za-z0-9]{16,64}$")


class EvidenceResolverError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class EvidenceResolver:
    def __init__(self, workspace_runtime_dir: Path, *, workspace_id: Optional[str] = None) -> None:
        self.workspace_runtime_dir = workspace_runtime_dir.resolve()
        self.workspace_id = str(workspace_id or self.workspace_runtime_dir.parent.name).strip()
        self.evidence_root = self.workspace_runtime_dir / "ontology-evidence"

    def lookup(self, evidence_id: str) -> dict[str, Any]:
        normalized = str(evidence_id or "").strip()
        if _STATIC_ID_RE.fullmatch(normalized):
            return self._lookup_static(normalized)
        if _RUNTIME_ID_RE.fullmatch(normalized):
            return self._lookup_runtime(normalized)
        raise EvidenceResolverError("INVALID_EVIDENCE_ID", "证据 ID 格式非法。")

    def _lookup_static(self, evidence_id: str) -> dict[str, Any]:
        catalog_path = self.evidence_root / "evidence-catalog.sqlite3"
        if not catalog_path.is_file():
            raise EvidenceResolverError(
                "EVIDENCE_CATALOG_UNAVAILABLE",
                "证据目录不可用。",
                retryable=True,
            )
        catalog = EvidenceCatalog(catalog_path)
        routes = catalog.lookup_routes(evidence_id)
        if not routes:
            raise EvidenceResolverError("EVIDENCE_NOT_FOUND", "未找到该证据。")

        missing_route = False
        for route in routes:
            index_path = self.evidence_root / str(route.get("index_relative_path") or "")
            if not index_path.is_file():
                missing_route = True
                continue
            try:
                index = SQLiteEvidenceIndex(index_path)
                metadata = index.metadata()
                chunk = index.lookup(evidence_id)
            except (EvidenceIndexError, sqlite3.Error) as exc:
                raise EvidenceResolverError("EVIDENCE_STORAGE_INCONSISTENT", str(exc)) from exc
            if chunk is None:
                raise EvidenceResolverError("EVIDENCE_STORAGE_INCONSISTENT", "证据路由未命中内容。")
            _verify_static_integrity(
                route=route,
                metadata=metadata,
                chunk=chunk,
                expected_workspace_id=self.workspace_id,
            )
            active, inactive_reason = self._static_validity(route=route, metadata=metadata)
            return {
                "contract": "EvidenceLookup/v1",
                "evidence_id": evidence_id,
                "kind": "ontology_evidence",
                "ontology_id": metadata["ontology_id"],
                "ontology_version": metadata["ontology_version"],
                "ontology_epoch": metadata["ontology_epoch"],
                "index_generation": metadata["index_generation"],
                "resource": chunk["resource"],
                "key": chunk["resource_key"],
                "title": chunk["title"],
                "section": chunk["section"],
                "section_anchor": chunk["section_anchor"],
                "chunk_type": chunk["chunk_type"],
                "content": chunk["text"],
                "source": {
                    "path": chunk["source_path"],
                    "content_hash": "sha256:" + str(chunk["content_hash"]).removeprefix("sha256:"),
                    "source_content_sha256": "sha256:" + str(chunk["source_content_hash"]).removeprefix("sha256:"),
                },
                "validity": {
                    "class": "ontology_evidence",
                    "active": active,
                    "inactive_reason": inactive_reason,
                },
            }
        if missing_route:
            raise EvidenceResolverError("EVIDENCE_STORAGE_INCONSISTENT", "证据路由文件缺失。")
        raise EvidenceResolverError("EVIDENCE_NOT_FOUND", "未找到该证据。")

    def _lookup_runtime(self, evidence_id: str) -> dict[str, Any]:
        path = self.evidence_root / "runtime-facts.sqlite3"
        if not path.is_file():
            raise EvidenceResolverError("EVIDENCE_NOT_FOUND", "未找到该证据。")
        store = RuntimeFactsStore(path)
        record = store.lookup(workspace_id=self.workspace_id, evidence_id=evidence_id)
        if record is None:
            raise EvidenceResolverError("EVIDENCE_NOT_FOUND", "未找到该证据。")
        return {
            "contract": "EvidenceLookup/v1",
            "evidence_id": record["evidence_id"],
            "kind": record["kind"],
            "status": record["status"],
            "summary": record["summary"],
            "row_count": record.get("row_count"),
            "grounding_status": record["grounding_status"],
            "grounding_warnings": record["grounding_warnings"],
            "ontology_id": record.get("ontology_id"),
            "ontology_epoch": record.get("ontology_epoch"),
            "subject": record.get("subject"),
            "provenance": record["provenance"],
            "validity": record["validity"],
            "created_at": record["created_at"],
            "redacted_fields": [],
        }

    def _static_validity(self, *, route: dict[str, Any], metadata: dict[str, Any]) -> tuple[bool, Optional[str]]:
        try:
            pointer = read_active_pointer_snapshot(
                self.workspace_runtime_dir,
                str(metadata["ontology_id"]),
            )
        except EvidenceIndexManagerError:
            return False, "pointer_unavailable"
        if not pointer:
            return False, "ontology_inactive"
        if str(pointer.get("workspace_id") or "") != self.workspace_id:
            return False, "workspace_mismatch"
        if not active_pointer_is_serviceable(pointer):
            return False, "index_{}".format(str(pointer.get("status") or "inactive"))
        if (
            str(pointer.get("expected_epoch") or "") != str(route.get("ontology_epoch") or "")
            or str(pointer.get("active_generation") or "") != str(route.get("index_generation") or "")
        ):
            return False, "superseded_generation"
        return True, None


def _verify_static_integrity(
    *,
    route: dict[str, Any],
    metadata: dict[str, Any],
    chunk: dict[str, Any],
    expected_workspace_id: str,
) -> None:
    checks = (
        (expected_workspace_id, str(metadata.get("workspace_id") or "")),
        (str(route.get("ontology_id") or ""), str(metadata.get("ontology_id") or "")),
        (str(route.get("ontology_epoch") or ""), str(metadata.get("ontology_epoch") or "")),
        (str(route.get("index_generation") or ""), str(metadata.get("index_generation") or "")),
        (str(route.get("chunk_contract_hash") or ""), str(chunk.get("chunk_contract_hash") or "")),
        (str(route.get("chunk_hash") or ""), str(chunk.get("chunk_hash") or "")),
        (str(route.get("content_hash") or ""), str(chunk.get("content_hash") or "")),
        (str(route.get("resource") or ""), str(chunk.get("resource") or "")),
        (str(route.get("resource_key") or ""), str(chunk.get("resource_key") or "")),
        (str(route.get("section_anchor") or ""), str(chunk.get("section_anchor") or "")),
    )
    if any(expected != actual for expected, actual in checks):
        raise EvidenceResolverError("EVIDENCE_INTEGRITY_FAILED", "证据目录与不可变索引不一致。")
