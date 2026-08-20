import { randomUUID } from 'node:crypto';
import { getDb, nowIso } from '../db/index.js';
import type { DocumentReadCoverage } from '../types.js';
import type { DocumentAnalysisChunk, DocumentAnalysisPlan } from './document-analysis-plan.js';
import { serializableDocumentAnalysisPlan, sha256 } from './document-analysis-plan.js';

export type MaterialAnalysisRunStatus = 'running' | 'paused' | 'failed' | 'completed';
export type MaterialAnalysisChunkStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface ChunkEvidence {
  offset: number;
  kind: 'object' | 'rule' | 'operation' | 'metric' | 'process' | 'fact';
  note: string;
}

export interface ChunkCandidate {
  kind: 'object' | 'rule' | 'operation' | 'metric' | 'process';
  name: string;
  rationale: string;
  evidenceOffsets: number[];
  readiness: 'extractable' | 'needs_clarification';
}

export interface ChunkAnalysisPayload {
  summary: string;
  evidence: ChunkEvidence[];
  candidates: ChunkCandidate[];
  openQuestions: Array<{ topic: string; why: string }>;
}

export interface DocumentAnalysisPayload {
  summary: string;
  suitableFor: Array<'object' | 'rule' | 'operation' | 'metric'>;
  rationale: string;
  candidates: ChunkCandidate[];
  openQuestions: Array<{ topic: string; why: string }>;
}

export interface MaterialAnalysisRunRecord {
  id: string;
  projectId: string;
  sourceSetFingerprint: string;
  briefFingerprint: string;
  modelId: string;
  protocolVersion: string;
  plan: ReturnType<typeof serializableDocumentAnalysisPlan>;
  status: MaterialAnalysisRunStatus;
  stage: string;
  result?: unknown;
  error?: string;
}

export interface MaterialAnalysisChunkRecord extends Omit<DocumentAnalysisChunk, 'content'> {
  runId: string;
  status: MaterialAnalysisChunkStatus;
  attemptCount: number;
  payload?: ChunkAnalysisPayload;
  error?: string;
}

function parse<T>(value: string | null | undefined): T | undefined {
  if (!value) return undefined;
  try { return JSON.parse(value) as T; } catch { return undefined; }
}

function runFromRow(row: any): MaterialAnalysisRunRecord {
  return {
    id: row.id,
    projectId: row.project_id,
    sourceSetFingerprint: row.source_set_fingerprint,
    briefFingerprint: row.brief_fingerprint,
    modelId: row.model_id,
    protocolVersion: row.protocol_version,
    plan: parse(row.plan_json)!,
    status: row.status,
    stage: row.stage,
    result: parse(row.result_json),
    error: row.error ?? undefined,
  };
}

function chunkFromRow(row: any): MaterialAnalysisChunkRecord {
  return {
    runId: row.run_id,
    documentKey: row.document_key,
    documentId: row.document_id ?? undefined,
    filename: row.filename,
    revisionFingerprint: row.revision_fingerprint,
    chunkIndex: row.chunk_index,
    startOffset: row.start_offset,
    endOffset: row.end_offset,
    contentHash: row.content_hash,
    status: row.status,
    attemptCount: row.attempt_count,
    payload: parse(row.payload_json),
    error: row.error ?? undefined,
  };
}

export function materialSourceSetFingerprint(input: {
  purpose?: 'analysis' | 'document_facts';
  tables: unknown[];
  documents: Array<{ id?: string; filename: string; revisionFingerprint?: string; textContent?: string }>;
}): string {
  return sha256(JSON.stringify({
    purpose: input.purpose ?? 'analysis',
    tables: input.tables,
    documents: input.documents.map((document) => ({
      id: document.id,
      filename: document.filename,
      revisionFingerprint: document.revisionFingerprint,
      contentHash: sha256(document.textContent ?? ''),
    })),
  }));
}

export function materialBriefFingerprint(brief: unknown): string {
  return brief ? sha256(JSON.stringify(brief)).slice(0, 16) : 'no-brief';
}

export class MaterialAnalysisCheckpointStore {
  startOrResume(input: {
    projectId: string;
    sourceSetFingerprint: string;
    briefFingerprint: string;
    plan: DocumentAnalysisPlan;
  }): MaterialAnalysisRunRecord {
    const db = getDb();
    const existing = db.prepare(`SELECT * FROM material_analysis_runs
      WHERE project_id = ? AND source_set_fingerprint = ? AND brief_fingerprint = ?
        AND model_id = ? AND protocol_version = ?`).get(
      input.projectId,
      input.sourceSetFingerprint,
      input.briefFingerprint,
      input.plan.modelId,
      input.plan.protocolVersion,
    ) as any;
    if (existing) {
      db.exec('BEGIN IMMEDIATE');
      try {
        db.prepare(`UPDATE material_analysis_chunks SET status = 'pending', error = NULL,
          started_at = NULL, updated_at = ? WHERE run_id = ? AND status = 'running'`)
          .run(nowIso(), existing.id);
        db.prepare(`UPDATE material_analysis_document_summaries SET status = 'pending', error = NULL,
          updated_at = ? WHERE run_id = ? AND status = 'running'`).run(nowIso(), existing.id);
        if (existing.status !== 'completed') {
          db.prepare(`UPDATE material_analysis_runs SET status = 'running', error = NULL,
            completed_at = NULL, updated_at = ? WHERE id = ?`).run(nowIso(), existing.id);
        }
        db.exec('COMMIT');
      } catch (error) {
        try { db.exec('ROLLBACK'); } catch { /* transaction 已结束 */ }
        throw error;
      }
      return runFromRow({ ...existing, status: existing.status === 'completed' ? 'completed' : 'running', error: null });
    }

    const id = `mar_${randomUUID()}`;
    const now = nowIso();
    db.exec('BEGIN IMMEDIATE');
    try {
      db.prepare(`INSERT INTO material_analysis_runs (
        id, project_id, source_set_fingerprint, brief_fingerprint, model_id,
        protocol_version, plan_json, status, stage, created_at, updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run(
        id, input.projectId, input.sourceSetFingerprint, input.briefFingerprint,
        input.plan.modelId, input.plan.protocolVersion,
        JSON.stringify(serializableDocumentAnalysisPlan(input.plan)),
        'running', 'planning', now, now,
      );
      const insert = db.prepare(`INSERT INTO material_analysis_chunks (
        run_id, document_key, document_id, filename, revision_fingerprint, chunk_index,
        start_offset, end_offset, content_hash, status, attempt_count, updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?,'pending',0,?)`);
      const reusable = db.prepare(`SELECT chunk.payload_json
        FROM material_analysis_chunks chunk
        JOIN material_analysis_runs run ON run.id = chunk.run_id
        WHERE chunk.document_key = ? AND chunk.revision_fingerprint = ?
          AND chunk.start_offset = ? AND chunk.end_offset = ? AND chunk.content_hash = ?
          AND chunk.status = 'completed' AND chunk.payload_json IS NOT NULL
          AND run.model_id = ? AND run.protocol_version = ?
          AND run.project_id = ?
        ORDER BY chunk.completed_at DESC LIMIT 1`);
      const markReusable = db.prepare(`UPDATE material_analysis_chunks SET status = 'completed',
        payload_json = ?, completed_at = ?, updated_at = ?
        WHERE run_id = ? AND document_key = ? AND chunk_index = ?`);
      for (const chunk of input.plan.chunks) {
        insert.run(
          id, chunk.documentKey, chunk.documentId ?? null, chunk.filename,
          chunk.revisionFingerprint, chunk.chunkIndex, chunk.startOffset, chunk.endOffset,
          chunk.contentHash, now,
        );
        const cached = reusable.get(
          chunk.documentKey, chunk.revisionFingerprint, chunk.startOffset, chunk.endOffset, chunk.contentHash,
          input.plan.modelId, input.plan.protocolVersion,
          input.projectId,
        ) as { payload_json?: string } | undefined;
        if (cached?.payload_json) {
          markReusable.run(cached.payload_json, now, now, id, chunk.documentKey, chunk.chunkIndex);
        }
      }
      const reusableSummary = db.prepare(`SELECT summary.run_id, summary.payload_json
        FROM material_analysis_document_summaries summary
        JOIN material_analysis_runs run ON run.id = summary.run_id
        WHERE summary.document_key = ? AND summary.revision_fingerprint = ?
          AND summary.status = 'completed' AND summary.payload_json IS NOT NULL
          AND run.model_id = ? AND run.protocol_version = ?
          AND run.project_id = ?
        ORDER BY summary.completed_at DESC LIMIT 10`);
      const summaryChunks = db.prepare(`SELECT chunk_index, start_offset, end_offset, content_hash
        FROM material_analysis_chunks WHERE run_id = ? AND document_key = ?
        ORDER BY chunk_index`);
      const insertSummary = db.prepare(`INSERT INTO material_analysis_document_summaries (
        run_id, document_key, document_id, filename, revision_fingerprint, status,
        attempt_count, payload_json, completed_at, updated_at
      ) VALUES (?,?,?,?,?,'completed',0,?,?,?)`);
      for (const documentKey of [...new Set(input.plan.chunks.map((chunk) => chunk.documentKey))]) {
        const documentChunks = input.plan.chunks.filter((chunk) => chunk.documentKey === documentKey);
        const completed = db.prepare(`SELECT COUNT(*) AS count FROM material_analysis_chunks
          WHERE run_id = ? AND document_key = ? AND status = 'completed'`)
          .get(id, documentKey) as { count: number };
        if (completed.count !== documentChunks.length) continue;
        const first = documentChunks[0];
        const cached = (reusableSummary.all(
          documentKey, first.revisionFingerprint, input.plan.modelId, input.plan.protocolVersion, input.projectId,
        ) as Array<{ run_id: string; payload_json?: string }>).find((candidate) => {
          const oldChunks = summaryChunks.all(candidate.run_id, documentKey) as Array<{
            chunk_index: number; start_offset: number; end_offset: number; content_hash: string;
          }>;
          return oldChunks.length === documentChunks.length && oldChunks.every((oldChunk, index) => {
            const current = documentChunks[index];
            return oldChunk.chunk_index === current.chunkIndex
              && oldChunk.start_offset === current.startOffset
              && oldChunk.end_offset === current.endOffset
              && oldChunk.content_hash === current.contentHash;
          });
        });
        if (cached?.payload_json) {
          insertSummary.run(
            id, documentKey, first.documentId ?? null, first.filename, first.revisionFingerprint,
            cached.payload_json, now, now,
          );
        }
      }
      db.exec('COMMIT');
    } catch (error) {
      try { db.exec('ROLLBACK'); } catch { /* transaction 已结束 */ }
      throw error;
    }
    return this.getRun(id)!;
  }

  getRun(runId: string): MaterialAnalysisRunRecord | undefined {
    const row = getDb().prepare('SELECT * FROM material_analysis_runs WHERE id = ?').get(runId);
    return row ? runFromRow(row) : undefined;
  }

  listChunks(runId: string): MaterialAnalysisChunkRecord[] {
    const rows = getDb().prepare(`SELECT * FROM material_analysis_chunks WHERE run_id = ?
      ORDER BY document_key, chunk_index`).all(runId);
    return (rows as any[]).map(chunkFromRow);
  }

  beginChunk(runId: string, chunk: Pick<MaterialAnalysisChunkRecord, 'documentKey' | 'chunkIndex'>): number {
    const now = nowIso();
    const result = getDb().prepare(`UPDATE material_analysis_chunks
      SET status = 'running', attempt_count = attempt_count + 1, error = NULL,
        started_at = ?, updated_at = ?
      WHERE run_id = ? AND document_key = ? AND chunk_index = ?
        AND status IN ('pending', 'failed')`).run(now, now, runId, chunk.documentKey, chunk.chunkIndex);
    if (!result.changes) throw new Error(`分段不可领取：${chunk.documentKey}#${chunk.chunkIndex}`);
    const row = getDb().prepare(`SELECT attempt_count FROM material_analysis_chunks
      WHERE run_id = ? AND document_key = ? AND chunk_index = ?`)
      .get(runId, chunk.documentKey, chunk.chunkIndex) as { attempt_count: number };
    return row.attempt_count;
  }

  completeChunk(
    runId: string,
    chunk: Pick<MaterialAnalysisChunkRecord, 'documentKey' | 'chunkIndex'>,
    payload: ChunkAnalysisPayload,
  ): void {
    const now = nowIso();
    getDb().prepare(`UPDATE material_analysis_chunks SET status = 'completed', payload_json = ?,
      error = NULL, completed_at = ?, updated_at = ?
      WHERE run_id = ? AND document_key = ? AND chunk_index = ?`).run(
      JSON.stringify(payload), now, now, runId, chunk.documentKey, chunk.chunkIndex,
    );
  }

  failChunk(
    runId: string,
    chunk: Pick<MaterialAnalysisChunkRecord, 'documentKey' | 'chunkIndex'>,
    error: unknown,
  ): void {
    const reason = (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
    getDb().prepare(`UPDATE material_analysis_chunks SET status = 'failed', error = ?, updated_at = ?
      WHERE run_id = ? AND document_key = ? AND chunk_index = ?`).run(
      reason, nowIso(), runId, chunk.documentKey, chunk.chunkIndex,
    );
  }

  getDocumentSummary(runId: string, documentKey: string): DocumentAnalysisPayload | undefined {
    const row = getDb().prepare(`SELECT payload_json FROM material_analysis_document_summaries
      WHERE run_id = ? AND document_key = ? AND status = 'completed'`)
      .get(runId, documentKey) as { payload_json?: string } | undefined;
    return parse(row?.payload_json);
  }

  beginDocumentSummary(runId: string, chunk: MaterialAnalysisChunkRecord): number {
    const now = nowIso();
    getDb().prepare(`INSERT INTO material_analysis_document_summaries (
      run_id, document_key, document_id, filename, revision_fingerprint, status,
      attempt_count, updated_at
    ) VALUES (?,?,?,?,?,'running',1,?)
    ON CONFLICT(run_id, document_key) DO UPDATE SET status = 'running',
      attempt_count = material_analysis_document_summaries.attempt_count + 1,
      error = NULL, updated_at = excluded.updated_at`).run(
      runId, chunk.documentKey, chunk.documentId ?? null, chunk.filename,
      chunk.revisionFingerprint, now,
    );
    const row = getDb().prepare(`SELECT attempt_count FROM material_analysis_document_summaries
      WHERE run_id = ? AND document_key = ?`).get(runId, chunk.documentKey) as { attempt_count: number };
    return row.attempt_count;
  }

  completeDocumentSummary(runId: string, documentKey: string, payload: DocumentAnalysisPayload): void {
    const now = nowIso();
    getDb().prepare(`UPDATE material_analysis_document_summaries SET status = 'completed',
      payload_json = ?, error = NULL, completed_at = ?, updated_at = ?
      WHERE run_id = ? AND document_key = ?`).run(JSON.stringify(payload), now, now, runId, documentKey);
  }

  failDocumentSummary(runId: string, documentKey: string, error: unknown): void {
    const reason = (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
    getDb().prepare(`UPDATE material_analysis_document_summaries SET status = 'failed',
      error = ?, updated_at = ? WHERE run_id = ? AND document_key = ?`)
      .run(reason, nowIso(), runId, documentKey);
  }

  updateStage(runId: string, stage: string): void {
    getDb().prepare(`UPDATE material_analysis_runs SET stage = ?, updated_at = ? WHERE id = ?`)
      .run(stage, nowIso(), runId);
  }

  pause(runId: string, error: unknown): void {
    const reason = (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
    getDb().prepare(`UPDATE material_analysis_runs SET status = 'paused', error = ?, updated_at = ? WHERE id = ?`)
      .run(reason, nowIso(), runId);
  }

  fail(runId: string, error: unknown): void {
    const reason = (error instanceof Error ? error.message : String(error)).slice(0, 4_000);
    getDb().prepare(`UPDATE material_analysis_runs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`)
      .run(reason, nowIso(), runId);
  }

  complete(runId: string, result: unknown): void {
    const now = nowIso();
    getDb().prepare(`UPDATE material_analysis_runs SET status = 'completed', stage = 'complete',
      result_json = ?, error = NULL, completed_at = ?, updated_at = ? WHERE id = ?`)
      .run(JSON.stringify(result), now, now, runId);
  }

  coverage(runId: string): DocumentReadCoverage[] {
    const chunks = this.listChunks(runId).filter((chunk) => chunk.status === 'completed');
    const keys = [...new Set(chunks.map((chunk) => chunk.documentKey))];
    return keys.map((key) => {
      const items = chunks.filter((chunk) => chunk.documentKey === key)
        .sort((left, right) => left.startOffset - right.startOffset);
      const first = items[0];
      const totalChars = Math.max(...items.map((chunk) => chunk.endOffset));
      const ranges = items.map((chunk) => [chunk.startOffset, chunk.endOffset] as [number, number]);
      let contiguousEnd = 0;
      for (const [start, end] of ranges) {
        if (start > contiguousEnd) break;
        contiguousEnd = Math.max(contiguousEnd, end);
      }
      return {
        ...(first.documentId ? { documentId: first.documentId } : {}),
        filename: first.filename,
        revisionFingerprint: first.revisionFingerprint,
        totalChars,
        ranges,
        contiguousEnd,
        eofSeen: contiguousEnd >= totalChars,
        complete: contiguousEnd >= totalChars,
      };
    });
  }
}
