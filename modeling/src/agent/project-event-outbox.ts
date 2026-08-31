import { config } from '../config.js';
import { getDb, nowIso } from '../db/index.js';
import type { DatabaseSync } from 'node:sqlite';

export interface MaterialParseProjectEvent {
  eventId: string;
  status: 'parsing' | 'parsed' | 'failed';
  documentId: string;
  sourceKey: string;
  filename: string;
  revisionNo: number;
  purpose: 'auto' | 'goal' | 'schema' | 'term' | 'evidence';
  readingMode: 'on_demand' | 'full';
  autoContinue?: boolean;
  uploadBatchId?: string;
  uploadBatchSize?: number;
  parseError?: string;
  changeKind?: 'added' | 'replaced' | 'deleted' | 'format_only';
  semanticChanged?: boolean;
}

export interface StudioReviewProjectEvent {
  eventId: string;
  clientMutationId: string;
  requestHash: string;
  workingRevision: string;
  changedPaths: string[];
  elementIds: string[];
  summary: string;
}

export interface FusionReviewProjectEvent {
  eventId: string;
  planId: string;
  candidateId: string;
  outputName: string;
  sourceCount: number;
  summary: string;
}

export interface PackageImportedProjectEvent {
  eventId: string;
  baselineRevision: string;
  packageName: string;
  packageVersion: string;
  sourceFilename: string;
  fileCount: number;
}

export interface EvaluationTerminalProjectEvent {
  eventId: string;
  roundId: string;
  status: 'done' | 'failed' | 'cancelled';
  done: number;
  total: number;
  passed: number;
  finishedAt: string;
}

export type AgentProjectEvent = MaterialParseProjectEvent
  | StudioReviewProjectEvent
  | FusionReviewProjectEvent
  | PackageImportedProjectEvent
  | EvaluationTerminalProjectEvent;

interface OutboxRow {
  id: string;
  project_id: string;
  event_type: 'material_parse' | 'studio_review' | 'fusion_review' | 'package_import' | 'evaluation_terminal';
  payload_json: string;
}

export function enqueueMaterialParseProjectEvent(
  projectId: string,
  event: MaterialParseProjectEvent,
  db: DatabaseSync = getDb(),
): void {
  enqueueAgentProjectEvent(db, projectId, 'material_parse', event);
}

export function enqueueStudioReviewProjectEvent(
  projectId: string,
  event: StudioReviewProjectEvent,
  db: DatabaseSync = getDb(),
): void {
  enqueueAgentProjectEvent(db, projectId, 'studio_review', event);
}

export function enqueueFusionReviewProjectEvent(
  projectId: string,
  event: FusionReviewProjectEvent,
  db: DatabaseSync = getDb(),
): void {
  enqueueAgentProjectEvent(db, projectId, 'fusion_review', event);
}

export function enqueuePackageImportedProjectEvent(
  projectId: string,
  event: PackageImportedProjectEvent,
  db: DatabaseSync = getDb(),
): void {
  enqueueAgentProjectEvent(db, projectId, 'package_import', event);
}

export function enqueueEvaluationTerminalProjectEvent(
  projectId: string,
  event: EvaluationTerminalProjectEvent,
  db: DatabaseSync = getDb(),
): void {
  enqueueAgentProjectEvent(db, projectId, 'evaluation_terminal', event);
}

function enqueueAgentProjectEvent(
  db: DatabaseSync,
  projectId: string,
  eventType: 'material_parse' | 'studio_review' | 'fusion_review' | 'package_import' | 'evaluation_terminal',
  event: AgentProjectEvent,
): void {
  db.prepare(`INSERT INTO agent_project_event_outbox (
    id, project_id, event_type, payload_json, created_at, delivered_at
  ) VALUES (?, ?, ?, ?, ?, NULL)
  ON CONFLICT(id) DO UPDATE SET
    event_type = excluded.event_type,
    payload_json = excluded.payload_json`)
    .run(event.eventId, projectId, eventType, JSON.stringify(event), nowIso());
}

export function pendingAgentProjectEvents(projectId: string, limit = 100): AgentProjectEvent[] {
  const rows = getDb().prepare(`SELECT id, project_id, event_type, payload_json
    FROM agent_project_event_outbox
    WHERE project_id = ? AND delivered_at IS NULL
    ORDER BY created_at, id LIMIT ?`).all(projectId, Math.max(1, Math.min(limit, 500))) as unknown as OutboxRow[];
  return rows.flatMap((row) => {
    try {
      const payload = JSON.parse(row.payload_json) as AgentProjectEvent;
      return payload?.eventId === row.id ? [payload] : [];
    } catch {
      return [];
    }
  });
}

export function materialParseDeliveryContext(
  projectId: string,
  documentId: string,
): { autoContinue: boolean; uploadBatchId: string; uploadBatchSize: number } {
  const row = getDb().prepare(`SELECT payload_json FROM agent_project_event_outbox
    WHERE project_id = ? AND id = ?`).get(
    projectId,
    `material:${documentId}:parsing`,
  ) as { payload_json?: string } | undefined;
  if (!row?.payload_json) {
    return { autoContinue: true, uploadBatchId: documentId, uploadBatchSize: 1 };
  }
  try {
    const event = JSON.parse(row.payload_json) as MaterialParseProjectEvent;
    return {
      autoContinue: event.autoContinue !== false,
      uploadBatchId: event.uploadBatchId || documentId,
      uploadBatchSize: Math.max(1, Number(event.uploadBatchSize) || 1),
    };
  } catch {
    return { autoContinue: true, uploadBatchId: documentId, uploadBatchSize: 1 };
  }
}

function reconcileMaterialParseTerminalEvents(projectId: string): void {
  const rows = getDb().prepare(`SELECT documents.id, documents.source_key, documents.filename,
      documents.revision_no, documents.purpose, documents.reading_mode,
      documents.parse_status, documents.parse_error,
      impacts.change_kind, impacts.semantic_changed, started.payload_json AS started_payload_json
    FROM documents
    JOIN agent_project_event_outbox started
      ON started.project_id = documents.project_id
      AND started.id = 'material:' || documents.id || ':parsing'
    LEFT JOIN material_revision_impacts impacts ON impacts.current_document_id = documents.id
    WHERE documents.project_id = ? AND documents.parse_status IN ('parsed', 'failed')
      AND NOT EXISTS (
        SELECT 1 FROM agent_project_event_outbox terminal
        WHERE terminal.id = 'material:' || documents.id || ':terminal'
      )
    ORDER BY impacts.created_at DESC`).all(projectId) as any[];
  const seen = new Set<string>();
  for (const row of rows) {
    const documentId = String(row.id);
    if (seen.has(documentId)) continue;
    seen.add(documentId);
    let autoContinue = true;
    let uploadBatchId = documentId;
    let uploadBatchSize = 1;
    try {
      const started = JSON.parse(String(row.started_payload_json || '{}')) as MaterialParseProjectEvent;
      autoContinue = started.autoContinue !== false;
      uploadBatchId = started.uploadBatchId || documentId;
      uploadBatchSize = Math.max(1, Number(started.uploadBatchSize) || 1);
    } catch {
      // 旧事件没有该字段时维持增量资料的默认自动续跑语义。
    }
    enqueueMaterialParseProjectEvent(projectId, {
      eventId: `material:${documentId}:terminal`,
      status: row.parse_status === 'parsed' ? 'parsed' : 'failed',
      documentId,
      sourceKey: String(row.source_key || documentId),
      filename: String(row.filename),
      revisionNo: Number(row.revision_no || 1),
      purpose: row.purpose || 'auto',
      readingMode: row.reading_mode || 'on_demand',
      autoContinue,
      uploadBatchId,
      uploadBatchSize,
      ...(row.parse_error ? { parseError: String(row.parse_error) } : {}),
      ...(row.change_kind ? { changeKind: row.change_kind } : {}),
      ...(row.semantic_changed != null ? { semanticChanged: Boolean(row.semantic_changed) } : {}),
    });
  }
}

export function reconcileEvaluationTerminalEvents(projectId: string): void {
  const rows = getDb().prepare(`WITH latest_round AS (
      SELECT id, status, progress_done, progress_total, pass_count, created_at, finished_at
      FROM eval_rounds
      WHERE project_id = ?
      ORDER BY created_at DESC, rowid DESC LIMIT 1
    )
    SELECT * FROM latest_round
    WHERE status IN ('done', 'failed', 'cancelled') AND NOT EXISTS (
      SELECT 1 FROM agent_project_event_outbox terminal
      WHERE terminal.id = 'evaluation:' || latest_round.id || ':terminal'
    )`).all(projectId) as any[];
  for (const row of rows) {
    const roundId = String(row.id);
    enqueueEvaluationTerminalProjectEvent(projectId, {
      eventId: `evaluation:${roundId}:terminal`,
      roundId,
      status: row.status,
      done: Number(row.progress_done || 0),
      total: Number(row.progress_total || 0),
      passed: Number(row.pass_count || 0),
      finishedAt: String(row.finished_at || row.created_at),
    });
  }
}

export async function deliverAgentProjectEvents(
  projectId: string,
  authorization: string,
): Promise<{ delivered: number; pending: number }> {
  reconcileMaterialParseTerminalEvents(projectId);
  reconcileEvaluationTerminalEvents(projectId);
  const events = pendingAgentProjectEvents(projectId);
  if (!authorization || !events.length) return { delivered: 0, pending: events.length };
  let delivered = 0;
  for (const event of events) {
    const endpoint = 'roundId' in event
      ? 'evaluation-events'
      : 'baselineRevision' in event
      ? 'package-import-events'
      : 'planId' in event
      ? 'fusion-events'
      : 'clientMutationId' in event ? 'studio-events' : 'material-events';
    let response: Response;
    try {
      response = await fetch(
        `${config.agentBaseUrl}/v1/internal/projects/${encodeURIComponent(projectId)}/${endpoint}`,
        {
          method: 'POST',
          headers: { authorization, 'content-type': 'application/json' },
          body: JSON.stringify(event),
          signal: AbortSignal.timeout(5_000),
        },
      );
    } catch {
      break;
    }
    if (!response.ok) break;
    getDb().prepare(`UPDATE agent_project_event_outbox SET delivered_at = ?
      WHERE id = ? AND delivered_at IS NULL`).run(nowIso(), event.eventId);
    delivered += 1;
  }
  return { delivered, pending: Math.max(0, events.length - delivered) };
}
