import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';

const uid = (p: string) => `${p}_${randomUUID().slice(0, 12)}`;

export interface Testcase {
  id: string; projectId: string; question: string; groundTruth: string;
  source: 'manual' | 'competency'; createdAt: string;
}
export interface EvalRound {
  id: string; projectId: string; packageVersion: string;
  status: 'running' | 'done' | 'failed';
  progressDone: number; progressTotal: number; passCount: number;
  log: string; createdAt: string; finishedAt?: string;
}
export interface EvalResult {
  id: string; roundId: string; testcaseId: string;
  question: string; groundTruth: string; finalAnswer?: string;
  verdict?: 'pass' | 'partial' | 'fail' | 'error';
  reason?: string; overridden: boolean; createdAt: string;
}

// ---- 用例 ----
const rowToTc = (r: any): Testcase => ({
  id: r.id, projectId: r.project_id, question: r.question, groundTruth: r.ground_truth,
  source: r.source, createdAt: r.created_at,
});

export function listTestcases(projectId: string): Testcase[] {
  return getDb().prepare('SELECT * FROM testcases WHERE project_id = ? ORDER BY created_at').all(projectId).map(rowToTc);
}
export function addTestcase(projectId: string, question: string, groundTruth = '', source: 'manual' | 'competency' = 'manual'): Testcase {
  const id = uid('tc');
  getDb().prepare('INSERT INTO testcases (id, project_id, question, ground_truth, source, created_at) VALUES (?,?,?,?,?,?)')
    .run(id, projectId, question, groundTruth, source, nowIso());
  return rowToTc(getDb().prepare('SELECT * FROM testcases WHERE id = ?').get(id));
}
export function updateTestcase(id: string, patch: { question?: string; groundTruth?: string }): void {
  const cur = getDb().prepare('SELECT * FROM testcases WHERE id = ?').get(id) as any;
  if (!cur) return;
  getDb().prepare('UPDATE testcases SET question = ?, ground_truth = ? WHERE id = ?')
    .run(patch.question ?? cur.question, patch.groundTruth ?? cur.ground_truth, id);
}
export function deleteTestcase(id: string): void {
  getDb().prepare('DELETE FROM testcases WHERE id = ?').run(id);
}
export function prefillFromCompetency(projectId: string, questions: string[]): number {
  const existing = new Set(listTestcases(projectId).map((t) => t.question));
  let added = 0;
  for (const q of questions) {
    if (existing.has(q)) continue;
    addTestcase(projectId, q, '', 'competency');
    added++;
  }
  return added;
}

// ---- 轮次 / 结果 ----
const rowToRound = (r: any): EvalRound => ({
  id: r.id, projectId: r.project_id, packageVersion: r.package_version, status: r.status,
  progressDone: r.progress_done, progressTotal: r.progress_total, passCount: r.pass_count,
  log: r.log, createdAt: r.created_at, finishedAt: r.finished_at ?? undefined,
});

export function createRound(projectId: string, packageVersion: string, total: number): EvalRound {
  const id = uid('rd');
  getDb().prepare(`INSERT INTO eval_rounds (id, project_id, package_version, status, progress_done, progress_total, pass_count, log, created_at)
    VALUES (?,?,?,'running',0,?,0,'',?)`).run(id, projectId, packageVersion, total, nowIso());
  return getRound(id)!;
}
export function getRound(id: string): EvalRound | null {
  const r = getDb().prepare('SELECT * FROM eval_rounds WHERE id = ?').get(id);
  return r ? rowToRound(r as any) : null;
}
export function listRounds(projectId: string): EvalRound[] {
  return getDb().prepare('SELECT * FROM eval_rounds WHERE project_id = ? ORDER BY created_at DESC').all(projectId).map(rowToRound);
}
export function roundLog(id: string, line: string): void {
  getDb().prepare("UPDATE eval_rounds SET log = log || ? WHERE id = ?").run(`${line}\n`, id);
}
export function roundProgress(id: string, done: number, passCount: number): void {
  getDb().prepare('UPDATE eval_rounds SET progress_done = ?, pass_count = ? WHERE id = ?').run(done, passCount, id);
}
export function finishRound(id: string, status: 'done' | 'failed'): void {
  getDb().prepare('UPDATE eval_rounds SET status = ?, finished_at = ? WHERE id = ?').run(status, nowIso(), id);
}

const rowToResult = (r: any): EvalResult => ({
  id: r.id, roundId: r.round_id, testcaseId: r.testcase_id, question: r.question,
  groundTruth: r.ground_truth, finalAnswer: r.final_answer ?? undefined,
  verdict: r.verdict ?? undefined, reason: r.reason ?? undefined,
  overridden: !!r.overridden, createdAt: r.created_at,
});

export function addResult(roundId: string, input: {
  testcaseId: string; question: string; groundTruth: string;
  finalAnswer?: string; verdict?: string; reason?: string;
}): EvalResult {
  const id = uid('er');
  getDb().prepare(`INSERT INTO eval_results (id, round_id, testcase_id, question, ground_truth, final_answer, verdict, reason, overridden, created_at)
    VALUES (?,?,?,?,?,?,?,?,0,?)`)
    .run(id, roundId, input.testcaseId, input.question, input.groundTruth,
      input.finalAnswer ?? null, input.verdict ?? null, input.reason ?? null, nowIso());
  return rowToResult(getDb().prepare('SELECT * FROM eval_results WHERE id = ?').get(id));
}
export function listResults(roundId: string): EvalResult[] {
  return getDb().prepare('SELECT * FROM eval_results WHERE round_id = ? ORDER BY created_at').all(roundId).map(rowToResult);
}
export function overrideVerdict(resultId: string, verdict: 'pass' | 'partial' | 'fail'): EvalResult | null {
  getDb().prepare('UPDATE eval_results SET verdict = ?, overridden = 1 WHERE id = ?').run(verdict, resultId);
  const r = getDb().prepare('SELECT * FROM eval_results WHERE id = ?').get(resultId);
  if (!r) return null;
  // 同步轮次通过数
  const res = rowToResult(r as any);
  const round = getDb().prepare("SELECT COUNT(*) AS c FROM eval_results WHERE round_id = ? AND verdict = 'pass'").get(res.roundId) as { c: number };
  getDb().prepare('UPDATE eval_rounds SET pass_count = ? WHERE id = ?').run(round.c, res.roundId);
  return res;
}
