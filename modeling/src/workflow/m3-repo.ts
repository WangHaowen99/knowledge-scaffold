import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { EvaluationExecutionTrace } from '../evaluation/trace-client.js';

const uid = (p: string) => `${p}_${randomUUID().slice(0, 12)}`;

export interface Testcase {
  id: string; projectId: string; question: string; groundTruth: string;
  source: 'manual' | 'competency'; createdAt: string;
}
export interface EvalRound {
  id: string; projectId: string; packageVersion: string;
  artifactRevision?: string;
  status: 'running' | 'done' | 'failed';
  phase: 'workspace' | 'database' | 'package' | 'data_source' | 'cases' | 'done' | 'failed';
  phaseStartedAt?: string; currentItemStartedAt?: string;
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
  artifactRevision: r.artifact_revision_oid ?? undefined,
  phase: r.phase ?? (r.status === 'running' ? 'workspace' : r.status),
  phaseStartedAt: r.phase_started_at ?? undefined,
  currentItemStartedAt: r.current_item_started_at ?? undefined,
  progressDone: r.progress_done, progressTotal: r.progress_total, passCount: r.pass_count,
  log: r.log, createdAt: r.created_at, finishedAt: r.finished_at ?? undefined,
});

export function createRound(
  projectId: string,
  packageVersion: string,
  total: number,
  artifactRevision?: string,
): EvalRound {
  const id = uid('rd');
  const createdAt = nowIso();
  getDb().prepare(`INSERT INTO eval_rounds
    (id, project_id, package_version, status, progress_done, progress_total, pass_count,
      phase, phase_started_at, log, artifact_revision_oid, created_at)
    VALUES (?,?,?,'running',0,?,0,'workspace',?,'',?,?)`)
    .run(id, projectId, packageVersion, total, createdAt, artifactRevision ?? null, createdAt);
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
export function roundPhase(id: string, phase: EvalRound['phase']): void {
  getDb().prepare(`UPDATE eval_rounds
    SET phase = ?, phase_started_at = ?, current_item_started_at = NULL WHERE id = ?`)
    .run(phase, nowIso(), id);
}
export function roundCaseStarted(id: string): void {
  getDb().prepare(`UPDATE eval_rounds
    SET phase = 'cases', current_item_started_at = ? WHERE id = ?`).run(nowIso(), id);
}
export function finishRound(id: string, status: 'done' | 'failed'): void {
  const finishedAt = nowIso();
  getDb().prepare(`UPDATE eval_rounds SET status = ?, phase = ?, phase_started_at = ?,
    current_item_started_at = NULL, finished_at = ? WHERE id = ?`)
    .run(status, status, finishedAt, finishedAt, id);
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
  trace?: EvaluationExecutionTrace;
}): EvalResult {
  const id = uid('er');
  getDb().prepare(`INSERT INTO eval_results (id, round_id, testcase_id, question, ground_truth, final_answer, verdict, reason, overridden, created_at)
    VALUES (?,?,?,?,?,?,?,?,0,?)`)
    .run(id, roundId, input.testcaseId, input.question, input.groundTruth,
      input.finalAnswer ?? null, input.verdict ?? null, input.reason ?? null, nowIso());
  if (input.trace) {
    getDb().prepare(`INSERT INTO eval_result_traces
      (result_id, run_id, session_id, tool_calls_json, tool_failures_json, trace_status, trace_error)
      VALUES (?,?,?,?,?,?,?)`).run(
      id,
      input.trace.runId ?? null,
      input.trace.sessionId ?? null,
      JSON.stringify(input.trace.toolCalls ?? []),
      JSON.stringify(input.trace.toolFailures ?? []),
      input.trace.status ?? 'complete',
      input.trace.error ?? null,
    );
  }
  return rowToResult(getDb().prepare('SELECT * FROM eval_results WHERE id = ?').get(id));
}
export function listResults(roundId: string): EvalResult[] {
  return getDb().prepare('SELECT * FROM eval_results WHERE round_id = ? ORDER BY created_at').all(roundId).map(rowToResult);
}

function rowToTrace(row: any): EvaluationExecutionTrace {
  return {
    runId: row.run_id ?? undefined,
    sessionId: row.session_id ?? undefined,
    status: row.trace_status ?? 'complete',
    ...(row.trace_error ? { error: row.trace_error } : {}),
    toolCalls: JSON.parse(row.tool_calls_json || '[]'),
    toolFailures: JSON.parse(row.tool_failures_json || '[]'),
  };
}

export function getResultTrace(resultId: string): EvaluationExecutionTrace | undefined {
  const row = getDb().prepare('SELECT * FROM eval_result_traces WHERE result_id = ?').get(resultId);
  return row ? rowToTrace(row) : undefined;
}

export function listResultTraces(roundId: string): Record<string, EvaluationExecutionTrace> {
  const rows = getDb().prepare(`SELECT trace.* FROM eval_result_traces trace
    JOIN eval_results result ON result.id = trace.result_id
    WHERE result.round_id = ?`).all(roundId) as any[];
  return Object.fromEntries(rows.map((row) => [row.result_id, rowToTrace(row)]));
}

export function getRoundDetails(roundId: string): {
  round: EvalRound;
  results: EvalResult[];
  traces: Record<string, EvaluationExecutionTrace>;
} | null {
  const round = getRound(roundId);
  if (!round) return null;
  return { round, results: listResults(round.id), traces: listResultTraces(round.id) };
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
