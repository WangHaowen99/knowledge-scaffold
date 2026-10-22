import { createHash } from 'node:crypto';

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
  rateLimitBackoffMs,
} from './engine.js';
import { ensureEvaluationWorkspace, shadowWorkspaceId, type ConsumerRequest } from './shadow-workspace.js';

// 求解契约端到端验证：把建模侧导出的原始 ZIP 导入 Consumer 评测工作空间、激活精确版本，
// 再按验证用例向 Consumer 的 Agent 提问（与用户在 Consumer 对话求解同一条链路）。
// Agent 调用知识包生成的求解工具，Consumer 经 Connector 调独立求解器并由 Verifier
// 给出权威结果；这里从运行事件里取求解工具的调用入参与公开结果摘要，
// 不接收也不返回 Solver Endpoint/Token。

export const MAX_SOLVER_VALIDATION_CASES = 20;
export const SOLVER_VALIDATION_DEADLINE_MS = 25 * 60_000;
// 单条用例：模型推理 + 求解工具（Consumer 默认 360 秒超时）+ 回复，留足余量
const CASE_DEADLINE_MS = 10 * 60_000;
const OPERATION_KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
const CASE_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const IDEMPOTENCY_PREFIX_RE = /^[A-Za-z0-9_-]{1,64}$/;
const MAX_ARGUMENTS_BYTES = 64 * 1024;
// 自动生成的提问内嵌入参 JSON，上限按入参上限留余量
const MAX_QUESTION_BYTES = 96 * 1024;
const MAX_SUMMARY_BYTES = 4 * 1024;
const MAX_TEXT = 512;
const MAX_ARGUMENT_DIFFS = 20;
const MAX_EVENT_BUFFER = 4 * 1024 * 1024;
const MAX_STREAM_RECONNECTS = 5;
const MAX_ADMISSION_RETRIES = 6;
const SOLVER_EVENT_TYPES = new Set([
  'solver.input.awaiting_confirmation',
  'solver.progress',
  'solver.verification.completed',
  'solver.verification.rejected',
  'solver.result.authoritative',
]);
// 求解工具卡名：solve_<operation_key 规范化>_<契约摘要前 8 位>；兼容卡名 solve 的入参带 operation_key
const SOLVER_CARD_RE = /^solve_([a-z0-9_]+)_[0-9a-f]{8}$/;
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
// 权威结果事件与工具结果都带这些字段时必须一致，否则工具结果不是这次权威完成的产物
const AUTHORITATIVE_EVENT_FIELDS = [
  'job_id',
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
  /** 向 Consumer Agent 提的问题；由 onto-modeling 生成或取用例里填写的提问。 */
  question: string;
  arguments: Record<string, unknown>;
}

export interface SolverValidationCaseResult {
  id: string;
  operation_key: string;
  status: 'completed' | 'error' | 'awaiting_input';
  authoritative: boolean;
  code?: string;
  phase?: string;
  /** Consumer 为调用方写的失败原因（已脱敏截断），没有时省略。 */
  detail?: string;
  retryable?: boolean;
  missing?: string[];
  candidate_count?: number | null;
  candidate_results_error?: string;
  summary?: unknown;
  /** Agent 是否调用了该用例求解能力对应的求解工具；Agent 运行没有开始时省略。 */
  solver_tool_called?: boolean;
  solver_tool?: string;
  /** 求解工具入参与用例 arguments 是否一致；没调用时省略。 */
  arguments_match?: boolean;
  /** 不一致的入参路径（如 items[2].process_days），最多 20 条。 */
  argument_diff?: string[];
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
    const question = typeof item.question === 'string' ? item.question.trim() : '';
    if (!question) throw invalidCases(`验证用例 ${id} 缺少提问 question`);
    if (Buffer.byteLength(question) > MAX_QUESTION_BYTES) {
      throw invalidCases(`验证用例 ${id} 的提问超过 96 KB`);
    }
    return { id, operationKey, question, arguments: item.arguments };
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
  detail?: string,
): SolverValidationCaseResult {
  const result: SolverValidationCaseResult = {
    id: item.id, operation_key: item.operationKey, status: 'error', code, phase, retryable, authoritative: false,
  };
  if (detail) result.detail = safeValidationMessage(detail);
  return result;
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

/** 单条用例内部失败：在 runSolverValidationCase 里统一转成用例结果，不中断整轮验证。 */
class CaseFailure extends Error {
  constructor(
    readonly code: string,
    readonly phase: string,
    readonly retryable: boolean,
    readonly detail?: string,
  ) {
    super(code);
    this.name = 'CaseFailure';
  }
}

class CaseTimeout extends Error {
  constructor() {
    super('solver_timeout');
    this.name = 'CaseTimeout';
  }
}

interface SolverToolCall {
  name: string;
  /** 兼容卡 solve 的入参里显式带 operation_key。 */
  operationKey?: string;
  /** 精确卡名里的 operation_key 规范化片段。 */
  slug?: string;
  arguments: unknown;
  output?: Record<string, unknown>;
  toolStatus?: string;
  toolErrorCode?: string;
  /** 同一 tool_call_id 的 solver.* 事件，后到的字段覆盖先到的。 */
  progress: Record<string, unknown>;
  /** 同一 tool_call_id 的 solver.result.authoritative 事件载荷；Consumer 只在 Verifier 通过后发出。 */
  authoritativeEvent?: Record<string, unknown>;
}

interface AgentRunObservation {
  status: string;
  content: string;
  interactionPrompt?: string;
  errorCode?: string;
  errorMessage?: string;
  calls: Map<string, SolverToolCall>;
}

interface SseFrame {
  event: string;
  data: string;
}

function parseSseFrame(raw: string): SseFrame | null {
  let event = 'message';
  const data: string[] = [];
  for (const rawLine of raw.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /u, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  return data.length ? { event, data: data.join('\n') } : null;
}

async function* sseFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const frame = parseSseFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (frame) yield frame;
        boundary = buffer.indexOf('\n\n');
      }
      if (buffer.length > MAX_EVENT_BUFFER) throw new CaseFailure('agent_event_too_large', 'events', false);
      if (done) return;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

/** 与 Consumer 生成精确卡名的规则一致：非字母数字折叠为下划线，卡名总长上限 64。 */
function cardSlug(operationKey: string): string {
  return operationKey
    .replace(/[^A-Za-z0-9]+/gu, '_')
    .replace(/^_+|_+$/gu, '')
    .toLowerCase()
    .slice(0, 64 - 'solve_'.length - '_xxxxxxxx'.length)
    .replace(/_+$/u, '');
}

function toolInput(payload: Record<string, any>): unknown {
  const input = payload.input_preview;
  if (typeof input !== 'string') return input;
  try {
    return JSON.parse(input);
  } catch {
    return input;
  }
}

function solverToolCall(payload: Record<string, any>): SolverToolCall | null {
  const name = String(payload.name ?? '');
  const input = toolInput(payload) as any;
  if (name === 'solve') {
    return { name, operationKey: String(input?.operation_key ?? ''), arguments: input?.arguments, progress: {} };
  }
  const match = SOLVER_CARD_RE.exec(name);
  return match ? { name, slug: match[1], arguments: input, progress: {} } : null;
}

function callsOperation(call: SolverToolCall, operationKey: string): boolean {
  return call.operationKey !== undefined ? call.operationKey === operationKey : call.slug === cardSlug(operationKey);
}

function parseToolOutput(preview: unknown): Record<string, unknown> | undefined {
  if (isPlainObject(preview)) return preview;
  if (typeof preview !== 'string') return undefined;
  try {
    const value = JSON.parse(preview);
    return isPlainObject(value) ? value : undefined;
  } catch {
    // 超过 4 KB 的工具输出被截断，改用同一调用的 solver.* 事件字段
    return undefined;
  }
}

function observeRunEvent(observation: AgentRunObservation, event: any): void {
  const payload: Record<string, any> = isPlainObject(event?.payload) ? event.payload : {};
  const type = String(event?.event_type ?? '');
  const toolCallId = String(payload.tool_call_id ?? '');
  if (type === 'tool.started') {
    const call = solverToolCall(payload);
    const key = toolCallId || `seq-${String(event?.seq ?? observation.calls.size)}`;
    if (call && !observation.calls.has(key)) observation.calls.set(key, call);
    return;
  }
  const call = toolCallId ? observation.calls.get(toolCallId) : undefined;
  if (type === 'tool.finished' && call) {
    call.toolStatus = text(payload.status);
    call.toolErrorCode = text(payload.error_code);
    call.output = parseToolOutput(payload.output_preview);
  } else if (SOLVER_EVENT_TYPES.has(type) && call) {
    Object.assign(call.progress, payload);
    if (type === 'solver.result.authoritative') call.authoritativeEvent = payload;
  } else if (type === 'interaction.requested') {
    observation.interactionPrompt = text(payload.prompt);
  } else if (type === 'turn.finished') {
    observation.status = String(payload.status ?? event?.status ?? '');
    observation.content = typeof payload.content === 'string' ? payload.content : '';
    if (isPlainObject(payload.error)) {
      observation.errorCode = text(payload.error.error_code);
      observation.errorMessage = text(payload.error.message);
    }
  }
}

function agentErrorCode(status: number, data: any): string {
  const code = text(data?.error?.error_code);
  return code ? code.toLowerCase() : `http_${status}`;
}

/** 每条用例一个独立会话：同一验证运行重复提交同一用例时，Idempotency-Key 让 Consumer 续接原运行。 */
async function startAgentRun(input: {
  request: ConsumerRequest;
  workspaceId: string;
  ontologyId: string;
  version: string;
  item: SolverValidationCase;
  idempotencyKey: string;
  signal: AbortSignal;
}): Promise<string> {
  const sessionId = `mw-solver-${createHash('sha256').update(input.idempotencyKey).digest('hex').slice(0, 24)}`;
  for (let attempt = 1; ; attempt += 1) {
    const response = await input.request('', '/agentic/runs', {
      method: 'POST',
      workspaceId: input.workspaceId,
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': input.idempotencyKey },
      body: JSON.stringify({
        request: input.item.question,
        session_id: sessionId,
        // 钉住刚激活的知识包精确版本，避免 Agent 落到工作空间里其他知识包
        arguments: { selected_ontology_id: input.ontologyId, selected_ontology_version: input.version },
      }),
      signal: input.signal,
    });
    const data = await readJson(response);
    if (response.ok) {
      const runId = text(data?.run_id);
      if (!runId) throw new CaseFailure('agent_run_invalid', 'admission', true, 'Consumer 未返回运行标识');
      return runId;
    }
    const retryable = data?.error?.retriable === true;
    if (retryable && attempt < MAX_ADMISSION_RETRIES) {
      await sleep(rateLimitBackoffMs(attempt), input.signal);
      continue;
    }
    throw new CaseFailure(agentErrorCode(response.status, data), 'admission', retryable, text(data?.error?.detail));
  }
}

/** 订阅运行事件直到 turn.finished；流意外中断时按已收到的 seq 续订。 */
async function followAgentRun(input: {
  request: ConsumerRequest;
  workspaceId: string;
  runId: string;
  observation: AgentRunObservation;
  signal: AbortSignal;
}): Promise<void> {
  const { observation, signal } = input;
  let afterSeq = 0;
  let reconnects = 0;
  for (;;) {
    const response = await input.request(
      '',
      `/agentic/runs/${encodeURIComponent(input.runId)}/events?after_seq=${afterSeq}`,
      { workspaceId: input.workspaceId, headers: { Accept: 'text/event-stream' }, signal },
    );
    if (!response.ok || !response.body) {
      const data = await readJson(response);
      if (data?.error?.retriable === true && reconnects < MAX_STREAM_RECONNECTS) {
        reconnects += 1;
        await sleep(rateLimitBackoffMs(reconnects), signal);
        continue;
      }
      throw new CaseFailure(
        agentErrorCode(response.status, data), 'events', data?.error?.retriable === true, text(data?.error?.detail),
      );
    }
    try {
      for await (const frame of sseFrames(response.body)) {
        let event: any;
        try {
          event = JSON.parse(frame.data);
        } catch {
          continue;
        }
        const seq = Number(event?.seq);
        if (Number.isSafeInteger(seq)) {
          if (seq <= afterSeq) continue;
          afterSeq = seq;
        }
        observeRunEvent(observation, event);
        if (event?.event_type === 'turn.finished') return;
      }
    } catch (error) {
      if (signal.aborted || error instanceof CaseFailure) throw error;
    }
    reconnects += 1;
    if (reconnects > MAX_STREAM_RECONNECTS) {
      throw new CaseFailure('agent_event_stream_lost', 'events', true, 'Consumer 运行事件流多次中断');
    }
    await sleep(rateLimitBackoffMs(reconnects), signal);
  }
}

async function cancelAgentRun(request: ConsumerRequest, workspaceId: string, runId: string, reason: string) {
  try {
    const response = await request('', `/agentic/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
      workspaceId,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
      signal: AbortSignal.timeout(10_000),
    });
    await response.body?.cancel();
  } catch {
    // 尽力取消：Consumer 不可达时运行会按自身超时结束
  }
}

const REDACTED = '[REDACTED]';

function sameScalar(expected: unknown, actual: unknown): boolean {
  if (expected === actual || (expected == null && actual == null)) return true;
  // Agent 可能把数字写成数字字符串（或反之），值相同即视为一致
  if (typeof expected === 'number' && typeof actual === 'string') {
    return actual.trim() !== '' && Number(actual) === expected;
  }
  if (typeof expected === 'string' && typeof actual === 'number') {
    return expected.trim() !== '' && Number(expected) === actual;
  }
  return false;
}

/** 列出 Agent 实际入参与用例 arguments 不一致的路径；对象键顺序不计，null 与缺省等价。 */
export function argumentDiffPaths(expected: unknown, actual: unknown, path = '', out: string[] = []): string[] {
  const label = path || '(全部入参)';
  // Consumer 事件对敏感键名脱敏，这类值无法比对
  if (out.length >= MAX_ARGUMENT_DIFFS || actual === REDACTED) return out;
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual) || actual.length !== expected.length) out.push(label);
    else expected.forEach((value, index) => argumentDiffPaths(value, actual[index], `${path}[${index}]`, out));
    return out;
  }
  if (isPlainObject(expected)) {
    if (!isPlainObject(actual)) {
      out.push(label);
      return out;
    }
    const keys = [...new Set([...Object.keys(expected), ...Object.keys(actual)])].sort();
    for (const key of keys) argumentDiffPaths(expected[key], actual[key], path ? `${path}.${key}` : key, out);
    return out;
  }
  if (!sameScalar(expected, actual)) out.push(label);
  return out;
}

function failureDetail(data: Record<string, any>): string | undefined {
  const message = text(data.message);
  if (message) return message;
  if (!Array.isArray(data.errors)) return undefined;
  const parts = data.errors
    .map((entry: any) => (typeof entry === 'string' ? text(entry) : text(entry?.message) ?? text(entry?.code)))
    .filter(Boolean)
    .slice(0, 3);
  return parts.length ? parts.join('；') : undefined;
}

function answerExcerpt(observation: AgentRunObservation): string {
  const answer = (observation.content || observation.interactionPrompt || '').replace(/\s+/gu, ' ').trim();
  return answer.slice(0, 200);
}

function notCalledResult(item: SolverValidationCase, observation: AgentRunObservation): SolverValidationCaseResult {
  const others = [...new Set([...observation.calls.values()].map((call) => call.name))];
  const excerpt = answerExcerpt(observation);
  const detail = others.length
    ? `Agent 调用了其他求解工具：${others.slice(0, 5).join('、')}`
    : `Agent 未调用求解工具${excerpt ? `，回复：${excerpt}` : ''}`;
  return { ...errorResult(item, 'solver_tool_not_called', 'agent', false, detail), solver_tool_called: false };
}

/**
 * 工具结果自称权威时，核对同一调用的 solver.result.authoritative 事件：没有事件、事件不是权威完成、
 * 或关键证据与工具结果不一致，都返回失败码。只看工具结果的 authoritative 字段会被伪造或残缺的输出蒙混。
 */
function authoritativeEventProblem(call: SolverToolCall): string | undefined {
  const event = call.authoritativeEvent;
  if (!event || event.authoritative !== true || !text(event.job_id)) return 'solver_authoritative_event_missing';
  // 工具输出超过 4 KB 被截断时没有可比对的字段，公开字段整份取自 solver.* 事件
  const output = call.output ?? {};
  const mismatched = AUTHORITATIVE_EVENT_FIELDS.some((key) => {
    const fromEvent = text(event[key]);
    const fromOutput = text(output[key]);
    return Boolean(fromEvent && fromOutput && fromEvent !== fromOutput);
  });
  return mismatched ? 'solver_authoritative_event_mismatch' : undefined;
}

const AUTHORITATIVE_EVENT_DETAILS: Record<string, string> = {
  solver_authoritative_event_missing: '没有收到该求解调用的权威结果事件',
  solver_authoritative_event_mismatch: '求解工具结果与权威结果事件的证据不一致',
};

/** 按求解工具的公开结果判定用例；工具输出缺失或被截断时用同一调用的 solver.* 事件字段补齐。 */
function solverCallResult(item: SolverValidationCase, call: SolverToolCall): SolverValidationCaseResult {
  const diff = argumentDiffPaths(item.arguments, call.arguments);
  const tool: Partial<SolverValidationCaseResult> = {
    solver_tool_called: true,
    solver_tool: call.name,
    arguments_match: diff.length === 0,
    ...(diff.length ? { argument_diff: diff } : {}),
  };
  const data: Record<string, any> = { ...call.progress, ...(call.output ?? {}) };
  const status = String(data.status ?? '');
  if (status === 'completed') {
    const problem = data.authoritative === true ? authoritativeEventProblem(call) : undefined;
    return {
      id: item.id,
      operation_key: item.operationKey,
      status: 'completed',
      authoritative: data.authoritative === true && !problem,
      ...(problem ? { code: problem, detail: AUTHORITATIVE_EVENT_DETAILS[problem] } : {}),
      ...publicFields(data),
      candidate_count: null,
      ...tool,
    };
  }
  if (status === 'awaiting_input') {
    const missing = Array.isArray(data.missing) ? data.missing.map(text).filter(Boolean).slice(0, 20) as string[] : [];
    return {
      id: item.id, operation_key: item.operationKey, status: 'awaiting_input',
      code: 'awaiting_input', phase: text(data.phase) ?? 'bind', retryable: false, authoritative: false, missing,
      ...tool,
    };
  }
  if (status === 'running' || (!status && !call.toolStatus)) {
    return { ...errorResult(item, 'solver_timeout', text(data.phase) ?? 'solve', true), ...tool };
  }
  const fallback = call.toolErrorCode ?? (call.toolStatus === 'failed' ? 'solver_tool_failed' : 'solver_result_rejected');
  return {
    ...errorResult(item, errorCode(data, fallback), text(data.phase) ?? 'unknown', data.retryable === true, failureDetail(data)),
    ...tool,
  };
}

/** 多次调用时以最后一次为准（Agent 可能补齐入参后重试）。 */
function lastMatchedCall(observation: AgentRunObservation, operationKey: string): SolverToolCall | undefined {
  return [...observation.calls.values()].filter((call) => callsOperation(call, operationKey)).pop();
}

function agentRunResult(item: SolverValidationCase, observation: AgentRunObservation): SolverValidationCaseResult {
  const call = lastMatchedCall(observation, item.operationKey);
  if (call) return solverCallResult(item, call);
  if (['failed', 'cancelled'].includes(observation.status) && !observation.calls.size) {
    return errorResult(
      item,
      observation.errorCode?.toLowerCase() ?? 'agent_run_failed',
      'agent',
      false,
      observation.errorMessage ?? `Agent 运行${observation.status === 'cancelled' ? '已取消' : '失败'}`,
    );
  }
  return notCalledResult(item, observation);
}

/**
 * 单条验证用例：向 Consumer Agent 提问（异步运行 + 事件流），从事件里取求解工具的调用入参与公开结果。
 * 只有求解工具返回 authoritative=true 的 completed、且同一调用有 solver.result.authoritative 事件佐证，
 * 才算权威结果；不可行也是有效终态。
 * Agent 未调用对应求解工具、入参与用例不一致，分别用 solver_tool_called / arguments_match 标出。
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
  now?: () => number;
}): Promise<SolverValidationCaseResult> {
  const { request, workspaceId, item, signal } = input;
  const now = input.now ?? Date.now;
  const caseDeadline = Math.min(input.deadline, now() + CASE_DEADLINE_MS);
  const controller = new AbortController();
  const onAbort = () => controller.abort(signal?.reason);
  if (signal?.aborted) throw signal.reason;
  signal?.addEventListener('abort', onAbort, { once: true });
  const timer = setTimeout(() => controller.abort(new CaseTimeout()), Math.max(0, caseDeadline - now()));
  const observation: AgentRunObservation = { status: '', content: '', calls: new Map() };
  let runId = '';
  let timedOut = false;
  try {
    runId = await startAgentRun({ ...input, signal: controller.signal });
    await followAgentRun({ request, workspaceId, runId, observation, signal: controller.signal });
  } catch (error) {
    if (signal?.aborted) {
      if (runId) await cancelAgentRun(request, workspaceId, runId, 'solver_validation_cancelled');
      throw error;
    }
    if (!(controller.signal.reason instanceof CaseTimeout)) {
      if (error instanceof CaseFailure) return errorResult(item, error.code, error.phase, error.retryable, error.detail);
      throw error;
    }
    if (runId) await cancelAgentRun(request, workspaceId, runId, 'solver_validation_timeout');
    timedOut = true;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }
  // 超时：求解工具已返回结果时仍按结果判定（仅 Agent 回复超时），未返回则记为超时
  const timedOutCall = timedOut ? lastMatchedCall(observation, item.operationKey) : undefined;
  let result: SolverValidationCaseResult;
  if (!timedOut) result = agentRunResult(item, observation);
  else if (timedOutCall) result = solverCallResult(item, timedOutCall);
  else result = errorResult(item, 'solver_timeout', runId ? 'agent' : 'admission', true);
  const jobId = text(result.job_id);
  if (result.status === 'completed' && result.authoritative && jobId && result.candidate_results_artifact_ref) {
    const candidates = await candidateCount(request, workspaceId, jobId, signal);
    result.candidate_count = candidates.count;
    if (candidates.error) result.candidate_results_error = candidates.error;
  }
  return result;
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
      results.push(errorResult(item, 'solver_timeout', 'admission', true));
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
      now,
    })));
  }
  return { package: { id: ontologyId, version }, cases: results };
}
