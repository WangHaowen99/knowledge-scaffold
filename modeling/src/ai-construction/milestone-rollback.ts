import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import type { PackageArtifactStore } from '../artifacts/index.js';
import { ArtifactRevisionRepo } from '../artifacts/revision-repo.js';
import { getDb, nowIso } from '../db/index.js';
import { AuthoringState, getAuthoringState } from '../authoring/state.js';
import { WorkflowStepProgressRepo } from '../authoring/workflow-progress.js';
import type { WorkflowStepId } from '../authoring/workflow-definition.js';
import {
  AiConstructionTaskGraph,
  AiConstructionTaskGraphError,
  type AiConstructionMilestoneRollbackPreview,
  type AiConstructionMilestoneRollbackSnapshot,
  type AiConstructionTaskGraphSnapshot,
} from './task-graph.js';

const DOWNSTREAM_WORKFLOW_STEPS: WorkflowStepId[] = [
  'scenario', 'data_source', 'evaluate', 'diagnose', 'optimize', 'release',
];

interface RollbackIntentRow {
  id: string;
  project_id: string;
  request_id: string;
  target_milestone_id: string;
  graph_revision_before: number;
  graph_revision_after: number | null;
  base_package_revision_oid: string;
  current_package_revision_oid: string;
  restored_package_revision_oid: string | null;
  actor_username: string;
  reason: string;
  impact_json: string;
  status: 'prepared' | 'package_restored' | 'completed' | 'failed';
  created_at: string;
}

export class MilestoneRollbackCoordinator {
  private readonly revisions: ArtifactRevisionRepo;
  private readonly workflow: WorkflowStepProgressRepo;

  constructor(private readonly options: {
    packages: Pick<PackageArtifactStore,
      'current' | 'restore' | 'supersedePendingCandidateRevisions'>;
    db?: DatabaseSync;
    graph?: AiConstructionTaskGraph;
    state?: AuthoringState;
  }) {
    const db = options.db ?? getDb();
    this.db = db;
    this.graph = options.graph ?? new AiConstructionTaskGraph(db);
    this.state = options.state ?? getAuthoringState();
    this.revisions = new ArtifactRevisionRepo(db);
    this.workflow = new WorkflowStepProgressRepo(db);
  }

  private readonly db: DatabaseSync;
  private readonly graph: AiConstructionTaskGraph;
  private readonly state: AuthoringState;

  preview(projectId: string, milestoneId: string, ignoreLeaseOperation = ''): AiConstructionMilestoneRollbackPreview {
    const graphPreview = this.graph.previewMilestoneRollback(projectId, milestoneId);
    const activeRun = this.state.snapshot(projectId).activeRuns.find(
      (run) => run.status === 'queued' || run.status === 'running',
    );
    const activeLease = this.db.prepare(`SELECT operation FROM project_operation_leases
      WHERE project_id = ? AND expires_at > ?`).get(projectId, nowIso()) as { operation?: string } | undefined;
    const escaped = `${projectId}:`.replaceAll('\\', '\\\\').replaceAll('%', '\\%').replaceAll('_', '\\_');
    const inflight = this.db.prepare(`SELECT operation FROM inflight_operations
      WHERE key LIKE ? ESCAPE '\\' AND expires_at > ? ORDER BY started_at LIMIT 1`)
      .get(`${escaped}%`, nowIso()) as { operation?: string } | undefined;
    const snapshot = this.graph.snapshot(projectId);
    const targetTaskInput = snapshot?.tasks.find((task) => (
      task.milestoneId === milestoneId && task.inputRevisionOid
    ))?.inputRevisionOid;
    const target = snapshot?.milestones.find((item) => item.id === milestoneId);
    // 新建项目的首个目标任务没有 inputRevisionOid。这里不能因此退到 project_created：
    // 那个 Git 提交只有项目元数据，不含 model/package-spec.json，普通包恢复必然失败。
    // 回退目标阶段应该复用该阶段确认时绑定的知识包修订；后续阶段仍优先使用上游确认修订。
    const hasPreviousConfirmed = snapshot?.milestones.some((item) => (
      item.ordinal < Number(target?.ordinal ?? 0) && item.status === 'confirmed' && item.confirmedRevisionOid
    ));
    const restoreRevisionOid = hasPreviousConfirmed
      ? graphPreview.restoreRevisionOid
      : targetTaskInput ?? target?.confirmedRevisionOid ?? graphPreview.restoreRevisionOid;
    const leaseBlocker = activeLease?.operation && activeLease.operation !== ignoreLeaseOperation
      ? activeLease.operation : undefined;
    const blocker = activeRun?.intent ?? leaseBlocker ?? inflight?.operation;
    const draft = this.db.prepare(`SELECT 1 FROM package_drafts
      WHERE project_id = ? AND status = 'active' LIMIT 1`).get(projectId);
    const draftBlocker = draft ? '存在未发布的知识文件草稿，请先发布或放弃草稿' : undefined;
    return {
      ...graphPreview,
      restoreRevisionOid,
      allowed: graphPreview.allowed && !blocker && !draftBlocker,
      ...(blocker ? { reason: `项目正在${blocker}，请先停止后再回退` }
        : draftBlocker ? { reason: draftBlocker } : {}),
    };
  }

  async apply(input: {
    projectId: string;
    targetMilestoneId: string;
    expectedGraphRevision: number;
    expectedPackageRevision: string;
    requestId: string;
    actor: string;
    reason?: string;
  }): Promise<{
    graph: AiConstructionTaskGraphSnapshot;
    rollback: AiConstructionMilestoneRollbackSnapshot;
    cancelledRuns: number;
    replayed: boolean;
  }> {
    const requestId = requiredText(input.requestId, '请求编号', 200);
    const actor = requiredText(input.actor, '操作人', 100);
    let intent = this.intent(input.projectId, requestId);
    const reason = String(input.reason ?? '').trim()
      || intent?.reason
      || '用户选择重新处理该阶段';
    if (reason.length > 500) {
      throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', '回退原因过长');
    }
    if (intent?.status === 'completed') return this.completedResult(intent);

    if (!intent) {
      // HTTP 路由会先持有本项目写租约；这里仍检查真实任务运行，但不把自己的租约误判成冲突。
      const preview = this.preview(input.projectId, input.targetMilestoneId, '回退建模阶段');
      if (!preview.allowed || !preview.restoreRevisionOid) {
        throw new AiConstructionTaskGraphError(
          'INVALID_TRANSITION', preview.reason ?? '当前不能回退到这个阶段',
        );
      }
      if (preview.graphRevision !== input.expectedGraphRevision) {
        throw new AiConstructionTaskGraphError('REVISION_CONFLICT', '任务图已经变化，请刷新后重新预览');
      }
      const current = await this.options.packages.current(input.projectId);
      if (!current) throw new AiConstructionTaskGraphError('INVALID_TRANSITION', '当前项目没有可恢复的知识包');
      if (current.revision.oid !== input.expectedPackageRevision) {
        throw new AiConstructionTaskGraphError('REVISION_CONFLICT', '知识包已经变化，请刷新后重新预览');
      }
      const rollbackId = `rollback_${randomUUID().slice(0, 12)}`;
      const createdAt = nowIso();
      this.db.prepare(`INSERT INTO ai_construction_milestone_rollbacks (
        id, project_id, request_id, target_milestone_id, graph_revision_before,
        base_package_revision_oid, current_package_revision_oid, actor_username,
        reason, impact_json, status, created_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,'prepared',?)`).run(
        rollbackId, input.projectId, requestId, input.targetMilestoneId,
        input.expectedGraphRevision, preview.restoreRevisionOid, current.revision.oid,
        actor, reason, JSON.stringify(preview), createdAt,
      );
      intent = this.intent(input.projectId, requestId)!;
    } else if (intent.target_milestone_id !== input.targetMilestoneId
      || intent.graph_revision_before !== input.expectedGraphRevision
      || intent.current_package_revision_oid !== input.expectedPackageRevision
      || intent.actor_username !== actor || intent.reason !== reason) {
      throw new AiConstructionTaskGraphError('REVISION_CONFLICT', '相同请求编号对应的回退内容不一致');
    }

    // 预览算法升级前可能已经留下 prepared 意图，但知识包尚未发生任何恢复。
    // 这时允许基于同一任务图和当前包重新校正恢复基线，使原弹窗可以幂等重试；
    // package_restored/completed 一旦落盘则不可再改，继续遵循原审计内容。
    if (intent.status === 'prepared') {
      const refreshedPreview = this.preview(
        input.projectId, intent.target_milestone_id, '回退建模阶段',
      );
      if (!refreshedPreview.allowed || !refreshedPreview.restoreRevisionOid) {
        throw new AiConstructionTaskGraphError(
          'INVALID_TRANSITION', refreshedPreview.reason ?? '当前不能继续阶段回退',
        );
      }
      if (refreshedPreview.graphRevision !== intent.graph_revision_before) {
        throw new AiConstructionTaskGraphError('REVISION_CONFLICT', '任务图已经变化，请刷新后重新预览');
      }
      if (refreshedPreview.restoreRevisionOid !== intent.base_package_revision_oid) {
        this.db.prepare(`UPDATE ai_construction_milestone_rollbacks
          SET base_package_revision_oid = ?, impact_json = ?
          WHERE id = ? AND project_id = ? AND status = 'prepared'`).run(
          refreshedPreview.restoreRevisionOid, JSON.stringify(refreshedPreview),
          intent.id, input.projectId,
        );
        intent = this.intent(input.projectId, requestId)!;
      }
    }

    let restoredRevision = intent.restored_package_revision_oid;
    if (intent.status === 'prepared') {
      const current = await this.options.packages.current(input.projectId);
      const currentRevision = current?.revision.oid;
      const latest = this.revisions.latest(input.projectId);
      const recoveredInterruptedRestore = Boolean(
        currentRevision
        && currentRevision !== intent.current_package_revision_oid
        && latest?.oid === currentRevision
        && latest.parentOid === intent.current_package_revision_oid
        && latest.cause === 'milestone_rollback'
        && latest.summary.includes(intent.id),
      );
      if (recoveredInterruptedRestore) {
        restoredRevision = currentRevision!;
      } else {
        const restored = await this.options.packages.restore(input.projectId, {
          expectedRevision: intent.current_package_revision_oid,
          targetRevision: intent.base_package_revision_oid,
          action: {
            cause: 'milestone_rollback',
            summary: `阶段回退 ${intent.id}：回到“${JSON.parse(intent.impact_json).targetMilestone.title}”重新处理`,
            actorKind: 'user',
            actorName: intent.actor_username,
          },
        });
        restoredRevision = restored.revision.oid;
      }
      this.db.prepare(`UPDATE ai_construction_milestone_rollbacks
        SET restored_package_revision_oid = ?, status = 'package_restored'
        WHERE id = ? AND status = 'prepared'`).run(restoredRevision, intent.id);
      intent = this.intent(input.projectId, requestId)!;
    }
    if (!restoredRevision) throw new Error('阶段回退没有形成恢复修订');

    const impact = JSON.parse(intent.impact_json) as AiConstructionMilestoneRollbackPreview;
    const safetyPreview = this.preview(input.projectId, intent.target_milestone_id, '回退建模阶段');
    if (!safetyPreview.allowed) {
      throw new AiConstructionTaskGraphError(
        'INVALID_TRANSITION', safetyPreview.reason ?? '当前不能继续阶段回退',
      );
    }
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const cancelledRuns = this.state.cancelWaitingRunsForRollback(
        input.projectId, intent.id, intent.target_milestone_id,
      );
      this.db.prepare(`UPDATE generation_batch_checkpoints SET
        invalidated_at = ?, invalidated_reason = ?, invalidated_by_rollback_id = ?
        WHERE project_id = ? AND invalidated_at IS NULL`)
        .run(nowIso(), `阶段回退到${impact.targetMilestone.title}`, intent.id, input.projectId);
      this.workflow.markStale(input.projectId, DOWNSTREAM_WORKFLOW_STEPS);
      const reanalyzeMaterials = !impact.preservedMilestones.some(
        (milestone) => milestone.id === 'materials_ready',
      );
      if (reanalyzeMaterials) {
        // 分段正文和每份文档摘要继续复用，只让领域汇总按新的目标/范围重算。
        this.db.prepare(`UPDATE material_analysis_runs SET status = 'paused',
          stage = 'document_reduce', result_json = NULL, error = ?, completed_at = NULL,
          updated_at = ? WHERE project_id = ? AND status = 'completed'`)
          .run('阶段回退后重新汇总资料分析', nowIso(), input.projectId);
      }
      this.db.prepare(`UPDATE wf_nodes SET status = 'stale', updated_at = ?
        WHERE project_id = ? AND type IN (
          ${reanalyzeMaterials ? "'analyze'," : ''}
          'interview', 'generate', 'review', 'data_source', 'testcases', 'evaluate', 'optimize'
        )
          AND status NOT IN ('pending', 'stale')`).run(nowIso(), input.projectId);
      const result = this.graph.rollbackMilestone(input.projectId, {
        rollbackId: intent.id,
        requestId,
        expectedRevision: intent.graph_revision_before,
        targetMilestoneId: intent.target_milestone_id,
        basePackageRevisionOid: intent.base_package_revision_oid,
        restoredPackageRevisionOid: restoredRevision,
        actor,
        reason,
        impact,
      });
      this.options.packages.supersedePendingCandidateRevisions(
        input.projectId, actor, `阶段已回退到${impact.targetMilestone.title}`,
      );
      this.db.exec('COMMIT');
      return { ...result, cancelledRuns, replayed: false };
    } catch (error) {
      try { this.db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
      throw error;
    }
  }

  private intent(projectId: string, requestId: string): RollbackIntentRow | null {
    return (this.db.prepare(`SELECT * FROM ai_construction_milestone_rollbacks
      WHERE project_id = ? AND request_id = ?`).get(projectId, requestId) as RollbackIntentRow | undefined) ?? null;
  }

  private completedResult(intent: RollbackIntentRow): {
    graph: AiConstructionTaskGraphSnapshot;
    rollback: AiConstructionMilestoneRollbackSnapshot;
    cancelledRuns: number;
    replayed: boolean;
  } {
    const graph = this.graph.snapshot(intent.project_id);
    if (!graph || !intent.graph_revision_after || !intent.restored_package_revision_oid) {
      throw new Error('阶段回退审计记录不完整');
    }
    return {
      graph,
      cancelledRuns: 0,
      replayed: true,
      rollback: {
        id: intent.id,
        projectId: intent.project_id,
        requestId: intent.request_id,
        targetMilestoneId: intent.target_milestone_id,
        graphRevisionBefore: intent.graph_revision_before,
        graphRevisionAfter: intent.graph_revision_after,
        basePackageRevisionOid: intent.base_package_revision_oid,
        restoredPackageRevisionOid: intent.restored_package_revision_oid,
        actor: intent.actor_username,
        reason: intent.reason,
        impact: JSON.parse(intent.impact_json),
        createdAt: intent.created_at,
      },
    };
  }
}


function requiredText(value: unknown, label: string, maxLength: number): string {
  const text = String(value ?? '').trim();
  if (!text) throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', `${label}不能为空`);
  if (text.length > maxLength) throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', `${label}过长`);
  return text;
}
