"""Workspace-local, idempotent Runtime Fact persistence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from ontology.file_lock import exclusive_file_lock


class RuntimeFactsStoreError(RuntimeError):
    pass


class RuntimeFactsStore:
    def __init__(self, path: Path, *, timeout_ms: int = 5000) -> None:
        self.path = path.resolve()
        self.evidence_root = self.path.parent
        self.retention_lock_path = self.evidence_root / "retention.lock"
        self.timeout_ms = max(1, int(timeout_ms))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def put(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return self.put_many([command])[0]

    def put_many(self, commands: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Persist a bounded batch in one transaction while preserving per-call idempotency."""
        if not commands:
            return []
        if len(commands) > 100:
            raise RuntimeFactsStoreError("Runtime Fact batch exceeds 100 commands")
        with exclusive_file_lock(self.retention_lock_path):
            return self._put_many_locked(commands)

    def _put_many_locked(
        self,
        commands: list[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        keys: list[tuple[str, str]] = []
        evidence_ids: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for command in commands:
                workspace_id = _required(command, "workspace_id")
                tool_call_id = _required(command, "tool_call_id")
                keys.append((workspace_id, tool_call_id))
                existing = connection.execute(
                    "SELECT evidence_id FROM runtime_facts WHERE workspace_id=? AND tool_call_id=?",
                    (workspace_id, tool_call_id),
                ).fetchone()
                if existing is not None:
                    evidence_ids.append(str(existing[0]))
                    continue

                now = str(command.get("created_at") or _utc_now())
                evidence_id = "ev_" + uuid.uuid4().hex
                evidence_ids.append(evidence_id)
                derived_from = _string_list(command.get("derived_from"))
                provider_evidence = _object_list(command.get("provider_evidence"))
                grounding_warnings = _string_list(command.get("grounding_warnings"))
                connection.execute(
                    """
                    INSERT INTO runtime_facts (
                      evidence_id, workspace_id, run_id, tool_call_id, kind, ontology_id,
                      ontology_epoch, subject, status, summary, provider, query_digest,
                      row_count, grounding_status, grounding_warnings_json,
                      provider_evidence_json, validity_class, read_at, created_at,
                      expires_at, payload_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        evidence_id,
                        workspace_id,
                        _required(command, "run_id"),
                        tool_call_id,
                        _required(command, "kind"),
                        _optional(command, "ontology_id"),
                        _optional(command, "ontology_epoch"),
                        _optional(command, "subject"),
                        _required(command, "status"),
                        _required(command, "summary")[:1000],
                        _optional(command, "provider"),
                        _optional(command, "query_digest"),
                        _optional_int(command.get("row_count")),
                        _required(command, "grounding_status"),
                        _json(grounding_warnings),
                        _json(provider_evidence),
                        _required(command, "validity_class"),
                        _optional(command, "read_at"),
                        now,
                        _optional(command, "expires_at"),
                    ),
                )
                for ordinal, derived_evidence_id in enumerate(derived_from):
                    connection.execute(
                        """
                        INSERT INTO runtime_fact_links (
                          fact_evidence_id, derived_evidence_id, derived_kind, ordinal, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            derived_evidence_id,
                            "ontology_evidence" if derived_evidence_id.startswith("ont_ev_") else "runtime_fact",
                            ordinal,
                            now,
                        ),
                    )
            connection.commit()
            records: list[dict[str, Any]] = []
            for (workspace_id, _tool_call_id), evidence_id in zip(keys, evidence_ids):
                record = self._lookup_with_connection(connection, workspace_id, evidence_id)
                if record is None:
                    raise RuntimeFactsStoreError("Runtime Fact write could not be read back")
                records.append(record)
            return records

    def lookup(self, *, workspace_id: str, evidence_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            return self._lookup_with_connection(connection, workspace_id, evidence_id)

    def exists(self, *, workspace_id: str, evidence_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM runtime_facts WHERE workspace_id=? AND evidence_id=?",
                (workspace_id, evidence_id),
            ).fetchone()
            return row is not None

    def cleanup(
        self,
        *,
        workspace_id: str,
        ontology_id: Optional[str] = None,
        dry_run: bool = True,
        older_than_days: int = 30,
        max_items: int = 1000,
    ) -> dict[str, Any]:
        if dry_run:
            return self._cleanup_locked(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                dry_run=True,
                older_than_days=older_than_days,
                max_items=max_items,
            )
        with exclusive_file_lock(self.retention_lock_path):
            return self._cleanup_locked(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                dry_run=False,
                older_than_days=older_than_days,
                max_items=max_items,
            )

    def _cleanup_locked(
        self,
        *,
        workspace_id: str,
        ontology_id: Optional[str],
        dry_run: bool,
        older_than_days: int,
        max_items: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=max(1, int(older_than_days)))).isoformat()
        limit = max(1, min(int(max_items), 5000))
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            filters = ["workspace_id=?"]
            parameters: list[Any] = [workspace_id]
            if ontology_id:
                filters.append("ontology_id=?")
                parameters.append(ontology_id)
            filters.append("((expires_at IS NOT NULL AND expires_at<=?) OR created_at<=?)")
            parameters.extend([now.isoformat(), cutoff, limit])
            rows = connection.execute(
                """
                SELECT evidence_id, kind, grounding_status, created_at, expires_at
                FROM runtime_facts
                WHERE {}
                ORDER BY created_at, evidence_id LIMIT ?
                """.format(
                    " AND ".join(filters)
                ),
                parameters,
            ).fetchall()
            items = [dict(row) for row in rows]
            if not dry_run and items:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "DELETE FROM runtime_facts WHERE workspace_id=? AND evidence_id=?",
                    [(workspace_id, item["evidence_id"]) for item in items],
                )
                connection.commit()
        return {
            "contract": "EvidenceCleanup/v1",
            "dry_run": bool(dry_run),
            "workspace_id": workspace_id,
            "kind": "runtime_facts",
            "summary": {
                "candidate_items": len(items),
                "deleted_items": 0 if dry_run else len(items),
            },
            "items": items,
        }

    @staticmethod
    def _lookup_with_connection(
        connection: sqlite3.Connection,
        workspace_id: str,
        evidence_id: str,
    ) -> Optional[dict[str, Any]]:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM runtime_facts WHERE workspace_id=? AND evidence_id=?",
            (workspace_id, evidence_id),
        ).fetchone()
        if row is None:
            return None
        links = connection.execute(
            """
            SELECT derived_evidence_id FROM runtime_fact_links
            WHERE fact_evidence_id=? ORDER BY ordinal, derived_evidence_id
            """,
            (evidence_id,),
        ).fetchall()
        payload = dict(row)
        return {
            "contract": "EvidenceRecord/v1",
            "evidence_id": payload["evidence_id"],
            "tool_call_id": payload["tool_call_id"],
            "persistence_status": "persisted",
            "kind": payload["kind"],
            "workspace_id": payload["workspace_id"],
            "ontology_id": payload["ontology_id"],
            "ontology_epoch": payload["ontology_epoch"],
            "subject": payload["subject"],
            "status": payload["status"],
            "summary": payload["summary"],
            "row_count": payload["row_count"],
            "grounding_status": payload["grounding_status"],
            "grounding_warnings": _loads_array(payload["grounding_warnings_json"]),
            "provenance": {
                "tool": "business_data_search" if payload["kind"] == "provider_query" else payload["kind"],
                "provider": payload["provider"],
                "query_digest": payload["query_digest"],
                "derived_from": [str(item[0]) for item in links],
                "provider_evidence": _loads_array(payload["provider_evidence_json"]),
            },
            "validity": {
                "class": payload["validity_class"],
                "read_at": payload["read_at"],
                "expires_at": payload["expires_at"],
            },
            "created_at": payload["created_at"],
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_ms / 1000.0)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout={}".format(self.timeout_ms))
        return connection


def _required(command: Mapping[str, Any], name: str) -> str:
    value = str(command.get(name) or "").strip()
    if not value:
        raise RuntimeFactsStoreError("{} is required".format(name))
    return value


def _optional(command: Mapping[str, Any], name: str) -> Optional[str]:
    value = str(command.get(name) or "").strip()
    return value or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, (list, tuple)) else []:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _object_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, (list, tuple)) else []


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads_array(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_facts (
  evidence_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ontology_id TEXT,
  ontology_epoch TEXT,
  subject TEXT,
  status TEXT NOT NULL CHECK(status IN ('success','partial','failed')),
  summary TEXT NOT NULL,
  provider TEXT,
  query_digest TEXT,
  row_count INTEGER,
  grounding_status TEXT NOT NULL CHECK(grounding_status IN ('grounded','unverified','not_applicable')),
  grounding_warnings_json TEXT NOT NULL,
  provider_evidence_json TEXT NOT NULL,
  validity_class TEXT NOT NULL CHECK(validity_class IN ('current_fact','historical_fact','execution_result')),
  read_at TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  payload_ref TEXT,
  UNIQUE(workspace_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_runtime_facts_workspace_run ON runtime_facts(workspace_id, run_id);
CREATE INDEX IF NOT EXISTS idx_runtime_facts_ontology ON runtime_facts(workspace_id, ontology_id);
CREATE INDEX IF NOT EXISTS idx_runtime_facts_grounding ON runtime_facts(workspace_id, grounding_status);
CREATE INDEX IF NOT EXISTS idx_runtime_facts_created ON runtime_facts(workspace_id, created_at);
CREATE TABLE IF NOT EXISTS runtime_fact_links (
  fact_evidence_id TEXT NOT NULL,
  derived_evidence_id TEXT NOT NULL,
  derived_kind TEXT NOT NULL CHECK(derived_kind IN ('ontology_evidence','runtime_fact')),
  ordinal INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(fact_evidence_id, derived_evidence_id),
  FOREIGN KEY(fact_evidence_id) REFERENCES runtime_facts(evidence_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runtime_fact_links_derived ON runtime_fact_links(derived_evidence_id);
"""
