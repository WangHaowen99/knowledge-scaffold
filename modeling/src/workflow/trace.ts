import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { TraceKind, TraceStatus, WorkflowTraceEvent } from '../types.js';
import {
  extractToolValidationError,
  normalizeAgentStopReason,
  sanitizeToolValidationError,
  type AgentCaptureErrorKind,
  type AgentCaptureOutcome,
} from './agent-capture-diagnostics.js';

const SECRET_KEY = /api.?key|token|secret|password/i;
const PRIVATE_CONTENT_KEY = /reasoning|thinking|content|textContent/i;
const SAFE_TOKEN_COUNT_KEYS = new Set(['inputTokens', 'outputTokens']);
const CAPTURE_ERROR_KINDS = new Set<AgentCaptureErrorKind>([
  'aborted',
  'context_length_exceeded',
  'provider_error',
  'request_timeout',
]);
const KNOWN_MODEL_APIS = new Set([
  'openai-completions',
  'mistral-conversations',
  'openai-responses',
  'azure-openai-responses',
  'openai-codex-responses',
  'anthropic-messages',
  'bedrock-converse-stream',
  'google-generative-ai',
  'google-vertex',
  'custom',
]);
const TOOL_CHOICE_MODES = new Set(['named', 'required', 'auto']);

export type TraceEventDraft = Pick<WorkflowTraceEvent, 'phase' | 'kind' | 'status' | 'title'>
  & Pick<Partial<WorkflowTraceEvent>, 'summary' | 'details'>;

const TOOL_LABELS: Record<string, string> = {
  list_tables: '列出数据表',
  describe_table: '查看表字段',
  list_documents: '列出文档',
  read_document: '读取文档',
  search_documents: '检索文档',
  submit_analysis: '提交领域分析',
  submit_document_chunk_analysis: '提交文档分段分析',
  submit_document_analysis: '提交文档汇总分析',
  submit_material_analysis_result: '提交领域资料分析',
  submit_objects: '提交业务对象',
  submit_rules_metrics: '提交规则和指标',
  submit_operations: '提交业务操作',
};

const CAPTURE_OUTCOME_TITLES: Record<AgentCaptureOutcome, string> = {
  captured: '结构化提交已捕获',
  model_error: '模型调用失败',
  output_truncated: '结构化提交输出被截断',
  tool_validation_failed: '结构化提交参数校验失败',
  tool_not_called: '模型未调用结构化提交工具',
};

function nonNegativeInteger(value: unknown): number {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? Math.floor(number) : 0;
}

function sanitizeValue(value: unknown): unknown {
  if (typeof value === 'string') return value.slice(0, 500);
  if (Array.isArray(value)) return value.slice(0, 20).map(sanitizeValue);
  if (value && typeof value === 'object') return sanitizeTraceDetails(value as Record<string, unknown>);
  return value;
}

export function sanitizeTraceDetails(input: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(input).flatMap(([key, value]) => {
    if (SAFE_TOKEN_COUNT_KEYS.has(key) && typeof value === 'number'
      && Number.isFinite(value) && value >= 0) {
      return [[key, Math.floor(value)]];
    }
    if (SECRET_KEY.test(key)) return [[key, '[已隐藏]']];
    if (PRIVATE_CONTENT_KEY.test(key)) return [];
    return [[key, sanitizeValue(value)]];
  }));
}

function toolDetails(toolName: string, args: Record<string, unknown>): Record<string, unknown> {
  const details: Record<string, unknown> = { toolName };
  for (const key of ['filename', 'offset', 'table', 'query']) {
    if (args[key] !== undefined) details[key] = args[key];
  }
  if (toolName === 'submit_document_chunk_analysis') {
    details.evidenceCount = Array.isArray(args.evidence) ? args.evidence.length : 0;
    details.candidateCount = Array.isArray(args.candidates) ? args.candidates.length : 0;
  }
  if (toolName === 'submit_document_analysis') {
    details.candidateCount = Array.isArray(args.candidates) ? args.candidates.length : 0;
  }
  if (toolName === 'submit_material_analysis_result') {
    details.candidateCount = Array.isArray(args.candidateObjects) ? args.candidateObjects.length : 0;
    details.questionCount = Array.isArray(args.openQuestions) ? args.openQuestions.length : 0;
  }
  if (toolName === 'submit_objects') {
    details.objectCount = Array.isArray(args.objects) ? args.objects.length : 0;
  }
  if (toolName === 'submit_rules_metrics') {
    details.ruleCount = Array.isArray(args.rules) ? args.rules.length : 0;
    details.metricCount = Array.isArray(args.metrics) ? args.metrics.length : 0;
  }
  if (toolName === 'submit_operations') {
    details.operationCount = Array.isArray(args.operations) ? args.operations.length : 0;
  }
  return details;
}

function toolSummary(toolName: string, details: Record<string, unknown>): string | undefined {
  if (toolName === 'read_document') return `${details.filename ?? '文档'} · offset ${details.offset ?? 0}`;
  if (toolName === 'search_documents') return String(details.query ?? '').slice(0, 200) || undefined;
  if (toolName === 'describe_table') return String(details.table ?? '').slice(0, 200) || undefined;
  if (toolName === 'submit_document_chunk_analysis') return `证据 ${details.evidenceCount ?? 0} · 候选 ${details.candidateCount ?? 0}`;
  if (toolName === 'submit_document_analysis') return `文档候选 ${details.candidateCount ?? 0}`;
  if (toolName === 'submit_material_analysis_result') return `候选对象 ${details.candidateCount ?? 0} · 开放问题 ${details.questionCount ?? 0}`;
  if (toolName === 'submit_objects') return `业务对象 ${details.objectCount ?? 0}`;
  if (toolName === 'submit_rules_metrics') return `规则 ${details.ruleCount ?? 0} · 指标 ${details.metricCount ?? 0}`;
  if (toolName === 'submit_operations') return `业务操作 ${details.operationCount ?? 0}`;
  return undefined;
}

export function traceEventFromAgentEvent(event: any): TraceEventDraft | null {
  if (event?.type === 'tool_choice_fallback') {
    const fromMode = typeof event.fromMode === 'string' && TOOL_CHOICE_MODES.has(event.fromMode)
      ? event.fromMode
      : undefined;
    const toMode = typeof event.toMode === 'string' && TOOL_CHOICE_MODES.has(event.toMode)
      ? event.toMode
      : undefined;
    if (!fromMode || !toMode) return null;
    const modelApi = typeof event.modelApi === 'string' && KNOWN_MODEL_APIS.has(event.modelApi)
      ? event.modelApi
      : 'custom';
    const toolName = typeof event.targetToolName === 'string' && event.targetToolName.trim()
      ? event.targetToolName.trim().slice(0, 200)
      : 'unknown';
    return {
      phase: 'agent',
      kind: 'validation_warning',
      status: 'warning',
      title: '模型工具调用模式已自动切换',
      summary: '兼容性切换已在当前任务内完成，资料分析继续运行',
      details: { fromMode, toMode, modelApi, toolName },
    };
  }
  if (event?.type === 'capture_attempt_end') {
    const diagnostics = event.diagnostics;
    if (!diagnostics || typeof diagnostics !== 'object') return null;
    const outcome = diagnostics.outcome as AgentCaptureOutcome;
    if (!Object.hasOwn(CAPTURE_OUTCOME_TITLES, outcome)) return null;
    const attempt = Math.max(1, nonNegativeInteger(diagnostics.attempt));
    const toolName = typeof diagnostics.targetToolName === 'string'
      ? diagnostics.targetToolName.slice(0, 200)
      : 'unknown';
    const stopReason = normalizeAgentStopReason(diagnostics.stopReason);
    const validationError = sanitizeToolValidationError(diagnostics.validationError);
    const modelApi = typeof diagnostics.modelApi === 'string' && KNOWN_MODEL_APIS.has(diagnostics.modelApi)
      ? diagnostics.modelApi
      : undefined;
    const errorKind = typeof diagnostics.errorKind === 'string'
      && CAPTURE_ERROR_KINDS.has(diagnostics.errorKind as AgentCaptureErrorKind)
      ? diagnostics.errorKind as AgentCaptureErrorKind
      : undefined;
    const toolChoiceMode = typeof diagnostics.toolChoiceMode === 'string'
      && TOOL_CHOICE_MODES.has(diagnostics.toolChoiceMode)
      ? diagnostics.toolChoiceMode
      : undefined;
    return {
      phase: 'agent',
      kind: 'capture_attempt',
      status: outcome === 'captured' ? 'done' : outcome === 'model_error' ? 'error' : 'warning',
      title: CAPTURE_OUTCOME_TITLES[outcome],
      summary: `第 ${attempt} 次结构化提交尝试`,
      details: {
        attempt,
        outcome,
        toolName,
        ...(stopReason ? { stopReason } : {}),
        targetToolStartedCount: nonNegativeInteger(diagnostics.targetToolStartedCount),
        targetToolSucceededCount: nonNegativeInteger(diagnostics.targetToolSucceededCount),
        targetToolFailedCount: nonNegativeInteger(diagnostics.targetToolFailedCount),
        inputTokens: nonNegativeInteger(diagnostics.inputTokens),
        outputTokens: nonNegativeInteger(diagnostics.outputTokens),
        ...(validationError ? { validationError } : {}),
        ...(modelApi ? { modelApi } : {}),
        ...(errorKind ? { errorKind } : {}),
        ...(toolChoiceMode ? { toolChoiceMode } : {}),
      },
    };
  }
  if (event?.type === 'tool_execution_start') {
    const toolName = String(event.toolName ?? 'unknown');
    const details = toolDetails(toolName, (event.args ?? {}) as Record<string, unknown>);
    return {
      phase: 'agent',
      kind: 'tool_started',
      status: 'running',
      title: TOOL_LABELS[toolName] ?? toolName,
      summary: toolSummary(toolName, details),
      details,
    };
  }
  if (event?.type === 'tool_execution_end') {
    const toolName = String(event.toolName ?? 'unknown');
    const isError = event.isError === true;
    const validationError = isError ? extractToolValidationError(event.result) : undefined;
    return {
      phase: 'agent',
      kind: 'tool_completed',
      status: isError ? 'error' : 'done',
      title: `${TOOL_LABELS[toolName] ?? toolName}${isError ? '失败' : '完成'}`,
      details: {
        toolName,
        isError,
        ...(validationError ? { validationError } : {}),
      },
    };
  }
  if (event?.type === 'turn_end') {
    const stopReason = normalizeAgentStopReason(event.message?.stopReason);
    if (stopReason === 'length') {
      const inputTokens = Number(event.message?.usage?.input);
      const outputTokens = Number(event.message?.usage?.output);
      return {
        phase: 'agent',
        kind: 'validation_warning',
        status: 'warning',
        title: '模型输出达到长度上限',
        details: {
          stopReason,
          ...(Number.isFinite(inputTokens) && inputTokens >= 0 ? { inputTokens: Math.floor(inputTokens) } : {}),
          ...(Number.isFinite(outputTokens) && outputTokens >= 0 ? { outputTokens: Math.floor(outputTokens) } : {}),
        },
      };
    }
    const content = Array.isArray(event.message?.content) ? event.message.content : [];
    const textBlocks = content.filter((block: any) => (
      block?.type === 'text' && typeof block.text === 'string' && block.text.trim()
    ));
    if (!textBlocks.length) return null;
    return {
      phase: 'agent',
      kind: 'model_summary',
      status: 'done',
      title: '模型公开说明',
      summary: '模型本轮返回了文本说明',
      details: {
        textBlockCount: textBlocks.length,
        textCharCount: textBlocks.reduce((total: number, block: any) => total + block.text.length, 0),
      },
    };
  }
  return null;
}

export function appendTraceEvent(input: {
  projectId: string;
  nodeId: string;
  phase: string;
  kind: TraceKind;
  status: TraceStatus;
  title: string;
  summary?: string;
  details?: Record<string, unknown>;
}): WorkflowTraceEvent {
  const db = getDb();
  const next = db.prepare(
    'SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM workflow_trace_events WHERE node_id = ?',
  ).get(input.nodeId) as { seq: number };
  const details = input.details ? sanitizeTraceDetails(input.details) : undefined;
  const event: WorkflowTraceEvent = {
    ...input,
    id: `tr_${randomUUID().slice(0, 12)}`,
    seq: next.seq,
    details,
    createdAt: nowIso(),
  };
  db.prepare(`INSERT INTO workflow_trace_events
    (id, project_id, node_id, seq, phase, kind, status, title, summary, details_json, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`)
    .run(
      event.id,
      event.projectId,
      event.nodeId,
      event.seq,
      event.phase,
      event.kind,
      event.status,
      event.title,
      event.summary ?? null,
      event.details ? JSON.stringify(event.details) : null,
      event.createdAt,
    );
  return event;
}

export function listTraceEvents(nodeId: string): WorkflowTraceEvent[] {
  const rows = getDb().prepare(
    'SELECT * FROM workflow_trace_events WHERE node_id = ? ORDER BY seq',
  ).all(nodeId) as any[];
  return rows.map((row) => ({
    id: row.id,
    projectId: row.project_id,
    nodeId: row.node_id,
    seq: row.seq,
    phase: row.phase,
    kind: row.kind,
    status: row.status,
    title: row.title,
    summary: row.summary ?? undefined,
    details: row.details_json ? JSON.parse(row.details_json) : undefined,
    createdAt: row.created_at,
  }));
}
