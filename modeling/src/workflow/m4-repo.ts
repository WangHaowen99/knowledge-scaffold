import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';

// M4 优化建议数据层：失败诊断 → 有证据的优化建议 → 专家采纳 → 应用修订。
export type SuggestionKind =
  | 'add_operation' | 'modify_operation'
  | 'add_object' | 'modify_object'
  | 'add_rule' | 'modify_rule'
  | 'add_metric' | 'modify_metric'
  | 'add_script' | 'modify_script'
  | 'clarify_description';
export type SuggestionStatus = 'pending' | 'candidate' | 'applied' | 'rejected';

export const SUGGESTION_KINDS: readonly SuggestionKind[] = [
  'add_operation', 'modify_operation',
  'add_object', 'modify_object',
  'add_rule', 'modify_rule',
  'add_metric', 'modify_metric',
  'add_script', 'modify_script',
  'clarify_description',
];

export interface OptSuggestion {
  id: string; projectId: string; roundId: string; basedVersion: string; basedRevisionOid: string;
  title: string; detail: string; kind: SuggestionKind;
  target?: string; relatedQuestion?: string;
  diagnosisIds?: string[]; evidence?: string[];
  generalizationPlan?: string; validationPlan?: string;
  status: SuggestionStatus; candidateRevisionId?: string; candidateFeedback?: string;
  appliedVersion?: string; appliedRevision?: string; createdAt: string;
}

const rowToSug = (r: any): OptSuggestion => ({
  id: r.id, projectId: r.project_id, roundId: r.round_id,
  basedVersion: r.based_version, basedRevisionOid: r.based_revision_oid,
  title: r.title, detail: r.detail, kind: r.kind,
  target: r.target ?? undefined, relatedQuestion: r.related_question ?? undefined,
  diagnosisIds: JSON.parse(r.diagnosis_ids_json || '[]'),
  evidence: JSON.parse(r.evidence_json || '[]'),
  generalizationPlan: r.generalization_plan ?? '',
  validationPlan: r.validation_plan ?? '',
  candidateRevisionId: r.candidate_revision_id ?? undefined,
  candidateFeedback: r.candidate_feedback ?? undefined,
  status: r.status,
  appliedVersion: r.applied_version ?? undefined,
  appliedRevision: r.applied_revision_oid ?? undefined,
  createdAt: r.created_at,
});

export function insertSuggestions(
  projectId: string,
  roundId: string,
  basedVersion: string,
  basedRevisionOid: string,
  items: {
  title: string; detail: string; kind: string; target?: string; relatedQuestion?: string;
  diagnosisIds?: string[]; evidence?: string[]; generalizationPlan?: string; validationPlan?: string;
  }[],
): OptSuggestion[] {
  const db = getDb();
  const stmt = db.prepare(`INSERT INTO opt_suggestions
    (id, project_id, round_id, based_version, based_revision_oid, title, detail, kind, target, related_question,
     diagnosis_ids_json, evidence_json, generalization_plan, validation_plan, status, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)`);
  const insertedIds: string[] = [];
  const ownsTransaction = !db.isTransaction;
  if (ownsTransaction) db.exec('BEGIN IMMEDIATE');
  try {
    for (const it of items) {
      const id = `sg_${randomUUID().slice(0, 12)}`;
      insertedIds.push(id);
      stmt.run(id, projectId, roundId, basedVersion, basedRevisionOid,
        it.title, it.detail, it.kind, it.target ?? null, it.relatedQuestion ?? null,
        JSON.stringify(it.diagnosisIds ?? []), JSON.stringify(it.evidence ?? []),
        it.generalizationPlan ?? '', it.validationPlan ?? '', nowIso());
    }
    if (ownsTransaction) db.exec('COMMIT');
  } catch (error) {
    if (ownsTransaction && db.isTransaction) db.exec('ROLLBACK');
    throw error;
  }
  return getSuggestions(insertedIds);
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

export function updatePendingSuggestion(input: {
  projectId: string;
  suggestionId: string;
  title: string;
  detail: string;
  kind: SuggestionKind;
  target?: string;
  relatedQuestion?: string;
  diagnosisIds: string[];
  evidence?: string[];
  generalizationPlan?: string;
  validationPlan?: string;
}): OptSuggestion {
  const result = getDb().prepare(`UPDATE opt_suggestions SET
      title = ?, detail = ?, kind = ?, target = ?, related_question = ?, diagnosis_ids_json = ?,
      evidence_json = ?, generalization_plan = ?, validation_plan = ?
    WHERE id = ? AND project_id = ? AND status = 'pending'`).run(
    input.title, input.detail, input.kind, input.target || null, input.relatedQuestion || null,
    JSON.stringify(input.diagnosisIds), JSON.stringify(input.evidence ?? []),
    input.generalizationPlan ?? '', input.validationPlan ?? '',
    input.suggestionId, input.projectId,
  );
  if (Number(result.changes) !== 1) throw new Error('优化建议不存在或已不再允许编辑');
  return getSuggestions([input.suggestionId])[0];
}

export function listCandidateSuggestions(
  projectId: string,
  candidateRevisionId: string,
): OptSuggestion[] {
  const rows = getDb().prepare(`SELECT * FROM opt_suggestions
    WHERE project_id = ? AND candidate_revision_id = ? ORDER BY created_at`)
    .all(projectId, candidateRevisionId);
  return rows.map(rowToSug);
}

export function markCandidateRevision(projectId: string, ids: string[], candidateRevisionId: string): void {
  const uniqueIds = [...new Set(ids)];
  if (!uniqueIds.length || !candidateRevisionId.trim()) throw new Error('候选修订关联信息不完整');
  const db = getDb();
  const stmt = db.prepare(`UPDATE opt_suggestions
    SET status = 'candidate', candidate_revision_id = ?
    WHERE id = ? AND project_id = ? AND status = 'pending'`);
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const id of uniqueIds) {
      const result = stmt.run(candidateRevisionId, id, projectId);
      if (Number(result.changes) !== 1) throw new Error(`优化建议 ${id} 已不再等待生成候选修订`);
    }
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

export function markCandidateRevisionApplied(
  projectId: string,
  candidateRevisionId: string,
  version: string,
  revisionOid: string,
): number {
  const result = getDb().prepare(`UPDATE opt_suggestions
    SET status = 'applied', applied_version = ?, applied_revision_oid = ?
    WHERE candidate_revision_id = ? AND project_id = ? AND status = 'candidate'`)
    .run(version, revisionOid, candidateRevisionId, projectId);
  return Number(result.changes);
}

export function returnCandidateRevision(projectId: string, candidateRevisionId: string, feedback: string): number {
  const db = getDb();
  const rows = db.prepare(`SELECT id, candidate_feedback FROM opt_suggestions
    WHERE candidate_revision_id = ? AND project_id = ? AND status = 'candidate'`)
    .all(candidateRevisionId, projectId) as { id: string; candidate_feedback?: string | null }[];
  const update = db.prepare(`UPDATE opt_suggestions
    SET status = 'pending', candidate_feedback = ?
    WHERE id = ? AND project_id = ? AND status = 'candidate'`);
  db.exec('BEGIN IMMEDIATE');
  try {
    let changed = 0;
    for (const row of rows) {
      const previous = row.candidate_feedback?.trim() ?? '';
      const next = feedback.trim();
      const entries = previous ? previous.split('\n\n---\n').map((item) => item.trim()) : [];
      const accumulated = !next || entries.includes(next)
        ? previous
        : [...entries, next].filter(Boolean).join('\n\n---\n');
      changed += Number(update.run(accumulated, row.id, projectId).changes);
    }
    db.exec('COMMIT');
    return changed;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

export function rejectCandidateRevision(projectId: string, candidateRevisionId: string): number {
  const result = getDb().prepare(`UPDATE opt_suggestions SET status = 'rejected'
    WHERE candidate_revision_id = ? AND project_id = ? AND status IN ('pending', 'candidate')`)
    .run(candidateRevisionId, projectId);
  return Number(result.changes);
}

export function markRejected(ids: string[], projectId: string): void {
  // project_id 条件防跨项目 id 注入（IDOR）
  const stmt = getDb().prepare("UPDATE opt_suggestions SET status = 'rejected' WHERE id = ? AND project_id = ? AND status = 'pending'");
  for (const id of ids) stmt.run(id, projectId);
}

export function appendPendingSuggestionFeedback(projectId: string, ids: string[], feedback: string): number {
  const normalized = feedback.trim();
  if (!normalized) return 0;
  const db = getDb();
  const rows = ids.map((id) => db.prepare(`SELECT id, candidate_feedback FROM opt_suggestions
    WHERE id = ? AND project_id = ? AND status = 'pending'`).get(id, projectId) as {
      id: string; candidate_feedback?: string | null;
    } | undefined).filter(Boolean) as { id: string; candidate_feedback?: string | null }[];
  const update = db.prepare(`UPDATE opt_suggestions SET candidate_feedback = ?
    WHERE id = ? AND project_id = ? AND status = 'pending'`);
  db.exec('BEGIN IMMEDIATE');
  try {
    let changed = 0;
    for (const row of rows) {
      const previous = row.candidate_feedback?.trim() ?? '';
      const entries = previous ? previous.split('\n\n---\n').map((item) => item.trim()) : [];
      const next = entries.includes(normalized)
        ? previous
        : [...entries, normalized].filter(Boolean).join('\n\n---\n');
      changed += Number(update.run(next, row.id, projectId).changes);
    }
    db.exec('COMMIT');
    return changed;
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
}

export function rejectOpenSuggestionsForRound(projectId: string, roundId: string): number {
  const result = getDb().prepare(`UPDATE opt_suggestions
    SET status = 'rejected'
    WHERE project_id = ? AND round_id = ? AND status IN ('pending', 'candidate')`).run(projectId, roundId);
  return Number(result.changes);
}
