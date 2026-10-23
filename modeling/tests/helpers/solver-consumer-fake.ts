import type { ConsumerRequest } from '../../src/evaluation/shadow-workspace.js';
import type { PackageSpec } from '../../src/types.js';

// 合成 Consumer：只模拟求解契约验证用到的接口（工作空间、导入、激活、数据源、
// Agent 运行与事件流、候选结果）。知识包、批次、工作空间与引用全部为测试虚构。

export const ONTOLOGY_ID = 'synthetic-route-package';
export const VERSION = '0.1.3';
export const WORKSPACE_ID = 'synthetic-ws';

export const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

export function batches(count: number) {
  return {
    plan_month: '2000-01',
    items: Array.from({ length: count }, (_, index) => ({ item_order: index + 1, item_id: `B${index + 1}` })),
  };
}

/** 合成 Agent 读得懂的提问：内嵌求解操作与入参，合成 Agent 据此调用求解工具。 */
export function solverCase(id: string, args: Record<string, unknown>, operationKey = 'plan_route') {
  return {
    id,
    operation_key: operationKey,
    question: JSON.stringify({ operation_key: operationKey, arguments: args }),
    arguments: args,
  };
}

/** 与 Consumer 精确卡名同形：solve_<operation_key>_<契约摘要前 8 位>。 */
export function cardName(operationKey: string): string {
  return `solve_${operationKey}_0a1b2c3d`;
}

/** Consumer AuthoritativeResult 的公开字段；不可行时去掉候选引用即可模拟「没有候选」。 */
export function authoritativeResult(jobId: string, extra: Record<string, unknown> = {}) {
  return {
    status: 'completed',
    authoritative: true,
    run_id: `run-${jobId}`,
    job_id: jobId,
    feasibility: 'feasible',
    optimality: 'proven_optimal',
    proof_kind: 'exact_search',
    summary: { selected: 1 },
    result_artifact_ref: `artifact://result/${jobId}`,
    verification_evidence_ref: `artifact://evidence/${jobId}`,
    knowledge_revision_id: VERSION,
    fact_snapshot_digest: 'sha256:synthetic-facts',
    problem_digest: `sha256:synthetic-problem-${jobId}`,
    candidate_results_artifact_ref: `artifact://candidates/${jobId}`,
    candidate_results_artifact_digest: `sha256:synthetic-candidates-${jobId}`,
    ...extra,
  };
}

export function infeasibleResult(jobId: string, extra: Record<string, unknown> = {}) {
  return authoritativeResult(jobId, {
    feasibility: 'infeasible', optimality: 'not_applicable', proof_kind: 'infeasibility_certificate', ...extra,
  });
}

type SyntheticEvent = Record<string, unknown> & { event_type: string };

function withoutUndefined(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined));
}

/**
 * 一次求解工具调用的事件序列，与 Consumer 的顺序一致：
 * tool.started →（Verifier 通过时）solver.result.authoritative → tool.finished → turn.finished。
 */
export function solverToolEvents(options: {
  operationKey: string;
  arguments: unknown;
  result: Record<string, unknown>;
  toolCallId?: string;
  toolName?: string;
  toolStatus?: string;
  /** false 表示 Consumer 没有发出权威结果事件；对象表示改写事件载荷。 */
  authoritativeEvent?: Record<string, unknown> | false;
  outputPreview?: unknown;
  content?: string;
}): SyntheticEvent[] {
  const toolCallId = options.toolCallId ?? 'call-1';
  const toolName = options.toolName ?? cardName(options.operationKey);
  const result = withoutUndefined(options.result);
  const events: SyntheticEvent[] = [
    {
      event_type: 'tool.started',
      payload: { name: toolName, tool_call_id: toolCallId, input_preview: JSON.stringify(options.arguments) },
    },
    {
      event_type: 'solver.progress',
      payload: { tool_call_id: toolCallId, tool_name: toolName, job_id: result.job_id, phase: 'solve', status: 'running' },
    },
  ];
  if (result.authoritative === true && options.authoritativeEvent !== false) {
    events.push({
      event_type: 'solver.result.authoritative',
      payload: { ...result, tool_call_id: toolCallId, tool_name: toolName, ...(options.authoritativeEvent ?? {}) },
    });
  }
  events.push(
    {
      event_type: 'tool.finished',
      payload: {
        tool_call_id: toolCallId,
        status: options.toolStatus ?? 'succeeded',
        output_preview: options.outputPreview ?? JSON.stringify(result),
      },
    },
    { event_type: 'turn.finished', payload: { status: 'completed', content: options.content ?? '已完成求解。' } },
  );
  return events;
}

export function agentReplyEvents(content: string): SyntheticEvent[] {
  return [{ event_type: 'turn.finished', payload: { status: 'completed', content } }];
}

export interface ConsumerCall {
  path: string;
  method: string;
  headers: Record<string, string>;
  workspaceId?: string;
  body?: any;
}

export interface AgentAsk {
  operationKey: string;
  arguments: Record<string, any>;
  idempotencyKey: string;
  body: any;
}

export type AgentScript =
  | { admission: { status: number; body: unknown } }
  | { events: SyntheticEvent[] };

/** 默认的合成 Agent：照提问原样调用求解工具，Consumer 给出可行的权威结果。 */
export function obedientAgent(ask: AgentAsk): AgentScript {
  return {
    events: solverToolEvents({
      operationKey: ask.operationKey,
      arguments: ask.arguments,
      result: authoritativeResult(`job-${ask.idempotencyKey}`),
    }),
  };
}

/** SSE 帧格式与 Consumer /agentic/runs/{id}/events 一致：retry、心跳注释、id/event/data。 */
function sse(runId: string, events: SyntheticEvent[]): Response {
  const frames = [
    'retry: 2000\n\n',
    `: heartbeat ${JSON.stringify({ run_id: runId, last_seq: 0 })}\n\n`,
    ...events.map((event) => `id: ${event.seq}\nevent: ${event.event_type}\ndata: ${JSON.stringify(event)}\n\n`),
  ];
  return new Response(frames.join(''), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

/** 按「N 个批次 → 2N 个规范候选」记账；权威结果事件出现时登记候选数。 */
export function fakeConsumer(options: {
  agent?: (ask: AgentAsk) => AgentScript | undefined;
  candidates?: (jobId: string) => { status?: number; body: unknown } | undefined;
  importZip?: () => Response | undefined;
  workspaceId?: string;
} = {}) {
  const calls: ConsumerCall[] = [];
  const runs = new Map<string, SyntheticEvent[]>();
  const counts = new Map<string, number>();
  const request: ConsumerRequest = async (_token, path, init = {}) => {
    const method = String(init.method ?? 'GET');
    const headers = { ...(init.headers as Record<string, string> ?? {}) };
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, headers, workspaceId: init.workspaceId, body });
    if (path === '/agentic/workspaces' && method === 'GET') {
      return json({ workspaces: [{ id: options.workspaceId ?? WORKSPACE_ID }] });
    }
    if (path === '/agentic/ontologies/import-zip') {
      return options.importZip?.() ?? json({ ontology_id: ONTOLOGY_ID, objects_count: 0 });
    }
    if (path.endsWith('/active-version')) return json({ active_version: body?.version });
    if (path.endsWith('/data-source')) return json({ ok: true });
    if (path === '/agentic/runs' && method === 'POST') {
      const asked = JSON.parse(body.request);
      const ask: AgentAsk = {
        operationKey: asked.operation_key,
        arguments: asked.arguments,
        idempotencyKey: headers['Idempotency-Key'],
        body,
      };
      const script = options.agent?.(ask) ?? obedientAgent(ask);
      if ('admission' in script) return json(script.admission.body, script.admission.status);
      const runId = `agent-run-${runs.size + 1}`;
      const events = script.events.map((event, index) => ({ ...event, seq: index + 1, run_id: runId }));
      runs.set(runId, events);
      for (const event of events) {
        const jobId = (event.payload as any)?.job_id;
        if (event.event_type === 'solver.result.authoritative' && jobId && !counts.has(jobId)) {
          counts.set(jobId, 2 * (ask.arguments?.items?.length ?? 0));
        }
      }
      return json({ run_id: runId, status: 'queued' }, 202);
    }
    const stream = /^\/agentic\/runs\/([^/]+)\/events\?after_seq=(\d+)$/.exec(path);
    if (stream) {
      const events = runs.get(decodeURIComponent(stream[1]));
      if (!events) return json({ error: { error_code: 'RUN_NOT_FOUND', retriable: false } }, 404);
      return sse(stream[1], events.filter((event) => Number(event.seq) > Number(stream[2])));
    }
    if (/^\/agentic\/runs\/[^/]+\/cancel$/.test(path)) return json({ ok: true });
    const candidates = /^\/api\/solver\/history\/([^/]+)\/candidate-results$/.exec(path);
    if (candidates) {
      const jobId = decodeURIComponent(candidates[1]);
      const outcome = options.candidates?.(jobId) ?? { body: { candidate_count: counts.get(jobId) } };
      return json(outcome.body, outcome.status ?? 200);
    }
    return json({ error: 'not found' }, 404);
  };
  return { request, calls, counts };
}

export const syntheticDataSource = {
  projectId: 'pj_synthetic',
  mode: 'none' as const,
  workspaceId: WORKSPACE_ID,
  config: {},
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
};

/** 验证路由只从 ZIP 里读包 ID 与版本，合成包不需要对象与操作。 */
export function packageSpec(): PackageSpec {
  return {
    id: ONTOLOGY_ID,
    name: '合成求解知识包',
    description: '用于验证求解契约端到端接口的合成知识包',
    version: VERSION,
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
}

export function multipartBody(fields: Record<string, string>, zip?: Uint8Array) {
  const boundary = 'solver-validation-test-boundary';
  const parts: Buffer[] = Object.entries(fields).map(([name, value]) => Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`,
  ));
  if (zip) {
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="package.zip"\r\n`
      + 'Content-Type: application/zip\r\n\r\n',
    ));
    parts.push(Buffer.from(zip));
    parts.push(Buffer.from('\r\n'));
  }
  parts.push(Buffer.from(`--${boundary}--\r\n`));
  return { payload: Buffer.concat(parts), contentType: `multipart/form-data; boundary=${boundary}` };
}
