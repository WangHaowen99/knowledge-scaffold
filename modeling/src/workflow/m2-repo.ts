import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { InterviewBatch, CompetencyQuestion, PackageSpec, CoverageResult } from '../types.js';

const uid = (p: string) => `${p}_${randomUUID().slice(0, 12)}`;

// ---- 访谈批次 ----
export function insertBatch(projectId: string, nodeId: string | undefined, questions: unknown[]): InterviewBatch {
  const db = getDb();
  const id = uid('bt');
  const r = db.prepare('SELECT MAX(ordinal) AS m FROM interview_batches WHERE project_id = ?').get(projectId) as { m: number | null };
  const ordinal = (r.m ?? -1) + 1;
  db.prepare(`INSERT INTO interview_batches (id, project_id, node_id, ordinal, questions_json, answered, created_at)
    VALUES (?,?,?,?,?,0,?)`).run(id, projectId, nodeId ?? null, ordinal, JSON.stringify(questions), nowIso());
  return getBatch(id)!;
}

function rowToBatch(r: any): InterviewBatch {
  const qs = JSON.parse(r.questions_json);
  return {
    id: r.id, projectId: r.project_id, nodeId: r.node_id ?? undefined, ordinal: r.ordinal,
    questions: qs.map((q: any, i: number) => ({ id: q.id ?? `q${i}`, kind: q.kind, q: q.q, options: q.options ?? [], topic: q.topic ?? '' })),
    answered: !!r.answered, createdAt: r.created_at,
  };
}

export function getBatch(id: string): InterviewBatch | null {
  const r = getDb().prepare('SELECT * FROM interview_batches WHERE id = ?').get(id);
  return r ? rowToBatch(r as any) : null;
}

export function listBatches(projectId: string): InterviewBatch[] {
  return getDb().prepare('SELECT * FROM interview_batches WHERE project_id = ? ORDER BY ordinal').all(projectId).map(rowToBatch);
}

export function saveAnswers(batchId: string, answers: { questionId: string; selected: string[]; other?: string; skipped?: boolean }[]): void {
  const db = getDb();
  const del = db.prepare('DELETE FROM interview_answers WHERE batch_id = ?');
  del.run(batchId); // 回溯修改：覆盖旧答案
  const ins = db.prepare('INSERT INTO interview_answers (id, batch_id, question_id, answer_json, created_at) VALUES (?,?,?,?,?)');
  for (const a of answers) {
    ins.run(uid('an'), batchId, a.questionId, JSON.stringify({ selected: a.selected, other: a.other, skipped: a.skipped }), nowIso());
  }
  db.prepare('UPDATE interview_batches SET answered = 1 WHERE id = ?').run(batchId);
}

export function getAnswers(batchId: string): { questionId: string; selected: string[]; other?: string; skipped?: boolean }[] {
  const rows = getDb().prepare('SELECT * FROM interview_answers WHERE batch_id = ?').all(batchId) as any[];
  return rows.map((r) => ({ questionId: r.question_id, ...JSON.parse(r.answer_json) }));
}

// ---- 胜任力 ----
export function replaceCompetency(projectId: string, questions: string[]): CompetencyQuestion[] {
  const db = getDb();
  db.prepare('DELETE FROM competency_questions WHERE project_id = ?').run(projectId);
  const ins = db.prepare('INSERT INTO competency_questions (id, project_id, question, selected, created_at) VALUES (?,?,?,0,?)');
  for (const q of questions) ins.run(uid('cq'), projectId, q, nowIso());
  return listCompetency(projectId);
}

function rowToCq(r: any): CompetencyQuestion {
  return { id: r.id, projectId: r.project_id, question: r.question, selected: !!r.selected, coveredOperation: r.covered_operation ?? undefined, createdAt: r.created_at };
}
export function listCompetency(projectId: string): CompetencyQuestion[] {
  return getDb().prepare('SELECT * FROM competency_questions WHERE project_id = ? ORDER BY created_at').all(projectId).map(rowToCq);
}
export function setCompetencySelection(projectId: string, selectedIds: string[]): void {
  const db = getDb();
  db.prepare('UPDATE competency_questions SET selected = 0 WHERE project_id = ?').run(projectId);
  const upd = db.prepare('UPDATE competency_questions SET selected = 1 WHERE id = ? AND project_id = ?');
  for (const id of selectedIds) upd.run(id, projectId);
}

// ---- 包版本 ----
export function insertPackageVersion(projectId: string, version: string, spec: PackageSpec, coverage: CoverageResult, note?: string): string {
  const db = getDb();
  const id = uid('pv');
  db.prepare(`INSERT INTO package_versions (id, project_id, version, spec_json, coverage_json, note, created_at)
    VALUES (?,?,?,?,?,?,?)`).run(id, projectId, version, JSON.stringify(spec), JSON.stringify(coverage), note ?? null, nowIso());
  return id;
}
export function latestPackageVersion(projectId: string): { id: string; version: string; spec: PackageSpec; coverage: CoverageResult } | null {
  const r = getDb().prepare('SELECT * FROM package_versions WHERE project_id = ? ORDER BY created_at DESC LIMIT 1').get(projectId) as any;
  if (!r) return null;
  return { id: r.id, version: r.version, spec: JSON.parse(r.spec_json), coverage: r.coverage_json ? JSON.parse(r.coverage_json) : { covered: [], uncovered: [] } };
}
export function listPackageVersions(projectId: string): { id: string; version: string; note?: string; createdAt: string }[] {
  return (getDb().prepare('SELECT id, version, note, created_at FROM package_versions WHERE project_id = ? ORDER BY created_at').all(projectId) as any[])
    .map((r) => ({ id: r.id, version: r.version, note: r.note ?? undefined, createdAt: r.created_at }));
}
export function getPackageVersion(projectId: string, version: string): { id: string; version: string; spec: PackageSpec; coverage: CoverageResult } | null {
  const r = getDb().prepare('SELECT * FROM package_versions WHERE project_id = ? AND version = ? ORDER BY created_at DESC LIMIT 1').get(projectId, version) as any;
  if (!r) return null;
  return { id: r.id, version: r.version, spec: JSON.parse(r.spec_json), coverage: r.coverage_json ? JSON.parse(r.coverage_json) : { covered: [], uncovered: [] } };
}
