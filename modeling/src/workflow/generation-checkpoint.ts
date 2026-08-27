import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type { CandidateDisposition, SpecMetric, SpecObject, SpecOperation, SpecRule } from '../types.js';
import type { GenerationBatchKind, GenerationGateFailure } from './generation-gate.js';

export type GenerationBatchOutput =
  | { objects: SpecObject[] }
  | { rules: SpecRule[]; metrics: SpecMetric[]; dispositions?: CandidateDisposition[] }
  | { operations: SpecOperation[] }
  | Record<string, never>;

export type GenerationBatchCheckpointStatus = 'completed' | 'failed' | 'skipped';

export interface GenerationBatchCheckpoint {
  projectId: string;
  runId: string;
  planHash: string;
  phase: string;
  batchKind: GenerationBatchKind;
  batchIndex: number;
  dependencyHash: string;
  status: GenerationBatchCheckpointStatus;
  output: GenerationBatchOutput;
  issues: string[];
  attemptCount: number;
  createdAt: string;
  updatedAt: string;
}

interface CheckpointRow {
  project_id: string;
  run_id: string;
  plan_hash: string;
  phase: string;
  batch_kind: GenerationBatchKind;
  batch_index: number;
  dependency_hash: string;
  status: GenerationBatchCheckpointStatus;
  output_json: string;
  issues_json: string;
  attempt_count: number;
  invalidated_at?: string | null;
  created_at: string;
  updated_at: string;
}

function decodeObject(value: string): GenerationBatchOutput {
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as GenerationBatchOutput
      : {};
  } catch {
    throw new Error('知识生成检查点产物损坏，请重新发起生成');
  }
}

function decodeIssues(value: string): string[] {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed) || parsed.some((item) => typeof item !== 'string')) throw new Error();
    return parsed;
  } catch {
    throw new Error('知识生成检查点问题记录损坏，请重新发起生成');
  }
}

function fromRow(row: CheckpointRow): GenerationBatchCheckpoint {
  const output = decodeObject(row.output_json);
  if (row.status === 'completed') {
    const valid = row.batch_kind === 'object'
      ? Array.isArray((output as { objects?: unknown }).objects)
      : row.batch_kind === 'knowledge'
        ? Array.isArray((output as { rules?: unknown }).rules)
          && Array.isArray((output as { metrics?: unknown }).metrics)
          && (!('dispositions' in output) || Array.isArray((output as { dispositions?: unknown }).dispositions))
        : Array.isArray((output as { operations?: unknown }).operations);
    if (!valid) throw new Error('知识生成检查点产物损坏，请重新发起生成');
  }
  return {
    projectId: row.project_id,
    runId: row.run_id,
    planHash: row.plan_hash,
    phase: row.phase,
    batchKind: row.batch_kind,
    batchIndex: row.batch_index,
    dependencyHash: row.dependency_hash ?? '',
    status: row.status,
    output,
    issues: decodeIssues(row.issues_json),
    attemptCount: Number(row.attempt_count),
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}

export class GenerationCheckpointStore {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  latestInterruptedRun(projectId: string, baseRevision: string): string | undefined {
    const row = this.db.prepare(`SELECT r.id
      FROM authoring_runs r
      WHERE r.project_id = ? AND r.intent = '生成知识包'
        AND r.status IN ('failed', 'cancelled')
        AND r.base_revision = ?
        AND r.error LIKE '服务重启，任务已中断%'
        AND EXISTS (
          SELECT 1 FROM generation_batch_checkpoints c
          WHERE c.project_id = r.project_id AND c.run_id = r.id
            AND c.status = 'completed' AND c.invalidated_at IS NULL
        )
      ORDER BY COALESCE(r.finished_at, r.updated_at) DESC, r.rowid DESC
      LIMIT 1`).get(projectId, baseRevision) as { id?: string } | undefined;
    return row?.id;
  }

  copyToRun(input: {
    projectId: string;
    fromRunId: string;
    toRunId: string;
    planHash: string;
    dependencyHashes?: Map<string, string>;
  }): void {
    const rows = this.db.prepare(`SELECT * FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND invalidated_at IS NULL ORDER BY
        CASE batch_kind WHEN 'object' THEN 1 WHEN 'knowledge' THEN 2 ELSE 3 END,
        batch_index`)
      .all(input.projectId, input.fromRunId) as unknown as CheckpointRow[];
    const targetRows = this.db.prepare(`SELECT phase, plan_hash FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND invalidated_at IS NULL`)
      .all(input.projectId, input.toRunId) as unknown as Array<{ phase: string; plan_hash: string }>;
    const targetByPhase = new Map(targetRows.map((row) => [row.phase, row.plan_hash]));
    for (const row of rows) {
      const expectedDependencyHash = input.dependencyHashes?.get(row.phase);
      if (expectedDependencyHash !== undefined && expectedDependencyHash !== (row.dependency_hash ?? '')) continue;
      if (expectedDependencyHash === undefined && row.plan_hash !== input.planHash) continue;
      if (targetByPhase.has(row.phase)) continue;
      this.upsert({
        projectId: input.projectId,
        runId: input.toRunId,
        planHash: input.planHash,
        phase: row.phase,
        batchKind: row.batch_kind,
        batchIndex: row.batch_index,
        dependencyHash: row.dependency_hash ?? '',
        status: row.status,
        output: decodeObject(row.output_json),
        issues: decodeIssues(row.issues_json),
        attemptCount: Number(row.attempt_count),
      });
    }
  }

  list(
    projectId: string,
    runId: string,
    planHash: string,
    dependencyHashes?: Map<string, string>,
  ): GenerationBatchCheckpoint[] {
    const rows = this.db.prepare(`SELECT * FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND invalidated_at IS NULL ORDER BY
        CASE batch_kind WHEN 'object' THEN 1 WHEN 'knowledge' THEN 2 ELSE 3 END,
        batch_index`)
      .all(projectId, runId) as unknown as CheckpointRow[];
    return rows
      .filter((row) => {
        const expected = dependencyHashes?.get(row.phase);
        return expected !== undefined ? expected === (row.dependency_hash ?? '') : row.plan_hash === planHash;
      })
      .map(fromRow);
  }

  saveCompleted(input: {
    projectId: string;
    runId: string;
    planHash: string;
    phase: string;
    batchKind: GenerationBatchKind;
    batchIndex: number;
    dependencyHash?: string;
    output: GenerationBatchOutput;
    attemptCount: number;
  }): GenerationBatchCheckpoint {
    return this.upsert({ ...input, status: 'completed', issues: [] });
  }

  saveFailed(input: {
    projectId: string;
    runId: string;
    planHash: string;
    failure: GenerationGateFailure;
    dependencyHash?: string;
    attemptCount: number;
  }): GenerationBatchCheckpoint {
    return this.upsert({
      projectId: input.projectId,
      runId: input.runId,
      planHash: input.planHash,
      phase: input.failure.phase,
      batchKind: input.failure.batchKind,
      batchIndex: input.failure.batchIndex,
      dependencyHash: input.dependencyHash ?? '',
      status: 'failed',
      output: {},
      issues: input.failure.issues,
      attemptCount: input.attemptCount,
    });
  }

  markSkipped(input: {
    projectId: string;
    runId: string;
    planHash: string;
    phase: string;
    batchKind: GenerationBatchKind;
    batchIndex: number;
    dependencyHash?: string;
    issues?: string[];
    attemptCount?: number;
  }): GenerationBatchCheckpoint {
    return this.upsert({
      ...input,
      status: 'skipped',
      output: {},
      issues: input.issues ?? [],
      attemptCount: input.attemptCount ?? 0,
    });
  }

  private upsert(input: {
    projectId: string;
    runId: string;
    planHash: string;
    phase: string;
    batchKind: GenerationBatchKind;
    batchIndex: number;
    dependencyHash?: string;
    status: GenerationBatchCheckpointStatus;
    output: GenerationBatchOutput;
    issues: string[];
    attemptCount: number;
  }): GenerationBatchCheckpoint {
    const dependencyHash = input.dependencyHash ?? '';
    const existing = this.db.prepare(`SELECT plan_hash, dependency_hash FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND phase = ?`)
      .get(input.projectId, input.runId, input.phase) as { plan_hash?: string; dependency_hash?: string } | undefined;
    if (existing?.dependency_hash && existing.dependency_hash !== dependencyHash) {
      throw new Error('知识生成输入或批次计划已变化，不能覆盖此前检查点，请重新发起生成');
    }
    const at = nowIso();
    this.db.prepare(`INSERT INTO generation_batch_checkpoints (
        project_id, run_id, plan_hash, phase, batch_kind, batch_index,
        dependency_hash, status, output_json, issues_json, attempt_count, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(run_id, phase) DO UPDATE SET
        status = excluded.status,
        plan_hash = excluded.plan_hash,
        dependency_hash = excluded.dependency_hash,
        output_json = excluded.output_json,
        issues_json = excluded.issues_json,
        attempt_count = excluded.attempt_count,
        updated_at = excluded.updated_at`)
      .run(
        input.projectId,
        input.runId,
        input.planHash,
        input.phase,
        input.batchKind,
        input.batchIndex,
        dependencyHash,
        input.status,
        JSON.stringify(input.output),
        JSON.stringify(input.issues),
        input.attemptCount,
        at,
        at,
      );
    const row = this.db.prepare(`SELECT * FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND phase = ?`)
      .get(input.projectId, input.runId, input.phase) as unknown as CheckpointRow;
    return fromRow(row);
  }
}
