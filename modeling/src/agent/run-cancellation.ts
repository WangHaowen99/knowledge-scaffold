import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';

/**
 * A durable cancellation fence shared by the Core routes and Studio writer.
 * The Python runtime owns the Run state; this table only prevents a request
 * already queued in the Node process from starting a new mutation.
 */
export interface RunCancellation {
  projectId: string;
  runId: string;
  reason: string;
  requestedAt: string;
}

export function requestRunCancellation(
  db: DatabaseSync,
  projectId: string,
  runId: string,
  reason = '用户已停止运行',
): RunCancellation {
  const timestamp = nowIso();
  db.prepare(`INSERT INTO agent_run_cancellations (
    project_id, run_id, reason, requested_at
  ) VALUES (?, ?, ?, ?)
  ON CONFLICT(project_id, run_id) DO UPDATE SET
    reason = excluded.reason,
    requested_at = CASE
      WHEN agent_run_cancellations.requested_at = '' THEN excluded.requested_at
      ELSE agent_run_cancellations.requested_at
    END`).run(projectId, runId, reason, timestamp);
  return getRunCancellation(db, projectId, runId)!;
}

export function getRunCancellation(
  db: DatabaseSync,
  projectId: string,
  runId: string,
): RunCancellation | null {
  const row = db.prepare(`SELECT project_id, run_id, reason, requested_at
    FROM agent_run_cancellations WHERE project_id = ? AND run_id = ?`)
    .get(projectId, runId) as {
      project_id: string;
      run_id: string;
      reason: string;
      requested_at: string;
    } | undefined;
  return row ? {
    projectId: row.project_id,
    runId: row.run_id,
    reason: row.reason,
    requestedAt: row.requested_at,
  } : null;
}

export function isRunCancellationRequested(
  db: DatabaseSync,
  projectId: string,
  runId: string,
): boolean {
  return Boolean(db.prepare(`SELECT 1 FROM agent_run_cancellations
    WHERE project_id = ? AND run_id = ?`).get(projectId, runId));
}

export function clearRunCancellation(
  db: DatabaseSync,
  projectId: string,
  runId: string,
): void {
  db.prepare(`DELETE FROM agent_run_cancellations
    WHERE project_id = ? AND run_id = ?`).run(projectId, runId);
}
