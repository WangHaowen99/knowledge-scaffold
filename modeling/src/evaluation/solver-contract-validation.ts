import { EvaluationAuthenticationError, type EvaluationAuthSession } from './auth-session.js';
import { importPackageZip } from './data-source-consumer-sync.js';
import { prepareProjectDataSourceForEvaluation } from './data-source-evaluation-service.js';
import type { ProjectDataSource } from './data-source-repo.js';
import {
  activateEvaluationPackageVersion,
  authenticatedConsumerRequest,
  EvaluationOperationError,
  evaluationWorkspaceForRun,
  isEnvironmentFailureMessage,
} from './engine.js';
import { ensureEvaluationWorkspace, shadowWorkspaceId, type ConsumerRequest } from './shadow-workspace.js';

// 求解契约端到端验证：把建模侧导出的原始 ZIP 导入 Consumer 评测工作空间、激活精确版本，
// 再按验证用例调用 Consumer 公开求解接口。Consumer 经 Connector 调独立求解器并由
// Verifier 给出权威结果；这里只回传公开摘要字段，不接收也不返回 Solver Endpoint/Token。

export const MAX_SOLVER_VALIDATION_CASES = 20;
export const SOLVER_VALIDATION_DEADLINE_MS = 25 * 60_000;
const OPERATION_KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
const CASE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const IDEMPOTENCY_PREFIX_RE = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_ARGUMENTS_BYTES = 64 * 1024;
const MAX_SUMMARY_BYTES = 4 * 1024;
const MAX_TEXT = 512;
const PUBLIC_RESULT_FIELDS = [
  'run_id',
  'job_id',
  'feasibility',
  'optimality',
  'proof_kind',
  'result_artifact_ref',
  'verification_evidence_ref',
  'knowledge_revision_id',
  'fact_snapshot_digest',
  'problem_digest',
  'candidate_results_artifact_ref',
  'candidate_results_artifact_digest',
] as const;

export interface SolverValidationCase {
  id: string;
  operationKey: string;
  arguments: Record<string, unknown>;
}

export interface SolverValidationCaseResult {
  id: string;
  operation_key: string;
  status: 'completed' | 'error' | 'awaiting_input';
  authoritative: boolean;
  code?: string;
  phase?: string;
  retryable?: boolean;
  missing?: string[];
  candidate_count?: number | null;
  candidate_results_error?: string;
  summary?: unknown;
  [field: string]: unknown;
}

export interface SolverContractValidationReport {
  package: { id: string; version: string };
  cases: SolverValidationCaseResult[];
}

export class SolverContractValidationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly statusCode: 400 | 401 | 404 | 409 | 422 | 502,
    readonly stage = '',
  ) {
    super(message);
    this.name = 'SolverContractValidationError';
  }
}

/** Consumer 的报错可能带地址或主机信息；回传与落证据前统一脱敏截断。 */
export function safeValidationMessage(message: unknown): string {
  return String(message ?? '')
    .replace(/[a-z][a-z0-9+.-]*:\/\/\S+/giu, '[地址已隐藏]')
    .replace(/\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b/gu, '[地址已隐藏]')
    .slice(0, 300);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function invalidCases(message: string): SolverContractValidationError {
  return new SolverContractValidationError('SOLVER_VALIDATION_CASES_INVALID', message, 400);
}

export function parseSolverValidationCases(raw: unknown): SolverValidationCase[] {
  let value = raw;
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw);
    } catch {
      throw invalidCases('验证用例必须是 JSON 数组');
    }
  }
  if (!Array.isArray(value) || !value.length) throw invalidCases('至少需要一条验证用例');
  if (value.length > MAX_SOLVER_VALIDATION_CASES) {
    throw invalidCases(`验证用例最多 ${MAX_SOLVER_VALIDATION_CASES} 条`);
  }
  const seen = new Set<string>();
  return value.map((item, index) => {
    if (!isPlainObject(item)) throw invalidCases(`第 ${index + 1} 条验证用例不是对象`);
    const id = String(item.id ?? '').trim();
    const operationKey = String(item.operation_key ?? '').trim();
    if (!CASE_ID_RE.test(id)) throw invalidCases(`第 ${index + 1} 条验证用例的 id 不合法`);
    if (seen.has(id)) throw invalidCases(`验证用例 id 重复：${id}`);
    seen.add(id);
    if (!OPERATION_KEY_RE.test(operationKey)) {
      throw invalidCases(`验证用例 ${id} 的 operation_key 不合法`);
    }
    if (!isPlainObject(item.arguments)) throw invalidCases(`验证用例 ${id} 的 arguments 必须是对象`);
    if (Buffer.byteLength(JSON.stringify(item.arguments)) > MAX_ARGUMENTS_BYTES) {
      throw invalidCases(`验证用例 ${id} 的 arguments 超过 64 KB`);
    }
    return { id, operationKey, arguments: item.arguments };
  });
}

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.slice(0, MAX_TEXT) : undefined;
}

function errorCode(data: any, fallback: string): string {
  return text(data?.code) ?? fallback;
}

function errorResult(
  item: SolverValidationCase,
  code: string,
  phase: string,
  retryable: boolean,
): SolverValidationCaseResult {
  return { id: item.id, operation_key: item.operationKey, status: 'error', code, phase, retryable, authoritative: false };
}

function publicFields(data: any): Record<string, unknown> {
  const fields: Record<string, unknown> = {};
  for (const key of PUBLIC_RESULT_FIELDS) {
    const value = text(data?.[key]);
    if (value) fields[key] = value;
  }
  if (data?.summary !== undefined) {
    const serialized = JSON.stringify(data.summary);
    if (serialized !== undefined && Buffer.byteLength(serialized) <= MAX_SUMMARY_BYTES) fields.summary = data.summary;
  }
  return fields;
}

async function readJson(response: Response): Promise<any> {
  return response.json().catch(() => ({}));
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(signal.reason); return; }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(signal.reason);
    }, { once: true });
  });
}

async function candidateCount(
  request: ConsumerRequest,
  workspaceId: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<{ count: number | null; error?: string }> {
  const response = await request('', `/api/solver/history/${encodeURIComponent(jobId)}/candidate-results`, {
    workspaceId,
    signal,
  });
  const data = await readJson(response);
  if (response.ok && Number.isSafeInteger(data?.candidate_count)) return { count: data.candidate_count };
  return { count: null, error: errorCode(data, `http_${response.status}`) };
}

/**
 * 单条验证用例：同一个 Idempotency-Key 重复提交即续接同一 Job，直到 Consumer 给出终态。
 * 只有 Consumer 明确标记 authoritative=true 的 completed 才算权威结果；不可行也是有效终态。
 */
export async function runSolverValidationCase(input: {
  request: ConsumerRequest;
  workspaceId: string;
  ontologyId: string;
  version: string;
  item: SolverValidationCase;
  idempotencyKey: string;
  deadline: number;
  signal?: AbortSignal;
  pollIntervalMs?: number;
  now?: () => number;
}): Promise<SolverValidationCaseResult> {
  const { request, workspaceId, item, signal } = input;
  const now = input.now ?? Date.now;
  const pollIntervalMs = input.pollIntervalMs ?? 2_000;
  for (;;) {
    const response = await request('', '/api/solver/runs', {
      method: 'POST',
      workspaceId,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': input.idempotencyKey },
      body: JSON.stringify({
        knowledge_package_id: input.ontologyId,
        knowledge_package_revision: input.version,
        operation_key: item.operationKey,
        arguments: item.arguments,
      }),
      signal,
    });
    const data = await readJson(response);
    if (!response.ok) {
      return errorResult(item, errorCode(data, `http_${response.status}`), 'request', data?.retryable === true);
    }
    const status = String(data?.status ?? '');
    if (status === 'running') {
      if (now() + pollIntervalMs > input.deadline) return errorResult(item, 'solver_timeout', 'poll', true);
      await sleep(pollIntervalMs, signal);
      continue;
    }
    if (status === 'awaiting_input') {
      const missing = Array.isArray(data?.missing) ? data.missing.map(text).filter(Boolean).slice(0, 20) as string[] : [];
      return {
        id: item.id, operation_key: item.operationKey, status: 'awaiting_input',
        code: 'awaiting_input', phase: text(data?.phase) ?? 'bind', retryable: false, authoritative: false, missing,
      };
    }
    if (status !== 'completed') {
      return errorResult(
        item,
        errorCode(data, 'solver_result_rejected'),
        text(data?.phase) ?? 'unknown',
        data?.retryable === true,
      );
    }
    const result: SolverValidationCaseResult = {
      id: item.id,
      operation_key: item.operationKey,
      status: 'completed',
      authoritative: data?.authoritative === true,
      ...publicFields(data),
      candidate_count: null,
    };
    const jobId = text(data?.job_id);
    if (result.authoritative && jobId && result.candidate_results_artifact_ref) {
      const candidates = await candidateCount(request, workspaceId, jobId, signal);
      result.candidate_count = candidates.count;
      if (candidates.error) result.candidate_results_error = candidates.error;
    }
    return result;
  }
}

function isAuthenticationError(error: unknown): boolean {
  return error instanceof EvaluationAuthenticationError
    || (error instanceof Error && error.name === 'EvaluationAuthenticationError');
}

const STAGE_FAILURES: Record<string, [string, 409 | 422 | 502]> = {
  workspace: ['CONSUMER_WORKSPACE_UNAVAILABLE', 502],
  package: ['SOLVER_PACKAGE_REJECTED', 422],
  data_source: ['EVALUATION_DATA_SOURCE_STALE', 409],
  cases: ['CONSUMER_UNAVAILABLE', 502],
};

export function solverValidationStageError(stage: string, error: unknown): SolverContractValidationError {
  if (error instanceof SolverContractValidationError) return error;
  if (error instanceof EvaluationOperationError) {
    return new SolverContractValidationError(error.code, safeValidationMessage(error.message), error.statusCode, stage);
  }
  const message = error instanceof Error ? error.message : String(error);
  if (isAuthenticationError(error)) {
    return new SolverContractValidationError('EVALUATION_AUTH_EXPIRED', safeValidationMessage(message), 401, stage);
  }
  if (isEnvironmentFailureMessage(message)) {
    return new SolverContractValidationError('CONSUMER_UNAVAILABLE', 'Consumer 暂不可用，请稍后重试', 502, stage);
  }
  const [code, statusCode] = STAGE_FAILURES[stage] ?? STAGE_FAILURES.cases;
  return new SolverContractValidationError(code, safeValidationMessage(message), statusCode, stage);
}

/**
 * 运行期优先使用调用方带来的令牌；Consumer 拒绝后再借用 Modeling Core 内存里可续期的
 * 登录会话。验证可能持续超过 access token 有效期，而调用方无法在同步请求中接力新令牌。
 */
export function relayAuthSession(
  accessToken: string,
  fallback?: EvaluationAuthSession,
): Pick<EvaluationAuthSession, 'getAccessToken' | 'refreshAfterUnauthorized'> {
  let delegated = false;
  return {
    getAccessToken: async () => (delegated && fallback ? fallback.getAccessToken() : accessToken),
    refreshAfterUnauthorized: async () => {
      if (!fallback) {
        throw new EvaluationAuthenticationError('评测访问凭据已过期，请重新登录后再启动验证');
      }
      delegated = true;
      return fallback.refreshAfterUnauthorized();
    },
  };
}

export async function runSolverContractValidation(input: {
  projectId: string;
  owner: string;
  auth: Pick<EvaluationAuthSession, 'getAccessToken' | 'refreshAfterUnauthorized'>;
  dataSource: ProjectDataSource;
  ontologyId: string;
  version: string;
  zip: Uint8Array;
  cases: SolverValidationCase[];
  idempotencyPrefix: string;
  signal?: AbortSignal;
  request?: ConsumerRequest;
  pollIntervalMs?: number;
  deadlineMs?: number;
  now?: () => number;
}): Promise<SolverContractValidationReport> {
  const { projectId, owner, auth, dataSource, ontologyId, version, signal } = input;
  if (!IDEMPOTENCY_PREFIX_RE.test(input.idempotencyPrefix)) {
    throw new SolverContractValidationError('SOLVER_VALIDATION_RUN_INVALID', '验证运行标识不合法', 400);
  }
  const now = input.now ?? Date.now;
  const deadline = now() + (input.deadlineMs ?? SOLVER_VALIDATION_DEADLINE_MS);
  const injected = input.request;
  const request: ConsumerRequest = injected
    ?? ((_token, path, options) => authenticatedConsumerRequest(auth, path, options));
  const stage = async <T>(name: string, run: () => Promise<T>): Promise<T> => {
    try {
      return await run();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw solverValidationStageError(name, error);
    }
  };

  const workspaceId = await stage('workspace', async () => {
    const id = evaluationWorkspaceForRun(projectId, dataSource, ontologyId);
    await ensureEvaluationWorkspace({
      projectId,
      workspaceId: id,
      owner,
      token: await auth.getAccessToken(),
      signal,
      // 与评测一致：默认影子工作空间用服务凭据开通，其余工作空间按用户成员关系访问。
      ...(id === shadowWorkspaceId(projectId) && owner && !injected ? {} : { request }),
    });
    return id;
  });

  await stage('package', async () => {
    const imported = await importPackageZip({
      token: await auth.getAccessToken(), workspaceId, ontologyId, zip: input.zip, signal, request,
    });
    if (String(imported?.ontology_id ?? '') !== ontologyId) {
      throw new Error(`Consumer 导入的知识包 ID 不一致：期望 ${ontologyId}`);
    }
    await activateEvaluationPackageVersion({
      token: await auth.getAccessToken(), workspaceId, ontologyId, version, signal, request,
    });
  });

  await stage('data_source', async () => prepareProjectDataSourceForEvaluation({
    token: await auth.getAccessToken(), ontologyId, dataSource, signal, request,
  }));

  const results: SolverValidationCaseResult[] = [];
  for (const item of input.cases) {
    if (now() >= deadline) {
      results.push(errorResult(item, 'solver_timeout', 'poll', true));
      continue;
    }
    results.push(await stage('cases', () => runSolverValidationCase({
      request,
      workspaceId,
      ontologyId,
      version,
      item,
      idempotencyKey: `${input.idempotencyPrefix}:${item.id}`,
      deadline,
      signal,
      pollIntervalMs: input.pollIntervalMs,
      now,
    })));
  }
  return { package: { id: ontologyId, version }, cases: results };
}
