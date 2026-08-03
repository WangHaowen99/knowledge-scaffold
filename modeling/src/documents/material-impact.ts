import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';
import type { PackageSpec } from '../types.js';
import type { MaterialRevisionChange } from './repo.js';
import { propagateImpact } from './knowledge-dependency.js';

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
): { graphRevision?: number; staleTaskIds: string[] } {
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
    const affectedTasks = db.prepare(`SELECT task.id, task.milestone_id, milestone.ordinal
      FROM ai_construction_tasks task
      JOIN ai_construction_milestones milestone
        ON milestone.project_id = task.project_id AND milestone.milestone_id = task.milestone_id
      WHERE task.project_id = ? AND milestone.ordinal >= ?
        AND task.status NOT IN ('cancelled')`).all(projectId, milestone.ordinal) as Array<{
          id: string; milestone_id: string; ordinal: number;
        }>;
    const earliestTaskIds = affectedTasks
      .filter((item) => item.ordinal === milestone.ordinal)
      .map((item) => item.id);
    const downstreamTaskIds = affectedTasks
      .filter((item) => item.ordinal > milestone.ordinal)
      .map((item) => item.id);

    db.prepare(`UPDATE ai_construction_graphs SET status = 'active', revision = revision + 1, updated_at = ?
      WHERE project_id = ?`).run(now, projectId);
    db.prepare(`UPDATE ai_construction_milestones SET status = 'active', confirmed_revision_oid = NULL, updated_at = ?
      WHERE project_id = ? AND milestone_id = ?`).run(now, projectId, analysis.earliestMilestone);
    db.prepare(`UPDATE ai_construction_milestones
      SET status = CASE WHEN status IN ('active', 'confirmed') THEN 'stale' ELSE status END,
        confirmed_revision_oid = CASE WHEN status IN ('active', 'confirmed') THEN NULL ELSE confirmed_revision_oid END,
        updated_at = ?
      WHERE project_id = ? AND ordinal > ?`).run(now, projectId, milestone.ordinal);

    if (earliestTaskIds.length) {
      const placeholders = earliestTaskIds.map(() => '?').join(',');
      db.prepare(`UPDATE ai_construction_tasks SET status = 'pending', output_revision_oid = NULL, updated_at = ?
        WHERE project_id = ? AND id IN (${placeholders})`).run(now, projectId, ...earliestTaskIds);
    }
    if (downstreamTaskIds.length) {
      const placeholders = downstreamTaskIds.map(() => '?').join(',');
      db.prepare(`UPDATE ai_construction_tasks SET status = 'stale', output_revision_oid = NULL, updated_at = ?
        WHERE project_id = ? AND id IN (${placeholders})`).run(now, projectId, ...downstreamTaskIds);
    }
    const reanalysisId = `task_${randomUUID().slice(0, 12)}`;
    const ordinal = Number((db.prepare(`SELECT COALESCE(MAX(ordinal), 0) AS ordinal
      FROM ai_construction_tasks WHERE project_id = ?`).get(projectId) as { ordinal: number }).ordinal) + 1;
    db.prepare(`INSERT INTO ai_construction_tasks
      (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
        required, reason, created_at, updated_at)
      VALUES (?, ?, 'material_analysis', ?, ?, '复核变更资料影响',
        '重新分析发生语义变化的资料，并仅重建受影响知识。', 'pending', 1,
        '资料修订影响了已确认知识证据', ?, ?)`)
      .run(reanalysisId, projectId, analysis.earliestMilestone, ordinal, now, now);
    const addDependency = db.prepare(`INSERT OR IGNORE INTO ai_construction_task_dependencies
      (project_id, task_id, depends_on_task_id) VALUES (?, ?, ?)`);
    earliestTaskIds.forEach((taskId) => addDependency.run(projectId, taskId, reanalysisId));

    const staleIds = [...earliestTaskIds, ...downstreamTaskIds];
    const pendingInteractions = db.prepare(`SELECT id, payload_json FROM authoring_interactions
      WHERE project_id = ? AND status = 'pending'`).all(projectId) as Array<{ id: string; payload_json: string }>;
    const staleSet = new Set(staleIds);
    for (const interaction of pendingInteractions) {
      try {
        const payload = JSON.parse(interaction.payload_json) as { taskId?: string };
        if (payload.taskId && staleSet.has(payload.taskId)) {
          db.prepare(`UPDATE authoring_interactions SET status = 'cancelled' WHERE id = ?`).run(interaction.id);
        }
      } catch { /* damaged historical payload does not block invalidation */ }
    }

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
    return { graphRevision: graph.revision + 1, staleTaskIds: staleIds };
  } catch (error) {
    if (ownsTransaction) {
      try { db.exec('ROLLBACK'); } catch { /* no-op */ }
    }
    throw error;
  }
}
