import {
  modelingServiceFetch,
  type ModelingServiceRequestOptions,
} from '../consumer/client.js';

export interface EvaluationExecutionTrace {
  runId?: string;
  sessionId?: string;
  status?: 'complete' | 'unavailable';
  error?: string;
  toolCalls: string[];
  toolFailures?: { toolName: string; error: string }[];
}

export interface FetchEvaluationRunTraceOptions {
  workspaceId: string;
  sessionId: string;
  runId: string;
  serviceOptions?: ModelingServiceRequestOptions;
  retryDelaysMs?: number[];
}

const sleep = (delayMs: number) => new Promise((resolve) => setTimeout(resolve, delayMs));

export function traceFromSyncResponse(data: any): EvaluationExecutionTrace | undefined {
  const rawCalls = data?.execution_trace?.tool_calls;
  if (!Array.isArray(rawCalls)) return undefined;
  const rawFailures = data?.execution_trace?.tool_failures;
  const toolFailures = Array.isArray(rawFailures)
    ? rawFailures.flatMap((item: any) => {
      const toolName = String(item?.tool_name || '').trim();
      const error = String(item?.error || '').trim();
      return toolName && error ? [{ toolName, error }] : [];
    })
    : [];
  const trace: EvaluationExecutionTrace = {
    sessionId: String(data.session_id || '') || undefined,
    runId: String(data.run_id || '') || undefined,
    status: 'complete',
    toolCalls: rawCalls.map((item: unknown) => String(item || '')).filter(Boolean),
  };
  if (toolFailures.length) trace.toolFailures = toolFailures;
  return trace;
}

export async function fetchEvaluationRunTrace({
  workspaceId,
  sessionId,
  runId,
  serviceOptions,
  retryDelaysMs = [250, 750],
}: FetchEvaluationRunTraceOptions): Promise<EvaluationExecutionTrace> {
  const fallback = (error: string): EvaluationExecutionTrace => ({
    sessionId,
    runId: runId || undefined,
    status: 'unavailable',
    error,
    toolCalls: [],
  });
  if (!runId) return fallback('Consumer 未返回 run_id');

  const path = `/agentic/internal/modeling/performance/runs/${encodeURIComponent(runId)}`
    + `?session_id=${encodeURIComponent(sessionId)}`;
  let lastError = '';
  for (let attempt = 0; attempt <= retryDelaysMs.length; attempt += 1) {
    try {
      const response = await modelingServiceFetch(
        path,
        { workspaceId },
        serviceOptions,
      );
      const data = (await response.json().catch(() => ({}))) as any;
      if (response.ok) {
        const toolCalls = Array.isArray(data.tools)
          ? data.tools.map((item: any) => String(item?.labels?.tool_name || '')).filter(Boolean)
          : [];
        return {
          sessionId,
          runId,
          status: 'complete',
          toolCalls,
        };
      }
      lastError = `Trace 接口返回 ${response.status}：${data.error || data.detail || '未知错误'}`;
      if (![404, 429, 503].includes(response.status)) break;
    } catch (error) {
      lastError = `Trace 请求失败：${error instanceof Error ? error.message : error}`;
    }
    const delay = retryDelaysMs[attempt];
    if (delay !== undefined) await sleep(delay);
  }
  return fallback(lastError || 'Trace 获取失败');
}
