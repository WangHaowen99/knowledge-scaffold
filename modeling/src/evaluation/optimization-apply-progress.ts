import { getDb, nowIso } from '../db/index.js';
import type { WorkflowNode } from '../types.js';
import { getNode, listNodes, nextOrdinal, upsertNode } from '../workflow/engine.js';
import { appendTraceEvent, listTraceEvents } from '../workflow/trace.js';
import type { OptimizationApplyProgressEvent } from './optimizer.js';

export type OptimizationApplyPhase = 'prepare' | 'revision' | 'validation' | 'expert_review' | 'candidate' | 'done' | 'failed';

export interface OptimizationApplyState {
  taskType: 'optimization_apply';
  phase: OptimizationApplyPhase;
  attempt: number;
  maxAttempts: number;
  selectedCount: number;
  suggestionIds: string[];
  resourceCount: number;
  startedAt: string;
  phaseStartedAt: string;
  finishedAt?: string;
  candidateId?: string;
  suggestionCount?: number;
  error?: string;
  validationErrors?: string[];
  expertDecision?: 'retry' | 'skip';
  expertNote?: string;
}

const stateOf = (node: WorkflowNode): OptimizationApplyState => node.output as OptimizationApplyState;

function updateTask(nodeId: string, patch: Partial<OptimizationApplyState>, status: WorkflowNode['status'] = 'running'): WorkflowNode {
  const node = getNode(nodeId);
  if (!node) throw new Error('优化候选任务不存在');
  return upsertNode({
    id: node.id,
    projectId: node.projectId,
    type: 'optimize',
    status,
    ordinal: node.ordinal,
    reason: status === 'failed' ? patch.error : undefined,
    output: { ...stateOf(node), ...patch },
  });
}

function trace(node: WorkflowNode, input: {
  phase: string; kind: 'phase_started' | 'phase_completed' | 'validation_warning';
  status: 'running' | 'done' | 'warning' | 'error'; title: string;
  summary?: string; details?: Record<string, unknown>;
}) {
  appendTraceEvent({ projectId: node.projectId, nodeId: node.id, ...input });
}

export function startOptimizationApplyTask(
  projectId: string,
  suggestions: number | string[],
  resourceCount = 0,
): WorkflowNode {
  const startedAt = nowIso();
  const suggestionIds = Array.isArray(suggestions) ? suggestions : [];
  const selectedCount = Array.isArray(suggestions) ? suggestions.length : suggestions;
  const node = upsertNode({
    projectId,
    type: 'optimize',
    status: 'running',
    ordinal: nextOrdinal(projectId),
    output: {
      taskType: 'optimization_apply', phase: 'prepare', attempt: 0, maxAttempts: 2,
      selectedCount, suggestionIds, resourceCount, startedAt, phaseStartedAt: startedAt,
    } satisfies OptimizationApplyState,
  });
  trace(node, {
    phase: 'prepare', kind: 'phase_started', status: 'running', title: '检查所选优化建议',
    details: { selectedCount, resourceCount },
  });
  trace(node, {
    phase: 'prepare', kind: 'phase_completed', status: 'done', title: '优化建议检查完成',
    details: { selectedCount, resourceCount },
  });
  return node;
}

export function waitOptimizationExpertReview(
  nodeId: string,
  error: string,
  validationErrors: string[],
): WorkflowNode {
  const current = getNode(nodeId);
  if (!current) throw new Error('优化候选任务不存在');
  trace(current, {
    phase: 'expert_review', kind: 'validation_warning', status: 'warning',
    title: '自动修复两次未通过，等待专家决策', summary: error,
    details: { validationErrors },
  });
  return updateTask(nodeId, {
    phase: 'expert_review', error, validationErrors: [...new Set(validationErrors)],
    finishedAt: undefined, phaseStartedAt: nowIso(),
  }, 'waiting_user');
}

export function resolveOptimizationExpertReview(
  nodeId: string,
  decision: 'retry' | 'skip',
  note: string,
): WorkflowNode {
  const current = getNode(nodeId);
  if (!current) throw new Error('优化候选任务不存在');
  const state = stateOf(current);
  if (current.status !== 'waiting_user' || state.phase !== 'expert_review') {
    throw new Error('当前优化任务不在等待专家决策状态');
  }
  trace(current, {
    phase: 'expert_review', kind: 'phase_completed', status: 'done',
    title: decision === 'retry' ? '专家确认带反馈重试' : '专家确认跳过所选建议',
    summary: note,
    details: { decision },
  });
  return updateTask(nodeId, {
    phase: 'done', expertDecision: decision, expertNote: note,
    finishedAt: nowIso(), phaseStartedAt: nowIso(),
  }, 'done');
}

export function recordOptimizationApplyProgress(nodeId: string, event: OptimizationApplyProgressEvent): WorkflowNode {
  const phaseStartedAt = nowIso();
  const node = updateTask(nodeId, {
    phase: event.phase === 'revision_started' ? 'revision' : 'validation',
    attempt: event.attempt,
    maxAttempts: event.maxAttempts,
    phaseStartedAt,
    error: event.error,
  });
  if (event.phase === 'revision_started') {
    trace(node, {
      phase: 'revision', kind: 'phase_started', status: 'running',
      title: `模型正在修改知识包（第 ${event.attempt}/${event.maxAttempts} 次）`,
      details: { attempt: event.attempt, maxAttempts: event.maxAttempts },
    });
  } else {
    trace(node, {
      phase: 'validation', kind: 'phase_started', status: 'running',
      title: `校验修改结果（第 ${event.attempt}/${event.maxAttempts} 次）`,
      details: { attempt: event.attempt, maxAttempts: event.maxAttempts },
    });
    trace(node, event.phase === 'validation_passed' ? {
      phase: 'validation', kind: 'phase_completed', status: 'done', title: '修改结果校验通过',
      details: { attempt: event.attempt, maxAttempts: event.maxAttempts },
    } : {
      phase: 'validation', kind: 'validation_warning', status: 'warning',
      title: '修改结果未通过校验，准备自动修正', summary: event.error,
      details: { attempt: event.attempt, maxAttempts: event.maxAttempts },
    });
  }
  return node;
}

export function markOptimizationCandidateSaving(nodeId: string): WorkflowNode {
  const node = updateTask(nodeId, { phase: 'candidate', phaseStartedAt: nowIso(), error: undefined });
  trace(node, { phase: 'candidate', kind: 'phase_started', status: 'running', title: '保存待确认候选修订' });
  return node;
}

export function completeOptimizationCandidateTask(
  nodeId: string,
  candidateId: string,
  suggestionCount: number,
): WorkflowNode {
  const current = getNode(nodeId);
  if (!current) throw new Error('优化候选任务不存在');
  trace(current, {
    phase: 'candidate', kind: 'phase_completed', status: 'done', title: '待确认候选修订已保存',
    details: { candidateId, suggestionCount },
  });
  return updateTask(nodeId, {
    phase: 'done', candidateId, suggestionCount,
    finishedAt: nowIso(), phaseStartedAt: nowIso(), error: undefined,
  }, 'done');
}

export function failOptimizationApplyTask(nodeId: string, error: string): WorkflowNode {
  const current = getNode(nodeId);
  if (!current) throw new Error('优化候选任务不存在');
  trace(current, {
    phase: stateOf(current).phase, kind: 'phase_completed', status: 'error',
    title: '优化候选生成失败', summary: error,
  });
  return updateTask(nodeId, {
    phase: 'failed', error, finishedAt: nowIso(), phaseStartedAt: nowIso(),
  }, 'failed');
}

export function latestOptimizationApplyTask(projectId: string) {
  const node = [...listNodes(projectId)].reverse()
    .find((candidate) => candidate.type === 'optimize'
      && (candidate.output as any)?.taskType === 'optimization_apply');
  return node ? { node, events: listTraceEvents(node.id) } : null;
}

export function failInterruptedOptimizationApplyTasks(): number {
  const rows = getDb().prepare(`SELECT id, output_json FROM wf_nodes
    WHERE type = 'optimize' AND status = 'running'`).all() as { id: string; output_json?: string }[];
  const interrupted = rows.filter((row) => {
    try { return JSON.parse(row.output_json || '{}').taskType === 'optimization_apply'; }
    catch { return false; }
  });
  for (const row of interrupted) {
    failOptimizationApplyTask(row.id, '服务重启导致本次优化候选生成中断，请重新生成候选修订');
  }
  return interrupted.length;
}
