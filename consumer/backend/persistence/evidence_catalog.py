# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Workspace-local routes from stable evidence IDs to immutable generations."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ontology.evidence.sqlite_index import SQLiteEvidenceIndex


class EvidenceCatalogError(RuntimeError):
    pass


class EvidenceCatalog:
    def __init__(self, path: Path, *, runtime_facts_path: Path | None = None) -> None:
        self.path = path
        self.runtime_facts_path = (
            runtime_facts_path.resolve()
            if runtime_facts_path is not None
            else path.with_name("runtime-facts.sqlite3").resolve()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def register_generation(self, *, index: SQLiteEvidenceIndex, index_relative_path: str) -> int:
        metadata = index.metadata()
        registered = 0
        with self._connect() as connection:
            for chunk in index.iter_chunks():
                canonical = (
                    metadata["ontology_id"],
                    metadata["ontology_epoch"],
                    chunk["chunk_contract_hash"],
                    chunk["chunk_hash"],
                    chunk["content_hash"],
                    chunk["resource"],
                    chunk["resource_key"],
                    chunk["section_anchor"],
                )
                existing = connection.execute(
                    """
                    SELECT ontology_id, ontology_epoch, chunk_contract_hash, chunk_hash,
                           content_hash, resource, resource_key, section_anchor
                    FROM canonical_evidence WHERE evidence_id=?
                    """,
                    (chunk["evidence_id"],),
                ).fetchone()
                if existing is not None and tuple(existing) != canonical:
                    raise EvidenceCatalogError("EVIDENCE_ID_COLLISION")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO canonical_evidence (
                      evidence_id, ontology_id, ontology_epoch, chunk_contract_hash, chunk_hash,
                      content_hash, resource, resource_key, section_anchor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chunk["evidence_id"], *canonical),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO generation_routes (
                      evidence_id, ontology_epoch, index_generation, generation_kind, index_relative_path
                    ) VALUES (?, ?, ?, 'lexical', ?)
                    """,
                    (
                        chunk["evidence_id"], metadata["ontology_epoch"],
                        metadata["index_generation"], index_relative_path,
                    ),
                )
                registered += 1
            connection.commit()
        return registered

    def lookup_routes(self, evidence_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT c.*, r.index_generation, r.generation_kind, r.index_relative_path,
                       r.retention_hold_count
                FROM canonical_evidence c JOIN generation_routes r USING(evidence_id)
                WHERE evidence_id=? ORDER BY r.route_id DESC
                """,
                (evidence_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def generation_hold_count(self, index_generation: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(retention_hold_count), 0) FROM generation_routes WHERE index_generation=?",
                (index_generation,),
            ).fetchone()
            legacy_holds = int(row[0] or 0) if row is not None else 0
            if not self.runtime_facts_path.is_file():
                return legacy_holds
            try:
                connection.execute(
                    "ATTACH DATABASE ? AS runtime_fact_store",
                    (str(self.runtime_facts_path),),
                )
                runtime_fact_holds = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM runtime_fact_store.runtime_fact_links links
                    JOIN runtime_fact_store.runtime_facts facts
                      ON facts.evidence_id=links.fact_evidence_id
                    WHERE links.derived_kind='ontology_evidence'
                      AND EXISTS (
                        SELECT 1 FROM main.generation_routes routes
                        WHERE routes.evidence_id=links.derived_evidence_id
                          AND routes.index_generation=?
                      )
                    """,
                    (index_generation,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                # A pre-feature or partially initialized runtime-facts database
                # has no live holds yet. Do not hide other catalog failures.
                if "no such table" not in str(exc).lower():
                    raise
                return legacy_holds
            return legacy_holds + int(runtime_fact_holds[0] or 0)

    def remove_generation(self, index_generation: str) -> dict[str, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            route_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM generation_routes WHERE index_generation=?",
                    (index_generation,),
                ).fetchone()[0]
            )
            connection.execute(
                "DELETE FROM generation_routes WHERE index_generation=?",
                (index_generation,),
            )
            orphan_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM canonical_evidence c
                    WHERE NOT EXISTS (
                      SELECT 1 FROM generation_routes r WHERE r.evidence_id=c.evidence_id
                    )
                    """
                ).fetchone()[0]
            )
            connection.execute(
                """
                DELETE FROM canonical_evidence
                WHERE NOT EXISTS (
                  SELECT 1 FROM generation_routes r WHERE r.evidence_id=canonical_evidence.evidence_id
                )
                """
            )
            connection.commit()
            return {"routes": route_count, "orphan_evidence": orphan_count}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection


_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_evidence (
  evidence_id TEXT PRIMARY KEY, ontology_id TEXT NOT NULL, ontology_epoch TEXT NOT NULL,
  chunk_contract_hash TEXT NOT NULL, chunk_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
  resource TEXT NOT NULL, resource_key TEXT NOT NULL, section_anchor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_routes (
  route_id INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL,
  ontology_epoch TEXT NOT NULL, index_generation TEXT NOT NULL, generation_kind TEXT NOT NULL,
  index_relative_path TEXT NOT NULL, retention_hold_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(evidence_id, index_generation),
  FOREIGN KEY(evidence_id) REFERENCES canonical_evidence(evidence_id)
);
CREATE INDEX IF NOT EXISTS idx_generation_routes_generation ON generation_routes(index_generation);
"""
