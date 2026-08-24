import type { DatabaseSync } from 'node:sqlite';

import { getDb } from '../db/index.js';
import { getAuthoringState, type AuthoringState } from './state.js';
import type { JsonObject } from './types.js';

/**
 * AI 辅助建模只把错误作为阻断项。警告和提示仍保留在 package_checks 中，
 * 但不应再占用助手待办或要求专家逐条确认。
 */
export function isNonBlockingFindingInteraction(payload: JsonObject): boolean {
  if (payload.purpose !== 'check_finding_ignore') return false;
  return String(payload.scope ?? '') !== 'errors';
}

/**
 * 部署升级时关闭旧版本遗留的 warning/info 待办。通过 AuthoringState 收口，
 * 这样会同时写入取消事件，前端刷新时不会继续把旧表单当成阻断项。
 */
export function archiveNonBlockingFindingInteractions(
  db: DatabaseSync = getDb(),
  projectId?: string,
  state: AuthoringState = getAuthoringState(),
): number {
  const rows = db.prepare(`SELECT project_id, run_id, payload_json
    FROM authoring_interactions
    WHERE status = 'pending'
      AND type = 'question_batch'
      ${projectId ? 'AND project_id = ?' : ''}
    ORDER BY created_at, rowid`).all(...(projectId ? [projectId] : [])) as Array<{
      project_id: string;
      run_id: string;
      payload_json: string;
  }>;
  let archived = 0;
  const archivedRuns = new Set<string>();
  for (const row of rows) {
    if (archivedRuns.has(row.run_id)) continue;
    let payload: JsonObject;
    try {
      const parsed = JSON.parse(row.payload_json) as unknown;
      payload = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed as JsonObject : {};
    } catch {
      payload = {};
    }
    if (!isNonBlockingFindingInteraction(payload)) continue;
    state.apply({
      kind: 'update_run',
      projectId: row.project_id,
      runId: row.run_id,
      status: 'cancelled',
      result: {
        purpose: 'check_finding_ignore',
        disposition: 'archived_non_blocking_finding',
        reason: 'AI 辅助建模不再要求处理警告或提示',
      },
    });
    archivedRuns.add(row.run_id);
    archived += 1;
  }
  return archived;
}
