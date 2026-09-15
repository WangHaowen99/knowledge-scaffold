import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { nowIso } from '../db/index.js';
import {
  getRunCancellation,
  isRunCancellationRequested,
  requestRunCancellation,
  clearRunCancellation,
  type RunCancellation,
} from './run-cancellation.js';

export const CHANGE_PLAN_CONTRACT_VERSION = 'change-plan-v1';
export const CHANGE_SET_CONTRACT_VERSION = 'change-set-v1';

const PLAN_MODES = new Set(['expert_edit', 'generation']);
const RESOURCE_KINDS = new Set(['object', 'rule', 'operation', 'metric', 'script', 'binding', 'other']);
export const CHANGE_SET_DOMAIN_TOOLS = new Set([
  'create_object', 'create_rule', 'create_operation', 'create_metric',
  'patch_object', 'patch_rule', 'patch_operation', 'patch_metric',
]);
export const CHANGE_PLAN_DIRECT_TOOLS = new Set([
  'sync_schema_objects', 'patch_object_source_binding', 'delete_domain_resource',
  'stage_schema_semantic_changes', 'confirm_schema_semantics',
  'stage_script_source', 'stage_script_patch',
]);
const CHANGE_PLAN_ALLOWED_TOOLS = new Set([
  ...CHANGE_SET_DOMAIN_TOOLS,
  ...CHANGE_PLAN_DIRECT_TOOLS,
]);
const MUTATION_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/u;
const CLIENT_KEY = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/u;

type PlanStatus = 'active' | 'paused' | 'completed' | 'failed' | 'cancelled';
type TaskStatus = 'pending' | 'staged' | 'validated' | 'waiting_user'
  | 'accepted' | 'rejected' | 'failed' | 'cancelled' | 'skipped';
type ChangeSetStatus = 'staging' | 'staged' | 'validated' | 'waiting_user'
  | 'accepted' | 'rejected' | 'failed' | 'discarded' | 'cancelled';

interface PlanRow {
  id: string;
  project_id: string;
  run_id: string;
  goal: string;
  mode: string;
  status: PlanStatus;
  approval_status: 'pending' | 'approved' | 'rejected';
  approved_by: string | null;
  approved_at: string | null;
  version: number;
  expected_revision: string;
  created_at: string;
  updated_at: string;
}

interface TaskRow {
  id: string;
  plan_id: string;
  client_key: string;
  ordinal: number;
  title: string;
  resource_kind: string;
  target_key: string;
  allowed_tools_json: string;
  depends_on_json: string;
  acceptance: string;
  required: number;
  status: TaskStatus;
  change_set_id: string | null;
  updated_at: string;
}

interface ChangeSetRow {
  id: string;
  project_id: string;
  run_id: string;
  plan_id: string;
  mutation_id: string;
  request_hash: string;
  expected_revision: string;
  resulting_revision: string | null;
  candidate_revision: string | null;
  group_id: string | null;
  status: ChangeSetStatus;
  actions_json: string;
  receipt_json: string | null;
  error_json: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunCancellationReconciliation {
  projectId: string;
  runId: string;
  requested: RunCancellation | null;
  quiescent: boolean;
  pendingChangeSets: Array<{ id: string; mutationId: string; status: string }>;
  settledChangeSets: string[];
  settledMutations: string[];
  pausedPlanIds: string[];
  /** @deprecated 保留响应兼容；正常停止不再取消计划。 */
  cancelledPlanIds: string[];
}

export interface ChangeSetActionInput {
  taskId: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ScriptCandidateDependencyAuthorization {
  planId: string;
  taskId: string;
  allowedGroupIds: string[];
  allowedPaths: string[];
}

interface TaskInput {
  clientKey: string;
  title: string;
  resourceKind: string;
  targetKey: string;
  allowedTools: string[];
  dependsOn: string[];
  acceptance: string;
  required: boolean;
}

export class ChangeControlError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly statusCode = 409,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = 'ChangeControlError';
  }
}

export class ChangeControlStore {
  constructor(private readonly db: DatabaseSync) {}

  createPlan(input: {
    projectId: string;
    runId: string;
    goal: unknown;
    mode: unknown;
    expectedRevision: unknown;
    currentRevision: string;
    tasks: unknown;
  }) {
    const existing = this.activePlan(input.projectId, input.runId);
    if (existing) {
      throw new ChangeControlError(
        'CHANGE_PLAN_ALREADY_ACTIVE',
        '当前运行已经有一份进行中的建模计划，请读取或修订原计划',
        409,
        { plan_id: existing.id },
      );
    }
    const paused = this.pausedPlanForProject(input.projectId);
    if (paused) {
      throw new ChangeControlError(
        'PAUSED_CHANGE_PLAN_DECISION_REQUIRED',
        '当前项目还有暂停的建模计划，请先继续该计划或明确结束后重新规划',
        409,
        { plan_id: paused.id, retryable: false, requires_user_decision: true },
      );
    }
    const goal = requiredText(input.goal, 'goal', 4_000);
    const mode = String(input.mode ?? 'expert_edit');
    if (!PLAN_MODES.has(mode)) throw invalid('mode', '计划模式只能是明确修改或正常生成');
    const expectedRevision = requiredText(input.expectedRevision, 'expected_revision', 200);
    if (expectedRevision !== input.currentRevision) {
      throw new ChangeControlError('REVISION_CONFLICT', '知识包内容已经变化，请基于最新内容重新规划', 409, {
        expected_revision: expectedRevision,
        current_revision: input.currentRevision,
        retryable: false,
      });
    }
    const tasks = parseTasks(input.tasks);
    const planId = `cp_${randomUUID().replaceAll('-', '').slice(0, 20)}`;
    const idsByClientKey = new Map(tasks.map((task, index) => [task.clientKey, `${planId}_t${index + 1}`]));
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const approvalStatus = mode === 'generation' ? 'pending' : 'approved';
      this.db.prepare(`INSERT INTO agent_change_plans (
        id, project_id, run_id, goal, mode, status, approval_status, approved_by, approved_at,
        version, expected_revision, created_at, updated_at
      ) VALUES (?,?,?,?,?,'active',?,?,?,1,?,?,?)`).run(
        planId, input.projectId, input.runId, goal, mode, approvalStatus,
        mode === 'generation' ? null : 'user_request',
        mode === 'generation' ? null : timestamp,
        expectedRevision, timestamp, timestamp,
      );
      const insert = this.db.prepare(`INSERT INTO agent_change_plan_tasks (
        id, plan_id, client_key, ordinal, title, resource_kind, target_key,
        allowed_tools_json, depends_on_json, acceptance, required, status, updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)`);
      tasks.forEach((task, index) => insert.run(
        idsByClientKey.get(task.clientKey)!,
        planId,
        task.clientKey,
        index + 1,
        task.title,
        task.resourceKind,
        task.targetKey,
        JSON.stringify(task.allowedTools),
        JSON.stringify(task.dependsOn.map((key) => idsByClientKey.get(key)!)),
        task.acceptance,
        task.required ? 1 : 0,
        timestamp,
      ));
      this.event(input.projectId, input.runId, planId, null, 'plan.created', '已建立受控建模计划', {
        taskCount: tasks.length, mode, approvalStatus,
      });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.snapshot(input.projectId, input.runId, planId);
  }

  revisePlan(input: {
    projectId: string;
    runId: string;
    planId: unknown;
    expectedVersion: unknown;
    reason: unknown;
    addTasks: unknown;
    skipTaskIds: unknown;
    scopeApproved: unknown;
  }) {
    const plan = this.requirePlan(input.projectId, input.runId, String(input.planId ?? ''));
    if (plan.status !== 'active') throw new ChangeControlError('CHANGE_PLAN_CLOSED', '这份建模计划已经结束', 409);
    const expectedVersion = Number(input.expectedVersion);
    if (!Number.isInteger(expectedVersion) || expectedVersion !== plan.version) {
      throw new ChangeControlError('CHANGE_PLAN_VERSION_CONFLICT', '建模计划已经变化，请重新读取后再修订', 409, {
        expected_version: expectedVersion,
        current_version: plan.version,
      });
    }
    const reason = requiredText(input.reason, 'reason', 1_000);
    const additions = input.addTasks === undefined ? [] : parseTasks(input.addTasks, true);
    const skipIds = stringArray(input.skipTaskIds, 'skip_task_ids', 100);
    if (!additions.length && !skipIds.length) throw invalid('revision', '至少新增一项任务或跳过一项可选任务');
    const currentTasks = this.taskRows(plan.id);
    if (additions.length && currentTasks.some((task) => task.status !== 'pending') && input.scopeApproved !== true) {
      throw new ChangeControlError(
        'CHANGE_PLAN_SCOPE_APPROVAL_REQUIRED',
        '执行开始后扩大修改范围需要先由用户明确确认',
        409,
        { retryable: false, requires_user_decision: true },
      );
    }
    const existingKeys = new Set(currentTasks.map((task) => task.client_key));
    for (const task of additions) {
      if (existingKeys.has(task.clientKey)) throw invalid('add_tasks.client_key', `任务标识重复：${task.clientKey}`);
      for (const dependency of task.dependsOn) {
        if (!existingKeys.has(dependency) && !additions.some((item) => item.clientKey === dependency)) {
          throw invalid('add_tasks.depends_on', `依赖任务不存在：${dependency}`);
        }
      }
      existingKeys.add(task.clientKey);
    }
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const taskId of skipIds) {
        const task = currentTasks.find((item) => item.id === taskId);
        if (!task) throw invalid('skip_task_ids', `任务不属于当前计划：${taskId}`);
        if (task.required) throw invalid('skip_task_ids', `必选任务不能跳过：${task.title}`);
        if (task.status !== 'pending') throw invalid('skip_task_ids', `只有尚未执行的任务可以跳过：${task.title}`);
        this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'skipped', updated_at = ? WHERE id = ?`)
          .run(timestamp, task.id);
      }
      const allTasks = this.taskRows(plan.id);
      const idByKey = new Map(allTasks.map((task) => [task.client_key, task.id]));
      additions.forEach((task, offset) => idByKey.set(
        task.clientKey,
        `${plan.id}_t${allTasks.length + offset + 1}`,
      ));
      const insert = this.db.prepare(`INSERT INTO agent_change_plan_tasks (
        id, plan_id, client_key, ordinal, title, resource_kind, target_key,
        allowed_tools_json, depends_on_json, acceptance, required, status, updated_at
      ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)`);
      additions.forEach((task, offset) => insert.run(
        idByKey.get(task.clientKey)!, plan.id, task.clientKey, allTasks.length + offset + 1,
        task.title, task.resourceKind, task.targetKey, JSON.stringify(task.allowedTools),
        JSON.stringify(task.dependsOn.map((key) => idByKey.get(key)!)), task.acceptance,
        task.required ? 1 : 0, timestamp,
      ));
      this.db.prepare(`UPDATE agent_change_plans SET version = version + 1,
        approval_status = CASE WHEN ? > 0 THEN 'pending' ELSE approval_status END,
        approved_by = CASE WHEN ? > 0 THEN NULL ELSE approved_by END,
        approved_at = CASE WHEN ? > 0 THEN NULL ELSE approved_at END,
        updated_at = ? WHERE id = ?`)
        .run(additions.length, additions.length, additions.length, timestamp, plan.id);
      this.event(plan.project_id, plan.run_id, plan.id, null, 'plan.revised', '已记录建模计划调整', {
        reason, added: additions.length, skipped: skipIds.length,
      });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.snapshot(input.projectId, input.runId, plan.id);
  }

  activePlan(projectId: string, runId: string): PlanRow | null {
    return (this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE project_id = ? AND run_id = ? AND status = 'active'
      ORDER BY created_at DESC LIMIT 1`).get(projectId, runId) as PlanRow | undefined) ?? null;
  }

  activePlanForProject(projectId: string, excludeRunId = ''): PlanRow | null {
    return (this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE project_id = ? AND status = 'active' AND run_id <> ?
      ORDER BY created_at DESC LIMIT 1`).get(projectId, excludeRunId) as PlanRow | undefined) ?? null;
  }

  pausedPlanForProject(projectId: string): PlanRow | null {
    return (this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE project_id = ? AND status = 'paused'
      ORDER BY updated_at DESC LIMIT 1`).get(projectId) as PlanRow | undefined) ?? null;
  }

  pausedPlanSnapshot(projectId: string) {
    const plan = this.pausedPlanForProject(projectId);
    return plan ? this.snapshot(projectId, plan.run_id, plan.id) : null;
  }

  incompleteRequiredPlanTasks(projectId: string): Array<{
    planId: string;
    planStatus: PlanStatus;
    taskId: string;
    title: string;
    taskStatus: TaskStatus;
  }> {
    return (this.db.prepare(`SELECT
        plan.id AS plan_id, plan.status AS plan_status,
        task.id AS task_id, task.title, task.status AS task_status
      FROM agent_change_plans plan
      JOIN agent_change_plan_tasks task ON task.plan_id = plan.id
      WHERE plan.project_id = ?
        AND plan.status IN ('active', 'paused')
        AND task.required = 1
        AND task.status NOT IN ('accepted', 'skipped')
      ORDER BY plan.created_at, task.ordinal`).all(projectId) as Array<{
        plan_id: string;
        plan_status: PlanStatus;
        task_id: string;
        title: string;
        task_status: TaskStatus;
      }>).map((row) => ({
        planId: row.plan_id,
        planStatus: row.plan_status,
        taskId: row.task_id,
        title: row.title,
        taskStatus: row.task_status,
      }));
  }

  resumePausedPlan(input: {
    projectId: string;
    planId: string;
    previousRunId: string;
    nextRunId: string;
    actor: string;
  }): void {
    const plan = this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE id = ? AND project_id = ?`).get(input.planId, input.projectId) as PlanRow | undefined;
    if (!plan) throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '暂停的建模计划不存在', 404);
    if (plan.status === 'active' && plan.run_id === input.nextRunId) return;
    if (plan.status !== 'paused' || plan.run_id !== input.previousRunId) {
      throw new ChangeControlError(
        'PAUSED_CHANGE_PLAN_STALE',
        '暂停计划状态已经变化，请刷新后再操作',
        409,
        { plan_id: plan.id, plan_status: plan.status, plan_run_id: plan.run_id },
      );
    }
    const active = this.activePlanForProject(input.projectId);
    if (active) {
      throw new ChangeControlError(
        'CHANGE_PLAN_ALREADY_ACTIVE',
        '当前项目已经有另一份进行中的建模计划',
        409,
        { plan_id: active.id },
      );
    }
    const timestamp = nowIso();
    this.db.prepare(`UPDATE agent_change_plans
      SET status = 'active', run_id = ?, version = version + 1, updated_at = ?
      WHERE id = ? AND status = 'paused' AND run_id = ?`)
      .run(input.nextRunId, timestamp, plan.id, input.previousRunId);
    this.db.prepare(`UPDATE agent_change_sets SET run_id = ?, updated_at = ?
      WHERE plan_id = ? AND run_id = ?`)
      .run(input.nextRunId, timestamp, plan.id, input.previousRunId);
    this.db.prepare(`UPDATE agent_change_control_events SET run_id = ?
      WHERE plan_id = ? AND run_id = ?`)
      .run(input.nextRunId, plan.id, input.previousRunId);
    this.event(input.projectId, input.nextRunId, plan.id, null, 'plan.resumed',
      '已从停止点继续未完成建模计划', {
        actor: input.actor,
        previous_run_id: input.previousRunId,
        next_run_id: input.nextRunId,
      });
  }

  transferActiveRun(projectId: string, previousRunId: string, nextRunId: string): void {
    if (!previousRunId || previousRunId === nextRunId) return;
    const plan = this.activePlan(projectId, previousRunId);
    if (!plan) return;
    this.db.prepare(`UPDATE agent_change_plans SET run_id = ?, updated_at = ?
      WHERE id = ? AND project_id = ? AND run_id = ? AND status = 'active'`)
      .run(nextRunId, nowIso(), plan.id, projectId, previousRunId);
    this.db.prepare('UPDATE agent_change_sets SET run_id = ?, updated_at = ? WHERE plan_id = ? AND run_id = ?')
      .run(nextRunId, nowIso(), plan.id, previousRunId);
    this.db.prepare('UPDATE agent_change_control_events SET run_id = ? WHERE plan_id = ? AND run_id = ?')
      .run(nextRunId, plan.id, previousRunId);
  }

  authorizeDirectTask(input: {
    projectId: string;
    runId: string;
    taskId: string;
    tool: string;
    targetKey: string;
  }) {
    const plan = this.activePlan(input.projectId, input.runId);
    if (!plan) return null;
    if (plan.approval_status !== 'approved') {
      throw new ChangeControlError(
        'CHANGE_PLAN_APPROVAL_REQUIRED',
        '正常生成计划尚未由用户批准，不能开始写入',
        409,
        { retryable: false, requires_user_decision: true, plan_id: plan.id },
      );
    }
    const task = this.taskRows(plan.id).find((item) => item.id === input.taskId);
    if (!task) throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', '修改动作不属于当前计划', 409);
    const allowedTools = parseStringArray(task.allowed_tools_json);
    if (task.status === 'pending'
      && ['stage_schema_semantic_changes', 'confirm_schema_semantics'].includes(input.tool)
      && allowedTools.includes('sync_schema_objects')) {
      throw new ChangeControlError(
        'CHANGE_SET_DEPENDENCY_MISSING',
        'Schema 物理对象尚未同步，不能提前进入末尾业务语义阶段',
        409,
        { task_id: task.id, required_tool: 'sync_schema_objects', retryable: false },
      );
    }
    if (!['pending', 'staged'].includes(task.status)) {
      throw new ChangeControlError('CHANGE_PLAN_TASK_ALREADY_SETTLED', `计划任务已经处理：${task.title}`, 409, {
        task_id: task.id, status: task.status,
      });
    }
    if (!allowedTools.includes(input.tool)) {
      throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', `计划未授权这项修改：${task.title}`, 409, {
        task_id: task.id, tool: input.tool,
      });
    }
    if (task.target_key !== input.targetKey) {
      throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', `实际修改目标与计划不一致：${task.title}`, 409, {
        task_id: task.id, expected_target: task.target_key, actual_target: input.targetKey,
      });
    }
    const taskById = new Map(this.taskRows(plan.id).map((item) => [item.id, item]));
    const unresolved = parseStringArray(task.depends_on_json).filter((dependencyId) => {
      const dependency = taskById.get(dependencyId);
      return dependency && !['staged', 'validated', 'waiting_user', 'accepted', 'skipped'].includes(dependency.status);
    });
    if (unresolved.length) {
      throw new ChangeControlError('CHANGE_SET_DEPENDENCY_MISSING', `计划任务依赖尚未完成：${task.title}`, 409, {
        task_id: task.id, unresolved,
      });
    }
    return { plan, task };
  }

  scriptCandidateDependencyAuthorization(input: {
    projectId: string;
    runId: string;
    taskId: string;
    tool: string;
    targetKey: string;
  }): ScriptCandidateDependencyAuthorization | null {
    // Incremental scripts may consume staged knowledge only through their declared dependency chain.
    // Returning both group IDs and paths prevents an unrelated candidate from sharing this exception.
    const plan = this.activePlan(input.projectId, input.runId);
    if (!plan || plan.mode !== 'expert_edit' || plan.approval_status !== 'approved') return null;
    const tasks = this.taskRows(plan.id);
    const taskById = new Map(tasks.map((task) => [task.id, task]));
    const task = taskById.get(input.taskId);
    if (!task || task.resource_kind !== 'script' || task.target_key !== input.targetKey) return null;
    if (!['pending', 'staged'].includes(task.status)) return null;
    if (!parseStringArray(task.allowed_tools_json).includes(input.tool)) return null;

    const allowedGroupIds = new Set<string>();
    const allowedPaths = new Set<string>();
    const visited = new Set<string>();
    const pending = [...parseStringArray(task.depends_on_json)];
    while (pending.length) {
      const dependencyId = pending.pop()!;
      if (visited.has(dependencyId)) continue;
      visited.add(dependencyId);
      const dependency = taskById.get(dependencyId);
      if (!dependency || !['staged', 'validated', 'waiting_user', 'accepted', 'skipped'].includes(dependency.status)) {
        return null;
      }
      pending.push(...parseStringArray(dependency.depends_on_json));
      const prefix = {
        object: 'objects',
        operation: 'operations',
        rule: 'rules',
        metric: 'metrics',
      }[dependency.resource_kind];
      if (!prefix || ['accepted', 'skipped'].includes(dependency.status)) continue;
      if (!dependency.change_set_id) return null;
      const changeSet = this.db.prepare(`SELECT * FROM agent_change_sets
        WHERE id = ? AND project_id = ? AND run_id = ? AND plan_id = ?`)
        .get(dependency.change_set_id, input.projectId, input.runId, plan.id) as ChangeSetRow | undefined;
      if (!changeSet?.group_id || !['staged', 'validated', 'waiting_user'].includes(changeSet.status)) return null;
      allowedGroupIds.add(changeSet.group_id);
      allowedPaths.add(`${prefix}/${dependency.target_key}.md`);
    }
    if (!allowedGroupIds.size || !allowedPaths.size) return null;
    return {
      planId: plan.id,
      taskId: task.id,
      allowedGroupIds: [...allowedGroupIds],
      allowedPaths: [...allowedPaths],
    };
  }

  completeDirectTask(input: {
    projectId: string;
    runId: string;
    taskId: string;
    tool: string;
    receiptId: string;
    resultingRevision?: string;
  }) {
    const plan = this.activePlan(input.projectId, input.runId);
    if (!plan) return null;
    const timestamp = nowIso();
    const result = this.db.prepare(`UPDATE agent_change_plan_tasks SET
      status = 'staged', change_set_id = ?, updated_at = ?
      WHERE id = ? AND plan_id = ? AND status IN ('pending', 'staged')`)
      .run(`direct:${input.receiptId}`, timestamp, input.taskId, plan.id);
    if (Number(result.changes) !== 1) {
      throw new ChangeControlError('CHANGE_PLAN_TASK_ALREADY_SETTLED', '计划任务状态已经变化，请重新读取计划', 409);
    }
    this.db.prepare(`UPDATE agent_change_plans SET version = version + 1,
      expected_revision = CASE WHEN ? <> '' THEN ? ELSE expected_revision END,
      updated_at = ? WHERE id = ?`)
      .run(input.resultingRevision ?? '', input.resultingRevision ?? '', timestamp, plan.id);
    const summary = ['sync_schema_objects', 'stage_schema_semantic_changes', 'confirm_schema_semantics']
      .includes(input.tool)
      ? 'Schema 同步计划任务已推进'
      : '受控独立任务已形成待确认修改';
    this.event(input.projectId, input.runId, plan.id, null, 'plan.direct_task_staged', summary, {
      taskId: input.taskId, tool: input.tool, receiptId: input.receiptId,
    });
    return this.snapshot(input.projectId, input.runId, plan.id);
  }

  respondPlan(input: {
    projectId: string;
    planId: string;
    username: string;
    approved: boolean;
  }) {
    const plan = this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE id = ? AND project_id = ?`).get(input.planId, input.projectId) as PlanRow | undefined;
    if (!plan) throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '建模计划不存在', 404);
    if (plan.status !== 'active') throw new ChangeControlError('CHANGE_PLAN_CLOSED', '这份建模计划已经结束', 409);
    if (plan.approval_status !== 'pending') {
      if ((plan.approval_status === 'approved') === input.approved) {
        return this.snapshot(plan.project_id, plan.run_id, plan.id);
      }
      throw new ChangeControlError('CHANGE_PLAN_DECISION_CONFLICT', '这份计划已经做过相反决定', 409);
    }
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_plans SET
        approval_status = ?, approved_by = ?, approved_at = ?,
        status = CASE WHEN ? = 'rejected' THEN 'cancelled' ELSE status END,
        version = version + 1, updated_at = ?
        WHERE id = ? AND approval_status = 'pending'`)
        .run(input.approved ? 'approved' : 'rejected', input.username, timestamp,
          input.approved ? 'approved' : 'rejected', timestamp, plan.id);
      this.event(plan.project_id, plan.run_id, plan.id, null,
        input.approved ? 'plan.approved' : 'plan.rejected',
        input.approved ? '用户已批准建模计划' : '用户已拒绝建模计划',
        { actor: input.username });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.snapshot(plan.project_id, plan.run_id, plan.id);
  }

  snapshot(projectId: string, runId: string, rawPlanId = '') {
    const plan = rawPlanId
      ? this.requirePlan(projectId, runId, rawPlanId)
      : this.activePlan(projectId, runId);
    if (!plan) {
      const otherRun = this.activePlanForProject(projectId, runId);
      if (otherRun) {
        throw new ChangeControlError('CHANGE_PLAN_STALE_RUN', '当前运行还没有接续服务重启前的建模计划', 409, {
          plan_id: otherRun.id,
          expected_run_id: otherRun.run_id,
          current_run_id: runId,
          next: ['从最近检查点恢复后继续，或安全终止失效计划后重新规划'],
          retryable: false,
        });
      }
      throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '当前运行还没有受控建模计划', 404);
    }
    const tasks = this.taskRows(plan.id).map((task) => ({
      id: task.id,
      client_key: task.client_key,
      ordinal: task.ordinal,
      title: task.title,
      resource_kind: task.resource_kind,
      target_key: task.target_key,
      allowed_tools: parseStringArray(task.allowed_tools_json),
      depends_on: parseStringArray(task.depends_on_json),
      acceptance: task.acceptance,
      required: Boolean(task.required),
      status: task.status,
      change_set_id: task.change_set_id,
    }));
    const completed = tasks.filter((task) => ['accepted', 'skipped'].includes(task.status)).length;
    return {
      id: plan.id,
      contract_version: CHANGE_PLAN_CONTRACT_VERSION,
      goal: plan.goal,
      mode: plan.mode,
      status: plan.status,
      approval_status: plan.approval_status,
      approved_by: plan.approved_by,
      approved_at: plan.approved_at,
      version: plan.version,
      expected_revision: plan.expected_revision,
      task_count: tasks.length,
      completed_count: completed,
      remaining_count: tasks.filter((task) => task.required && !['accepted', 'skipped'].includes(task.status)).length,
      tasks,
      created_at: plan.created_at,
      updated_at: plan.updated_at,
    };
  }

  prepareChangeSet(input: {
    projectId: string;
    runId: string;
    planId: string;
    mutationId: string;
    requestHash: string;
    expectedRevision: string;
    actions: ChangeSetActionInput[];
  }): { changeSetId: string; replay?: Record<string, unknown>; staging?: boolean } {
    if (!MUTATION_ID.test(input.mutationId)) throw invalid('mutation_id', 'mutation_id 格式无效');
    const existing = this.db.prepare(`SELECT * FROM agent_change_sets
      WHERE project_id = ? AND mutation_id = ?`).get(input.projectId, input.mutationId) as ChangeSetRow | undefined;
    if (existing) {
      if (existing.request_hash !== input.requestHash) {
        throw new ChangeControlError('MUTATION_ID_REUSED', '这个操作标识已经用于另一批修改', 409);
      }
      if (existing.receipt_json) return {
        changeSetId: existing.id,
        replay: JSON.parse(existing.receipt_json) as Record<string, unknown>,
      };
      return { changeSetId: existing.id, staging: existing.status === 'staging' };
    }
    const { plan, actions } = this.authorizeChangeSet(input);
    const changeSetId = `cs_${randomUUID().replaceAll('-', '').slice(0, 20)}`;
    const timestamp = nowIso();
    this.db.prepare(`INSERT INTO agent_change_sets (
      id, project_id, run_id, plan_id, mutation_id, request_hash, expected_revision,
      status, actions_json, created_at, updated_at
    ) VALUES (?,?,?,?,?,?,?,'staging',?,?,?)`).run(
      changeSetId, input.projectId, input.runId, plan.id, input.mutationId,
      input.requestHash, input.expectedRevision, JSON.stringify(actions), timestamp, timestamp,
    );
    this.event(input.projectId, input.runId, plan.id, changeSetId, 'change_set.started', '正在原子应用计划任务', {
      actionCount: actions.length,
    });
    return { changeSetId };
  }

  completeChangeSet(changeSetId: string, receipt: Record<string, unknown>) {
    const row = this.requireChangeSet(changeSetId);
    const timestamp = nowIso();
    const group = isRecord(receipt.change_group) ? receipt.change_group : {};
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_sets SET status = 'staged', resulting_revision = ?,
        group_id = ?, receipt_json = ?, error_json = NULL, updated_at = ? WHERE id = ?`).run(
        String(receipt.working_revision ?? ''), String(group.id ?? ''), JSON.stringify(receipt), timestamp, row.id,
      );
      const actionTaskIds = parseActions(row.actions_json).map((action) => action.taskId);
      const update = this.db.prepare(`UPDATE agent_change_plan_tasks
        SET status = 'staged', change_set_id = ?, updated_at = ?
        WHERE plan_id = ? AND id = ? AND status = 'pending'`);
      for (const taskId of actionTaskIds) update.run(row.id, timestamp, row.plan_id, taskId);
      const resultingRevision = String(receipt.working_revision ?? '');
      this.db.prepare(`UPDATE agent_change_plans SET version = version + 1,
        expected_revision = CASE WHEN ? <> '' THEN ? ELSE expected_revision END,
        updated_at = ? WHERE id = ?`)
        .run(resultingRevision, resultingRevision, timestamp, row.plan_id);
      this.event(row.project_id, row.run_id, row.plan_id, row.id, 'change_set.staged', '计划任务已形成待确认修改', {
        workingRevision: receipt.working_revision, changedPaths: receipt.changed_paths,
      });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.changeSetReceipt(row.project_id, row.run_id, row.mutation_id);
  }

  failChangeSet(changeSetId: string, error: Record<string, unknown>) {
    const row = this.requireChangeSet(changeSetId);
    const timestamp = nowIso();
    this.db.prepare(`UPDATE agent_change_sets SET status = 'failed', error_json = ?, updated_at = ? WHERE id = ?`)
      .run(JSON.stringify(error), timestamp, row.id);
    this.event(row.project_id, row.run_id, row.plan_id, row.id, 'change_set.failed', '本批修改未生效', error);
  }

  refreshChangeSetRevision(input: {
    changeSetId: string;
    projectId: string;
    runId: string;
    expectedRevision: string;
  }) {
    const row = this.requireChangeSet(input.changeSetId);
    if (row.project_id !== input.projectId || row.run_id !== input.runId || row.status !== 'staging') {
      throw new ChangeControlError('CHANGE_SET_NOT_PENDING', '这批修改已结束或不属于当前运行，不能自动重试', 409);
    }
    // Recheck approval, task status and dependencies after refreshing the snapshot.
    // Keep normalized task IDs and original resource hashes unchanged.
    this.authorizeChangeSet({
      projectId: input.projectId, runId: input.runId, planId: row.plan_id,
      actions: parseActions(row.actions_json),
    });
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_sets SET expected_revision = ?, updated_at = ? WHERE id = ?`)
        .run(input.expectedRevision, timestamp, row.id);
      this.event(row.project_id, row.run_id, row.plan_id, row.id, 'change_set.rebased',
        '目标内容校验通过，已同步最新版本并自动重试', {
          previousRevision: row.expected_revision, expectedRevision: input.expectedRevision, attempt: 1,
        });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private authorizeChangeSet(input: {
    projectId: string;
    runId: string;
    planId: string;
    actions: ChangeSetActionInput[];
  }) {
    const plan = this.requirePlan(input.projectId, input.runId, input.planId);
    if (plan.status !== 'active') throw new ChangeControlError('CHANGE_PLAN_CLOSED', '这份建模计划已经结束', 409);
    if (plan.approval_status !== 'approved') {
      throw new ChangeControlError(
        'CHANGE_PLAN_APPROVAL_REQUIRED',
        '正常生成计划尚未由用户批准，不能开始写入',
        409,
        { retryable: false, requires_user_decision: true, plan_id: plan.id },
      );
    }
    const tasks = this.taskRows(plan.id);
    const taskById = new Map(tasks.map((task) => [task.id, task]));
    const actions = input.actions.map((action) => {
      const aligned = alignSameKindActionTask(tasks, taskById, action);
      const task = taskById.get(aligned.taskId);
      return task ? fillPlannedActionTarget(task, aligned) : aligned;
    });
    const included = new Set(actions.map((action) => action.taskId));
    if (included.size !== actions.length) throw invalid('actions.task_id', '同一批不能重复执行同一计划任务');
    for (const action of actions) {
      const task = taskById.get(action.taskId);
      if (!task) throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', '修改动作不属于当前计划', 409, { task_id: action.taskId });
      if (task.status !== 'pending') {
        throw new ChangeControlError('CHANGE_PLAN_TASK_ALREADY_SETTLED', `计划任务已经处理：${task.title}`, 409, {
          task_id: task.id, status: task.status,
        });
      }
      const allowed = new Set(parseStringArray(task.allowed_tools_json));
      if (!allowed.has(action.tool)) {
        throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', `计划未授权这项修改：${task.title}`, 409, {
          task_id: task.id, tool: action.tool, allowed_tools: [...allowed],
        });
      }
      assertActionTarget(task, action);
      const dependencies = parseStringArray(task.depends_on_json);
      const unresolved = dependencies.filter((dependencyId) => {
        const dependency = taskById.get(dependencyId);
        return !included.has(dependencyId)
          && dependency && !['staged', 'validated', 'waiting_user', 'accepted', 'skipped'].includes(dependency.status);
      });
      if (unresolved.length) {
        throw new ChangeControlError('CHANGE_SET_DEPENDENCY_MISSING', `计划任务依赖尚未完成：${task.title}`, 409, {
          task_id: task.id, unresolved,
        });
      }
    }
    return { plan, actions };
  }

  changeSetReceipt(projectId: string, runId: string, mutationId: string) {
    const row = this.db.prepare(`SELECT * FROM agent_change_sets
      WHERE project_id = ? AND run_id = ? AND mutation_id = ?`).get(projectId, runId, mutationId) as ChangeSetRow | undefined;
    if (!row) throw new ChangeControlError('CHANGE_SET_NOT_FOUND', '没有找到这批修改的执行回执', 404);
    return changeSetPublic(row);
  }

  changeSetContext(projectId: string, runId: string, mutationId: string) {
    const row = this.db.prepare(`SELECT * FROM agent_change_sets
      WHERE project_id = ? AND run_id = ? AND mutation_id = ?`).get(projectId, runId, mutationId) as ChangeSetRow | undefined;
    if (!row) throw new ChangeControlError('CHANGE_SET_NOT_FOUND', '没有找到这批修改的执行回执', 404);
    return {
      id: row.id,
      requestHash: row.request_hash,
      status: row.status,
      receipt: row.receipt_json ? JSON.parse(row.receipt_json) as Record<string, unknown> : null,
      error: row.error_json ? JSON.parse(row.error_json) as Record<string, unknown> : null,
    };
  }

  readyForValidation(projectId: string, runId: string, planId: string) {
    const plan = this.requirePlan(projectId, runId, planId);
    const tasks = this.taskRows(plan.id);
    const incomplete = tasks.filter((task) => task.required
      && !['staged', 'validated', 'waiting_user', 'accepted'].includes(task.status));
    if (incomplete.length) {
      throw new ChangeControlError('CHANGE_PLAN_INCOMPLETE', `还有 ${incomplete.length} 项必选任务没有形成候选`, 409, {
        incomplete_tasks: incomplete.map((task) => ({ id: task.id, title: task.title, status: task.status })),
        retryable: false,
      });
    }
    return plan;
  }

  markValidated(projectId: string, runId: string, planId: string, result: Record<string, unknown>) {
    const plan = this.readyForValidation(projectId, runId, planId);
    const candidateRevision = String(result.candidate_revision ?? '');
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_sets SET status = 'validated', candidate_revision = ?,
        updated_at = ? WHERE plan_id = ? AND status = 'staged'`).run(candidateRevision, timestamp, plan.id);
      this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'validated', updated_at = ?
        WHERE plan_id = ? AND status = 'staged'`).run(timestamp, plan.id);
      this.event(projectId, runId, plan.id, null, 'plan.validated', '完整建模计划已通过候选校验', {
        candidateRevision,
      });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return this.snapshot(projectId, runId, plan.id);
  }

  markWaitingUser(projectId: string, runId: string, candidateRevision: string) {
    const plan = this.activePlan(projectId, runId);
    if (!plan) return;
    const timestamp = nowIso();
    this.db.prepare(`UPDATE agent_change_sets SET status = 'waiting_user', candidate_revision = ?, updated_at = ?
      WHERE plan_id = ? AND status IN ('staged', 'validated')`).run(candidateRevision, timestamp, plan.id);
    this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'waiting_user', updated_at = ?
      WHERE plan_id = ? AND status IN ('staged', 'validated')`).run(timestamp, plan.id);
  }

  settlePlan(projectId: string, runId: string, outcome: 'accepted' | 'rejected', candidateRevision: string) {
    const plan = this.activePlan(projectId, runId);
    if (!plan) return;
    const timestamp = nowIso();
    const planStatus: PlanStatus = outcome === 'accepted' ? 'completed' : 'failed';
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_sets SET status = ?, candidate_revision = ?, updated_at = ?
        WHERE plan_id = ? AND status IN ('staged', 'validated', 'waiting_user')`)
        .run(outcome, candidateRevision, timestamp, plan.id);
      this.db.prepare(`UPDATE agent_change_plan_tasks SET status = ?, updated_at = ?
        WHERE plan_id = ? AND status IN ('staged', 'validated', 'waiting_user')`)
        .run(outcome, timestamp, plan.id);
      this.db.prepare(`UPDATE agent_change_plans SET status = ?, version = version + 1, updated_at = ? WHERE id = ?`)
        .run(planStatus, timestamp, plan.id);
      this.event(projectId, runId, plan.id, null, `plan.${outcome}`, outcome === 'accepted'
        ? '建模计划已全部确认'
        : '建模计划已退回', { candidateRevision });
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  discardPlan(projectId: string, runId: string, planId: string) {
    const plan = this.requirePlan(projectId, runId, planId);
    const sets = this.db.prepare(`SELECT * FROM agent_change_sets WHERE plan_id = ? ORDER BY created_at`)
      .all(plan.id) as unknown as ChangeSetRow[];
    if (sets.some((item) => ['validated', 'waiting_user', 'accepted'].includes(item.status))) {
      throw new ChangeControlError('CHANGE_SET_FROZEN', '已经进入确认的修改不能直接丢弃，请先退回确认', 409);
    }
    const timestamp = nowIso();
    this.db.prepare(`UPDATE agent_change_sets SET status = 'discarded', updated_at = ?
      WHERE plan_id = ? AND status IN ('staging', 'staged', 'failed')`).run(timestamp, plan.id);
    this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'cancelled', updated_at = ?
      WHERE plan_id = ? AND status NOT IN ('accepted', 'skipped')`).run(timestamp, plan.id);
    this.db.prepare(`UPDATE agent_change_plans SET status = 'cancelled', version = version + 1,
      updated_at = ? WHERE id = ?`).run(timestamp, plan.id);
  }

  private requirePlan(projectId: string, runId: string, planId: string): PlanRow {
    const row = this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE id = ? AND project_id = ?`).get(planId, projectId) as PlanRow | undefined;
    if (!row) throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '建模计划不存在或不属于当前运行', 404);
    if (row.run_id !== runId) {
      if (row.status === 'active') {
        throw new ChangeControlError(
          'CHANGE_PLAN_STALE_RUN',
          '建模计划属于另一段运行，服务重启后尚未接续',
          409,
          {
            plan_id: row.id,
            expected_run_id: row.run_id,
            current_run_id: runId,
            next: ['从最近检查点恢复后继续，或安全终止失效计划后重新规划'],
            retryable: false,
          },
        );
      }
      throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '建模计划不存在或不属于当前运行', 404);
    }
    return row;
  }

  safeTerminatePlan(input: {
    projectId: string;
    planId: string;
    actor: string;
  }): { terminated: boolean; planId: string } {
    const plan = this.db.prepare(`SELECT * FROM agent_change_plans
      WHERE id = ? AND project_id = ?`).get(input.planId, input.projectId) as PlanRow | undefined;
    if (!plan) throw new ChangeControlError('CHANGE_PLAN_NOT_FOUND', '建模计划不存在', 404);
    if (!['active', 'paused'].includes(plan.status)) {
      throw new ChangeControlError('CHANGE_PLAN_CLOSED', '这份建模计划已经结束，无需终止', 409);
    }
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      this.db.prepare(`UPDATE agent_change_plans SET status = 'cancelled', version = version + 1,
        updated_at = ? WHERE id = ? AND status IN ('active', 'paused')`).run(timestamp, plan.id);
      this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'cancelled', updated_at = ?
        WHERE plan_id = ? AND status NOT IN ('accepted', 'skipped', 'cancelled')`).run(timestamp, plan.id);
      // 候选修改与回执不删除，仅把未结算批次标记为取消，保留审计链。
      this.db.prepare(`UPDATE agent_change_sets SET status = 'cancelled', updated_at = ?
        WHERE plan_id = ? AND status IN ('staging', 'staged', 'failed')`).run(timestamp, plan.id);
      this.event(
        input.projectId,
        plan.run_id,
        plan.id,
        null,
        'plan.safe_terminated',
        '已安全终止失效建模计划，候选内容与审计记录保留',
        { actor: input.actor, plan_run_id: plan.run_id },
      );
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return { terminated: true, planId: plan.id };
  }

  safeTerminateActivePlansForProject(input: {
    projectId: string;
    actor: string;
    runId?: string;
  }): { terminated: number; planIds: string[] } {
    const plans = (input.runId
      ? this.db.prepare(`SELECT * FROM agent_change_plans
          WHERE project_id = ? AND status IN ('active', 'paused') AND run_id = ? ORDER BY created_at`)
          .all(input.projectId, input.runId)
      : this.db.prepare(`SELECT * FROM agent_change_plans
          WHERE project_id = ? AND status IN ('active', 'paused') ORDER BY created_at`)
          .all(input.projectId)) as unknown as PlanRow[];
    const planIds = plans.map((plan) => plan.id);
    if (!planIds.length) return { terminated: 0, planIds };
    const timestamp = nowIso();
    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const plan of plans) {
        this.db.prepare(`UPDATE agent_change_plans SET status = 'cancelled', version = version + 1,
          updated_at = ? WHERE id = ? AND status IN ('active', 'paused')`).run(timestamp, plan.id);
        this.db.prepare(`UPDATE agent_change_plan_tasks SET status = 'cancelled', updated_at = ?
          WHERE plan_id = ? AND status NOT IN ('accepted', 'skipped', 'cancelled')`).run(timestamp, plan.id);
        this.db.prepare(`UPDATE agent_change_sets SET status = 'cancelled', updated_at = ?
          WHERE plan_id = ? AND status IN ('staging', 'staged', 'failed')`).run(timestamp, plan.id);
        this.event(
          input.projectId,
          plan.run_id,
          plan.id,
          null,
          'plan.safe_terminated',
          '已安全终止失效建模计划，候选内容与审计记录保留',
          { actor: input.actor, plan_run_id: plan.run_id },
        );
      }
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
    return { terminated: planIds.length, planIds };
  }

  /**
   * Record the cancellation fence before the Python runtime starts draining
   * its tasks. StudioWriteService checks the same row inside its write
   * transaction, so either the write commits before the fence or is rejected.
   */
  requestRunCancellation(projectId: string, runId: string, reason = '用户已停止运行'): RunCancellation {
    return requestRunCancellation(this.db, projectId, runId, reason);
  }

  isRunCancellationRequested(projectId: string, runId: string): boolean {
    return isRunCancellationRequested(this.db, projectId, runId);
  }

  getRunCancellation(projectId: string, runId: string): RunCancellation | null {
    return getRunCancellation(this.db, projectId, runId);
  }

  resumeRunAfterCancellation(
    projectId: string,
    runId: string,
    actor = 'modeling-agent',
  ): RunCancellationReconciliation {
    // Recovery is allowed to continue the same plan after an unknown write
    // has been reconciled; plan cancellation belongs to the terminal cancel
    // path, not the resume path.
    const reconciliation = this.reconcileRunCancellation(projectId, runId, actor, false);
    if (reconciliation.quiescent) clearRunCancellation(this.db, projectId, runId);
    return reconciliation;
  }

  /**
   * Reconcile Change Sets which were marked staging when the caller lost its
   * HTTP response. A receipt already committed by Studio is promoted to
   * staged; an absent receipt remains pending and keeps cancellation honest.
   */
  reconcileRunCancellation(
    projectId: string,
    runId: string,
    actor = 'modeling-agent',
    finalizePlans = true,
  ): RunCancellationReconciliation {
    const requested = this.getRunCancellation(projectId, runId);
    const pendingChangeSets: RunCancellationReconciliation['pendingChangeSets'] = [];
    const settledChangeSets: string[] = [];
    const settledMutations: string[] = [];
    const rows = this.db.prepare(`SELECT * FROM agent_change_sets
      WHERE project_id = ? AND run_id = ? AND status = 'staging'
      ORDER BY created_at`).all(projectId, runId) as unknown as ChangeSetRow[];

    this.db.exec('BEGIN IMMEDIATE');
    try {
      for (const row of rows) {
        const receiptRow = this.db.prepare(`SELECT request_hash, response_json
          FROM studio_mutation_receipts
          WHERE project_id = ? AND client_mutation_id = ?`)
          .get(projectId, row.mutation_id) as { request_hash: string; response_json: string } | undefined;
        if (!receiptRow || receiptRow.request_hash !== row.request_hash) {
          pendingChangeSets.push({ id: row.id, mutationId: row.mutation_id, status: row.status });
          continue;
        }
        let response: Record<string, unknown>;
        try {
          response = JSON.parse(receiptRow.response_json) as Record<string, unknown>;
        } catch {
          pendingChangeSets.push({ id: row.id, mutationId: row.mutation_id, status: row.status });
          continue;
        }
        const workingRevision = String(response.workingRevision || response.working_revision || '');
        const changedPathsValue = response.changedPaths ?? response.changed_paths;
        const changedPaths = Array.isArray(changedPathsValue)
          ? changedPathsValue.map(String)
          : [];
        if (!workingRevision) {
          pendingChangeSets.push({ id: row.id, mutationId: row.mutation_id, status: row.status });
          continue;
        }
        const group = response.changeGroup && typeof response.changeGroup === 'object'
          ? response.changeGroup as Record<string, unknown>
          : {};
        const timestamp = nowIso();
        this.db.prepare(`UPDATE agent_change_sets SET status = 'staged',
          resulting_revision = ?, group_id = ?, receipt_json = ?, error_json = NULL,
          updated_at = ? WHERE id = ? AND status IN ('staging', 'staged')`).run(
          workingRevision,
          String(group.id || ''),
          JSON.stringify({
            ...response,
            working_revision: workingRevision,
            changed_paths: changedPaths,
            change_group: group,
          }),
          timestamp,
          row.id,
        );
        const taskIds = parseActions(row.actions_json).map((action) => action.taskId);
        const updateTask = this.db.prepare(`UPDATE agent_change_plan_tasks
          SET status = 'staged', change_set_id = ?, updated_at = ?
          WHERE plan_id = ? AND id = ? AND status = 'pending'`);
        for (const taskId of taskIds) updateTask.run(row.id, timestamp, row.plan_id, taskId);
        this.db.prepare(`UPDATE agent_change_plans SET version = version + 1,
          expected_revision = CASE WHEN ? <> '' THEN ? ELSE expected_revision END,
          updated_at = ? WHERE id = ?`).run(workingRevision, workingRevision, timestamp, row.plan_id);
        this.event(projectId, runId, row.plan_id, row.id, 'change_set.reconciled',
          '已核对在途修改回执并保留候选', {
            actor,
            workingRevision,
            changedPaths,
          });
        settledChangeSets.push(row.id);
        settledMutations.push(row.mutation_id);
      }
      const alreadySettled = this.db.prepare(`SELECT id, mutation_id FROM agent_change_sets
        WHERE project_id = ? AND run_id = ? AND status = 'staged' ORDER BY created_at`)
        .all(projectId, runId) as Array<{ id: string; mutation_id: string }>;
      for (const row of alreadySettled) {
        if (!settledChangeSets.includes(row.id)) settledChangeSets.push(row.id);
        if (!settledMutations.includes(row.mutation_id)) settledMutations.push(row.mutation_id);
      }
      this.db.exec('COMMIT');
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }

    const quiescent = pendingChangeSets.length === 0;
    const pausedPlanIds: string[] = [];
    if (quiescent && requested && finalizePlans) {
      const plans = this.db.prepare(`SELECT id FROM agent_change_plans
        WHERE project_id = ? AND run_id = ? AND status = 'active' ORDER BY created_at`)
        .all(projectId, runId) as Array<{ id: string }>;
      if (plans.length) {
        const timestamp = nowIso();
        this.db.exec('BEGIN IMMEDIATE');
        try {
          for (const plan of plans) {
            // 停止的是本次执行，不是用户已批准的业务目标。任务与候选回执保持原状，
            // 由显式 resumePlanId 迁移到下一次 run 后继续。
            this.db.prepare(`UPDATE agent_change_plans SET status = 'paused',
              version = version + 1, updated_at = ? WHERE id = ? AND status = 'active'`)
              .run(timestamp, plan.id);
            this.event(projectId, runId, plan.id, null, 'plan.paused',
              '运行已停止，未完成计划已暂停，候选内容与任务进度保留', { actor });
            pausedPlanIds.push(plan.id);
          }
          this.db.exec('COMMIT');
        } catch (error) {
          if (this.db.isTransaction) this.db.exec('ROLLBACK');
          throw error;
        }
      }
    }
    return {
      projectId,
      runId,
      requested,
      quiescent,
      pendingChangeSets,
      settledChangeSets,
      settledMutations,
      pausedPlanIds,
      cancelledPlanIds: [],
    };
  }

  private taskRows(planId: string): TaskRow[] {
    return this.db.prepare(`SELECT * FROM agent_change_plan_tasks WHERE plan_id = ? ORDER BY ordinal`)
      .all(planId) as unknown as TaskRow[];
  }

  private requireChangeSet(changeSetId: string): ChangeSetRow {
    const row = this.db.prepare(`SELECT * FROM agent_change_sets WHERE id = ?`).get(changeSetId) as ChangeSetRow | undefined;
    if (!row) throw new ChangeControlError('CHANGE_SET_NOT_FOUND', '没有找到这批修改', 404);
    return row;
  }

  private event(
    projectId: string,
    runId: string,
    planId: string,
    changeSetId: string | null,
    eventType: string,
    summary: string,
    detail: Record<string, unknown>,
  ) {
    this.db.prepare(`INSERT INTO agent_change_control_events (
      id, project_id, run_id, plan_id, change_set_id, event_type, summary, detail_json, created_at
    ) VALUES (?,?,?,?,?,?,?,?,?)`).run(
      `cce_${randomUUID().replaceAll('-', '').slice(0, 20)}`,
      projectId, runId, planId, changeSetId, eventType, summary, JSON.stringify(detail), nowIso(),
    );
  }
}

export function parseChangeSetActions(value: unknown): ChangeSetActionInput[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 100) {
    throw invalid('actions', 'Change Set 必须包含 1 到 100 项领域动作');
  }
  return value.map((raw, index) => {
    if (!isRecord(raw)) throw invalid(`actions[${index}]`, '动作必须是对象');
    const taskId = requiredText(raw.task_id, `actions[${index}].task_id`, 160);
    const tool = requiredText(raw.tool, `actions[${index}].tool`, 80);
    if (!CHANGE_SET_DOMAIN_TOOLS.has(tool)) {
      throw invalid(`actions[${index}].tool`, 'Change Set 只接受对象、规则、操作和指标的创建或增量修改');
    }
    if (!isRecord(raw.arguments)) throw invalid(`actions[${index}].arguments`, 'arguments 必须是对象');
    return { taskId, tool, arguments: raw.arguments };
  });
}

function parseTasks(value: unknown, allowEmpty = false): TaskInput[] {
  if (!Array.isArray(value) || (!allowEmpty && value.length < 1) || value.length > 100) {
    throw invalid('tasks', `计划必须包含 ${allowEmpty ? '0' : '1'} 到 100 项任务`);
  }
  const tasks = value.map((raw, index) => {
    if (!isRecord(raw)) throw invalid(`tasks[${index}]`, '任务必须是对象');
    const clientKey = requiredText(raw.client_key, `tasks[${index}].client_key`, 64);
    if (!CLIENT_KEY.test(clientKey)) throw invalid(`tasks[${index}].client_key`, '任务标识格式无效');
    const resourceKind = requiredText(raw.resource_kind, `tasks[${index}].resource_kind`, 30);
    if (!RESOURCE_KINDS.has(resourceKind)) throw invalid(`tasks[${index}].resource_kind`, '资源类型无效');
    const allowedTools = stringArray(raw.allowed_tools, `tasks[${index}].allowed_tools`, 20);
    if (!allowedTools.length || allowedTools.some((tool) => !CHANGE_PLAN_ALLOWED_TOOLS.has(tool))) {
      throw invalid(`tasks[${index}].allowed_tools`, '任务必须声明受支持的领域创建、增量或受控独立工具');
    }
    return {
      clientKey,
      title: requiredText(raw.title, `tasks[${index}].title`, 300),
      resourceKind,
      targetKey: normalizePlanTargetKey(resourceKind, requiredText(raw.target_key, `tasks[${index}].target_key`, 160)),
      allowedTools: [...new Set(allowedTools)],
      dependsOn: stringArray(raw.depends_on, `tasks[${index}].depends_on`, 100),
      acceptance: requiredText(raw.acceptance, `tasks[${index}].acceptance`, 1_000),
      required: raw.required !== false,
    };
  });
  const keys = new Set<string>();
  for (const task of tasks) {
    if (keys.has(task.clientKey)) throw invalid('tasks.client_key', `任务标识重复：${task.clientKey}`);
    keys.add(task.clientKey);
  }
  for (const task of tasks) {
    for (const dependency of task.dependsOn) {
      if (!keys.has(dependency)) throw invalid('tasks.depends_on', `依赖任务不存在：${dependency}`);
      if (dependency === task.clientKey) throw invalid('tasks.depends_on', '任务不能依赖自己');
    }
  }
  assertAcyclic(tasks);
  return tasks;
}

function assertAcyclic(tasks: TaskInput[]) {
  const byKey = new Map(tasks.map((task) => [task.clientKey, task]));
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (key: string) => {
    if (visiting.has(key)) throw invalid('tasks.depends_on', `任务依赖形成循环：${key}`);
    if (visited.has(key)) return;
    visiting.add(key);
    for (const dependency of byKey.get(key)?.dependsOn || []) visit(dependency);
    visiting.delete(key);
    visited.add(key);
  };
  for (const task of tasks) visit(task.clientKey);
}

/**
 * 计划经用户批准，是目标资源标识的权威来源。create_* 动作省略 key 时按计划补齐，
 * 免去模型跨轮次凭记忆复述自己写下的字符串——那是 CHANGE_SET_OUT_OF_SCOPE 的主要来源。
 * 显式给了 key 仍然照常校验，避免掩盖模型真想改别的资源的情况。
 */
function fillPlannedActionTarget(task: TaskRow, action: ChangeSetActionInput): ChangeSetActionInput {
  if (!action.tool.startsWith('create_')) return action;
  if (action.tool.split('_').at(-1) !== task.resource_kind) return action;
  if (String(action.arguments.key ?? '').trim()) return action;
  return { ...action, arguments: { ...action.arguments, key: task.target_key } };
}

function assertActionTarget(task: TaskRow, action: ChangeSetActionInput) {
  const expectedKind = action.tool.split('_').at(-1);
  if (expectedKind !== task.resource_kind) {
    throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', `动作资源类型与计划不一致：${task.title}`, 409, {
      task_id: task.id, expected_kind: task.resource_kind, actual_kind: expectedKind,
    });
  }
  const target = normalizePlanTargetKey(task.resource_kind, actionTarget(action));
  if (target !== task.target_key) {
    throw new ChangeControlError('CHANGE_SET_OUT_OF_SCOPE', `动作目标与计划不一致：${task.title}`, 409, {
      task_id: task.id, expected_target: task.target_key, actual_target: target,
    });
  }
}

/** 计划里的 target_key 必须是裸资源 key。模型常照搬 resource_id 的 `<kind>:<key>` 写法，
 *  不剥掉前缀就会一路传到 create_* 的 key，最终落成 `rules/rule:xxx.md` 这种带冒号的文件名
 *  （Windows 上非法）。patch_* 分支本来就按同样规则剥前缀，这里保持一致。 */
export function normalizePlanTargetKey(resourceKind: string, rawTargetKey: string): string {
  const value = rawTargetKey.trim();
  const prefix = `${resourceKind}:`;
  return value.toLowerCase().startsWith(prefix.toLowerCase())
    ? value.slice(prefix.length).trim() || value
    : value;
}

function actionTarget(action: ChangeSetActionInput): string {
  return action.tool.startsWith('create_')
    ? String(action.arguments.key ?? '')
    : String(action.arguments.resource_id ?? '').split(':').slice(1).join(':');
}

function alignSameKindActionTask(
  tasks: TaskRow[],
  taskById: Map<string, TaskRow>,
  action: ChangeSetActionInput,
): ChangeSetActionInput {
  const selected = taskById.get(action.taskId);
  const actualKind = action.tool.split('_').at(-1);
  const actualTarget = actionTarget(action);
  if (!selected || selected.resource_kind !== actualKind || selected.target_key === actualTarget) return action;
  const matches = tasks.filter((task) => (
    task.status === 'pending'
    && task.resource_kind === actualKind
    && task.target_key === actualTarget
    && parseStringArray(task.allowed_tools_json).includes(action.tool)
  ));
  if (matches.length !== 1) return action;
  return { ...action, taskId: matches[0].id };
}

function changeSetPublic(row: ChangeSetRow) {
  return {
    id: row.id,
    contract_version: CHANGE_SET_CONTRACT_VERSION,
    plan_id: row.plan_id,
    mutation_id: row.mutation_id,
    status: row.status,
    expected_revision: row.expected_revision,
    resulting_revision: row.resulting_revision,
    candidate_revision: row.candidate_revision,
    group_id: row.group_id,
    action_count: parseActions(row.actions_json).length,
    receipt: row.receipt_json ? JSON.parse(row.receipt_json) : null,
    error: row.error_json ? JSON.parse(row.error_json) : null,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

function parseActions(value: string): ChangeSetActionInput[] {
  try { return JSON.parse(value) as ChangeSetActionInput[]; } catch { return []; }
}

function parseStringArray(value: string): string[] {
  try {
    const result = JSON.parse(value);
    return Array.isArray(result) ? result.map(String) : [];
  } catch { return []; }
}

function stringArray(value: unknown, path: string, maximum: number): string[] {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value) || value.length > maximum) throw invalid(path, `必须是最多 ${maximum} 项的数组`);
  return value.map((item, index) => requiredText(item, `${path}[${index}]`, 160));
}

function requiredText(value: unknown, path: string, maximum: number): string {
  if (typeof value !== 'string' || !value.trim()) throw invalid(path, '不能为空');
  const result = value.trim();
  if (result.length > maximum) throw invalid(path, `不能超过 ${maximum} 个字符`);
  return result;
}

function invalid(path: string, message: string) {
  return new ChangeControlError('CHANGE_CONTROL_INVALID', message, 400, { path, retryable: false });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
