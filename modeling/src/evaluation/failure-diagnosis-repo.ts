import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { FailureDiagnosis } from './failure-diagnosis.js';

export interface StoredFailureDiagnosis extends FailureDiagnosis {
  id: string;
  projectId: string;
  roundId: string;
  basedVersion: string;
  basedRevisionOid: string;
  createdAt: string;
  resolutionStatus: 'pending' | 'confirmed' | 'resolved' | 'deferred' | 'irrelevant';
  resolutionAction?: DiagnosisStoredAction;
  resolutionNote?: string;
  resolutionActor?: string;
  resolutionPayload?: Record<string, unknown>;
  resolvedAt?: string;
}

export type DiagnosisResolutionAction =
  | 'confirm_rule'
  | 'clarify_semantics'
  | 'data_updated'
  | 'not_applicable'
  | 'defer';

export type DiagnosisStoredAction = DiagnosisResolutionAction | 'plan_repair';

function rowToDiagnosis(row: any): StoredFailureDiagnosis {
  return {
    id: row.id,
    projectId: row.project_id,
    roundId: row.round_id,
    basedVersion: row.based_version,
    basedRevisionOid: row.based_revision_oid,
    category: row.category,
    title: row.title,
    summary: row.summary,
    evidence: JSON.parse(row.evidence_json || '[]'),
    confidence: row.confidence,
    blocking: !!row.blocking,
    repairScope: row.repair_scope,
    packageOptimizable: !!row.package_optimizable,
    resultIds: JSON.parse(row.result_ids_json || '[]'),
    relatedQuestions: JSON.parse(row.related_questions_json || '[]'),
    suggestedAction: row.suggested_action,
    createdAt: row.created_at,
    resolutionStatus: row.resolution_status || 'pending',
    resolutionAction: row.resolution_action ?? undefined,
    resolutionNote: row.resolution_note ?? undefined,
    resolutionActor: row.resolution_actor ?? undefined,
    resolutionPayload: JSON.parse(row.resolution_payload_json || '{}'),
    resolvedAt: row.resolved_at ?? undefined,
  };
}

export function replaceFailureDiagnoses(
  projectId: string,
  roundId: string,
  basedVersion: string,
  basedRevisionOid: string,
  diagnoses: FailureDiagnosis[],
): StoredFailureDiagnosis[] {
  const db = getDb();
  const stmt = db.prepare(`INSERT INTO failure_diagnoses
    (id, project_id, round_id, based_version, based_revision_oid, category, title, summary, evidence_json, confidence,
     blocking, repair_scope, package_optimizable, result_ids_json, related_questions_json, suggested_action, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  db.exec('BEGIN IMMEDIATE');
  try {
    db.prepare('DELETE FROM failure_diagnoses WHERE project_id = ? AND round_id = ?').run(projectId, roundId);
    for (const diagnosis of diagnoses) {
      stmt.run(
        `dg_${randomUUID().slice(0, 12)}`,
        projectId,
        roundId,
        basedVersion,
        basedRevisionOid,
        diagnosis.category,
        diagnosis.title,
        diagnosis.summary,
        JSON.stringify(diagnosis.evidence),
        diagnosis.confidence,
        diagnosis.blocking ? 1 : 0,
        diagnosis.repairScope,
        diagnosis.packageOptimizable ? 1 : 0,
        JSON.stringify(diagnosis.resultIds),
        JSON.stringify(diagnosis.relatedQuestions),
        diagnosis.suggestedAction,
        nowIso(),
      );
    }
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
  return listFailureDiagnoses(projectId, roundId);
}

export function listFailureDiagnoses(projectId: string, roundId?: string): StoredFailureDiagnosis[] {
  const rows = roundId
    ? getDb().prepare('SELECT * FROM failure_diagnoses WHERE project_id = ? AND round_id = ? ORDER BY created_at').all(projectId, roundId)
    : getDb().prepare('SELECT * FROM failure_diagnoses WHERE project_id = ? ORDER BY created_at DESC').all(projectId);
  return (rows as any[]).map(rowToDiagnosis);
}

export function hasConfirmedOptimizableDiagnoses(
  diagnoses: readonly StoredFailureDiagnosis[],
): boolean {
  return diagnoses.some((diagnosis) => diagnosis.resolutionStatus === 'confirmed'
    && diagnosis.packageOptimizable
    && !diagnosis.blocking);
}

/**
 * 记录用户已将可修复的知识包诊断加入评测修缮任务。
 * 这是一次显式的修复范围确认；不放宽阻断条件，但会把知识包范围固化为可优化。
 */
export function markFailureDiagnosesForRepair(input: {
  projectId: string;
  diagnosisIds: string[];
  actor: string;
  note?: string;
  payload?: Record<string, unknown>;
}): StoredFailureDiagnosis[] {
  const diagnosisIds = [...new Set(input.diagnosisIds.map((id) => String(id)).filter(Boolean))];
  if (!diagnosisIds.length) return [];

  const db = getDb();
  const select = db.prepare(`SELECT * FROM failure_diagnoses
    WHERE id = ? AND project_id = ?`);
  const update = db.prepare(`UPDATE failure_diagnoses SET
      resolution_status = 'confirmed', resolution_action = ?, resolution_note = ?, resolution_actor = ?,
      resolution_payload_json = ?, resolved_at = ?, package_optimizable = 1, repair_scope = 'package'
    WHERE id = ? AND project_id = ?`);
  const note = String(input.note ?? '').trim() || '用户选择转为建模修缮任务';
  const payload = JSON.stringify(input.payload ?? {});
  const resolvedAt = nowIso();

  db.exec('BEGIN IMMEDIATE');
  try {
    for (const diagnosisId of diagnosisIds) {
      const current = select.get(diagnosisId, input.projectId) as any;
      if (!current) throw new Error('失败诊断不存在');
      if (['resolved', 'irrelevant'].includes(String(current.resolution_status))) {
        throw new Error('该失败诊断已关闭，不能再次加入建模修缮');
      }
      if (current.repair_scope !== 'package' || current.blocking) {
        throw new Error('只有不阻断的知识包缺陷才能进入优化更新');
      }
      update.run(
        'plan_repair', note, input.actor, payload, resolvedAt,
        diagnosisId, input.projectId,
      );
    }
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }

  return diagnosisIds.map((diagnosisId) => rowToDiagnosis(
    db.prepare('SELECT * FROM failure_diagnoses WHERE id = ? AND project_id = ?')
      .get(diagnosisId, input.projectId),
  ));
}

export function resolveFailureDiagnosis(input: {
  projectId: string;
  diagnosisId: string;
  action: DiagnosisResolutionAction;
  note: string;
  actor: string;
  payload?: Record<string, unknown>;
}): StoredFailureDiagnosis {
  const db = getDb();
  const current = db.prepare(`SELECT * FROM failure_diagnoses
    WHERE id = ? AND project_id = ?`).get(input.diagnosisId, input.projectId) as any;
  if (!current) throw new Error('失败诊断不存在');
  const status = input.action === 'data_updated'
    ? 'resolved'
    : input.action === 'not_applicable'
      ? 'irrelevant'
      : input.action === 'defer'
        ? 'deferred'
        : 'confirmed';
  const enablesPackageRepair = ['confirm_rule', 'clarify_semantics'].includes(input.action);
  const nextSuggestedAction = enablesPackageRepair
    ? `${current.suggested_action}\n专家确认：${input.note}`.trim()
    : current.suggested_action;
  db.prepare(`UPDATE failure_diagnoses SET
      resolution_status = ?, resolution_action = ?, resolution_note = ?, resolution_actor = ?,
      resolution_payload_json = ?, resolved_at = ?,
      blocking = ?, package_optimizable = ?, repair_scope = ?, suggested_action = ?
    WHERE id = ? AND project_id = ?`).run(
    status, input.action, input.note.trim(), input.actor,
    JSON.stringify(input.payload ?? {}), nowIso(),
    enablesPackageRepair ? 0 : (input.action === 'data_updated' || input.action === 'not_applicable' || input.action === 'defer' ? 0 : current.blocking),
    enablesPackageRepair ? 1 : 0,
    enablesPackageRepair ? 'package' : current.repair_scope,
    nextSuggestedAction,
    input.diagnosisId, input.projectId,
  );
  return rowToDiagnosis(db.prepare('SELECT * FROM failure_diagnoses WHERE id = ?').get(input.diagnosisId));
}
