import { getDb } from '../db/index.js';
import type { WorkflowTraceEvent } from '../types.js';

export interface PerformanceEvent extends WorkflowTraceEvent {
  durationMs?: number;
  toolName?: string;
  inputTokens?: number;
  outputTokens?: number;
}

export interface PerformanceRunSummary {
  runId: string;
  projectId: string;
  status: string;
  startedAt?: string;
  finishedAt?: string;
  durationMs: number;
  eventCount: number;
  errorCount: number;
  retryCount: number;
  inputTokens: number;
  outputTokens: number;
  cacheHits: number;
  phases: Array<{ phase: string; durationMs: number; eventCount: number; errorCount: number }>;
  units: Array<Record<string, unknown>>;
}

function json(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'string') return {};
  try { return JSON.parse(value) as Record<string, unknown>; } catch { return {}; }
}

function number(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : 0;
}

function readEvents(nodeId: string): PerformanceEvent[] {
  const rows = getDb().prepare('SELECT * FROM workflow_trace_events WHERE node_id = ? ORDER BY seq').all(nodeId) as any[];
  const active = new Map<string, PerformanceEvent>();
  return rows.map((row) => {
    const details = json(row.details_json);
    const event: PerformanceEvent = {
      id: row.id, projectId: row.project_id, nodeId: row.node_id, seq: row.seq,
      phase: row.phase, kind: row.kind, status: row.status, title: row.title,
      summary: row.summary ?? undefined, details: Object.keys(details).length ? details : undefined,
      createdAt: row.created_at,
      toolName: typeof details.toolName === 'string' ? details.toolName : undefined,
      inputTokens: number(details.inputTokens), outputTokens: number(details.outputTokens),
    };
    const key = event.toolName || event.title;
    if (event.status === 'running') active.set(key, event);
    else if ((event.kind === 'tool_completed' || event.kind.endsWith('completed')) && active.has(key)) {
      const start = active.get(key)!;
      const duration = Date.parse(event.createdAt) - Date.parse(start.createdAt);
      if (duration >= 0) { event.durationMs = duration; start.durationMs = duration; }
      active.delete(key);
    }
    return event;
  });
}

function units(projectId: string, runId: string): Array<Record<string, unknown>> {
  const rows = getDb().prepare(`SELECT unit_id, batch_index, phase, status, target_ids_json,
      created_at, updated_at, issues_json, attempt_count
    FROM generation_batch_checkpoints WHERE project_id = ? AND run_id = ? ORDER BY batch_index`).all(projectId, runId) as any[];
  return rows.map((row) => ({
    unitId: row.unit_id || `batch-${row.batch_index}`,
    batchIndex: row.batch_index,
    phase: row.phase,
    status: row.status,
    targetIds: row.target_ids_json ? JSON.parse(row.target_ids_json) : [],
    startedAt: row.created_at,
    completedAt: row.status === 'running' ? undefined : row.updated_at,
    durationMs: row.created_at && row.status !== 'running' ? Math.max(0, Date.parse(row.updated_at) - Date.parse(row.created_at)) : 0,
    retryCount: Math.max(0, number(row.attempt_count) - 1),
    error: row.issues_json ? (JSON.parse(row.issues_json)[0] || undefined) : undefined,
  }));
}

export function getPerformanceRun(projectId: string, runId: string): PerformanceRunSummary | null {
  const run = getDb().prepare('SELECT id, project_id, status, state_json, created_at, updated_at, finished_at FROM authoring_runs WHERE id = ? AND project_id = ?').get(runId, projectId) as any;
  if (!run) return null;
  const state = json(run.state_json);
  const nodeId = typeof state.nodeId === 'string' ? state.nodeId : runId;
  const events = readEvents(nodeId);
  const timestamps = events.map((e) => Date.parse(e.createdAt)).filter(Number.isFinite);
  const startedAt = timestamps.length ? new Date(Math.min(...timestamps)).toISOString() : run.created_at;
  const finishedAt = run.status === 'running' ? undefined : (run.finished_at || run.updated_at);
  const durationMs = finishedAt ? Math.max(0, Date.parse(finishedAt) - Date.parse(startedAt)) : Math.max(0, Date.now() - Date.parse(startedAt));
  const phaseMap = new Map<string, { durationMs: number; eventCount: number; errorCount: number; first: number; last: number }>();
  for (const event of events) {
    const t = Date.parse(event.createdAt);
    const p = phaseMap.get(event.phase) ?? { durationMs: 0, eventCount: 0, errorCount: 0, first: t, last: t };
    p.eventCount += 1; if (event.status === 'error') p.errorCount += 1;
    p.first = Math.min(p.first, t); p.last = Math.max(p.last, t); p.durationMs = Math.max(0, p.last - p.first);
    phaseMap.set(event.phase, p);
  }
  const details = events.map((e) => e.details ?? {});
  const generationUnits = units(projectId, runId);
  return {
    runId, projectId, status: run.status, startedAt, finishedAt, durationMs,
    eventCount: events.length,
    errorCount: events.filter((e) => e.status === 'error').length,
    retryCount: details.reduce((sum, d) => {
      const attempt = number(d.attempt);
      return sum + (attempt > 1 ? attempt - 1 : number(d.retryCount));
    }, 0) + generationUnits.reduce((sum, unit) => sum + number(unit.retryCount), 0),
    inputTokens: details.reduce((sum, d) => sum + number(d.inputTokens), 0),
    outputTokens: details.reduce((sum, d) => sum + number(d.outputTokens), 0),
    cacheHits: details.reduce((sum, d) => sum + (d.cacheHit === true ? 1 : number(d.cacheHits)), 0),
    phases: [...phaseMap.entries()].map(([phase, p]) => ({ phase, durationMs: p.durationMs, eventCount: p.eventCount, errorCount: p.errorCount })),
    units: generationUnits,
  };
}

export function listPerformanceRuns(projectId: string, limit = 20): PerformanceRunSummary[] {
  const rows = getDb().prepare('SELECT id FROM authoring_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?').all(projectId, Math.max(1, Math.min(100, limit))) as any[];
  return rows.map((row) => getPerformanceRun(projectId, row.id)).filter(Boolean) as PerformanceRunSummary[];
}

export function getPerformanceEvents(projectId: string, runId: string): PerformanceEvent[] | null {
  const run = getDb().prepare('SELECT state_json FROM authoring_runs WHERE id = ? AND project_id = ?').get(runId, projectId) as { state_json?: string } | undefined;
  if (!run) return null;
  const state = json(run.state_json);
  return readEvents(typeof state.nodeId === 'string' ? state.nodeId : runId);
}
