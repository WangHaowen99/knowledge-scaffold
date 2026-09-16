# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Immutable SQLite FTS5 backend for ontology evidence generations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from ontology.evidence.chunker import EvidenceChunk
from ontology.evidence.identity import INDEX_SCHEMA_VERSION
from ontology.evidence.lexical import fts_query, lexical_tokens, normalize_query


SCHEMA_VERSION = INDEX_SCHEMA_VERSION


class EvidenceIndexError(RuntimeError):
    pass


class SQLiteEvidenceIndex:
    @staticmethod
    def build(path: Path, *, metadata: Mapping[str, Any], chunks: Iterable[EvidenceChunk]) -> dict[str, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise EvidenceIndexError("immutable evidence index already exists")
        connection = sqlite3.connect(path)
        try:
            connection.executescript(_SCHEMA)
            _insert_metadata(connection, metadata)
            chunk_count = 0
            edge_count = 0
            for chunk in chunks:
                rowid = _insert_chunk(connection, chunk)
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, lexical_terms) VALUES (?, ?)",
                    (rowid, _chunk_lexical_terms(chunk)),
                )
                edge_count += _insert_edges(connection, chunk)
                chunk_count += 1
            connection.execute("UPDATE metadata SET chunk_count=?, edge_count=? WHERE id=1", (chunk_count, edge_count))
            connection.commit()
            connection.execute("PRAGMA optimize")
            return {"chunks": chunk_count, "edges": edge_count}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise EvidenceIndexError("evidence index not found")
        self.path = path

    def metadata(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM metadata WHERE id=1").fetchone()
            if row is None:
                raise EvidenceIndexError("evidence metadata missing")
            return dict(row)

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        normalized = normalize_query(query)
        if not normalized:
            return []
        candidates: dict[int, dict[str, Any]] = {}
        with self._connect() as connection:
            exact_rows = connection.execute(
                """
                SELECT *, 0.0 AS lexical_rank FROM chunks
                WHERE resource_key=? OR canonical_key=? OR title=?
                  OR EXISTS (
                    SELECT 1 FROM json_each(chunks.aliases_json) AS aliases
                    WHERE aliases.value=?
                  )
                ORDER BY resource, resource_key, section_anchor, ordinal LIMIT ?
                """,
                (query.strip(), query.strip(), query.strip(), query.strip(), limit),
            ).fetchall()
            for rank, row in enumerate(exact_rows, start=1):
                item = dict(row)
                item["exact_rank"] = rank
                candidates[int(row["chunk_rowid"])] = item

            expression = fts_query(lexical_tokens(query))
            if expression:
                lexical_rows = connection.execute(
                    """
                    SELECT chunks.*, bm25(chunks_fts) AS lexical_rank
                    FROM chunks_fts JOIN chunks ON chunks.chunk_rowid=chunks_fts.rowid
                    WHERE chunks_fts MATCH ? ORDER BY lexical_rank, chunks.chunk_rowid LIMIT ?
                    """,
                    (expression, limit),
                ).fetchall()
                for rank, row in enumerate(lexical_rows, start=1):
                    item = candidates.setdefault(int(row["chunk_rowid"]), dict(row))
                    item["lexical_position"] = rank
        return sorted(candidates.values(), key=_candidate_sort_key)[:limit]

    def lookup(self, evidence_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chunks WHERE evidence_id=?", (evidence_id,)).fetchone()
            return dict(row) if row is not None else None

    def find_chunks(self, *, resource: str, key: str, section_anchor: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM chunks WHERE resource=? AND resource_key=?"
        params: list[Any] = [resource, key]
        if section_anchor:
            sql += " AND section_anchor=?"
            params.append(section_anchor)
        sql += " ORDER BY section_anchor, chunk_type, ordinal"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def iter_chunks(self) -> Iterable[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chunks ORDER BY chunk_rowid"
            ).fetchall()
            for row in rows:
                yield dict(row)

    def linked_candidates(self, resource: str, key: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT target.* FROM edges
                JOIN chunks AS target
                  ON target.resource=edges.target_resource AND target.resource_key=edges.target_key
                WHERE edges.source_resource=? AND edges.source_key=?
                ORDER BY target.resource, target.resource_key, target.section_anchor LIMIT ?
                """,
                (resource, key, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect("file:{}?mode=ro".format(self.path.as_posix()), uri=True)
        connection.row_factory = sqlite3.Row
        return connection


def _insert_metadata(connection: sqlite3.Connection, metadata: Mapping[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO metadata (
          id, index_schema_version, workspace_id, ontology_id, ontology_version, ontology_epoch,
          index_generation, generation_kind, parser_version, chunker_version,
          lexical_analyzer_version, relation_extractor_version, file_count,
          chunk_count, edge_count, built_at, build_id
        ) VALUES (1, ?, ?, ?, ?, ?, ?, 'lexical', ?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        (
            SCHEMA_VERSION,
            metadata["workspace_id"],
            metadata["ontology_id"],
            metadata["ontology_version"],
            metadata["ontology_epoch"],
            metadata["index_generation"],
            metadata["parser_version"],
            metadata["chunker_version"],
            metadata["lexical_analyzer_version"],
            metadata["relation_extractor_version"],
            int(metadata.get("file_count") or 0),
            metadata["built_at"],
            metadata["build_id"],
        ),
    )


def _insert_chunk(connection: sqlite3.Connection, chunk: EvidenceChunk) -> int:
    cursor = connection.execute(
        """
        INSERT INTO chunks (
          evidence_id, resource, resource_key, canonical_key, title, section,
          section_anchor, chunk_type, ordinal, text, snippet, source_path,
          content_hash, source_content_hash, chunk_contract_hash, chunk_hash,
          tags_json, aliases_json, intent_examples_json, links_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.evidence_id, chunk.resource, chunk.key, chunk.canonical_key, chunk.title,
            chunk.section, chunk.section_anchor, chunk.chunk_type, chunk.ordinal, chunk.text,
            chunk.snippet, chunk.source_path, chunk.content_hash, chunk.source_content_hash,
            chunk.chunk_contract_hash, chunk.chunk_hash, _json(chunk.tags), _json(chunk.aliases),
            _json(chunk.intent_examples), _json(chunk.links),
        ),
    )
    return int(cursor.lastrowid)


def _insert_edges(connection: sqlite3.Connection, chunk: EvidenceChunk) -> int:
    count = 0
    for ordinal, link in enumerate(chunk.links):
        identity = "{}:{}:{}:{}".format(chunk.evidence_id, link["resource"], link["key"], ordinal)
        connection.execute(
            "INSERT OR IGNORE INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', ?)",
            (
                identity, chunk.resource, chunk.key, link["resource"], link["key"],
                link.get("relation") or "references", chunk.evidence_id, ordinal,
            ),
        )
        count += 1
    return count


def _chunk_lexical_terms(chunk: EvidenceChunk) -> str:
    values = [chunk.key, chunk.canonical_key, chunk.title, chunk.section, chunk.text]
    values.extend(chunk.aliases)
    values.extend(chunk.tags)
    values.extend(chunk.intent_examples)
    return " ".join(lexical_tokens(*values))


def _candidate_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    exact_rank = int(item.get("exact_rank") or 1_000_000)
    lexical_rank = int(item.get("lexical_position") or 1_000_000)
    return (min(exact_rank, lexical_rank), exact_rank, lexical_rank, item.get("evidence_id"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


_SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE metadata (
  id INTEGER PRIMARY KEY CHECK(id=1), index_schema_version TEXT NOT NULL,
  workspace_id TEXT NOT NULL, ontology_id TEXT NOT NULL,
  ontology_version TEXT NOT NULL, ontology_epoch TEXT NOT NULL,
  index_generation TEXT NOT NULL, generation_kind TEXT NOT NULL, parser_version TEXT NOT NULL,
  chunker_version TEXT NOT NULL, lexical_analyzer_version TEXT NOT NULL,
  relation_extractor_version TEXT NOT NULL, file_count INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL, edge_count INTEGER NOT NULL, built_at TEXT NOT NULL, build_id TEXT NOT NULL
);
CREATE TABLE chunks (
  chunk_rowid INTEGER PRIMARY KEY, evidence_id TEXT NOT NULL UNIQUE, resource TEXT NOT NULL,
  resource_key TEXT NOT NULL, canonical_key TEXT, title TEXT NOT NULL, section TEXT NOT NULL,
  section_anchor TEXT NOT NULL, chunk_type TEXT NOT NULL, ordinal INTEGER NOT NULL,
  text TEXT NOT NULL, snippet TEXT NOT NULL, source_path TEXT NOT NULL,
  content_hash TEXT NOT NULL, source_content_hash TEXT NOT NULL,
  chunk_contract_hash TEXT NOT NULL, chunk_hash TEXT NOT NULL,
  tags_json TEXT NOT NULL, aliases_json TEXT NOT NULL, intent_examples_json TEXT NOT NULL,
  links_json TEXT NOT NULL
);
CREATE INDEX idx_chunks_resource_key ON chunks(resource, resource_key);
CREATE INDEX idx_chunks_title ON chunks(title);
CREATE VIRTUAL TABLE chunks_fts USING fts5(lexical_terms, content='');
CREATE TABLE edges (
  edge_id TEXT PRIMARY KEY, source_resource TEXT NOT NULL, source_key TEXT NOT NULL,
  target_resource TEXT NOT NULL, target_key TEXT NOT NULL, edge_type TEXT NOT NULL,
  source_evidence_id TEXT NOT NULL, confidence TEXT NOT NULL, ordinal INTEGER NOT NULL
);
CREATE INDEX idx_edges_source ON edges(source_resource, source_key);
CREATE INDEX idx_edges_target ON edges(target_resource, target_key);
"""
