import { randomUUID } from 'node:crypto';
import { consumerFetch } from '../consumer/client.js';
import { zipPackage } from '../workflow/export.js';
import { publicKnowledgePackageFiles } from '../workflow/package-file-scope.js';
import { judge, type Verdict } from './judge.js';
import { runCasesWithLimit } from './case-runner.js';
import { config } from '../config.js';
import * as repo from '../workflow/m3-repo.js';
import { getProjectDataSource, type ProjectDataSource } from './data-source-repo.js';
import { prepareProjectDataSourceForEvaluation } from './data-source-evaluation-service.js';
import { getRunningEvaluationDatabaseImportRun } from './eval-db-import-repo.js';
import { importPackageZip } from './data-source-consumer-sync.js';
import {
  ensureEvaluationWorkspace,
  evaluationWorkspaceId,
  shadowWorkspaceId,
  type ConsumerRequest,
} from './shadow-workspace.js';
import { getPackageArtifactStore } from '../artifacts/runtime.js';
import {
  fetchEvaluationRunTrace,
  traceFromSyncResponse,
  type EvaluationExecutionTrace,
} from './trace-client.js';
import { validateSpecIntegrity } from '../workflow/spec-validation.js';
import { EvaluationAuthenticationError, EvaluationAuthSession } from './auth-session.js';
import type { Testcase } from '../workflow/m3-repo.js';
import { currentLlmUser, runWithLlmUser } from '../llm/user-context.js';
import {
  deliverAgentProjectEvents,
  enqueueEvaluationTerminalProjectEvent,
} from '../agent/project-event-outbox.js';

// 评测引擎（M3 核心）：评测工作空间 → 导入包 → 复用本体级数据源 →
// 逐用例 runs/sync（串行 + 退避）→ Judge → 落轮次结果。
// 后台执行：startEvaluation 立即返回 roundId，前端轮询 GET round。
export { shadowWorkspaceId } from './shadow-workspace.js';

export class EvaluationOperationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly statusCode: 400 | 404 | 409 | 422,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'EvaluationOperationError';
  }
}

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
    throw new EvaluationOperationError(
      'EVALUATION_TESTCASES_REQUIRED',
      '请先录入用户真实场景用例，知识包派生问题不作为正式评测用例',
      409,
    );
  }
  if (official.some((testcase) => !testcase.question.trim())) {
    throw new EvaluationOperationError(
      'EVALUATION_TESTCASE_INVALID',
      '存在正式评测用例问题为空，请先修正后再运行评测',
      422,
    );
  }
  return official;
}

export async function judgeEvaluationAnswer(
  question: string,
  groundTruth: string,
  finalAnswer: string,
  judgeAnswer: typeof judge = judge,
  signal?: AbortSignal,
): Promise<Verdict> {
  if (!groundTruth.trim()) {
    return { verdict: 'partial', reason: '缺少参考答案，等待人工判定' };
  }
  return judgeAnswer(question, groundTruth, finalAnswer, undefined, signal);
}

export async function activateEvaluationPackageVersion(input: {
  token: string;
  workspaceId: string;
  ontologyId: string;
  version: string;
  signal?: AbortSignal;
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
        signal: input.signal,
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
  authSession?: EvaluationAuthSession; // Agent 评测复用 Core 内存中的可续期会话
  caseIds?: string[]; // 为空时全量；有值时只运行受影响的真实用例
  evaluationScope?: repo.EvaluationScope;
  packageSnapshot: {
    version: string;
    spec: any;
    files: Record<string, string>;
    revisionOid: string;
  };
}

export interface EvaluationExecutionDependencies {
  runRound?: (input: {
    roundId: string;
    projectId: string;
    signal: AbortSignal;
  }) => Promise<void>;
  /** 开轮前的同步环境门；仅测试覆盖。默认实现见 preflightEvaluationEnvironment。 */
  preflight?: (input: EvaluationPreflightInput) => void;
}

export interface EvaluationPreflightInput {
  projectId: string;
  workspaceId: string;
  needsServiceCredential: boolean;
}

/**
 * 环境类失败的判别：凭据、令牌、网络连接这类和知识包质量无关的失败。
 * 实测一轮 151 条用例全部因「凭据已过期」判 error——环境坏了必须尽早停轮，
 * 而不是把每条用例都烧成同一个错误。执行期熔断按此判据提前终止整轮。
 */
export function isEnvironmentFailureMessage(message: unknown): boolean {
  const text = String(message ?? '');
  return /评测访问凭据已过期|重新登录后再启动评测|缺少有效的 MODELING_SERVICE_TOKEN|Consumer 请求失败|fetch failed|ECONNREFUSED|ECONNRESET|EAI_AGAIN|ETIMEDOUT/u.test(text);
}

const ENVIRONMENT_FAILURE_BREAKER_THRESHOLD = 3;

/**
 * 开轮前的同步环境门：只做零 I/O 的确定性检查（服务令牌是否配齐），
 * 环境不就绪就在创建轮次前拒绝，避免留下 failed 轮次污染统计。
 * 连接类失败不在这里探测——那会阻塞「创建即返回」契约，且已由执行期熔断兜底。
 */
function preflightEvaluationEnvironment(input: EvaluationPreflightInput): void {
  if (input.needsServiceCredential && String(config.modelingServiceToken ?? '').trim().length < 32) {
    throw new EvaluationOperationError(
      'EVALUATION_ENVIRONMENT_UNAVAILABLE',
      '缺少有效的 MODELING_SERVICE_TOKEN，评测未启动；请在部署配置中补齐服务令牌后重试',
      409,
    );
  }
}

const activeProjects = new Set<string>();
const activeWorkspaces = new Set<string>();
const activeEvaluations = new Map<string, {
  roundId: string;
  controller: AbortController;
  completion: Promise<void>;
}>();

function assertEvaluationNotCancelled(signal: AbortSignal): void {
  if (signal.aborted) throw new Error('本轮评测已由用户停止');
}

export async function cancelEvaluation(projectId: string, roundId?: string): Promise<repo.EvalRound> {
  const active = activeEvaluations.get(projectId);
  if (!active || (roundId && active.roundId !== roundId)) {
    throw new EvaluationOperationError(
      'EVALUATION_NOT_RUNNING',
      '该项目当前没有可停止的评测任务',
      409,
    );
  }
  const round = repo.getRound(active.roundId);
  if (!round) {
    throw new EvaluationOperationError('EVALUATION_NOT_FOUND', '评测轮次不存在', 404);
  }
  if (round.status !== 'running') {
    throw new EvaluationOperationError('EVALUATION_NOT_RUNNING', '该轮评测已经结束', 409);
  }
  repo.roundLog(active.roundId, '用户从评测页面停止本轮；已保留当前完成结果，可重新启动正式评测');
  repo.finishRound(active.roundId, 'failed');
  active.controller.abort(new Error('本轮评测已由用户停止'));
  await active.completion;
  return repo.getRound(active.roundId)!;
}

export async function cancelEvaluationsForProject(projectId: string): Promise<void> {
  const active = activeEvaluations.get(projectId);
  if (!active) return;
  const round = repo.getRound(active.roundId);
  if (round?.status === 'running') {
    repo.roundLog(active.roundId, '项目删除，本轮评测已停止');
    repo.finishRound(active.roundId, 'failed');
  }
  active.controller.abort(new Error('项目删除，本轮评测已停止'));
  await active.completion;
}

export function isEvaluationActive(projectId: string): boolean {
  return activeProjects.has(projectId);
}

export async function waitForEvaluation(projectId: string, roundId: string): Promise<repo.EvalRound> {
  const active = activeEvaluations.get(projectId);
  if (active?.roundId === roundId) await active.completion;
  const round = repo.getRound(roundId);
  if (!round || round.projectId !== projectId) throw new Error('评测轮次不存在');
  return round;
}

export function evaluationWorkspaceForRun(
  projectId: string,
  dataSource: ProjectDataSource,
  ontologyId: string,
): string {
  if (dataSource.mode !== 'connection') return dataSource.workspaceId;
  if (!dataSource.ontologyId) {
    throw new EvaluationOperationError(
      'EVALUATION_DATA_SOURCE_REQUIRED',
      '数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存',
      409,
    );
  }
  if (dataSource.ontologyId !== ontologyId) {
    throw new EvaluationOperationError(
      'EVALUATION_DATA_SOURCE_STALE',
      '本体数据源与当前知识包不匹配，请返回“数据源配置”重新保存',
      409,
    );
  }
  const expectedWorkspaceId = evaluationWorkspaceId(projectId, dataSource);
  if (dataSource.workspaceId !== expectedWorkspaceId) {
    throw new EvaluationOperationError(
      'EVALUATION_DATA_SOURCE_STALE',
      '数据源配置缺少可用的本体数据源，请返回“数据源配置”重新输入密钥并保存',
      409,
    );
  }
  return expectedWorkspaceId;
}

export function startEvaluation(
  opts: RunOptions,
  dependencies: EvaluationExecutionDependencies = {},
): { roundId: string } {
  const { projectId } = opts;
  if (activeProjects.has(projectId)) {
    throw new EvaluationOperationError('EVALUATION_ALREADY_RUNNING', '该项目已有评测在运行', 409);
  }
  const latest = opts.packageSnapshot;
  if (!latest) throw new Error('启动评测必须提供知识包快照');
  if (!String(latest.revisionOid ?? '').trim()) throw new Error('知识包快照缺少工件修订');
  // 评测必须能够记录存量脚本的执行契约失败，而不是在轮次创建前整体拦截。
  // 安全检查仍然强制执行；候选接受、导出和发布继续使用严格契约。
  try {
    validateSpecIntegrity(latest.spec, [], undefined, { validateScriptExecutionContract: false });
  } catch (error) {
    throw new EvaluationOperationError(
      'EVALUATION_PACKAGE_INVALID',
      error instanceof Error ? error.message : '当前知识包未通过评测前校验',
      422,
      error,
    );
  }
  const officialTestcases = selectOfficialEvaluationTestcases(repo.listTestcases(projectId));
  const requestedCaseIds = new Set((opts.caseIds ?? []).map((item) => String(item).trim()).filter(Boolean));
  const testcases = requestedCaseIds.size
    ? officialTestcases.filter((item) => requestedCaseIds.has(item.id))
    : officialTestcases;
  if (requestedCaseIds.size && testcases.length !== requestedCaseIds.size) {
    const found = new Set(testcases.map((item) => item.id));
    const missing = [...requestedCaseIds].filter((id) => !found.has(id));
    throw new EvaluationOperationError(
      'EVALUATION_TESTCASE_NOT_FOUND',
      `定向评测用例不存在或不是人工真实用例：${missing.join('、')}`,
      404,
    );
  }
  const dataSource = getProjectDataSource(projectId);
  if (!dataSource) {
    throw new EvaluationOperationError(
      'EVALUATION_DATA_SOURCE_REQUIRED',
      '尚未配置数据源，请先完成“数据源配置”步骤',
      409,
    );
  }
  const runningDatabaseImport = getRunningEvaluationDatabaseImportRun(projectId);
  if (runningDatabaseImport) {
    throw new EvaluationOperationError(
      'EVALUATION_DATABASE_IMPORT_ALREADY_RUNNING',
      `评测库数据仍在后台导入（${runningDatabaseImport.filename}），请等待导入完成后再运行评测`,
      409,
    );
  }
  const workspaceId = evaluationWorkspaceForRun(projectId, dataSource, latest.spec.id);
  if (activeWorkspaces.has(workspaceId)) {
    throw new EvaluationOperationError(
      'EVALUATION_WORKSPACE_BUSY',
      `评测工作空间 ${workspaceId} 已有评测在运行`,
      409,
    );
  }
  // 假 runRound（测试注入）不触达真实环境，默认不做环境门；显式传入 preflight 时始终执行。
  const preflight = dependencies.preflight
    ?? (dependencies.runRound ? null : preflightEvaluationEnvironment);
  if (preflight) {
    preflight({
      projectId,
      workspaceId,
      // 影子工作空间由服务凭据开通（与 runRound 的开通条件保持一致）。
      needsServiceCredential: workspaceId === shadowWorkspaceId(projectId) && Boolean(opts.owner),
    });
  }

  const round = repo.createRound(
    projectId,
    latest.version,
    testcases.length,
    latest.revisionOid,
    opts.evaluationScope,
  );
  activeProjects.add(projectId);
  activeWorkspaces.add(workspaceId);
  const controller = new AbortController();
  const llmUser = currentLlmUser() ?? opts.owner;
  // 后台执行（单进程内异步任务；进度与日志写库供轮询）
  const executeRound = () => Promise.resolve().then(() => {
    assertEvaluationNotCancelled(controller.signal);
    return dependencies.runRound
      ? dependencies.runRound({ roundId: round.id, projectId, signal: controller.signal })
      : runRound(round.id, opts, latest, testcases, dataSource, workspaceId, controller.signal);
  });
  const roundExecution = llmUser ? runWithLlmUser(llmUser, executeRound) : executeRound();
  const completion = roundExecution
    .catch((e) => {
      if (controller.signal.aborted) return;
      repo.roundLog(round.id, `轮次失败：${e instanceof Error ? e.message : e}`);
      repo.finishRound(round.id, 'failed');
    })
    .finally(async () => {
      if (activeEvaluations.get(projectId)?.roundId === round.id) activeEvaluations.delete(projectId);
      activeProjects.delete(projectId);
      activeWorkspaces.delete(workspaceId);
      const terminal = repo.getRound(round.id);
      if (!terminal || terminal.status === 'running') return;
      enqueueEvaluationTerminalProjectEvent(projectId, {
        eventId: `evaluation:${terminal.id}:terminal`,
        roundId: terminal.id,
        status: terminal.status,
        done: terminal.progressDone,
        total: terminal.progressTotal,
        passed: terminal.passCount,
        finishedAt: terminal.finishedAt ?? new Date().toISOString(),
      });
      try {
        const token = opts.authSession ? await opts.authSession.getAccessToken() : opts.token;
        await deliverAgentProjectEvents(projectId, `Bearer ${token}`);
      } catch {
        // outbox 保留未投递事件，下一次项目请求会继续投递。
      }
    });
  activeEvaluations.set(projectId, { roundId: round.id, controller, completion });
  void completion;
  return { roundId: round.id };
}

export async function startCurrentEvaluation(
  opts: Omit<RunOptions, 'packageSnapshot'>,
  dependencies: EvaluationExecutionDependencies = {},
): Promise<{ roundId: string }> {
  const snapshot = await getPackageArtifactStore().readPackageFiles(opts.projectId);
  return startEvaluation({
    ...opts,
    packageSnapshot: {
      version: snapshot.spec.version,
      spec: snapshot.spec,
      files: snapshot.files,
      revisionOid: snapshot.revision.oid,
    },
  }, dependencies);
}

async function runRound(
  roundId: string,
  opts: RunOptions,
  latest: RunOptions['packageSnapshot'],
  testcases: { id: string; question: string; groundTruth: string }[],
  dataSource: ProjectDataSource,
  workspaceId: string,
  signal: AbortSignal,
): Promise<void> {
  const { projectId, owner } = opts;
  const auth = opts.authSession
    ?? new EvaluationAuthSession({ accessToken: opts.token, refreshToken: opts.refreshToken });
  const request: ConsumerRequest = (_token, path, options) => authenticatedConsumerRequest(auth, path, options);
  const ws = workspaceId;
  const log = (m: string) => repo.roundLog(roundId, m);
  assertEvaluationNotCancelled(signal);

  // 1. 评测工作空间
  const workspace = await ensureEvaluationWorkspace({
    projectId, workspaceId: ws, owner, token: await auth.getAccessToken(),
    signal,
    // Default shadow workspaces use service-credential provisioning. Other workspaces
    // use the renewable user session because Consumer checks their membership.
    ...(ws === shadowWorkspaceId(projectId) && owner ? {} : { request }),
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
  const files = { ...latest.files };
  const zip = zipPackage(publicKnowledgePackageFiles(files));
  const importResult = await importPackageZip({
    token: await auth.getAccessToken(),
    workspaceId: ws,
    ontologyId: latest.spec.id,
    zip,
    signal,
    request,
  });
  assertEvaluationNotCancelled(signal);
  log(`知识包已导入评测工作空间：${importResult.ontology_id}（对象 ${importResult.objects_count ?? '-'} / 操作 ${importResult.operations_count ?? '-'}）`);
  // 导入 ≠ 生效：consumer 可能仍激活旧版本（实测吃过亏）。显式激活本次版本并确认。
  await activateEvaluationPackageVersion({
    token: await auth.getAccessToken(),
    workspaceId: ws,
    ontologyId: importResult.ontology_id,
    version: latest.spec.version,
    signal,
    request,
  });
  log(`已激活并确认版本：${latest.spec.version}`);

  // 4. 复用“数据源配置”步骤保存的本体级数据源，不在评测运行时传递密钥
  repo.roundPhase(roundId, 'data_source');
  await prepareProjectDataSourceForEvaluation({
    token: await auth.getAccessToken(), ontologyId: importResult.ontology_id, dataSource,
    signal,
    request,
  });
  assertEvaluationNotCancelled(signal);
  if (dataSource.mode === 'connection') {
    log(`已复用本体数据源：${dataSource.ontologyId}（${dataSource.provider}）`);
  } else {
    log('已按数据源配置清除本体数据源（判断类问题仍可评测）');
  }

  // 5. 用例按有界并发运行 + judge；结果仍按用例顺序落库
  repo.roundPhase(roundId, 'cases');
  const concurrency = Math.min(config.evaluationCaseConcurrency, testcases.length || 1);
  if (concurrency > 1) log(`并发运行评测用例：${concurrency} 条并行`);
  let done = 0;
  let pass = 0;
  let environmentFailureStreak = 0;

  type CaseOutcome = {
    finalAnswer: string;
    trace?: EvaluationExecutionTrace;
    verdict: 'pass' | 'partial' | 'fail' | 'error';
    reason: string;
    traceWarning?: string;
  };

  await runCasesWithLimit<typeof testcases[number], CaseOutcome>(testcases, concurrency, {
    onStart: () => repo.roundCaseStarted(roundId),
    run: async (tc) => {
      assertEvaluationNotCancelled(signal);
      // runCase 内部只处理「响应返回了非 2xx」；网关限流是直接抛异常的，
      // 那条路径绕过了内层重试，外面再包一层才兜得住。
      const runHooks: RunCaseHooks = {
        onRateLimited: (attempt, waitMs, failure) => {
          log(`⏳ 被限流，${Math.round(waitMs / 1000)} 秒后重试（第 ${attempt} 次）：${failure.slice(0, 80)}`);
        },
      };
      const run = await withRateLimitRetry(
        '用例执行',
        () => runCase(request, ws, tc.question, signal, runHooks),
        runHooks,
        signal,
      );
      assertEvaluationNotCancelled(signal);
      const v = await withRateLimitRetry(
        '评测判分',
        () => judgeEvaluationAnswer(tc.question, tc.groundTruth, run.finalAnswer, judge, signal),
        {
          onRateLimited: (attempt, waitMs, failure) => {
            log(`⏳ 判分被限流，${Math.round(waitMs / 1000)} 秒后重试（第 ${attempt} 次）：${failure.slice(0, 80)}`);
          },
        },
        signal,
      );
      assertEvaluationNotCancelled(signal);
      environmentFailureStreak = 0;
      return {
        finalAnswer: run.finalAnswer,
        trace: run.trace,
        verdict: v.verdict,
        reason: v.reason,
        traceWarning: run.trace.status === 'unavailable'
          ? `Trace 不可用：${run.trace.error ?? '未知错误'}`
          : undefined,
      };
    },
    // 单条用例失败只记一条 error 判定；鉴权失败和取消属于致命错误，向上抛出终止整轮。
    // 环境类失败（凭据/令牌/网络）连续出现时提前熔断，不把整批用例烧成同一个错误。
    recover: (tc, _index, error) => {
      if (signal.aborted) throw signal.reason;
      if (error instanceof EvaluationAuthenticationError) throw error;
      // instanceof 会因模块被重复加载而失真，按异常名兜底，确保鉴权失败仍终止整轮。
      if (error instanceof Error && error.name === 'EvaluationAuthenticationError') throw error;
      const message = error instanceof Error ? error.message : String(error);
      if (isEnvironmentFailureMessage(message)) {
        environmentFailureStreak += 1;
        if (environmentFailureStreak >= ENVIRONMENT_FAILURE_BREAKER_THRESHOLD) {
          throw new Error(
            `评测环境持续异常，已提前终止本轮：连续 ${environmentFailureStreak} 条用例因环境问题失败（${message.slice(0, 120)}）`,
          );
        }
      } else {
        environmentFailureStreak = 0;
      }
      return {
        finalAnswer: '',
        verdict: 'error',
        reason: `运行失败：${message}`,
      };
    },
    commit: (tc, _index, outcome) => {
      done += 1;
      if (outcome.verdict === 'pass') pass += 1;
      if (outcome.traceWarning) log(`⚠ [${done}/${testcases.length}] ${outcome.traceWarning}`);
      repo.addResult(roundId, {
        testcaseId: tc.id,
        question: tc.question,
        groundTruth: tc.groundTruth,
        finalAnswer: outcome.finalAnswer,
        verdict: outcome.verdict,
        reason: outcome.reason,
        trace: outcome.trace,
      });
      repo.roundProgress(roundId, done, pass);
      log(`[${done}/${testcases.length}] ${outcome.verdict} · ${tc.question.slice(0, 40)}`);
    },
  }, signal);
  assertEvaluationNotCancelled(signal);

  assertEvaluationNotCancelled(signal);
  repo.finishRound(roundId, 'done');
  log(`轮次完成：通过 ${pass}/${testcases.length}`);
}

// runs/sync：429/503 退避重试
/**
 * 上游模型端点的限流不一定体现为 HTTP 429：agent 在 consumer 内部调模型被限流时，
 * 错误会包在响应体里带上来（例如 OpenAI SDK 的「429 status code (no body)」）。
 * 并发跑用例后这类错误明显变多，必须一并当作可重试，否则一条用例会直接判 error。
 */
export function isRateLimitedFailure(status: number, message: unknown): boolean {
  if (status === 429 || status === 503) return true;
  const text = String(message ?? '');
  // 中文文案来自网关而不是模型 SDK（「模型服务请求过于频繁」），
  // 同仓 material-analysis-map-reduce 早已覆盖，评测这条路径先前漏了。
  return /(^|\D)429(\D|$)/u.test(text)
    || /rate[\s_-]?limit/iu.test(text)
    || /too many requests/iu.test(text)
    || /请求过于频繁/u.test(text)
    || /限流/u.test(text);
}

/** 指数退避 + 抖动：并发下固定退避会让多条用例同时重试、再次撞限流。 */
export function rateLimitBackoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(2000 * (2 ** Math.max(0, attempt - 1)), 30_000);
  return Math.round(base * (0.8 + (0.4 * random())));
}

const RATE_LIMIT_MAX_ATTEMPTS = 6;

/** 限流重试包装：runs/sync 与判分调用共用同一套退避策略。 */
export async function withRateLimitRetry<T>(
  label: string,
  fn: () => Promise<T>,
  hooks: RunCaseHooks,
  signal: AbortSignal,
): Promise<T> {
  let attempts = 0;
  for (;;) {
    try {
      return await fn();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      // 判分走的是 LLM SDK，限流只会以异常文本出现，没有 HTTP 状态码可查。
      if (!isRateLimitedFailure(0, message) || signal.aborted) throw error;
      attempts += 1;
      if (attempts >= RATE_LIMIT_MAX_ATTEMPTS) {
        throw new Error(`${label} 重试 ${RATE_LIMIT_MAX_ATTEMPTS} 次后仍被限流：${message}`);
      }
      const waitMs = (hooks.backoffMs ?? rateLimitBackoffMs)(attempts);
      hooks.onRateLimited?.(attempts, waitMs, message);
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, waitMs);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          reject(new Error('本轮评测已由用户停止'));
        }, { once: true });
      });
    }
  }
}

export interface RunCaseHooks {
  /** 每次因限流而等待前回调，用于把重试写进轮次日志。 */
  onRateLimited?: (attempt: number, waitMs: number, failure: string) => void;
  /** 退避时长，仅测试会覆盖，避免真的等满一轮指数退避。 */
  backoffMs?: (attempt: number) => number;
}

/** 导出供测试直接驱动限流重试循环；生产路径只由 runRound 调用。 */
export async function runCase(
  request: ConsumerRequest,
  ws: string,
  question: string,
  signal: AbortSignal,
  hooks: RunCaseHooks = {},
): Promise<{ finalAnswer: string; trace: EvaluationExecutionTrace }> {
  const sessionId = `mw-eval-${randomUUID().slice(0, 8)}`;
  let rateLimitAttempts = 0;
  for (;;) {
    assertEvaluationNotCancelled(signal);
    const res = await request('', '/agentic/runs/sync', {
      method: 'POST', workspaceId: ws, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request: question, session_id: sessionId, include_trace: true }),
      signal,
    });
    const data = (await res.json().catch(() => ({}))) as any;
    const failure = res.ok ? '' : (data.error || data.detail || `runs/sync ${res.status}`);
    if (!res.ok && isRateLimitedFailure(res.status, failure)) {
      rateLimitAttempts += 1;
      if (rateLimitAttempts >= RATE_LIMIT_MAX_ATTEMPTS) {
        throw new Error(`runs/sync 重试 ${RATE_LIMIT_MAX_ATTEMPTS} 次后仍被限流：${failure}`);
      }
      const waitMs = (hooks.backoffMs ?? rateLimitBackoffMs)(rateLimitAttempts);
      hooks.onRateLimited?.(rateLimitAttempts, waitMs, String(failure));
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, waitMs);
        signal.addEventListener('abort', () => {
          clearTimeout(timer);
          reject(new Error('本轮评测已由用户停止'));
        }, { once: true });
      });
      continue;
    }
    if (!res.ok) throw new Error(failure);
    const resolvedSessionId = String(data.session_id || sessionId);
    const runId = String(data.run_id || '');
    const trace = traceFromSyncResponse(data) ?? await fetchEvaluationRunTrace({
      workspaceId: ws, sessionId: resolvedSessionId, runId,
      signal,
    });
    return { finalAnswer: evaluationFinalAnswer(data), trace };
  }
}

async function authenticatedConsumerRequest(
  auth: EvaluationAuthSession,
  path: string,
  options: RequestInit & { workspaceId?: string } = {},
): Promise<Response> {
  let response = await consumerFetch(await auth.getAccessToken(), path, options);
  if (response.status !== 401) return response;

  await response.body?.cancel();
  await auth.refreshAfterUnauthorized();
  response = await consumerFetch(await auth.getAccessToken(), path, options);
  if (response.status === 401) {
    await response.body?.cancel();
    throw new EvaluationAuthenticationError('评测登录续期后仍未通过 Consumer 认证，请重新登录后再启动评测');
  }
  return response;
}
