import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { EvaluationExecutionTrace } from '../evaluation/trace-client.js';

const uid = (p: string) => `${p}_${randomUUID().slice(0, 12)}`;

export interface Testcase {
  id: string; projectId: string; question: string; groundTruth: string;
  source: 'manual' | 'competency' | 'package'; createdAt: string;
}
export interface EvalRound {
  id: string; projectId: string; packageVersion: string;
  artifactRevision?: string;
  status: 'running' | 'done' | 'failed';
  phase: 'workspace' | 'database' | 'package' | 'data_source' | 'cases' | 'done' | 'failed';
  phaseStartedAt?: string; currentItemStartedAt?: string;
  progressDone: number; progressTotal: number; passCount: number;
  log: string; createdAt: string; finishedAt?: string;
  scope: EvaluationScope;
}
export type EvaluationScope = { kind: 'current' } | {
  kind: 'fusion_source';
  sourceId: string;
  sourceName: string;
  fingerprint: string;
};
export interface EvalResult {
  id: string; roundId: string; testcaseId: string;
  question: string; groundTruth: string; finalAnswer?: string;
  verdict?: 'pass' | 'partial' | 'fail' | 'error';
  reason?: string; overridden: boolean;
  originalVerdict?: 'pass' | 'partial' | 'fail' | 'error';
  overrideReason?: string; overrideActor?: string; overriddenAt?: string;
  createdAt: string;
}

// ---- 用例 ----
const rowToTc = (r: any): Testcase => ({
  id: r.id, projectId: r.project_id, question: r.question, groundTruth: r.ground_truth,
  source: r.source, createdAt: r.created_at,
});

export function listTestcases(projectId: string): Testcase[] {
  return getDb().prepare('SELECT * FROM testcases WHERE project_id = ? ORDER BY created_at').all(projectId).map(rowToTc);
}
export function addTestcase(
  projectId: string,
  question: string,
  groundTruth = '',
  source: 'manual' | 'competency' | 'package' = 'manual',
): Testcase {
  const id = uid('tc');
  getDb().prepare('INSERT INTO testcases (id, project_id, question, ground_truth, source, created_at) VALUES (?,?,?,?,?,?)')
    .run(id, projectId, question, groundTruth, source, nowIso());
  return rowToTc(getDb().prepare('SELECT * FROM testcases WHERE id = ?').get(id));
}
export function addTestcases(
  projectId: string,
  cases: Array<{ question: string; groundTruth?: string }>,
): { added: number; skipped: number; testcases: Testcase[] } {
  if (!Array.isArray(cases) || !cases.length) throw new Error('请提供至少一条真实评测用例');
  if (cases.length > 500) throw new Error('一次最多导入 500 条真实评测用例');
  const normalized = (question: string) => question.trim().replace(/\s+/g, ' ').toLocaleLowerCase('zh-CN');
  const existing = new Set(listTestcases(projectId).map((item) => normalized(item.question)));
  let added = 0;
  let skipped = 0;
  const db = getDb();
  db.exec('BEGIN IMMEDIATE');
  try {
    for (const [index, item] of cases.entries()) {
      const question = String(item?.question ?? '').trim();
      if (!question) throw new Error(`第 ${index + 1} 条缺少问题`);
      const key = normalized(question);
      if (existing.has(key)) {
        skipped += 1;
        continue;
      }
      addTestcase(projectId, question, String(item.groundTruth ?? '').trim(), 'manual');
      existing.add(key);
      added += 1;
    }
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  }
  return { added, skipped, testcases: listTestcases(projectId) };
}
export function testcaseBelongsToProject(id: string, projectId: string): boolean {
  return Boolean(getDb().prepare('SELECT 1 FROM testcases WHERE id = ? AND project_id = ?').get(id, projectId));
}
export function updateTestcase(
  projectId: string,
  id: string,
  patch: { question?: string; groundTruth?: string },
): Testcase | null {
  const cur = getDb().prepare('SELECT * FROM testcases WHERE id = ? AND project_id = ?').get(id, projectId) as any;
  if (!cur) return null;
  getDb().prepare('UPDATE testcases SET question = ?, ground_truth = ? WHERE id = ? AND project_id = ?')
    .run(patch.question ?? cur.question, patch.groundTruth ?? cur.ground_truth, id, projectId);
  return rowToTc(getDb().prepare('SELECT * FROM testcases WHERE id = ? AND project_id = ?').get(id, projectId));
}
export function deleteTestcase(projectId: string, id: string): boolean {
  const result = getDb().prepare('DELETE FROM testcases WHERE id = ? AND project_id = ?').run(id, projectId);
  return Number(result.changes) > 0;
}
export function prefillFromQuestions(
  projectId: string,
  questions: string[],
  source: 'competency' | 'package',
): number {
  const existing = new Set(listTestcases(projectId).map((t) => t.question));
  let added = 0;
  for (const q of questions) {
    if (existing.has(q)) continue;
    addTestcase(projectId, q, '', source);
    added++;
  }
  return added;
}

export function prefillFromPackage(projectId: string, questions: string[]): number {
  return prefillFromQuestions(projectId, questions, 'package');
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
  scope: parseEvaluationScope(r.evaluation_scope_json),
});

function parseEvaluationScope(raw: unknown): EvaluationScope {
  try {
    const parsed = JSON.parse(String(raw || '{"kind":"current"}')) as Record<string, unknown>;
    if (parsed.kind === 'fusion_source') {
      const sourceId = String(parsed.sourceId ?? '').trim();
      const sourceName = String(parsed.sourceName ?? '').trim();
      const fingerprint = String(parsed.fingerprint ?? '').trim();
      if (sourceId && sourceName && fingerprint) {
        return { kind: 'fusion_source', sourceId, sourceName, fingerprint };
      }
    }
  } catch {
    // 旧数据或损坏的可选元数据不得改变正式评测语义。
  }
  return { kind: 'current' };
}

export function createRound(
  projectId: string,
  packageVersion: string,
  total: number,
  artifactRevision?: string,
  scope: EvaluationScope = { kind: 'current' },
): EvalRound {
  const id = uid('rd');
  const createdAt = nowIso();
  getDb().prepare(`INSERT INTO eval_rounds
    (id, project_id, package_version, status, progress_done, progress_total, pass_count,
      phase, phase_started_at, log, artifact_revision_oid, evaluation_scope_json, created_at)
    VALUES (?,?,?,'running',0,?,0,'workspace',?,'',?,?,?)`)
    .run(id, projectId, packageVersion, total, createdAt, artifactRevision ?? null, JSON.stringify(scope), createdAt);
  return getRound(id)!;
}
export function getRound(id: string): EvalRound | null {
  const r = getDb().prepare('SELECT * FROM eval_rounds WHERE id = ?').get(id);
  return r ? rowToRound(r as any) : null;
}
export function listRounds(projectId: string): EvalRound[] {
  return getDb().prepare(`SELECT * FROM eval_rounds
    WHERE project_id = ? ORDER BY created_at DESC, rowid DESC`).all(projectId).map(rowToRound);
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

export function failInterruptedEvaluationRounds(reason: string): number {
  const detail = String(reason ?? '').trim() || '服务重启，本轮评测已中断';
  const finishedAt = nowIso();
  const result = getDb().prepare(`UPDATE eval_rounds
    SET status = 'failed', phase = 'failed', phase_started_at = ?,
      current_item_started_at = NULL, finished_at = ?, log = log || ?
    WHERE status = 'running'`).run(finishedAt, finishedAt, `${detail}\n`);
  return Number(result.changes);
}

const rowToResult = (r: any): EvalResult => ({
  id: r.id, roundId: r.round_id, testcaseId: r.testcase_id, question: r.question,
  groundTruth: r.ground_truth, finalAnswer: r.final_answer ?? undefined,
  verdict: r.verdict ?? undefined, reason: r.reason ?? undefined,
  overridden: !!r.overridden,
  originalVerdict: r.original_verdict ?? undefined,
  overrideReason: r.override_reason ?? undefined,
  overrideActor: r.override_actor ?? undefined,
  overriddenAt: r.overridden_at ?? undefined,
  createdAt: r.created_at,
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

export function latestEffectiveEvaluation(
  projectId: string,
  artifactRevision: string | null | undefined,
  testcaseIds?: string[],
): { latestRound: EvalRound | null; results: EvalResult[] } {
  const revision = String(artifactRevision ?? '').trim();
  if (!revision) return { latestRound: null, results: [] };
  const latestRound = listRounds(projectId).find((round) => (
    round.scope.kind === 'current' && round.artifactRevision === revision && round.status === 'done'
  )) ?? null;

  const requiredIds = testcaseIds ? new Set(testcaseIds) : null;
  const rows = getDb().prepare(`SELECT result.*
    FROM eval_results result
    JOIN eval_rounds round ON round.id = result.round_id
    WHERE round.project_id = ? AND round.artifact_revision_oid = ? AND round.status = 'done'
      AND json_extract(round.evaluation_scope_json, '$.kind') = 'current'
    ORDER BY round.created_at DESC, round.rowid DESC, result.created_at DESC, result.rowid DESC`)
    .all(projectId, revision) as any[];
  const seen = new Set<string>();
  const results: EvalResult[] = [];
  for (const row of rows) {
    const testcaseId = String(row.testcase_id);
    if (seen.has(testcaseId) || (requiredIds && !requiredIds.has(testcaseId))) continue;
    seen.add(testcaseId);
    results.push(rowToResult(row));
  }
  return { latestRound, results };
}

export function resultBelongsToProject(resultId: string, projectId: string): boolean {
  return Boolean(getDb().prepare(`SELECT 1 FROM eval_results result
    JOIN eval_rounds round ON round.id = result.round_id
    WHERE result.id = ? AND round.project_id = ?`).get(resultId, projectId));
}

function rowToTrace(row: any): EvaluationExecutionTrace {
  const toolFailures = JSON.parse(row.tool_failures_json || '[]');
  return {
    runId: row.run_id ?? undefined,
    sessionId: row.session_id ?? undefined,
    status: row.trace_status ?? 'complete',
    ...(row.trace_error ? { error: row.trace_error } : {}),
    toolCalls: JSON.parse(row.tool_calls_json || '[]'),
    ...(toolFailures.length ? { toolFailures } : {}),
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
export function overrideVerdict(
  resultId: string,
  verdict: 'pass' | 'partial' | 'fail',
  audit: { reason: string; actor: string },
): EvalResult | null {
  const current = getDb().prepare('SELECT * FROM eval_results WHERE id = ?').get(resultId) as any;
  if (!current) return null;
  const reason = String(audit?.reason ?? '').trim();
  const actor = String(audit?.actor ?? '').trim();
  if (!reason) throw new Error('请填写人工改判理由');
  if (reason.length > 1_000) throw new Error('人工改判理由不能超过 1000 字');
  if (!actor) throw new Error('人工改判操作人不能为空');
  if (current.verdict === 'error') throw new Error('运行错误不能人工改判，请先修复运行环境后重试');
  getDb().prepare(`UPDATE eval_results
    SET verdict = ?, overridden = 1,
      original_verdict = COALESCE(original_verdict, verdict),
      override_reason = ?, override_actor = ?, overridden_at = ?
    WHERE id = ?`).run(verdict, reason, actor, nowIso(), resultId);
  const r = getDb().prepare('SELECT * FROM eval_results WHERE id = ?').get(resultId);
  // 同步轮次通过数
  const res = rowToResult(r as any);
  const round = getDb().prepare("SELECT COUNT(*) AS c FROM eval_results WHERE round_id = ? AND verdict = 'pass'").get(res.roundId) as { c: number };
  getDb().prepare('UPDATE eval_rounds SET pass_count = ? WHERE id = ?').run(round.c, res.roundId);
  return res;
}
