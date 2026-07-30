import type { DatabaseSync } from 'node:sqlite';

import { buildMaterialAnalysisFailureBlocks } from '../ai-construction/material-analysis-progress.js';
import {
  AiConstructionMessageStore,
  notifyAiConstructionMessageWritten,
} from '../ai-construction/messages.js';
import { getAuthoringState } from './state.js';
import { getDb, nowIso } from '../db/index.js';

export interface AuthoringRestartRecoveryResult {
  authoringRuns: number;
  constructionTasks: number;
  workflowNodes: number;
  projectLeases: number;
}

export interface AuthoringRestartRecoveryOptions {
  now?: string;
  reason?: string;
}

const DEFAULT_REASON = '服务重启，任务已中断，请重新执行当前步骤';

/**
 * 服务进程不能安全恢复尚未完成的模型调用。启动时把真正执行中的任务结束为可重试失败，
 * 但保留等待用户回答/确认的任务与交互，避免用户已经看到的待办被重启吞掉。
 */
export function recoverInterruptedAuthoringWork(
  db: DatabaseSync = getDb(),
  options: AuthoringRestartRecoveryOptions = {},
): AuthoringRestartRecoveryResult {
  const at = options.now ?? nowIso();
  const reason = options.reason?.trim() || DEFAULT_REASON;
  const result: AuthoringRestartRecoveryResult = {
    authoringRuns: 0,
    constructionTasks: 0,
    workflowNodes: 0,
    projectLeases: 0,
  };
  const ownsTransaction = !db.isTransaction;
  if (ownsTransaction) db.exec('BEGIN IMMEDIATE');
  try {
    const interruptedAnalyses = db.prepare(`SELECT task.project_id, task.id AS task_id,
      task.milestone_id, message.message_key
      FROM ai_construction_tasks task
      LEFT JOIN ai_construction_messages message ON message.id = (
        SELECT candidate.id FROM ai_construction_messages candidate
        WHERE candidate.project_id = task.project_id AND candidate.task_id = task.id
          AND candidate.message_key LIKE 'analysis.progress.%'
        ORDER BY candidate.seq DESC LIMIT 1
      )
      WHERE (task.status = 'running' OR (task.status = 'failed' AND task.reason = ?))
        AND task.task_kind IN ('material_analysis', 'package_baseline_review')`).all(reason) as Array<{
          project_id: string;
          task_id: string;
          milestone_id: string;
          message_key: string | null;
        }>;

    // 租约只代表当前进程内正在执行的写操作；进程重启后其 owner 不可能继续续租。
    // 即使原 TTL 尚未到期也必须清理，否则恢复后的待确认事项会被旧租约错误阻塞。
    result.projectLeases = Number(db.prepare('DELETE FROM project_operation_leases').run().changes);

    result.authoringRuns = Number(db.prepare(`UPDATE authoring_runs
      SET status = 'failed', error = ?, updated_at = ?, finished_at = ?
      WHERE status IN ('queued', 'running')`).run(reason, at, at).changes);

    result.constructionTasks = Number(db.prepare(`UPDATE ai_construction_tasks
      SET status = 'failed', reason = ?, updated_at = ?
      WHERE status = 'running'`).run(reason, at).changes);

    result.workflowNodes = Number(db.prepare(`UPDATE wf_nodes
      SET status = 'failed', reason = ?, updated_at = ?
      WHERE status = 'running'`).run(reason, at).changes);

    const messages = new AiConstructionMessageStore(
      db,
      notifyAiConstructionMessageWritten((command) => getAuthoringState().apply(command)),
    );
    for (const analysis of interruptedAnalyses) {
      if (!analysis.message_key) continue;
      messages.upsert(analysis.project_id, {
        key: analysis.message_key,
        role: 'assistant',
        taskId: analysis.task_id,
        blocks: buildMaterialAnalysisFailureBlocks({
          reason,
          milestoneId: analysis.milestone_id,
          taskId: analysis.task_id,
          progress: 0,
        }),
      });
    }

    if (ownsTransaction) db.exec('COMMIT');
    return result;
  } catch (error) {
    if (ownsTransaction) {
      try { db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
    }
    throw error;
  }
}
