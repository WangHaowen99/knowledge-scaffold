import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import {
  AiConstructionTaskGraph,
  AiConstructionTaskGraphError,
  aiConstructionTaskDuration,
  type AiConstructionTaskGraphSnapshot,
  type AiConstructionTaskKind,
  type AiConstructionTaskSnapshot,
} from './task-graph.js';

type NewTask = {
  kind: AiConstructionTaskKind;
  title: string;
  goal: string;
  required?: boolean;
  reason?: string;
  dependsOn?: string[];
};

export type AiConstructionTaskEditRequest =
  | { action: 'add'; milestoneId: string; task: NewTask; beforeTaskId?: string }
  | { action: 'reorder'; taskId: string; beforeTaskId?: string }
  | { action: 'split'; taskId: string; parts: Array<Omit<NewTask, 'required' | 'dependsOn'>> }
  | { action: 'merge'; taskIds: string[]; task: Omit<NewTask, 'required' | 'dependsOn'> };

export interface AiConstructionTaskEditPreview {
  projectId: string;
  graphRevision: number;
  action: AiConstructionTaskEditRequest['action'];
  allowed: boolean;
  reason?: string;
  affectedTasks: Array<Pick<AiConstructionTaskSnapshot, 'id' | 'title' | 'status' | 'milestoneId'>>;
  affectedMilestones: string[];
  proposedTitles: string[];
  etaDelta?: { minMinutes: number; maxMinutes: number };
}

export interface AiConstructionTaskEditImpact extends AiConstructionTaskEditPreview {
  createdTaskIds: string[];
}

export interface AiConstructionTaskEditSnapshot {
  id: string;
  projectId: string;
  action: AiConstructionTaskEditRequest['action'];
  request: AiConstructionTaskEditRequest;
  graphRevisionBefore: number;
  graphRevisionAfter: number;
  actor: string;
  reason: string;
  impact: AiConstructionTaskEditImpact;
  createdAt: string;
}

const EDITABLE_STATUSES = new Set(['pending', 'ready', 'failed', 'waiting_user', 'waiting_confirmation']);
const TASK_KINDS = new Set<AiConstructionTaskKind>([
  'material_analysis', 'package_baseline_review', 'modeling_brief', 'process_interview',
  'process_diagram', 'process_review', 'operation_extraction', 'knowledge_extraction',
  'script_decision', 'script_generation', 'package_validation', 'package_repair',
  'evaluation_repair', 'completion_review',
]);

class TaskEditValidationError extends Error {}

function cleanText(value: unknown, label: string, max: number) {
  const text = String(value ?? '').trim();
  if (!text || text.length > max) throw new TaskEditValidationError(`${label}不正确`);
  return text;
}

function unique(values: string[]) {
  return [...new Set(values)];
}

function taskView(task: AiConstructionTaskSnapshot) {
  return { id: task.id, title: task.title, status: task.status, milestoneId: task.milestoneId };
}

function validateTopologicalOrder(tasks: AiConstructionTaskSnapshot[], order: string[], dependencies?: Map<string, string[]>) {
  const index = new Map(order.map((id, ordinal) => [id, ordinal]));
  for (const task of tasks) {
    const taskIndex = index.get(task.id);
    if (taskIndex == null) continue;
    for (const dependency of dependencies?.get(task.id) ?? task.dependsOn) {
      const dependencyIndex = index.get(dependency);
      if (dependencyIndex != null && dependencyIndex > taskIndex) {
        throw new TaskEditValidationError(`任务“${task.title}”必须排在它依赖的任务之后`);
      }
    }
  }
}

function assertAcyclic(taskIds: string[], dependencies: Map<string, string[]>) {
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (id: string) => {
    if (visiting.has(id)) throw new TaskEditValidationError('任务调整会形成循环依赖');
    if (visited.has(id)) return;
    visiting.add(id);
    for (const dependency of dependencies.get(id) ?? []) if (taskIds.includes(dependency)) visit(dependency);
    visiting.delete(id);
    visited.add(id);
  };
  taskIds.forEach(visit);
}

function duration(kind: AiConstructionTaskKind) {
  const [minMinutes, maxMinutes] = aiConstructionTaskDuration(kind);
  return { minMinutes, maxMinutes };
}

export class AiConstructionTaskEditor {
  private readonly graph: AiConstructionTaskGraph;

  constructor(private readonly db: DatabaseSync = getDb()) {
    this.graph = new AiConstructionTaskGraph(db);
  }

  preview(projectId: string, edit: AiConstructionTaskEditRequest): AiConstructionTaskEditPreview {
    const graph = this.graph.snapshot(projectId);
    if (!graph) throw new AiConstructionTaskGraphError('GRAPH_NOT_FOUND', '项目尚未初始化 AI 辅助构建');
    try {
      return this.validate(projectId, graph, edit);
    } catch (error) {
      if (!(error instanceof TaskEditValidationError)) throw error;
      const affected = this.affectedTasks(graph, edit);
      return {
        projectId,
        graphRevision: graph.revision,
        action: edit.action,
        allowed: false,
        reason: error.message,
        affectedTasks: affected.map(taskView),
        affectedMilestones: unique(affected.map((task) => task.milestoneId)),
        proposedTitles: this.proposedTitles(edit),
        etaDelta: { minMinutes: 0, maxMinutes: 0 },
      };
    }
  }

  apply(projectId: string, input: {
    expectedRevision: number;
    actor: string;
    reason: string;
    edit: AiConstructionTaskEditRequest;
  }): { graph: AiConstructionTaskGraphSnapshot; edit: AiConstructionTaskEditSnapshot } {
    const actor = cleanText(input.actor, '操作人', 100);
    const reason = cleanText(input.reason, '调整原因', 500);
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const before = this.graph.snapshot(projectId);
      if (!before) throw new AiConstructionTaskGraphError('GRAPH_NOT_FOUND', '项目尚未初始化 AI 辅助构建');
      if (before.revision !== input.expectedRevision) {
        throw new AiConstructionTaskGraphError('REVISION_CONFLICT', `任务图已更新，请基于 revision ${before.revision} 重试`);
      }
      const preview = this.validate(projectId, before, input.edit);
      const now = nowIso();
      const createdTaskIds = this.mutate(projectId, before, input.edit, now);
      this.db.prepare(`UPDATE ai_construction_graphs SET revision = revision + 1, updated_at = ?
        WHERE project_id = ?`).run(now, projectId);
      const nextRevision = before.revision + 1;
      const impact: AiConstructionTaskEditImpact = { ...preview, createdTaskIds };
      const id = `task_edit_${randomUUID().slice(0, 12)}`;
      this.db.prepare(`INSERT INTO ai_construction_task_edits
        (id, project_id, action, edit_json, graph_revision_before, graph_revision_after,
          actor_username, reason, impact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
        id, projectId, input.edit.action, JSON.stringify(input.edit), before.revision, nextRevision,
        actor, reason, JSON.stringify(impact), now,
      );
      if (ownsTransaction) this.db.exec('COMMIT');
      return {
        graph: this.graph.snapshot(projectId)!,
        edit: {
          id, projectId, action: input.edit.action, request: input.edit,
          graphRevisionBefore: before.revision, graphRevisionAfter: nextRevision,
          actor, reason, impact, createdAt: now,
        },
      };
    } catch (error) {
      if (ownsTransaction) {
        try { this.db.exec('ROLLBACK'); } catch { /* ignore */ }
      }
      if (error instanceof TaskEditValidationError) {
        throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', error.message);
      }
      throw error;
    }
  }

  list(projectId: string): AiConstructionTaskEditSnapshot[] {
    return (this.db.prepare(`SELECT * FROM ai_construction_task_edits
      WHERE project_id = ? ORDER BY created_at, rowid`).all(projectId) as any[]).map((row) => ({
      id: row.id,
      projectId: row.project_id,
      action: row.action,
      request: JSON.parse(row.edit_json),
      graphRevisionBefore: Number(row.graph_revision_before),
      graphRevisionAfter: Number(row.graph_revision_after),
      actor: row.actor_username,
      reason: row.reason,
      impact: JSON.parse(row.impact_json),
      createdAt: row.created_at,
    }));
  }

  private proposedTitles(edit: AiConstructionTaskEditRequest) {
    if (edit.action === 'add' || edit.action === 'merge') return [String(edit.task?.title ?? '').trim()].filter(Boolean);
    if (edit.action === 'split') return edit.parts.map((part) => String(part.title ?? '').trim()).filter(Boolean);
    return [];
  }

  private affectedTasks(graph: AiConstructionTaskGraphSnapshot, edit: AiConstructionTaskEditRequest) {
    if (edit.action === 'add') {
      return edit.beforeTaskId ? graph.tasks.filter((task) => task.id === edit.beforeTaskId) : [];
    }
    if (edit.action === 'merge') return graph.tasks.filter((task) => edit.taskIds.includes(task.id));
    return graph.tasks.filter((task) => task.id === edit.taskId);
  }

  private validate(
    projectId: string,
    graph: AiConstructionTaskGraphSnapshot,
    edit: AiConstructionTaskEditRequest,
  ): AiConstructionTaskEditPreview {
    if (!edit || !['add', 'reorder', 'split', 'merge'].includes(edit.action)) {
      throw new TaskEditValidationError('任务编辑动作不正确');
    }
    const activeMilestone = graph.milestones.find((item) => item.status === 'active');
    if (!activeMilestone) throw new TaskEditValidationError('当前没有可编辑的活动阶段');
    const affected = this.affectedTasks(graph, edit);
    let milestoneId = activeMilestone.id;
    let etaDelta = { minMinutes: 0, maxMinutes: 0 };

    if (edit.action === 'add') {
      if (edit.milestoneId !== activeMilestone.id) throw new TaskEditValidationError('只能在当前活动阶段新增任务');
      this.validateNewTask(graph, edit.task, activeMilestone.ordinal);
      if (edit.beforeTaskId) {
        const before = graph.tasks.find((task) => task.id === edit.beforeTaskId);
        if (!before || before.milestoneId !== edit.milestoneId) throw new TaskEditValidationError('插入位置不属于当前阶段');
      }
      etaDelta = duration(edit.task.kind);
    } else if (edit.action === 'reorder') {
      const task = graph.tasks.find((item) => item.id === edit.taskId);
      if (!task) throw new TaskEditValidationError('任务不存在或不属于当前项目');
      milestoneId = task.milestoneId;
      this.assertEditableTask(task, activeMilestone.id);
      if (edit.beforeTaskId === task.id) throw new TaskEditValidationError('任务不能排到自己之前');
      const sameMilestone = graph.tasks.filter((item) => item.milestoneId === task.milestoneId && item.status !== 'cancelled');
      if (edit.beforeTaskId && !sameMilestone.some((item) => item.id === edit.beforeTaskId)) {
        throw new TaskEditValidationError('目标位置不属于当前阶段');
      }
      const order = sameMilestone.map((item) => item.id).filter((id) => id !== task.id);
      const target = edit.beforeTaskId ? order.indexOf(edit.beforeTaskId) : order.length;
      order.splice(target, 0, task.id);
      validateTopologicalOrder(sameMilestone, order);
    } else if (edit.action === 'split') {
      const task = graph.tasks.find((item) => item.id === edit.taskId);
      if (!task) throw new TaskEditValidationError('任务不存在或不属于当前项目');
      milestoneId = task.milestoneId;
      this.assertEditableTask(task, activeMilestone.id);
      if (!Array.isArray(edit.parts) || edit.parts.length < 2 || edit.parts.length > 5) {
        throw new TaskEditValidationError('拆分任务必须包含 2 到 5 个子任务');
      }
      edit.parts.forEach((part) => this.validateNewTask(graph, { ...part, required: task.required, dependsOn: [] }, activeMilestone.ordinal, false));
      const original = duration(task.kind);
      const parts = edit.parts.map((part) => duration(part.kind)).reduce((sum, item) => ({
        minMinutes: sum.minMinutes + item.minMinutes,
        maxMinutes: sum.maxMinutes + item.maxMinutes,
      }), { minMinutes: 0, maxMinutes: 0 });
      etaDelta = { minMinutes: parts.minMinutes - original.minMinutes, maxMinutes: parts.maxMinutes - original.maxMinutes };
    } else {
      const ids = unique(edit.taskIds ?? []);
      if (ids.length < 2 || ids.length > 5 || ids.length !== edit.taskIds.length) {
        throw new TaskEditValidationError('合并任务必须选择 2 到 5 个不同任务');
      }
      const tasks = graph.tasks.filter((task) => ids.includes(task.id));
      if (tasks.length !== ids.length) throw new TaskEditValidationError('合并任务不存在或不属于当前项目');
      tasks.forEach((task) => this.assertEditableTask(task, activeMilestone.id));
      milestoneId = tasks[0].milestoneId;
      if (tasks.some((task) => task.milestoneId !== milestoneId)) throw new TaskEditValidationError('只能合并同一阶段的任务');
      if (tasks.some((task) => task.kind !== edit.task.kind)) throw new TaskEditValidationError('只能合并同一任务类型');
      this.validateNewTask(graph, { ...edit.task, required: tasks.some((task) => task.required), dependsOn: [] }, activeMilestone.ordinal, false);
      const dependencies = new Map(graph.tasks.map((task) => [task.id, [...task.dependsOn]]));
      const mergedId = '__merged_preview__';
      const selected = new Set(ids);
      dependencies.set(mergedId, unique(tasks.flatMap((task) => task.dependsOn).filter((id) => !selected.has(id))));
      for (const [taskId, deps] of dependencies) {
        if (selected.has(taskId)) continue;
        if (deps.some((id) => selected.has(id))) {
          dependencies.set(taskId, unique(deps.filter((id) => !selected.has(id)).concat(mergedId)));
        }
      }
      assertAcyclic(graph.tasks.map((task) => task.id).filter((id) => !selected.has(id)).concat(mergedId), dependencies);
      const beforeDuration = tasks.map((task) => duration(task.kind)).reduce((sum, item) => ({
        minMinutes: sum.minMinutes + item.minMinutes,
        maxMinutes: sum.maxMinutes + item.maxMinutes,
      }), { minMinutes: 0, maxMinutes: 0 });
      const afterDuration = duration(edit.task.kind);
      etaDelta = {
        minMinutes: afterDuration.minMinutes - beforeDuration.minMinutes,
        maxMinutes: afterDuration.maxMinutes - beforeDuration.maxMinutes,
      };
    }

    return {
      projectId,
      graphRevision: graph.revision,
      action: edit.action,
      allowed: true,
      affectedTasks: affected.map(taskView),
      affectedMilestones: [milestoneId],
      proposedTitles: this.proposedTitles(edit),
      etaDelta,
    };
  }

  private assertEditableTask(task: AiConstructionTaskSnapshot, activeMilestoneId: string) {
    if (task.milestoneId !== activeMilestoneId) throw new TaskEditValidationError('只能调整当前活动阶段的任务');
    if (!EDITABLE_STATUSES.has(task.status)) throw new TaskEditValidationError('只有未完成且未运行的任务可以调整结构');
  }

  private validateNewTask(
    graph: AiConstructionTaskGraphSnapshot,
    task: NewTask,
    activeMilestoneOrdinal: number,
    validateDependencies = true,
  ) {
    cleanText(task.title, '任务标题', 200);
    cleanText(task.goal, '任务目标', 2_000);
    if (task.reason != null && String(task.reason).trim().length > 500) throw new TaskEditValidationError('任务原因过长');
    if (!TASK_KINDS.has(task.kind)) throw new TaskEditValidationError('任务类型不受支持');
    if (!validateDependencies) return;
    const dependencies = unique(task.dependsOn ?? []);
    if (dependencies.length !== (task.dependsOn ?? []).length) throw new TaskEditValidationError('任务依赖不能重复');
    const milestoneOrdinal = new Map(graph.milestones.map((item) => [item.id, item.ordinal]));
    for (const id of dependencies) {
      const dependency = graph.tasks.find((item) => item.id === id);
      if (!dependency) throw new TaskEditValidationError('任务依赖不存在或不属于当前项目');
      if ((milestoneOrdinal.get(dependency.milestoneId) ?? Infinity) > activeMilestoneOrdinal) {
        throw new TaskEditValidationError('任务不能依赖后续阶段');
      }
    }
  }

  private mutate(projectId: string, graph: AiConstructionTaskGraphSnapshot, edit: AiConstructionTaskEditRequest, now: string) {
    if (edit.action === 'add') return this.addTask(projectId, graph, edit, now);
    if (edit.action === 'reorder') return this.reorderTask(projectId, graph, edit);
    if (edit.action === 'split') return this.splitTask(projectId, graph, edit, now);
    return this.mergeTasks(projectId, graph, edit, now);
  }

  private insertTask(projectId: string, input: {
    id: string; milestoneId: string; ordinal: number; task: NewTask; required: boolean; now: string;
    inputRevisionOid?: string;
  }) {
    this.db.prepare(`INSERT INTO ai_construction_tasks
      (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
        required, reason, input_revision_oid, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)`).run(
      input.id, projectId, input.task.kind, input.milestoneId, input.ordinal,
      cleanText(input.task.title, '任务标题', 200), cleanText(input.task.goal, '任务目标', 2_000),
      input.required ? 1 : 0, input.task.reason?.trim() || null, input.inputRevisionOid ?? null,
      input.now, input.now,
    );
    const insert = this.db.prepare(`INSERT INTO ai_construction_task_dependencies
      (project_id, task_id, depends_on_task_id) VALUES (?, ?, ?)`);
    unique(input.task.dependsOn ?? []).forEach((dependency) => insert.run(projectId, input.id, dependency));
  }

  private setOrder(projectId: string, ids: string[]) {
    this.db.prepare('UPDATE ai_construction_tasks SET ordinal = -ordinal WHERE project_id = ?').run(projectId);
    const update = this.db.prepare('UPDATE ai_construction_tasks SET ordinal = ? WHERE project_id = ? AND id = ?');
    ids.forEach((id, index) => update.run(index + 1, projectId, id));
  }

  private insertionIndex(graph: AiConstructionTaskGraphSnapshot, milestoneId: string, beforeTaskId?: string) {
    if (beforeTaskId) return graph.tasks.findIndex((task) => task.id === beforeTaskId);
    const indexes = graph.tasks.map((task, index) => ({ task, index })).filter((item) => item.task.milestoneId === milestoneId);
    return indexes.length ? indexes.at(-1)!.index + 1 : graph.tasks.findIndex((task) => {
      const current = graph.milestones.find((item) => item.id === milestoneId)!;
      const candidate = graph.milestones.find((item) => item.id === task.milestoneId)!;
      return candidate.ordinal > current.ordinal;
    });
  }

  private addTask(projectId: string, graph: AiConstructionTaskGraphSnapshot, edit: Extract<AiConstructionTaskEditRequest, { action: 'add' }>, now: string) {
    const id = `task_${randomUUID().slice(0, 12)}`;
    const order = graph.tasks.map((task) => task.id);
    let index = this.insertionIndex(graph, edit.milestoneId, edit.beforeTaskId);
    if (index < 0) index = order.length;
    this.insertTask(projectId, {
      id, milestoneId: edit.milestoneId, ordinal: graph.tasks.length + 1,
      task: edit.task, required: edit.task.required ?? true, now,
    });
    order.splice(index, 0, id);
    this.setOrder(projectId, order);
    return [id];
  }

  private reorderTask(projectId: string, graph: AiConstructionTaskGraphSnapshot, edit: Extract<AiConstructionTaskEditRequest, { action: 'reorder' }>) {
    const task = graph.tasks.find((item) => item.id === edit.taskId)!;
    const active = graph.tasks.filter((item) => item.milestoneId === task.milestoneId && item.status !== 'cancelled');
    const activeOrder = active.map((item) => item.id).filter((id) => id !== task.id);
    const target = edit.beforeTaskId ? activeOrder.indexOf(edit.beforeTaskId) : activeOrder.length;
    activeOrder.splice(target, 0, task.id);
    const all = graph.tasks.map((item) => item.id).filter((id) => !active.some((item) => item.id === id));
    const firstIndex = Math.min(...active.map((item) => graph.tasks.findIndex((taskItem) => taskItem.id === item.id)));
    all.splice(firstIndex, 0, ...activeOrder);
    this.setOrder(projectId, all);
    return [];
  }

  private splitTask(projectId: string, graph: AiConstructionTaskGraphSnapshot, edit: Extract<AiConstructionTaskEditRequest, { action: 'split' }>, now: string) {
    const source = graph.tasks.find((task) => task.id === edit.taskId)!;
    const ids = edit.parts.map(() => `task_${randomUUID().slice(0, 12)}`);
    edit.parts.forEach((part, index) => this.insertTask(projectId, {
      id: ids[index], milestoneId: source.milestoneId, ordinal: graph.tasks.length + index + 1,
      task: {
        ...part,
        reason: part.reason || `由“${source.title}”拆分`,
        dependsOn: index === 0 ? source.dependsOn : [ids[index - 1]],
      },
      required: source.required, now, inputRevisionOid: source.inputRevisionOid,
    }));
    const dependents = this.db.prepare(`SELECT task_id FROM ai_construction_task_dependencies
      WHERE project_id = ? AND depends_on_task_id = ?`).all(projectId, source.id) as Array<{ task_id: string }>;
    this.db.prepare(`DELETE FROM ai_construction_task_dependencies
      WHERE project_id = ? AND depends_on_task_id = ?`).run(projectId, source.id);
    const addDependency = this.db.prepare(`INSERT OR IGNORE INTO ai_construction_task_dependencies
      (project_id, task_id, depends_on_task_id) VALUES (?, ?, ?)`);
    dependents.forEach((item) => addDependency.run(projectId, item.task_id, ids.at(-1)!));
    this.db.prepare(`UPDATE ai_construction_tasks SET status = 'cancelled', updated_at = ?
      WHERE project_id = ? AND id = ?`).run(now, projectId, source.id);
    const order = graph.tasks.map((task) => task.id).filter((id) => id !== source.id);
    const index = graph.tasks.findIndex((task) => task.id === source.id);
    order.splice(index, 0, ...ids);
    const milestoneEnd = order.reduce((last, id, current) => {
      const task = graph.tasks.find((item) => item.id === id);
      return task?.milestoneId === source.milestoneId || ids.includes(id) ? current + 1 : last;
    }, 0);
    order.splice(milestoneEnd, 0, source.id);
    this.setOrder(projectId, order);
    return ids;
  }

  private mergeTasks(projectId: string, graph: AiConstructionTaskGraphSnapshot, edit: Extract<AiConstructionTaskEditRequest, { action: 'merge' }>, now: string) {
    const selected = graph.tasks.filter((task) => edit.taskIds.includes(task.id));
    const selectedIds = new Set(edit.taskIds);
    const id = `task_${randomUUID().slice(0, 12)}`;
    const externalDependencies = unique(selected.flatMap((task) => task.dependsOn).filter((dependency) => !selectedIds.has(dependency)));
    this.insertTask(projectId, {
      id, milestoneId: selected[0].milestoneId, ordinal: graph.tasks.length + 1,
      task: { ...edit.task, reason: edit.task.reason || `合并 ${selected.length} 个相关任务`, dependsOn: externalDependencies },
      required: selected.some((task) => task.required), now,
      inputRevisionOid: selected.find((task) => task.inputRevisionOid)?.inputRevisionOid,
    });
    const dependentRows = this.db.prepare(`SELECT task_id, depends_on_task_id
      FROM ai_construction_task_dependencies WHERE project_id = ?`).all(projectId) as Array<{
        task_id: string; depends_on_task_id: string;
      }>;
    const downstream = unique(dependentRows
      .filter((row) => !selectedIds.has(row.task_id) && selectedIds.has(row.depends_on_task_id))
      .map((row) => row.task_id));
    const remove = this.db.prepare(`DELETE FROM ai_construction_task_dependencies
      WHERE project_id = ? AND task_id = ? AND depends_on_task_id = ?`);
    dependentRows.filter((row) => downstream.includes(row.task_id) && selectedIds.has(row.depends_on_task_id))
      .forEach((row) => remove.run(projectId, row.task_id, row.depends_on_task_id));
    const add = this.db.prepare(`INSERT OR IGNORE INTO ai_construction_task_dependencies
      (project_id, task_id, depends_on_task_id) VALUES (?, ?, ?)`);
    downstream.forEach((taskId) => add.run(projectId, taskId, id));
    const placeholders = edit.taskIds.map(() => '?').join(',');
    this.db.prepare(`UPDATE ai_construction_tasks SET status = 'cancelled', updated_at = ?
      WHERE project_id = ? AND id IN (${placeholders})`).run(now, projectId, ...edit.taskIds);
    const firstIndex = Math.min(...selected.map((task) => graph.tasks.findIndex((item) => item.id === task.id)));
    const order = graph.tasks.map((task) => task.id).filter((taskId) => !selectedIds.has(taskId));
    order.splice(firstIndex, 0, id);
    const milestoneEnd = order.reduce((last, taskId, current) => {
      const task = graph.tasks.find((item) => item.id === taskId);
      return task?.milestoneId === selected[0].milestoneId || taskId === id ? current + 1 : last;
    }, 0);
    order.splice(milestoneEnd, 0, ...edit.taskIds);
    this.setOrder(projectId, order);
    return [id];
  }
}
