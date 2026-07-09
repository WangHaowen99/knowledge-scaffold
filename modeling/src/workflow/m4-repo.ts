import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';

// M4 优化建议数据层：Judge 未通过结果 → 优化器建议 → 专家采纳 → 应用产生新版本。
export type SuggestionKind = 'add_operation' | 'modify_operation' | 'add_object' | 'modify_object' | 'clarify_description';
export type SuggestionStatus = 'pending' | 'applied' | 'rejected';

export interface OptSuggestion {
  id: string; projectId: string; roundId: string; basedVersion: string;
  title: string; detail: string; kind: SuggestionKind;
  target?: string; relatedQuestion?: string;
  status: SuggestionStatus; appliedVersion?: string; createdAt: string;
}

const rowToSug = (r: any): OptSuggestion => ({
  id: r.id, projectId: r.project_id, roundId: r.round_id, basedVersion: r.based_version,
  title: r.title, detail: r.detail, kind: r.kind,
  target: r.target ?? undefined, relatedQuestion: r.related_question ?? undefined,
  status: r.status, appliedVersion: r.applied_version ?? undefined, createdAt: r.created_at,
});

export function insertSuggestions(projectId: string, roundId: string, basedVersion: string, items: {
  title: string; detail: string; kind: string; target?: string; relatedQuestion?: string;
}[]): OptSuggestion[] {
  const db = getDb();
  const stmt = db.prepare(`INSERT INTO opt_suggestions
    (id, project_id, round_id, based_version, title, detail, kind, target, related_question, status, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,'pending',?)`);
  for (const it of items) {
    stmt.run(`sg_${randomUUID().slice(0, 12)}`, projectId, roundId, basedVersion,
      it.title, it.detail, it.kind, it.target ?? null, it.relatedQuestion ?? null, nowIso());
  }
  return listSuggestions(projectId, roundId);
}

export function listSuggestions(projectId: string, roundId?: string): OptSuggestion[] {
  const db = getDb();
  const rows = roundId
    ? db.prepare('SELECT * FROM opt_suggestions WHERE project_id = ? AND round_id = ? ORDER BY created_at').all(projectId, roundId)
    : db.prepare('SELECT * FROM opt_suggestions WHERE project_id = ? ORDER BY created_at').all(projectId);
  return rows.map(rowToSug);
}

export function getSuggestions(ids: string[]): OptSuggestion[] {
  const db = getDb();
  return ids.map((id) => db.prepare('SELECT * FROM opt_suggestions WHERE id = ?').get(id))
    .filter(Boolean).map((r) => rowToSug(r as any));
}

export function markApplied(ids: string[], version: string): void {
  const stmt = getDb().prepare("UPDATE opt_suggestions SET status = 'applied', applied_version = ? WHERE id = ?");
  for (const id of ids) stmt.run(version, id);
}

export function markRejected(ids: string[], projectId: string): void {
  // project_id 条件防跨项目 id 注入（IDOR）
  const stmt = getDb().prepare("UPDATE opt_suggestions SET status = 'rejected' WHERE id = ? AND project_id = ? AND status = 'pending'");
  for (const id of ids) stmt.run(id, projectId);
}
