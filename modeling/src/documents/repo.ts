import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { DocumentRecord } from '../types.js';

function rowToDoc(r: any): DocumentRecord {
  return {
    id: r.id, projectId: r.project_id, filename: r.filename, kind: r.kind,
    parseStatus: r.parse_status, parseError: r.parse_error ?? undefined,
    textContent: r.text_content ?? undefined,
    tables: r.tables_json ? JSON.parse(r.tables_json) : undefined,
    createdAt: r.created_at,
  };
}

export function insertDocument(input: {
  projectId: string; filename: string; kind: DocumentRecord['kind'];
  parseStatus: DocumentRecord['parseStatus']; parseError?: string;
  textContent?: string; tables?: unknown;
}): DocumentRecord {
  const db = getDb();
  const id = `doc_${randomUUID().slice(0, 12)}`;
  db.prepare(`INSERT INTO documents (id, project_id, filename, kind, parse_status, parse_error, text_content, tables_json, created_at)
    VALUES (?,?,?,?,?,?,?,?,?)`)
    .run(id, input.projectId, input.filename, input.kind, input.parseStatus,
      input.parseError ?? null, input.textContent ?? null,
      input.tables ? JSON.stringify(input.tables) : null, nowIso());
  return rowToDoc(getDb().prepare('SELECT * FROM documents WHERE id = ?').get(id));
}

export function listDocuments(projectId: string): DocumentRecord[] {
  const rows = getDb().prepare('SELECT * FROM documents WHERE project_id = ? ORDER BY created_at').all(projectId);
  return rows.map(rowToDoc);
}
