import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type { SpecMetric, SpecObject, SpecOperation, SpecRule } from '../types.js';
import type { GenerationBatchKind, GenerationGateFailure } from './generation-gate.js';

export type GenerationBatchOutput =
  | { objects: SpecObject[] }
  | { rules: SpecRule[]; metrics: SpecMetric[] }
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
  status: GenerationBatchCheckpointStatus;
  output_json: string;
  issues_json: string;
  attempt_count: number;
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

  copyToRun(input: {
    projectId: string;
    fromRunId: string;
    toRunId: string;
    planHash: string;
  }): void {
    const rows = this.db.prepare(`SELECT * FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? ORDER BY batch_index, phase`)
      .all(input.projectId, input.fromRunId) as unknown as CheckpointRow[];
    const targetRows = this.db.prepare(`SELECT phase, plan_hash FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ?`)
      .all(input.projectId, input.toRunId) as unknown as Array<{ phase: string; plan_hash: string }>;
    const targetByPhase = new Map(targetRows.map((row) => [row.phase, row.plan_hash]));
    if (targetRows.some((row) => row.plan_hash !== input.planHash)) {
      throw new Error('知识生成输入或批次计划已变化，不能沿用此前检查点，请重新发起生成');
    }
    for (const row of rows) {
      if (row.plan_hash !== input.planHash) {
        throw new Error('知识生成输入或批次计划已变化，不能沿用此前检查点，请重新发起生成');
      }
      if (targetByPhase.has(row.phase)) continue;
      this.upsert({
        projectId: input.projectId,
        runId: input.toRunId,
        planHash: input.planHash,
        phase: row.phase,
        batchKind: row.batch_kind,
        batchIndex: row.batch_index,
        status: row.status,
        output: decodeObject(row.output_json),
        issues: decodeIssues(row.issues_json),
        attemptCount: Number(row.attempt_count),
      });
    }
  }

  list(projectId: string, runId: string, planHash: string): GenerationBatchCheckpoint[] {
    const rows = this.db.prepare(`SELECT * FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? ORDER BY batch_index, phase`)
      .all(projectId, runId) as unknown as CheckpointRow[];
    const mismatched = rows.find((row) => row.plan_hash !== planHash);
    if (mismatched) {
      throw new Error('知识生成输入或批次计划已变化，不能沿用此前检查点，请重新发起生成');
    }
    return rows.map(fromRow);
  }

  saveCompleted(input: {
    projectId: string;
    runId: string;
    planHash: string;
    phase: string;
    batchKind: GenerationBatchKind;
    batchIndex: number;
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
    attemptCount: number;
  }): GenerationBatchCheckpoint {
    return this.upsert({
      projectId: input.projectId,
      runId: input.runId,
      planHash: input.planHash,
      phase: input.failure.phase,
      batchKind: input.failure.batchKind,
      batchIndex: input.failure.batchIndex,
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
    status: GenerationBatchCheckpointStatus;
    output: GenerationBatchOutput;
    issues: string[];
    attemptCount: number;
  }): GenerationBatchCheckpoint {
    const existing = this.db.prepare(`SELECT plan_hash FROM generation_batch_checkpoints
      WHERE project_id = ? AND run_id = ? AND phase = ?`)
      .get(input.projectId, input.runId, input.phase) as { plan_hash?: string } | undefined;
    if (existing?.plan_hash && existing.plan_hash !== input.planHash) {
      throw new Error('知识生成输入或批次计划已变化，不能覆盖此前检查点，请重新发起生成');
    }
    const at = nowIso();
    this.db.prepare(`INSERT INTO generation_batch_checkpoints (
        project_id, run_id, plan_hash, phase, batch_kind, batch_index,
        status, output_json, issues_json, attempt_count, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(run_id, phase) DO UPDATE SET
        status = excluded.status,
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
