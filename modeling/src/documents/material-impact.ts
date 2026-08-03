import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';
import type { PackageSpec } from '../types.js';
import type { MaterialRevisionChange } from './repo.js';
import { describeImpact, propagateImpact } from './knowledge-dependency.js';

export type AffectedKnowledgeKind = 'object' | 'relation' | 'operation' | 'rule' | 'metric' | 'script';

export interface AffectedKnowledgeResource {
  kind: AffectedKnowledgeKind;
  key: string;
}

export interface MaterialImpactAnalysis {
  semanticChanged: boolean;
  affectedResources: AffectedKnowledgeResource[];
  earliestMilestone?: 'knowledge_confirmed' | 'deterministic_confirmed';
  staleWorkflowSteps: string[];
}

export interface StoredMaterialImpact extends MaterialImpactAnalysis {
  id: string;
  projectId: string;
  sourceKey: string;
  changeKind: MaterialRevisionChange['kind'];
  previousDocumentId?: string;
  currentDocumentId?: string;
  createdAt: string;
}

const VALIDATION_STEPS = ['scenario', 'data_source', 'evaluate', 'diagnose', 'optimize', 'release'];

function evidenceMatches(evidence: string[] | undefined, change: MaterialRevisionChange): boolean {
  const previousId = change.previous?.id;
  const filename = change.previous?.filename;
  return (evidence ?? []).some((item) => (
    (previousId && item === `material-revision:${previousId}`)
    || (filename && (item === `material:${filename}` || item.startsWith(`material:${filename}#`)))
  ));
}

export function analyzeMaterialImpact(
  change: MaterialRevisionChange,
  spec?: PackageSpec | null,
): MaterialImpactAnalysis {
  if (!change.semanticChanged || !change.previous || !spec) {
    return { semanticChanged: change.semanticChanged, affectedResources: [], staleWorkflowSteps: [] };
  }
  const affectedResources: AffectedKnowledgeResource[] = [];
  for (const object of spec.objects) {
    const sourceAffected = object.sourceRefs?.some((source) => (
      source.documentId === change.previous?.id
      || (source.kind === 'document' && source.filename === change.previous?.filename)
    )) ?? false;
    if (sourceAffected) affectedResources.push({ kind: 'object', key: object.key });
    for (const relation of object.relations ?? []) {
      if (sourceAffected || evidenceMatches(relation.evidence, change)) {
        affectedResources.push({ kind: 'relation', key: `${object.key}.${relation.relation}` });
      }
    }
  }
  const collect = (
    kind: Exclude<AffectedKnowledgeKind, 'object' | 'relation'>,
    resources: Array<{ key: string; evidence?: string[] }> | undefined,
  ) => {
    for (const resource of resources ?? []) {
      if (evidenceMatches(resource.evidence, change)) affectedResources.push({ kind, key: resource.key });
    }
  };
  collect('operation', spec.operations);
  collect('rule', spec.rules);
  collect('metric', spec.metrics);
  collect('script', spec.scripts);
  const direct = [...new Map(affectedResources.map((item) => [`${item.kind}:${item.key}`, item])).values()];
  // 直接引用了这份资料的只是起点。改一条规则，引用它的业务操作和绑定的脚本同样要复核——
  // 沿 spec 里的显式引用关系把影响面算全，用户才知道这次改动到底牵动了什么。
  const unique = propagateImpact(spec, direct);
  const earliestMilestone = unique.some((item) => item.kind !== 'script')
    ? 'knowledge_confirmed' as const
    : unique.some((item) => item.kind === 'script')
      ? 'deterministic_confirmed' as const
      : undefined;
  return {
    semanticChanged: true,
    affectedResources: unique,
    ...(earliestMilestone ? { earliestMilestone } : {}),
    staleWorkflowSteps: earliestMilestone ? [...VALIDATION_STEPS] : [],
  };
}

export function storeMaterialImpact(
  db: DatabaseSync,
  projectId: string,
  change: MaterialRevisionChange,
  analysis: MaterialImpactAnalysis,
): StoredMaterialImpact {
  const id = `impact_${randomUUID().slice(0, 12)}`;
  const createdAt = nowIso();
  db.prepare(`INSERT INTO material_revision_impacts
    (id, project_id, source_key, previous_document_id, current_document_id,
      change_kind, semantic_changed, affected_json, created_at)
    VALUES (?,?,?,?,?,?,?,?,?)`).run(
    id, projectId, change.sourceKey, change.previous?.id ?? null, change.current?.id ?? null,
    change.kind, analysis.semanticChanged ? 1 : 0, JSON.stringify(analysis), createdAt,
  );
  return {
    id, projectId, sourceKey: change.sourceKey, changeKind: change.kind,
    previousDocumentId: change.previous?.id,
    currentDocumentId: change.current?.id,
    createdAt,
    ...analysis,
  };
}

export function listMaterialImpacts(db: DatabaseSync, projectId: string): StoredMaterialImpact[] {
  return (db.prepare(`SELECT * FROM material_revision_impacts
    WHERE project_id = ? ORDER BY created_at DESC`).all(projectId) as any[]).map((row) => ({
      id: row.id, projectId: row.project_id, sourceKey: row.source_key,
      changeKind: row.change_kind,
      previousDocumentId: row.previous_document_id ?? undefined,
      currentDocumentId: row.current_document_id ?? undefined,
      createdAt: row.created_at,
      ...(JSON.parse(row.affected_json || '{}') as MaterialImpactAnalysis),
    }));
}

export function applyMaterialInvalidation(
  db: DatabaseSync,
  projectId: string,
  analysis: MaterialImpactAnalysis,
): {
  graphRevision?: number;
  /** 保留字段：新策略不再作废任务，始终为空 */
  staleTaskIds: string[];
  /** 列清影响面的定向复核任务 */
  reviewTaskId?: string;
  affectedResources?: AffectedKnowledgeResource[];
} {
  if (!analysis.earliestMilestone) return { staleTaskIds: [] };
  const graph = db.prepare(`SELECT revision FROM ai_construction_graphs WHERE project_id = ?`)
    .get(projectId) as { revision: number } | undefined;
  if (!graph) return { staleTaskIds: [] };
  const milestone = db.prepare(`SELECT ordinal FROM ai_construction_milestones
    WHERE project_id = ? AND milestone_id = ?`).get(projectId, analysis.earliestMilestone) as {
      ordinal: number;
    } | undefined;
  if (!milestone) return { staleTaskIds: [] };
  const now = nowIso();
  const ownsTransaction = !db.isTransaction;
  if (ownsTransaction) db.exec('BEGIN IMMEDIATE');
  try {
    // 旧策略是按里程碑序号一刀切：受影响点之后的阶段全部 stale、任务全部 pending。
    // 改一份流程资料就要从头走一遍，代价远大于实际影响面。
    //
    // 新策略：已确认的中间阶段一律不动——它们确认的是当时那部分内容，而现在我们
    // 精确知道哪些内容受影响。只撤销「建模完成确认」，因为它断言的是「整体已一致」，
    // 资料一变这个断言就不成立了。其余改成一条列清楚影响面的定向复核任务。
    const completedMilestone = db.prepare(`SELECT status FROM ai_construction_milestones
      WHERE project_id = ? AND milestone_id = 'construction_completed'`)
      .get(projectId) as { status: string } | undefined;
    if (completedMilestone?.status === 'confirmed') {
      db.prepare(`UPDATE ai_construction_milestones
        SET status = 'active', confirmed_revision_oid = NULL, updated_at = ?
        WHERE project_id = ? AND milestone_id = 'construction_completed'`).run(now, projectId);
    }
    db.prepare(`UPDATE ai_construction_graphs SET status = 'active', revision = revision + 1, updated_at = ?
      WHERE project_id = ?`).run(now, projectId);

    // 同一批资料反复变更只保留一条复核任务，不要攒出一串
    const existingReview = db.prepare(`SELECT id FROM ai_construction_tasks
      WHERE project_id = ? AND task_kind = 'material_change_review'
        AND status NOT IN ('completed', 'skipped', 'cancelled')`)
      .get(projectId) as { id: string } | undefined;
    const reason = `资料变更后需要复核：${describeImpact(analysis.affectedResources)}`;
    let reviewTaskId: string;
    if (existingReview) {
      reviewTaskId = existingReview.id;
      db.prepare(`UPDATE ai_construction_tasks SET reason = ?, status = 'pending', updated_at = ?
        WHERE id = ?`).run(reason, now, reviewTaskId);
    } else {
      reviewTaskId = `task_${randomUUID().slice(0, 12)}`;
      const ordinal = Number((db.prepare(`SELECT COALESCE(MAX(ordinal), 0) AS ordinal
        FROM ai_construction_tasks WHERE project_id = ?`).get(projectId) as { ordinal: number }).ordinal) + 1;
      db.prepare(`INSERT INTO ai_construction_tasks
        (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
          required, reason, created_at, updated_at)
        VALUES (?, ?, 'material_change_review', ?, ?, '复核资料变更影响',
          '资料改动之后，逐项确认受影响的内容是否还成立，不需要重做没被影响的部分。',
          'pending', 1, ?, ?, ?)`)
        .run(reviewTaskId, projectId, analysis.earliestMilestone, ordinal, reason, now, now);
    }

    // 不再作废任何任务，因此也没有待办需要连带取消——这正是新策略的意义。

    db.prepare(`UPDATE wf_nodes SET status = 'stale', updated_at = ?
      WHERE project_id = ? AND type = 'analyze' AND status = 'done'`).run(now, projectId);
    db.prepare(`UPDATE wf_nodes SET status = 'stale', updated_at = ?
      WHERE project_id = ? AND type = 'generate' AND status = 'done'`).run(now, projectId);
    const hasWorkflowStepProgress = Boolean(db.prepare(`SELECT 1 FROM sqlite_schema
      WHERE type = 'table' AND name = 'workflow_step_progress'`).get());
    if (hasWorkflowStepProgress) {
      for (const stepId of analysis.staleWorkflowSteps) {
        const existing = db.prepare(`SELECT output_json FROM workflow_step_progress
          WHERE project_id = ? AND step_id = ?`).get(projectId, stepId) as { output_json: string } | undefined;
        if (!existing) continue;
        let output: Record<string, unknown> = {};
        try { output = JSON.parse(existing.output_json || '{}'); } catch { /* use empty */ }
        output.materialRevisionImpact = true;
        db.prepare(`UPDATE workflow_step_progress SET status = 'stale', output_json = ?, updated_at = ?
          WHERE project_id = ? AND step_id = ?`).run(JSON.stringify(output), now, projectId, stepId);
      }
    }
    if (ownsTransaction) db.exec('COMMIT');
    return {
      graphRevision: graph.revision + 1,
      staleTaskIds: [],
      reviewTaskId,
      affectedResources: analysis.affectedResources,
    };
  } catch (error) {
    if (ownsTransaction) {
      try { db.exec('ROLLBACK'); } catch { /* no-op */ }
    }
    throw error;
  }
}
