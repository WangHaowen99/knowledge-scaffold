import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type { JsonObject } from './types.js';
import {
  WORKFLOW_STEP_IDS,
  type ObservedWorkflowStatus,
  type WorkflowStepId,
} from './workflow-definition.js';

export interface WorkflowStepProgress {
  projectId: string;
  stepId: WorkflowStepId;
  status: ObservedWorkflowStatus;
  output: JsonObject;
  revision?: string;
  updatedAt: string;
}

export interface SaveWorkflowStepProgress {
  projectId: string;
  stepId: WorkflowStepId;
  status: ObservedWorkflowStatus;
  output?: JsonObject;
  revision?: string;
}

const OBSERVED_STATUSES = new Set<ObservedWorkflowStatus>([
  'running', 'waiting_answer', 'waiting_review', 'completed', 'skipped', 'stale', 'failed',
]);

export class WorkflowStepProgressRepo {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  save(input: SaveWorkflowStepProgress): WorkflowStepProgress {
    this.assertProject(input.projectId);
    if (!WORKFLOW_STEP_IDS.includes(input.stepId)) throw new Error('流程步骤不正确');
    if (!OBSERVED_STATUSES.has(input.status)) throw new Error('步骤状态不正确');
    const output = input.output ?? {};
    const encoded = JSON.stringify(output);
    if (encoded.length > 1_000_000) throw new Error('步骤结果过大');
    const updatedAt = nowIso();
    this.db.prepare(`INSERT INTO workflow_step_progress
      (project_id, step_id, status, output_json, revision_oid, updated_at)
      VALUES (?,?,?,?,?,?)
      ON CONFLICT(project_id, step_id) DO UPDATE SET
        status = excluded.status,
        output_json = excluded.output_json,
        revision_oid = excluded.revision_oid,
        updated_at = excluded.updated_at`)
      .run(input.projectId, input.stepId, input.status, encoded, input.revision ?? null, updatedAt);
    return this.get(input.projectId, input.stepId)!;
  }

  get(projectId: string, stepId: WorkflowStepId): WorkflowStepProgress | null {
    const row = this.db.prepare(`SELECT * FROM workflow_step_progress
      WHERE project_id = ? AND step_id = ?`).get(projectId, stepId);
    return row ? rowToProgress(row as any) : null;
  }

  list(projectId: string): WorkflowStepProgress[] {
    this.assertProject(projectId);
    return (this.db.prepare(`SELECT * FROM workflow_step_progress
      WHERE project_id = ? ORDER BY updated_at, step_id`).all(projectId) as any[])
      .map(rowToProgress);
  }

  markStale(projectId: string, stepIds: WorkflowStepId[]): WorkflowStepProgress[] {
    this.assertProject(projectId);
    const uniqueStepIds = [...new Set(stepIds)];
    if (uniqueStepIds.some((stepId) => !WORKFLOW_STEP_IDS.includes(stepId))) {
      throw new Error('流程步骤不正确');
    }
    if (!uniqueStepIds.length) return [];
    const placeholders = uniqueStepIds.map(() => '?').join(',');
    this.db.prepare(`UPDATE workflow_step_progress
      SET status = 'stale', updated_at = ?
      WHERE project_id = ? AND step_id IN (${placeholders})`)
      .run(nowIso(), projectId, ...uniqueStepIds);
    return uniqueStepIds.flatMap((stepId) => {
      const progress = this.get(projectId, stepId);
      return progress ? [progress] : [];
    });
  }

  private assertProject(projectId: string): void {
    if (!this.db.prepare('SELECT 1 FROM projects WHERE id = ?').get(projectId)) {
      throw new Error('项目不存在');
    }
  }
}

function rowToProgress(row: any): WorkflowStepProgress {
  return {
    projectId: row.project_id,
    stepId: row.step_id,
    status: row.status,
    output: JSON.parse(row.output_json || '{}'),
    revision: row.revision_oid ?? undefined,
    updatedAt: row.updated_at,
  };
}
