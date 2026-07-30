import { createHash, randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type { DataDictTable, DocumentRecord } from '../types.js';

export type MaterialChangeKind = 'added' | 'replaced' | 'deleted' | 'format_only';

export interface MaterialRevisionChange {
  kind: MaterialChangeKind;
  semanticChanged: boolean;
  sourceKey: string;
  previous?: DocumentRecord;
  current?: DocumentRecord;
}

function rowToDoc(r: any): DocumentRecord {
  return {
    id: r.id, projectId: r.project_id, sourceKey: r.source_key || r.id,
    revisionNo: Number(r.revision_no ?? 1), filename: r.filename, kind: r.kind,
    parseStatus: r.parse_status, parseError: r.parse_error ?? undefined,
    textContent: r.text_content ?? undefined,
    tables: r.tables_json ? JSON.parse(r.tables_json) : undefined,
    contentHash: r.content_hash || undefined,
    semanticHash: r.semantic_hash || undefined,
    lifecycleStatus: r.lifecycle_status ?? 'active',
    supersedesId: r.supersedes_id ?? undefined,
    createdAt: r.created_at,
    retiredAt: r.retired_at ?? undefined,
  };
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]));
  }
  return value;
}

function normalizedText(value: string): string {
  return value.normalize('NFKC')
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .map((line) => line.trim().replace(/[\t ]+/g, ' '))
    .filter(Boolean)
    .join('\n')
    .trim();
}

function digest(value: string | Buffer): string {
  return createHash('sha256').update(value).digest('hex');
}

export function materialSemanticHash(input: {
  kind: DocumentRecord['kind'];
  parseStatus: DocumentRecord['parseStatus'];
  parseError?: string;
  textContent?: string;
  tables?: DataDictTable[];
}): string {
  if (input.parseStatus === 'failed') {
    return digest(JSON.stringify({ status: 'failed', error: normalizedText(input.parseError ?? '') }));
  }
  if (input.tables) return digest(JSON.stringify(stableValue(input.tables)));
  return digest(normalizedText(input.textContent ?? ''));
}

function materialContentHash(input: {
  rawContent?: Buffer;
  textContent?: string;
  tables?: DataDictTable[];
  parseError?: string;
}): string {
  if (input.rawContent) return digest(input.rawContent);
  return digest(JSON.stringify(stableValue({
    textContent: input.textContent ?? '',
    tables: input.tables ?? null,
    parseError: input.parseError ?? '',
  })));
}

function normalizedFilename(value: string): string {
  return value.normalize('NFKC').trim().toLocaleLowerCase('zh-CN');
}

export class DocumentRevisionRepo {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  insert(input: {
    projectId: string; filename: string; kind: DocumentRecord['kind'];
    parseStatus: DocumentRecord['parseStatus']; parseError?: string;
    textContent?: string; tables?: DataDictTable[]; rawContent?: Buffer;
    sourceKey?: string;
  }): { document: DocumentRecord; change: MaterialRevisionChange } {
    const filename = input.filename.trim();
    if (!filename) throw new Error('资料文件名不能为空');
    const activeRows = this.db.prepare(`SELECT * FROM documents
      WHERE project_id = ? AND lifecycle_status = 'active' ORDER BY created_at DESC`).all(input.projectId) as any[];
    const previousRow = input.sourceKey
      ? activeRows.find((row) => row.source_key === input.sourceKey)
      : activeRows.find((row) => normalizedFilename(row.filename) === normalizedFilename(filename));
    const previous = previousRow ? this.ensureHashes(rowToDoc(previousRow)) : undefined;
    const semanticHash = materialSemanticHash(input);
    const contentHash = materialContentHash(input);
    const formatOnly = Boolean(previous && previous.semanticHash === semanticHash && previous.contentHash !== contentHash);
    const sourceKey = previous?.sourceKey
      ?? (input.sourceKey?.trim() || `material_${randomUUID().slice(0, 12)}`);
    const revisionNo = previous ? Number(previous.revisionNo ?? 1) + 1 : 1;
    const id = `doc_${randomUUID().slice(0, 12)}`;
    const now = nowIso();
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      if (previous) {
        this.db.prepare(`UPDATE documents SET lifecycle_status = 'superseded', retired_at = ?
          WHERE id = ? AND lifecycle_status = 'active'`).run(now, previous.id);
      }
      this.db.prepare(`INSERT INTO documents
        (id, project_id, source_key, revision_no, filename, kind, parse_status, parse_error,
          text_content, tables_json, content_hash, semantic_hash, lifecycle_status,
          supersedes_id, created_at, retired_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?, ?, NULL)`)
        .run(
          id, input.projectId, sourceKey, revisionNo, filename, input.kind, input.parseStatus,
          input.parseError ?? null, input.textContent ?? null,
          input.tables ? JSON.stringify(input.tables) : null,
          contentHash, semanticHash, previous?.id ?? null, now,
        );
      if (ownsTransaction) this.db.exec('COMMIT');
    } catch (error) {
      if (ownsTransaction) {
        try { this.db.exec('ROLLBACK'); } catch { /* no-op */ }
      }
      throw error;
    }
    const document = this.get(id)!;
    return {
      document,
      change: {
        kind: previous ? (formatOnly ? 'format_only' : 'replaced') : 'added',
        semanticChanged: !previous || previous.semanticHash !== semanticHash,
        sourceKey,
        ...(previous ? { previous } : {}),
        current: document,
      },
    };
  }

  list(projectId: string): DocumentRecord[] {
    const rows = this.db.prepare(`SELECT * FROM documents
      WHERE project_id = ? AND lifecycle_status = 'active' ORDER BY created_at`).all(projectId);
    return (rows as any[]).map(rowToDoc);
  }

  history(projectId: string, sourceKey?: string): DocumentRecord[] {
    const rows = sourceKey
      ? this.db.prepare(`SELECT * FROM documents WHERE project_id = ? AND source_key = ?
          ORDER BY revision_no DESC`).all(projectId, sourceKey)
      : this.db.prepare(`SELECT * FROM documents WHERE project_id = ?
          ORDER BY source_key, revision_no DESC`).all(projectId);
    return (rows as any[]).map(rowToDoc);
  }

  get(id: string): DocumentRecord | null {
    const row = this.db.prepare('SELECT * FROM documents WHERE id = ?').get(id);
    return row ? rowToDoc(row as any) : null;
  }

  retire(id: string): MaterialRevisionChange {
    const previous = this.get(id);
    if (!previous || previous.lifecycleStatus !== 'active') throw new Error('资料修订不存在或已经停用');
    const now = nowIso();
    this.db.prepare(`UPDATE documents SET lifecycle_status = 'deleted', retired_at = ?
      WHERE id = ? AND lifecycle_status = 'active'`).run(now, id);
    return {
      kind: 'deleted', semanticChanged: true,
      sourceKey: previous.sourceKey ?? previous.id,
      previous: { ...previous, lifecycleStatus: 'deleted', retiredAt: now },
    };
  }

  private ensureHashes(document: DocumentRecord): DocumentRecord {
    const semanticHash = document.semanticHash || materialSemanticHash(document);
    const contentHash = document.contentHash || materialContentHash(document);
    if (!document.semanticHash || !document.contentHash) {
      this.db.prepare('UPDATE documents SET semantic_hash = ?, content_hash = ? WHERE id = ?')
        .run(semanticHash, contentHash, document.id);
    }
    return { ...document, semanticHash, contentHash };
  }
}

const repo = () => new DocumentRevisionRepo();

export function insertDocument(input: Parameters<DocumentRevisionRepo['insert']>[0]): DocumentRecord {
  return repo().insert(input).document;
}

export function insertDocumentRevision(input: Parameters<DocumentRevisionRepo['insert']>[0]) {
  return repo().insert(input);
}

export function listDocuments(projectId: string): DocumentRecord[] {
  return repo().list(projectId);
}

export function listDocumentRevisions(projectId: string, sourceKey?: string): DocumentRecord[] {
  return repo().history(projectId, sourceKey);
}

export function getDocument(id: string): DocumentRecord | null {
  return repo().get(id);
}

export function deleteDocument(id: string): MaterialRevisionChange {
  return repo().retire(id);
}
