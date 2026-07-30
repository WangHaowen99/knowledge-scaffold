import { randomUUID } from 'node:crypto';
import { consumerFetch } from '../consumer/client.js';
import { latestPackageVersion } from '../workflow/m2-repo.js';
import { renderPackage } from '../workflow/package-render.js';
import { zipPackage } from '../workflow/export.js';
import { judge, type Verdict } from './judge.js';
import * as repo from '../workflow/m3-repo.js';
import { getProjectDataSource, type ProjectDataSource } from './data-source-repo.js';
import { prepareProjectDataSourceForEvaluation } from './data-source-evaluation-service.js';
import { getRunningEvaluationDatabaseImportRun } from './eval-db-import-repo.js';
import { importPackageZip } from './data-source-consumer-sync.js';
import {
  ensureEvaluationWorkspace,
  evaluationWorkspaceId,
  shadowWorkspaceId,
} from './shadow-workspace.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import {
  fetchEvaluationRunTrace,
  traceFromSyncResponse,
  type EvaluationExecutionTrace,
} from './trace-client.js';
import { validateSpecIntegrity } from '../workflow/spec-validation.js';
import { EvaluationAuthSession } from './auth-session.js';
import type { Testcase } from '../workflow/m3-repo.js';

// 评测引擎（M3 核心）：评测工作空间 → 导入包 → 复用本体级数据源 →
// 逐用例 runs/sync（串行 + 退避）→ Judge → 落轮次结果。
// 后台执行：startEvaluation 立即返回 roundId，前端轮询 GET round。
export { shadowWorkspaceId } from './shadow-workspace.js';

export function evaluationFinalAnswer(data: any): string {
  const pending = data?.pending_user_interaction;
  if (data?.status === 'awaiting_input' || (pending && typeof pending === 'object')) {
    const prompt = String(pending?.prompt ?? '').trim();
    throw new Error(`评测用例缺少运行所需输入${prompt ? `：${prompt}` : ''}`);
  }
  const status = String(data?.status ?? '').trim().toLowerCase();
  if (['error', 'failed', 'cancelled'].includes(status)) {
    const detail = String(data?.error ?? data?.detail ?? data?.final_answer ?? '').trim();
    throw new Error(detail || `评测服务返回 ${status} 状态`);
  }
  if (typeof data?.final_answer !== 'string') {
    throw new Error('评测服务返回了非文本答案');
  }
  if (!data.final_answer.trim()) {
    throw new Error('评测服务未返回有效回答');
  }
  return data.final_answer;
}

export function selectOfficialEvaluationTestcases(testcases: Testcase[]): Testcase[] {
  const official = testcases.filter((testcase) => testcase.source === 'manual');
  if (!official.length) {
    throw new Error('请先录入用户真实场景用例，知识包派生问题不作为正式评测用例');
  }
  return official;
}

export async function judgeEvaluationAnswer(
  question: string,
  groundTruth: string,
  finalAnswer: string,
  judgeAnswer: typeof judge = judge,
): Promise<Verdict> {
  if (!groundTruth.trim()) {
    return { verdict: 'partial', reason: '缺少参考答案，等待人工判定' };
  }
  return judgeAnswer(question, groundTruth, finalAnswer);
}

export async function activateEvaluationPackageVersion(input: {
  token: string;
  workspaceId: string;
  ontologyId: string;
  version: string;
  request?: typeof consumerFetch;
}): Promise<void> {
  const request = input.request ?? consumerFetch;
  let response: Response;
  try {
    response = await request(
      input.token,
      `/agentic/ontologies/${encodeURIComponent(input.ontologyId)}/active-version`,
      {
        method: 'PUT',
        workspaceId: input.workspaceId,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: input.version }),
      },
    );
  } catch (error) {
    throw new Error(`激活评测目标版本失败：${error instanceof Error ? error.message : error}`);
  }
  const data = (await response.json().catch(() => ({}))) as any;
  if (!response.ok) {
    throw new Error(`激活评测目标版本失败（${response.status}）：${data.error ?? data.detail ?? '未知错误'}`);
  }
  const activeVersion = String(data.active_version ?? '').trim();
  if (activeVersion !== input.version) {
    throw new Error(`评测工作空间激活版本不一致：期望 ${input.version}，实际 ${activeVersion || '未返回'}`);
  }
}

interface RunOptions {
  projectId: string;
  owner?: string;
  token: string; // 专家 token 透传
  refreshToken?: string; // 仅保留在评测任务内存中，用于长任务续期
  packageSnapshot?: {
    version: string;
    spec: any;
    files: Record<string, string>;
    revisionOid: string;
  };
}

const activeProjects = new Set<string>();
const activeWorkspaces = new Set<string>();
const activeEvaluations = new Map<string, { roundId: string; controller: AbortController }>();

function assertEvaluationNotCancelled(signal: AbortSignal): void {
  if (signal.aborted) throw new Error('本轮评测已由用户停止');
}

async function waitForEvaluationRelease(projectId: string): Promise<void> {
  for (let attempt = 0; attempt < 100 && activeProjects.has(projectId); attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

export async function cancelEvaluation(projectId: string, roundId?: string): Promise<repo.EvalRound> {
  const active = activeEvaluations.get(projectId);
  if (!active || (roundId && active.roundId !== roundId)) {
    throw new Error('该项目当前没有可停止的评测任务');
  }
  const round = repo.getRound(active.roundId);
  if (!round || round.status !== 'running') throw new Error('该轮评测已经结束');
  repo.roundLog(active.roundId, '用户从评测页面停止本轮；已保留当前完成结果，可重新启动正式评测');
  repo.finishRound(active.roundId, 'failed');
  active.controller.abort(new Error('本轮评测已由用户停止'));
  await waitForEvaluationRelease(projectId);
  return repo.getRound(active.roundId)!;
}

export function isEvaluationActive(projectId: string): boolean {
  return activeProjects.has(projectId);
}

export function evaluationWorkspaceForRun(
  projectId: string,
  dataSource: ProjectDataSource,
  ontologyId: string,
): string {
  if (dataSource.mode !== 'connection') return dataSource.workspaceId;
  if (!dataSource.ontologyId) {
    throw new Error('数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存');
  }
  if (dataSource.ontologyId !== ontologyId) {
    throw new Error('本体数据源与当前知识包不匹配，请返回“数据源配置”重新保存');
  }
  const expectedWorkspaceId = evaluationWorkspaceId(projectId, dataSource);
  if (dataSource.workspaceId !== expectedWorkspaceId) {
    throw new Error('数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存');
  }
  return expectedWorkspaceId;
}

export function startEvaluation(opts: RunOptions): { roundId: string } {
  const { projectId } = opts;
  if (activeProjects.has(projectId)) throw new Error('该项目已有评测在运行');
  const latest = opts.packageSnapshot ?? latestPackageVersion(projectId);
  if (!latest) throw new Error('尚未生成知识包');
  // 评测必须能够记录存量脚本的执行契约失败，而不是在轮次创建前整体拦截。
  // 安全检查仍然强制执行；候选接受、导出和发布继续使用严格契约。
  validateSpecIntegrity(latest.spec, [], undefined, { validateScriptExecutionContract: false });
  const testcases = selectOfficialEvaluationTestcases(repo.listTestcases(projectId));
  const dataSource = getProjectDataSource(projectId);
  if (!dataSource) throw new Error('尚未配置数据源，请先完成“数据源配置”步骤');
  const runningDatabaseImport = getRunningEvaluationDatabaseImportRun(projectId);
  if (runningDatabaseImport) {
    throw new Error(`评测库数据仍在后台导入（${runningDatabaseImport.filename}），请等待导入完成后再运行评测`);
  }
  const workspaceId = evaluationWorkspaceForRun(projectId, dataSource, latest.spec.id);
  if (activeWorkspaces.has(workspaceId)) {
    throw new Error(`评测工作空间 ${workspaceId} 已有评测在运行`);
  }

  const round = repo.createRound(
    projectId,
    latest.version,
    testcases.length,
    'revisionOid' in latest ? latest.revisionOid : undefined,
  );
  activeProjects.add(projectId);
  activeWorkspaces.add(workspaceId);
  const controller = new AbortController();
  activeEvaluations.set(projectId, { roundId: round.id, controller });
  // 后台执行（单进程内异步任务；进度与日志写库供轮询）
  void runRound(round.id, opts, latest, testcases, dataSource, workspaceId, controller.signal)
    .catch((e) => {
      if (controller.signal.aborted) return;
      repo.roundLog(round.id, `轮次失败：${e instanceof Error ? e.message : e}`);
      repo.finishRound(round.id, 'failed');
    })
    .finally(() => {
      if (activeEvaluations.get(projectId)?.roundId === round.id) activeEvaluations.delete(projectId);
      activeProjects.delete(projectId);
      activeWorkspaces.delete(workspaceId);
    });
  return { roundId: round.id };
}

export async function startCurrentEvaluation(opts: Omit<RunOptions, 'packageSnapshot'>): Promise<{ roundId: string }> {
  const snapshot = await getPackageArtifactStore().readPackageFiles(opts.projectId);
  return startEvaluation({
    ...opts,
    packageSnapshot: {
      version: snapshot.spec.version,
      spec: snapshot.spec,
      files: snapshot.files,
      revisionOid: snapshot.revision.oid,
    },
  });
}

async function runRound(
  roundId: string,
  opts: RunOptions,
  latest: { version: string; spec: any; files?: Record<string, string>; revisionOid?: string },
  testcases: { id: string; question: string; groundTruth: string }[],
  dataSource: ProjectDataSource,
  workspaceId: string,
  signal: AbortSignal,
): Promise<void> {
  const { projectId, owner } = opts;
  const auth = new EvaluationAuthSession({ accessToken: opts.token, refreshToken: opts.refreshToken });
  const ws = workspaceId;
  const log = (m: string) => repo.roundLog(roundId, m);
  assertEvaluationNotCancelled(signal);

  // 1. 评测工作空间
  const workspace = await ensureEvaluationWorkspace({
    projectId, workspaceId: ws, owner, token: await auth.getAccessToken(),
  });
  assertEvaluationNotCancelled(signal);
  if (workspace.created) {
    log(`评测工作空间已创建：${ws}`);
  } else {
    log(`复用评测工作空间：${ws}`);
  }

  // 2. 评测数据已经由独立导入任务准备；这里只记录启动前门禁已通过。
  repo.roundPhase(roundId, 'database');
  log('评测数据环境启动门禁已通过');

  // 3. 导入知识包（已存在则覆盖激活版本；人工修订的文件以修订稿为准）
  repo.roundPhase(roundId, 'package');
  const files = latest.files ? { ...latest.files } : renderPackage(latest.spec);
  const zip = zipPackage(files);
  const importResult = await importPackageZip({
    token: await auth.getAccessToken(),
    workspaceId: ws,
    ontologyId: latest.spec.id,
    zip,
  });
  assertEvaluationNotCancelled(signal);
  log(`知识包已导入评测工作空间：${importResult.ontology_id}（对象 ${importResult.objects_count ?? '-'} / 操作 ${importResult.operations_count ?? '-'}）`);
  // 导入 ≠ 生效：consumer 可能仍激活旧版本（实测吃过亏）。显式激活本次版本并确认。
  await activateEvaluationPackageVersion({
    token: await auth.getAccessToken(),
    workspaceId: ws,
    ontologyId: importResult.ontology_id,
    version: latest.spec.version,
  });
  log(`已激活并确认版本：${latest.spec.version}`);

  // 4. 复用“数据源配置”步骤保存的本体级数据源，不在评测运行时传递密钥
  repo.roundPhase(roundId, 'data_source');
  await prepareProjectDataSourceForEvaluation({
    token: await auth.getAccessToken(), ontologyId: importResult.ontology_id, dataSource,
  });
  assertEvaluationNotCancelled(signal);
  if (dataSource.mode === 'connection') {
    log(`已复用本体数据源：${dataSource.ontologyId}（${dataSource.provider}）`);
  } else {
    log('已按数据源配置清除本体数据源（判断类问题仍可评测）');
  }

  // 5. 逐用例串行运行 + judge
  repo.roundPhase(roundId, 'cases');
  let done = 0;
  let pass = 0;
  for (const tc of testcases) {
    assertEvaluationNotCancelled(signal);
    repo.roundCaseStarted(roundId);
    let finalAnswer = '';
    let trace: EvaluationExecutionTrace | undefined;
    let verdict: 'pass' | 'partial' | 'fail' | 'error' = 'error';
    let reason = '';
    try {
      const run = await runCase(auth, ws, tc.question, signal);
      finalAnswer = run.finalAnswer;
      trace = run.trace;
      if (trace.status === 'unavailable') {
        log(`⚠ [${done + 1}/${testcases.length}] Trace 不可用：${trace.error ?? '未知错误'}`);
      }
      const v = await judgeEvaluationAnswer(tc.question, tc.groundTruth, finalAnswer);
      verdict = v.verdict;
      reason = v.reason;
      repo.addResult(roundId, {
        testcaseId: tc.id, question: tc.question, groundTruth: tc.groundTruth,
        finalAnswer, verdict, reason, trace,
      });
    } catch (e) {
      reason = `运行失败：${e instanceof Error ? e.message : e}`;
      repo.addResult(roundId, { testcaseId: tc.id, question: tc.question, groundTruth: tc.groundTruth, finalAnswer, verdict, reason, trace });
    }
    done++;
    if (verdict === 'pass') pass++;
    repo.roundProgress(roundId, done, pass);
    log(`[${done}/${testcases.length}] ${verdict} · ${tc.question.slice(0, 40)}`);
  }

  repo.finishRound(roundId, 'done');
  log(`轮次完成：通过 ${pass}/${testcases.length}`);
}

// runs/sync：429/503 退避重试
async function runCase(
  auth: EvaluationAuthSession,
  ws: string,
  question: string,
  signal: AbortSignal,
): Promise<{ finalAnswer: string; trace: EvaluationExecutionTrace }> {
  const sessionId = `mw-eval-${randomUUID().slice(0, 8)}`;
  let rateLimitAttempts = 0;
  let authRetried = false;
  while (rateLimitAttempts < 4) {
    assertEvaluationNotCancelled(signal);
    const res = await consumerFetch(await auth.getAccessToken(), '/agentic/runs/sync', {
      method: 'POST', workspaceId: ws, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: question, session_id: sessionId, include_trace: true }),
      signal,
    });
    if (res.status === 401 && !authRetried) {
      await auth.refreshAfterUnauthorized();
      authRetried = true;
      continue;
    }
    if (res.status === 429 || res.status === 503) {
      rateLimitAttempts++;
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, rateLimitAttempts * 3000);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          reject(new Error('本轮评测已由用户停止'));
        }, { once: true });
      });
      continue;
    }
    const data = (await res.json().catch(() => ({}))) as any;
    if (!res.ok) throw new Error(data.error || data.detail || `runs/sync ${res.status}`);
    const resolvedSessionId = String(data.session_id || sessionId);
    const runId = String(data.run_id || '');
    const trace = traceFromSyncResponse(data) ?? await fetchEvaluationRunTrace({
      workspaceId: ws, sessionId: resolvedSessionId, runId,
    });
    return { finalAnswer: evaluationFinalAnswer(data), trace };
  }
  throw new Error('runs/sync 重试后仍被限流');
}
