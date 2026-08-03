import { randomUUID } from 'node:crypto';
import type { DatabaseSync } from 'node:sqlite';

import { getDb, nowIso } from '../db/index.js';
import type { ProjectKind } from '../types.js';

export type AiConstructionGraphStatus = 'active' | 'completed' | 'superseded';
export type AiConstructionGraphSource = 'new' | 'package_import';
export type AiConstructionMilestoneStatus = 'pending' | 'active' | 'confirmed' | 'stale';
export type AiConstructionTaskStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'waiting_user'
  | 'waiting_confirmation'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'stale'
  | 'cancelled';
export type AiConstructionTaskKind =
  | 'material_analysis'
  | 'package_baseline_review'
  | 'modeling_brief'
  | 'process_interview'
  | 'process_diagram'
  | 'process_review'
  | 'operation_extraction'
  | 'knowledge_extraction'
  | 'script_decision'
  | 'script_generation'
  | 'package_validation'
  | 'package_repair'
  // 评测失败反推的修缮：与 package_repair 分开，因为完成判据是「重新评测通过」
  // 而不是「知识包校验通过」，且执行引擎是优化更新而非受控修复
  | 'evaluation_repair'
  // 资料变更后的定向复核：按引用关系算出受影响的内容并逐项确认，
  // 取代「受影响点之后的阶段全部推倒重来」
  | 'material_change_review'
  // 增量补充：知识包建完之后补新资料，只吸收增量并提出变更方案
  | 'incremental_intake'
  | 'completion_review';

export interface AiConstructionMilestoneSnapshot {
  id: string;
  title: string;
  ordinal: number;
  status: AiConstructionMilestoneStatus;
  confirmedRevisionOid?: string;
  updatedAt: string;
}

export interface AiConstructionTaskSnapshot {
  id: string;
  kind: AiConstructionTaskKind;
  milestoneId: string;
  ordinal: number;
  title: string;
  goal: string;
  status: AiConstructionTaskStatus;
  required: boolean;
  reason?: string;
  inputRevisionOid?: string;
  outputRevisionOid?: string;
  dependsOn: string[];
  createdAt: string;
  updatedAt: string;
}

export interface AiConstructionTaskGraphSnapshot {
  projectId: string;
  revision: number;
  status: AiConstructionGraphStatus;
  source: AiConstructionGraphSource;
  createdAt: string;
  updatedAt: string;
  milestones: AiConstructionMilestoneSnapshot[];
  tasks: AiConstructionTaskSnapshot[];
  confirmations: AiConstructionConfirmationSnapshot[];
  progress: AiConstructionProgressSnapshot;
}

export type AiConstructionWaitingFor =
  | 'task_execution'
  | 'task_failed'
  | 'user_input'
  | 'milestone_confirmation'
  | 'task_planning'
  | 'completed';

export interface AiConstructionEtaSnapshot {
  minMinutes: number | null;
  maxMinutes: number | null;
  confidence: 'low' | 'medium' | 'high';
  basis: 'planned_tasks';
  note: string;
}

export interface AiConstructionProgressSnapshot {
  confirmedMilestones: number;
  totalMilestones: number;
  completedTasks: number;
  totalTasks: number;
  percent: number;
  currentMilestoneId?: string;
  currentTaskId?: string;
  waitingFor: AiConstructionWaitingFor;
  eta: AiConstructionEtaSnapshot;
}

export type AiConstructionActorRole =
  | 'project_owner'
  | 'modeler'
  | 'domain_expert'
  | 'validator';

export interface AiConstructionConfirmationSnapshot {
  id: string;
  milestoneId: string;
  graphRevision: number;
  packageRevisionOid: string;
  actor: { username: string; role: AiConstructionActorRole };
  note?: string;
  createdAt: string;
}

export type AiConstructionTaskAdjustmentAction = 'skip' | 'resume' | 'defer' | 'redo';

export interface AiConstructionTaskAdjustmentPreview {
  projectId: string;
  graphRevision: number;
  action: AiConstructionTaskAdjustmentAction;
  task: Pick<AiConstructionTaskSnapshot, 'id' | 'title' | 'status' | 'required' | 'milestoneId'>;
  allowed: boolean;
  reason?: string;
  affectedTasks: Array<Pick<AiConstructionTaskSnapshot, 'id' | 'title' | 'status' | 'milestoneId'>>;
  affectedMilestones: string[];
  restoreRevisionOid?: string;
  etaDelta?: { minMinutes: number; maxMinutes: number };
}

export interface AiConstructionTaskAdjustmentSnapshot {
  id: string;
  projectId: string;
  taskId: string;
  action: AiConstructionTaskAdjustmentAction;
  graphRevisionBefore: number;
  graphRevisionAfter: number;
  actor: string;
  reason: string;
  impact: AiConstructionTaskAdjustmentPreview;
  createdAt: string;
}

export interface CompleteAiConstructionTaskCommand {
  type: 'complete_task';
  expectedRevision: number;
  taskId: string;
  outputRevisionOid?: string;
}

export interface StartAiConstructionTaskCommand {
  type: 'start_task';
  expectedRevision: number;
  taskId: string;
}

export interface FailAiConstructionTaskCommand {
  type: 'fail_task';
  expectedRevision: number;
  taskId: string;
  /**
   * 失败原因。原先只写进助手消息，任务列表里那条就只显示「需重试」——
   * 要知道为什么失败得去聊天流里翻，而聊天流可能已经滚过去了。
   */
  reason?: string;
}

export interface WaitForAiConstructionUserCommand {
  type: 'wait_for_user';
  expectedRevision: number;
  taskId: string;
}

export interface ReopenAiConstructionTaskCommand {
  type: 'reopen_task';
  expectedRevision: number;
  taskId: string;
}

export interface ConfirmAiConstructionMilestoneCommand {
  type: 'confirm_milestone';
  expectedRevision: number;
  milestoneId: string;
  packageRevisionOid: string;
  actor: { username: string; role: AiConstructionActorRole };
  note?: string;
}

export interface PlanAiConstructionTaskCommand {
  type: 'plan_task';
  expectedRevision: number;
  task: {
    kind: AiConstructionTaskKind;
    milestoneId: string;
    title: string;
    goal: string;
    required: boolean;
    reason?: string;
    inputRevisionOid?: string;
    dependsOn: string[];
  };
}

export type AiConstructionTaskGraphCommand =
  | StartAiConstructionTaskCommand
  | FailAiConstructionTaskCommand
  | CompleteAiConstructionTaskCommand
  | WaitForAiConstructionUserCommand
  | ReopenAiConstructionTaskCommand
  | PlanAiConstructionTaskCommand
  | ConfirmAiConstructionMilestoneCommand;

/**
 * 增量补充阶段：知识包建完之后补新资料时进入，可反复进入。
 * 不放进初始里程碑列表——见 ensureIncrementMilestone。
 */
export const INCREMENT_MILESTONE_ID = 'increment_confirmed';
export const INCREMENT_MILESTONE_TITLE = '增量补充确认';

const MATERIAL_FIRST_MILESTONES = [
  ['materials_ready', '资料就绪'],
  ['goal_confirmed', '建模目标确认'],
  ['process_confirmed', '业务流程确认'],
  ['knowledge_confirmed', '知识结构确认'],
  ['deterministic_confirmed', '确定性能力确认'],
  ['construction_completed', '建模完成确认'],
] as const;

const GOAL_FIRST_MILESTONES = [
  ['goal_confirmed', '建模目标确认'],
  ['materials_ready', '资料就绪'],
  ...MATERIAL_FIRST_MILESTONES.slice(2),
] as const;

const INITIAL_TASKS: Record<AiConstructionGraphSource, {
  kind: AiConstructionTaskKind;
  milestoneId: string;
  title: string;
  goal: string;
  reason: string;
}> = {
  new: {
    kind: 'modeling_brief',
    milestoneId: 'goal_confirmed',
    title: '确认建模目标',
    goal: '先明确知识包要解决的问题、目标用户、典型场景和成功标准。',
    reason: '新项目先确认建模目标，再围绕目标分析资料',
  },
  package_import: {
    kind: 'package_baseline_review',
    milestoneId: 'materials_ready',
    title: '检查导入知识包基线',
    goal: '检查导入知识包的结构、现有证据和待补充建模内容。',
    reason: '以导入知识包作为建模基线',
  },
};

export function aiConstructionGraphSourceForProjectKind(
  kind: ProjectKind,
): AiConstructionGraphSource {
  return kind === 'package' ? 'package_import' : 'new';
}

const TASK_DURATION_MINUTES: Partial<Record<AiConstructionTaskKind, [number, number]>> = {
  material_analysis: [2, 8],
  package_baseline_review: [3, 10],
  modeling_brief: [5, 15],
  process_interview: [15, 45],
  process_diagram: [5, 20],
  process_review: [5, 15],
  operation_extraction: [10, 30],
  knowledge_extraction: [15, 45],
  script_decision: [5, 15],
  script_generation: [10, 40],
  package_validation: [3, 15],
  package_repair: [5, 30],
  evaluation_repair: [10, 45],
  material_change_review: [5, 20],
  incremental_intake: [10, 30],
  completion_review: [5, 15],
};

export function aiConstructionTaskDuration(kind: AiConstructionTaskKind): [number, number] {
  return TASK_DURATION_MINUTES[kind] ?? [5, 20];
}

const MINUTE_MS = 60_000;

function taskElapsedMinutes(task: AiConstructionTaskSnapshot): number | null {
  const start = Date.parse(task.createdAt);
  const end = Date.parse(task.updatedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return (end - start) / MINUTE_MS;
}

/** 跨项目的同类任务实测耗时（分钟），由调用方从库里查出来传入 */
export type MeasuredTaskDurations = Partial<Record<AiConstructionTaskKind, number[]>>;

/**
 * 低于这个耗时的样本不算数。任务可能被程序瞬间置为 completed（跳过、命中缓存、
 * 状态补写），这类记录不是对真实工作量的测量。混进来会把区间下限拉到 0，
 * 让所有人都看到「预计 1 分钟」。
 */
const MIN_CREDIBLE_SAMPLE_MINUTES = 0.5;

/**
 * 同类任务的实测耗时区间。静态表只是冷启动兜底：不同项目的资料规模、模型速度
 * 差别很大，写死的 [2, 8] 分钟在真实项目里往往离谱。
 *
 * 样本必须跨项目取。每种 task kind 在一个项目里通常只跑一次，只看本项目历史的话，
 * 等你有样本时这个任务早就做完了、不需要估算了——那等于永远退回静态表，
 * 界面上的「预计 28–90 分钟」就成了一个从头到尾不动的摆设。
 *
 * 有样本时用实测的最小/最大值（各留 20% 余量）；只有一个样本时与静态表取并集，
 * 避免单次样本给出过窄的区间。
 */
export function aiConstructionTaskEstimate(
  kind: AiConstructionTaskKind,
  history: AiConstructionTaskSnapshot[],
  measured: MeasuredTaskDurations = {},
): [number, number] {
  const fallback = aiConstructionTaskDuration(kind);
  const samples = [
    ...history
      .filter((task) => task.kind === kind && task.status === 'completed')
      .map(taskElapsedMinutes)
      .filter((value): value is number => value !== null),
    ...(measured[kind] ?? []).filter((value) => Number.isFinite(value)),
  ].filter((value) => value >= MIN_CREDIBLE_SAMPLE_MINUTES);
  if (!samples.length) return fallback;

  const min = Math.min(...samples) * 0.8;
  const max = Math.max(...samples) * 1.2;
  // 单个样本会给出一个过窄的区间，与静态表取并集保留合理的不确定度
  const low = samples.length > 1 ? min : Math.min(min, fallback[0]);
  const high = samples.length > 1 ? max : Math.max(max, fallback[1]);
  return [Math.max(1, Math.round(low)), Math.max(1, Math.round(high))];
}

/**
 * 正在跑的任务已经花掉的时间要从剩余里扣掉，否则跑了 20 分钟数字还是原来那个，
 * 看上去就是「不会动的固定值」。下限扣到 0，上限至少留 1 分钟避免显示成已完成。
 */
function remainingForTask(
  task: AiConstructionTaskSnapshot,
  estimate: [number, number],
): [number, number] {
  if (task.status !== 'running') return estimate;
  const started = Date.parse(task.updatedAt);
  if (!Number.isFinite(started)) return estimate;
  const elapsed = Math.max(0, (Date.now() - started) / MINUTE_MS);
  return [
    Math.max(0, Math.round(estimate[0] - elapsed)),
    Math.max(1, Math.round(estimate[1] - elapsed)),
  ];
}

function durationForTasks(tasks: AiConstructionTaskSnapshot[]): [number, number] {
  return tasks.reduce(([min, max], task) => {
    const estimate = aiConstructionTaskDuration(task.kind);
    return [min + estimate[0], max + estimate[1]] as [number, number];
  }, [0, 0] as [number, number]);
}

function isTaskResolved(task: AiConstructionTaskSnapshot): boolean {
  return task.status === 'completed' || task.status === 'skipped' || task.status === 'cancelled';
}

function isSupersededStaleTask(
  task: AiConstructionTaskSnapshot,
  tasks: AiConstructionTaskSnapshot[],
): boolean {
  return task.status === 'stale' && tasks.some((replacement) => (
    replacement.id !== task.id
    && replacement.milestoneId === task.milestoneId
    && replacement.kind === task.kind
    && !['stale', 'cancelled'].includes(replacement.status)
  ));
}

function deriveProgress(
  graphStatus: AiConstructionGraphStatus,
  milestones: AiConstructionMilestoneSnapshot[],
  tasks: AiConstructionTaskSnapshot[],
  measured: MeasuredTaskDurations = {},
): AiConstructionProgressSnapshot {
  const effectiveTasks = tasks.filter((task) => !isSupersededStaleTask(task, tasks));
  const confirmedMilestones = milestones.filter((item) => item.status === 'confirmed').length;
  const completedTasks = effectiveTasks.filter((item) => item.status === 'completed').length;
  const currentMilestone = milestones.find((item) => item.status === 'active');
  const currentTasks = currentMilestone
    ? effectiveTasks.filter((item) => item.milestoneId === currentMilestone.id)
    : [];
  const currentTask = currentTasks.find((item) => (
    item.status === 'running' || item.status === 'waiting_user' || item.status === 'waiting_confirmation'
  )) ?? currentTasks.find((item) => !isTaskResolved(item));
  const currentCompleted = currentTasks.filter(isTaskResolved).length;
  const activeFraction = currentTasks.length > 0
    ? (currentCompleted / currentTasks.length) * 0.8
    : 0;
  const percent = graphStatus === 'completed'
    ? 100
    : Math.round(((confirmedMilestones + activeFraction) / Math.max(1, milestones.length)) * 100);

  let waitingFor: AiConstructionWaitingFor;
  if (graphStatus === 'completed') waitingFor = 'completed';
  else if (!currentTask && currentTasks.length > 0) waitingFor = 'milestone_confirmation';
  else if (!currentTask) waitingFor = 'task_planning';
  else if (currentTask.status === 'failed') waitingFor = 'task_failed';
  else if (currentTask.status === 'waiting_user' || currentTask.status === 'waiting_confirmation') {
    waitingFor = 'user_input';
  } else waitingFor = 'task_execution';

  const plannedRemaining = effectiveTasks.filter((item) => !isTaskResolved(item)
    && item.status !== 'waiting_user' && item.status !== 'waiting_confirmation'
    && item.status !== 'failed');
  const duration = plannedRemaining.reduce(([min, max], task) => {
    const estimate = remainingForTask(
      task, aiConstructionTaskEstimate(task.kind, tasks, measured),
    );
    return [min + estimate[0], max + estimate[1]] as [number, number];
  }, [0, 0] as [number, number]);
  const hasEstimate = plannedRemaining.length > 0;
  // 有实测样本时置信度提升，界面据此决定措辞
  const measuredKinds = new Set([
    ...tasks
      .filter((task) => task.status === 'completed' && taskElapsedMinutes(task) !== null)
      .map((task) => task.kind),
    ...(Object.keys(measured) as AiConstructionTaskKind[])
      .filter((kind) => (measured[kind] ?? []).length > 0),
  ]);
  const measuredShare = plannedRemaining.length
    ? plannedRemaining.filter((task) => measuredKinds.has(task.kind)).length / plannedRemaining.length
    : 0;

  return {
    confirmedMilestones,
    totalMilestones: milestones.length,
    completedTasks,
    totalTasks: effectiveTasks.length,
    percent,
    currentMilestoneId: currentMilestone?.id,
    currentTaskId: currentTask?.id,
    waitingFor,
    eta: {
      minMinutes: hasEstimate ? duration[0] : null,
      maxMinutes: hasEstimate ? duration[1] : null,
      confidence: measuredShare >= 0.6 ? 'high' : measuredShare > 0 ? 'medium' : 'low',
      basis: 'planned_tasks',
      note: currentTask?.status === 'failed'
        ? '当前步骤已停止，重试后会重新估算剩余时长'
        : hasEstimate
        ? '只估算当前已安排的步骤；后面还会新增步骤，剩余时长会随之更新'
        : '当前已安排的步骤都已完成，下一批步骤安排好后会重新估算',
    },
  };
}

type TaskStatusCommandType =
  | 'start_task'
  | 'fail_task'
  | 'complete_task'
  | 'wait_for_user'
  | 'reopen_task';

interface TaskStatusTransition {
  to: AiConstructionTaskStatus;
  from: ReadonlySet<AiConstructionTaskStatus>;
  requiresResolvedDependencies?: boolean;
  clearsOutputRevision?: boolean;
  rejection: string;
}

/**
 * 任务状态的合法迁移。所有迁移都要求任务所属里程碑处于 active——
 * 已确认里程碑下的任务不能再被推进，否则确认结论会被事后改写。
 *
 * 'ready' 与 'waiting_confirmation' 不是任何一条迁移的目标状态：任务图本身不会产生它们，
 * 只在作为来源时被容忍（历史数据与外部写入）。要启用需在此补一条迁移，并同步
 * src/authoring/ai-construction-workflow.ts 的展示映射。
 */
const TASK_STATUS_TRANSITIONS: Record<TaskStatusCommandType, TaskStatusTransition> = {
  start_task: {
    to: 'running',
    from: new Set(['pending', 'ready', 'failed', 'waiting_user']),
    requiresResolvedDependencies: true,
    rejection: '当前任务不能开始执行',
  },
  fail_task: {
    to: 'failed',
    from: new Set(['running']),
    rejection: '当前任务不能标记为失败',
  },
  complete_task: {
    to: 'completed',
    from: new Set(['pending', 'ready', 'running', 'waiting_user', 'waiting_confirmation', 'failed']),
    rejection: '当前任务状态不能完成',
  },
  wait_for_user: {
    to: 'waiting_user',
    from: new Set(['pending', 'ready', 'running']),
    rejection: '当前任务不能等待用户回答',
  },
  reopen_task: {
    to: 'waiting_user',
    from: new Set(['completed']),
    clearsOutputRevision: true,
    rejection: '当前任务不能退回补充',
  },
};

function isTaskStatusCommand(
  command: AiConstructionTaskGraphCommand,
): command is Extract<AiConstructionTaskGraphCommand, { taskId: string }> {
  return Object.prototype.hasOwnProperty.call(TASK_STATUS_TRANSITIONS, command.type);
}

export function aiConstructionTaskStatusTransitions(): Record<TaskStatusCommandType, {
  to: AiConstructionTaskStatus;
  from: AiConstructionTaskStatus[];
}> {
  return Object.fromEntries(Object.entries(TASK_STATUS_TRANSITIONS).map(([command, rule]) => [
    command, { to: rule.to, from: [...rule.from].sort() },
  ])) as Record<TaskStatusCommandType, { to: AiConstructionTaskStatus; from: AiConstructionTaskStatus[] }>;
}

export class AiConstructionTaskGraphError extends Error {
  constructor(
    public readonly code:
      | 'PROJECT_NOT_FOUND'
      | 'GRAPH_NOT_FOUND'
      | 'REVISION_CONFLICT'
      | 'TASK_NOT_FOUND'
      | 'INVALID_TRANSITION'
      | 'INVALID_TASK_PLAN'
      | 'MILESTONE_NOT_FOUND'
      | 'MILESTONE_NOT_READY',
    message: string,
  ) {
    super(message);
    this.name = 'AiConstructionTaskGraphError';
  }
}

export class AiConstructionTaskGraph {
  constructor(private readonly db: DatabaseSync = getDb()) {}

  initialize(
    projectId: string,
    source: AiConstructionGraphSource = 'new',
  ): AiConstructionTaskGraphSnapshot {
    const existing = this.snapshot(projectId);
    if (existing) return existing;

    const project = this.db.prepare('SELECT id FROM projects WHERE id = ?').get(projectId);
    if (!project) {
      throw new AiConstructionTaskGraphError('PROJECT_NOT_FOUND', `项目不存在：${projectId}`);
    }

    const now = nowIso();
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const lockedExisting = this.snapshot(projectId);
      if (lockedExisting) {
        if (ownsTransaction) this.db.exec('COMMIT');
        return lockedExisting;
      }
      this.db.prepare(`INSERT INTO ai_construction_graphs
        (project_id, revision, status, source, created_at, updated_at)
        VALUES (?, 1, 'active', ?, ?, ?)`)
        .run(projectId, source, now, now);

      const milestones = source === 'new' ? GOAL_FIRST_MILESTONES : MATERIAL_FIRST_MILESTONES;
      const insertMilestone = this.db.prepare(`INSERT INTO ai_construction_milestones
        (project_id, milestone_id, ordinal, title, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)`);
      milestones.forEach(([id, title], index) => {
        insertMilestone.run(projectId, id, index + 1, title, index === 0 ? 'active' : 'pending', now);
      });

      const initialTask = INITIAL_TASKS[source];
      this.db.prepare(`INSERT INTO ai_construction_tasks
        (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
          required, reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, 'pending', 1, ?, ?, ?)`)
        .run(
          `task_${randomUUID().slice(0, 12)}`,
          projectId,
          initialTask.kind,
          initialTask.milestoneId,
          initialTask.title,
          initialTask.goal,
          initialTask.reason,
          now,
          now,
        );
      if (ownsTransaction) this.db.exec('COMMIT');
    } catch (error) {
      if (ownsTransaction) {
        try { this.db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
      }
      const concurrent = this.snapshot(projectId);
      if (concurrent) return concurrent;
      throw error;
    }

    return this.snapshot(projectId)!;
  }

  apply(
    projectId: string,
    command: AiConstructionTaskGraphCommand,
  ): AiConstructionTaskGraphSnapshot {
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const graph = this.db.prepare(`SELECT revision FROM ai_construction_graphs
        WHERE project_id = ?`).get(projectId) as { revision: number } | undefined;
      if (!graph) {
        throw new AiConstructionTaskGraphError('GRAPH_NOT_FOUND', `项目尚未初始化 AI 辅助构建：${projectId}`);
      }
      if (Number(graph.revision) !== command.expectedRevision) {
        throw new AiConstructionTaskGraphError(
          'REVISION_CONFLICT',
          `任务图已更新，请基于 revision ${graph.revision} 重试`,
        );
      }

      const now = nowIso();
      if (isTaskStatusCommand(command)) {
        this.applyTaskStatusTransition(
          projectId, command, TASK_STATUS_TRANSITIONS[command.type], now,
        );
      } else if (command.type === 'plan_task') {
        const milestone = this.db.prepare(`SELECT status FROM ai_construction_milestones
          WHERE project_id = ? AND milestone_id = ?`)
          .get(projectId, command.task.milestoneId) as { status: string } | undefined;
        if (!milestone) {
          throw new AiConstructionTaskGraphError('MILESTONE_NOT_FOUND', '任务计划引用的阶段不存在');
        }
        if (milestone.status !== 'active') {
          throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', '只能为当前活动阶段规划任务');
        }
        const title = command.task.title.trim();
        const goal = command.task.goal.trim();
        const dependencies = [...new Set(command.task.dependsOn)];
        if (!title || !goal || title.length > 200 || goal.length > 2_000
          || dependencies.length !== command.task.dependsOn.length) {
          throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', '任务计划内容不正确');
        }
        if (dependencies.length) {
          const placeholders = dependencies.map(() => '?').join(',');
          const found = this.db.prepare(`SELECT COUNT(*) AS count FROM ai_construction_tasks
            WHERE project_id = ? AND id IN (${placeholders})`)
            .get(projectId, ...dependencies) as { count: number };
          if (Number(found.count) !== dependencies.length) {
            throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', '任务依赖不存在或不属于当前项目');
          }
        }
        const ordinal = Number((this.db.prepare(`SELECT COALESCE(MAX(ordinal), 0) AS ordinal
          FROM ai_construction_tasks WHERE project_id = ?`).get(projectId) as { ordinal: number }).ordinal) + 1;
        const taskId = `task_${randomUUID().slice(0, 12)}`;
        this.db.prepare(`INSERT INTO ai_construction_tasks
          (id, project_id, task_kind, milestone_id, ordinal, title, goal, status,
            required, reason, input_revision_oid, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)`)
          .run(
            taskId,
            projectId,
            command.task.kind,
            command.task.milestoneId,
            ordinal,
            title,
            goal,
            command.task.required ? 1 : 0,
            command.task.reason?.trim() || null,
            command.task.inputRevisionOid ?? null,
            now,
            now,
          );
        const insertDependency = this.db.prepare(`INSERT INTO ai_construction_task_dependencies
          (project_id, task_id, depends_on_task_id) VALUES (?, ?, ?)`);
        dependencies.forEach((dependencyId) => insertDependency.run(projectId, taskId, dependencyId));
      } else {
        const milestone = this.db.prepare(`SELECT ordinal, status FROM ai_construction_milestones
          WHERE project_id = ? AND milestone_id = ?`)
          .get(projectId, command.milestoneId) as { ordinal: number; status: string } | undefined;
        if (!milestone) {
          throw new AiConstructionTaskGraphError('MILESTONE_NOT_FOUND', '阶段不存在或不属于当前项目');
        }
        const taskCounts = this.db.prepare(`SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN task.required = 1
            AND task.status NOT IN ('completed', 'skipped', 'cancelled')
            AND NOT (
              task.status = 'stale'
              AND EXISTS (
                SELECT 1 FROM ai_construction_tasks replacement
                WHERE replacement.project_id = task.project_id
                  AND replacement.milestone_id = task.milestone_id
                  AND replacement.task_kind = task.task_kind
                  AND replacement.id <> task.id
                  AND replacement.status NOT IN ('stale', 'cancelled')
              )
            )
            THEN 1 ELSE 0 END) AS blocking
          FROM ai_construction_tasks task WHERE task.project_id = ? AND task.milestone_id = ?`)
          .get(projectId, command.milestoneId) as { total: number; blocking: number | null };
        if (milestone.status !== 'active' || Number(taskCounts.total) === 0 || Number(taskCounts.blocking ?? 0) > 0) {
          throw new AiConstructionTaskGraphError(
            'MILESTONE_NOT_READY',
            '阶段尚未满足确认条件：必选任务必须全部完成',
          );
        }
        const nextRevision = command.expectedRevision + 1;
        this.db.prepare(`UPDATE ai_construction_milestones
          SET status = 'confirmed', confirmed_revision_oid = ?, updated_at = ?
          WHERE project_id = ? AND milestone_id = ?`)
          .run(command.packageRevisionOid, now, projectId, command.milestoneId);
        const next = this.db.prepare(`SELECT milestone_id FROM ai_construction_milestones
          WHERE project_id = ? AND ordinal > ? ORDER BY ordinal LIMIT 1`)
          .get(projectId, milestone.ordinal) as { milestone_id: string } | undefined;
        if (next) {
          this.db.prepare(`UPDATE ai_construction_milestones SET status = 'active', updated_at = ?
            WHERE project_id = ? AND milestone_id = ?`)
            .run(now, projectId, next.milestone_id);
        }
        this.db.prepare(`INSERT INTO ai_construction_confirmations
          (id, project_id, milestone_id, graph_revision, package_revision_oid,
            actor_username, actor_role, note, created_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`)
          .run(
            `confirm_${randomUUID().slice(0, 12)}`,
            projectId,
            command.milestoneId,
            nextRevision,
            command.packageRevisionOid,
            command.actor.username,
            command.actor.role,
            command.note?.trim() || null,
            now,
          );
        if (!next) {
          this.db.prepare(`UPDATE ai_construction_graphs SET status = 'completed'
            WHERE project_id = ?`).run(projectId);
        }
      }
      this.db.prepare(`UPDATE ai_construction_graphs
        SET revision = revision + 1, updated_at = ? WHERE project_id = ?`)
        .run(now, projectId);
      if (ownsTransaction) this.db.exec('COMMIT');
    } catch (error) {
      if (ownsTransaction) {
        try { this.db.exec('ROLLBACK'); } catch { /* 没有活动事务时忽略 */ }
      }
      throw error;
    }
    return this.snapshot(projectId)!;
  }

  private applyTaskStatusTransition(
    projectId: string,
    command: Extract<AiConstructionTaskGraphCommand, { taskId: string }>,
    transition: TaskStatusTransition,
    now: string,
  ): void {
    const task = this.db.prepare(`SELECT status, milestone_id FROM ai_construction_tasks
      WHERE project_id = ? AND id = ?`).get(projectId, command.taskId) as {
        status: AiConstructionTaskStatus;
        milestone_id: string;
      } | undefined;
    if (!task) throw new AiConstructionTaskGraphError('TASK_NOT_FOUND', '任务不存在或不属于当前项目');

    const milestone = this.db.prepare(`SELECT status FROM ai_construction_milestones
      WHERE project_id = ? AND milestone_id = ?`).get(projectId, task.milestone_id) as {
        status: string;
      } | undefined;
    if (!milestone || milestone.status !== 'active' || !transition.from.has(task.status)) {
      throw new AiConstructionTaskGraphError(
        'INVALID_TRANSITION',
        `${transition.rejection}：${task.status}`,
      );
    }
    if (transition.requiresResolvedDependencies) {
      const blocking = this.db.prepare(`SELECT COUNT(*) AS count
        FROM ai_construction_task_dependencies dependency
        JOIN ai_construction_tasks task
          ON task.project_id = dependency.project_id AND task.id = dependency.depends_on_task_id
        WHERE dependency.project_id = ? AND dependency.task_id = ?
          AND task.status NOT IN ('completed', 'skipped', 'cancelled')`)
        .get(projectId, command.taskId) as { count: number };
      if (Number(blocking.count) > 0) {
        throw new AiConstructionTaskGraphError('INVALID_TRANSITION', `${transition.rejection}：依赖任务尚未结束`);
      }
    }

    // 失败原因跟着任务走；重新开始时清掉，免得旧原因挂在新一轮上误导人
    if (command.type === 'fail_task' && command.reason?.trim()) {
      this.db.prepare(`UPDATE ai_construction_tasks SET reason = ? WHERE project_id = ? AND id = ?`)
        .run(command.reason.trim().slice(0, 2_000), projectId, command.taskId);
    } else if (command.type === 'start_task' || command.type === 'reopen_task') {
      this.db.prepare(`UPDATE ai_construction_tasks SET reason = NULL
        WHERE project_id = ? AND id = ? AND status = 'failed'`).run(projectId, command.taskId);
    }

    const outputRevisionOid = command.type === 'complete_task'
      ? command.outputRevisionOid ?? null
      : transition.clearsOutputRevision ? null : undefined;
    if (outputRevisionOid === undefined) {
      this.db.prepare(`UPDATE ai_construction_tasks SET status = ?, updated_at = ?
        WHERE project_id = ? AND id = ?`).run(transition.to, now, projectId, command.taskId);
      return;
    }
    this.db.prepare(`UPDATE ai_construction_tasks
      SET status = ?, output_revision_oid = ?, updated_at = ?
      WHERE project_id = ? AND id = ?`)
      .run(transition.to, outputRevisionOid, now, projectId, command.taskId);
  }

  previewAdjustment(
    projectId: string,
    taskId: string,
    action: AiConstructionTaskAdjustmentAction,
  ): AiConstructionTaskAdjustmentPreview {
    const graph = this.snapshot(projectId);
    if (!graph) throw new AiConstructionTaskGraphError('GRAPH_NOT_FOUND', '项目尚未初始化 AI 辅助构建');
    const task = graph.tasks.find((item) => item.id === taskId);
    if (!task) throw new AiConstructionTaskGraphError('TASK_NOT_FOUND', '任务不存在或不属于当前项目');
    const milestone = graph.milestones.find((item) => item.id === task.milestoneId)!;
    const active = milestone.status === 'active';
    let allowed = false;
    let reason: string | undefined;
    if (action === 'skip') {
      allowed = active && !task.required
        && ['pending', 'ready', 'failed', 'waiting_user', 'waiting_confirmation'].includes(task.status);
      if (!allowed) reason = task.required ? '必选任务不能跳过' : '只有当前阶段中的未完成任务可以跳过';
    } else if (action === 'resume') {
      allowed = active && ['skipped', 'stale'].includes(task.status);
      if (!allowed) reason = '只有当前阶段中已跳过或需复核的任务可以恢复';
    } else if (action === 'defer') {
      allowed = active && ['pending', 'ready', 'failed', 'waiting_user'].includes(task.status);
      if (!allowed) reason = '正在执行、已完成或不在当前阶段的任务不能暂缓';
    } else if (action === 'redo') {
      allowed = task.status === 'completed';
      if (!allowed) reason = '只有已完成任务可以重做';
    }

    let affectedTasks = [task];
    let affectedMilestones = [task.milestoneId];
    if (action === 'redo') {
      const milestoneOrdinal = milestone.ordinal;
      const affectedMilestoneSet = new Set(graph.milestones
        .filter((item) => item.ordinal >= milestoneOrdinal && (item.id === milestone.id || item.status !== 'pending'))
        .map((item) => item.id));
      affectedTasks = graph.tasks.filter((item) => (
        item.id === task.id
        || (item.milestoneId === task.milestoneId && item.ordinal > task.ordinal)
        || affectedMilestoneSet.has(item.milestoneId) && item.milestoneId !== task.milestoneId
      ));
      affectedMilestones = graph.milestones
        .filter((item) => affectedMilestoneSet.has(item.id))
        .map((item) => item.id);
    }
    const previousMilestone = action === 'redo'
      ? [...graph.milestones].reverse().find((item) => item.ordinal < milestone.ordinal && item.confirmedRevisionOid)
      : undefined;
    const restoreRevisionOid = action === 'redo'
      ? task.inputRevisionOid ?? previousMilestone?.confirmedRevisionOid
      : undefined;
    const [minMinutes, maxMinutes] = durationForTasks(action === 'skip' ? [task] : affectedTasks);
    const sign = action === 'skip' ? -1 : action === 'defer' ? 0 : 1;
    return {
      projectId,
      graphRevision: graph.revision,
      action,
      task: {
        id: task.id, title: task.title, status: task.status,
        required: task.required, milestoneId: task.milestoneId,
      },
      allowed,
      ...(reason ? { reason } : {}),
      affectedTasks: affectedTasks.map((item) => ({
        id: item.id, title: item.title, status: item.status, milestoneId: item.milestoneId,
      })),
      affectedMilestones,
      ...(restoreRevisionOid ? { restoreRevisionOid } : {}),
      etaDelta: { minMinutes: minMinutes * sign, maxMinutes: maxMinutes * sign },
    };
  }

  adjust(projectId: string, input: {
    expectedRevision: number;
    taskId: string;
    action: AiConstructionTaskAdjustmentAction;
    actor: string;
    reason: string;
  }): { graph: AiConstructionTaskGraphSnapshot; adjustment: AiConstructionTaskAdjustmentSnapshot } {
    const actor = String(input.actor ?? '').trim();
    const reason = String(input.reason ?? '').trim();
    if (!actor || actor.length > 100 || !reason || reason.length > 500) {
      throw new AiConstructionTaskGraphError('INVALID_TASK_PLAN', '任务调整必须填写有效的操作人和原因');
    }
    const ownsTransaction = !this.db.isTransaction;
    if (ownsTransaction) this.db.exec('BEGIN IMMEDIATE');
    try {
      const before = this.snapshot(projectId);
      if (!before) throw new AiConstructionTaskGraphError('GRAPH_NOT_FOUND', '项目尚未初始化 AI 辅助构建');
      if (before.revision !== input.expectedRevision) {
        throw new AiConstructionTaskGraphError('REVISION_CONFLICT', `任务图已更新，请基于 revision ${before.revision} 重试`);
      }
      const preview = this.previewAdjustment(projectId, input.taskId, input.action);
      if (!preview.allowed) {
        throw new AiConstructionTaskGraphError('INVALID_TRANSITION', preview.reason ?? '当前任务不能执行该调整');
      }
      const now = nowIso();
      if (input.action === 'skip') {
        this.db.prepare(`UPDATE ai_construction_tasks SET status = 'skipped', updated_at = ?
          WHERE project_id = ? AND id = ?`).run(now, projectId, input.taskId);
      } else if (input.action === 'resume') {
        this.db.prepare(`UPDATE ai_construction_tasks SET status = 'pending', updated_at = ?
          WHERE project_id = ? AND id = ?`).run(now, projectId, input.taskId);
      } else if (input.action === 'defer') {
        const ordinal = Number((this.db.prepare(`SELECT COALESCE(MAX(ordinal), 0) AS ordinal
          FROM ai_construction_tasks WHERE project_id = ?`).get(projectId) as { ordinal: number }).ordinal) + 1;
        this.db.prepare(`UPDATE ai_construction_tasks SET ordinal = ?, updated_at = ?
          WHERE project_id = ? AND id = ?`).run(ordinal, now, projectId, input.taskId);
      } else {
        const targetMilestone = before.milestones.find((item) => item.id === preview.task.milestoneId)!;
        this.db.prepare(`UPDATE ai_construction_graphs SET status = 'active' WHERE project_id = ?`).run(projectId);
        this.db.prepare(`UPDATE ai_construction_milestones
          SET status = 'active', confirmed_revision_oid = NULL, updated_at = ?
          WHERE project_id = ? AND milestone_id = ?`).run(now, projectId, targetMilestone.id);
        this.db.prepare(`UPDATE ai_construction_milestones
          SET status = CASE WHEN status IN ('active', 'confirmed') THEN 'stale' ELSE status END,
            confirmed_revision_oid = CASE WHEN status IN ('active', 'confirmed') THEN NULL ELSE confirmed_revision_oid END,
            updated_at = ?
          WHERE project_id = ? AND ordinal > ?`).run(now, projectId, targetMilestone.ordinal);
        this.db.prepare(`UPDATE ai_construction_tasks
          SET status = 'pending', output_revision_oid = NULL, updated_at = ?
          WHERE project_id = ? AND id = ?`).run(now, projectId, input.taskId);
        const downstreamIds = preview.affectedTasks.map((item) => item.id).filter((id) => id !== input.taskId);
        if (downstreamIds.length) {
          const placeholders = downstreamIds.map(() => '?').join(',');
          this.db.prepare(`UPDATE ai_construction_tasks
            SET status = 'stale', output_revision_oid = NULL, updated_at = ?
            WHERE project_id = ? AND id IN (${placeholders})`)
            .run(now, projectId, ...downstreamIds);
        }
      }
      this.db.prepare(`UPDATE ai_construction_graphs
        SET revision = revision + 1, updated_at = ? WHERE project_id = ?`).run(now, projectId);
      const nextRevision = before.revision + 1;
      const adjustmentId = `adjust_${randomUUID().slice(0, 12)}`;
      this.db.prepare(`INSERT INTO ai_construction_task_adjustments
        (id, project_id, task_id, action, graph_revision_before, graph_revision_after,
          actor_username, reason, impact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
        adjustmentId, projectId, input.taskId, input.action, before.revision, nextRevision,
        actor, reason, JSON.stringify(preview), now,
      );
      if (ownsTransaction) this.db.exec('COMMIT');
      const graph = this.snapshot(projectId)!;
      return {
        graph,
        adjustment: {
          id: adjustmentId, projectId, taskId: input.taskId, action: input.action,
          graphRevisionBefore: before.revision, graphRevisionAfter: nextRevision,
          actor, reason, impact: preview, createdAt: now,
        },
      };
    } catch (error) {
      if (ownsTransaction) {
        try { this.db.exec('ROLLBACK'); } catch { /* ignore */ }
      }
      throw error;
    }
  }

  listAdjustments(projectId: string): AiConstructionTaskAdjustmentSnapshot[] {
    return (this.db.prepare(`SELECT * FROM ai_construction_task_adjustments
      WHERE project_id = ? ORDER BY created_at, rowid`).all(projectId) as any[]).map((row) => ({
      id: row.id,
      projectId: row.project_id,
      taskId: row.task_id,
      action: row.action,
      graphRevisionBefore: Number(row.graph_revision_before),
      graphRevisionAfter: Number(row.graph_revision_after),
      actor: row.actor_username,
      reason: row.reason,
      impact: JSON.parse(row.impact_json) as AiConstructionTaskAdjustmentPreview,
      createdAt: row.created_at,
    }));
  }

  snapshot(projectId: string): AiConstructionTaskGraphSnapshot | null {
    const graph = this.db.prepare(`SELECT project_id, revision, status, source,
      created_at, updated_at FROM ai_construction_graphs WHERE project_id = ?`).get(projectId) as any;
    if (!graph) return null;

    const milestones = this.db.prepare(`SELECT milestone_id, ordinal, title, status,
      confirmed_revision_oid, updated_at
      FROM ai_construction_milestones WHERE project_id = ? ORDER BY ordinal`).all(projectId) as any[];
    const tasks = this.db.prepare(`SELECT id, task_kind, milestone_id, ordinal, title, goal,
      status, required, reason, input_revision_oid, output_revision_oid, created_at, updated_at
      FROM ai_construction_tasks WHERE project_id = ? ORDER BY ordinal`).all(projectId) as any[];
    const dependencyRows = this.db.prepare(`SELECT task_id, depends_on_task_id
      FROM ai_construction_task_dependencies WHERE project_id = ?
      ORDER BY task_id, depends_on_task_id`).all(projectId) as any[];
    const dependencies = new Map<string, string[]>();
    dependencyRows.forEach((item) => {
      const list = dependencies.get(item.task_id) ?? [];
      list.push(item.depends_on_task_id);
      dependencies.set(item.task_id, list);
    });
    const confirmationRows = this.db.prepare(`SELECT id, milestone_id, graph_revision,
      package_revision_oid, actor_username, actor_role, note, created_at
      FROM ai_construction_confirmations WHERE project_id = ? ORDER BY created_at, rowid`)
      .all(projectId) as any[];

    const milestoneSnapshots: AiConstructionMilestoneSnapshot[] = milestones.map((item) => ({
      id: item.milestone_id,
      title: item.title,
      ordinal: Number(item.ordinal),
      status: item.status,
      confirmedRevisionOid: item.confirmed_revision_oid ?? undefined,
      updatedAt: item.updated_at,
    }));
    const taskSnapshots: AiConstructionTaskSnapshot[] = tasks.map((item) => ({
      id: item.id,
      kind: item.task_kind,
      milestoneId: item.milestone_id,
      ordinal: Number(item.ordinal),
      title: item.title,
      goal: item.goal,
      status: item.status,
      required: Boolean(item.required),
      reason: item.reason ?? undefined,
      inputRevisionOid: item.input_revision_oid ?? undefined,
      outputRevisionOid: item.output_revision_oid ?? undefined,
      dependsOn: dependencies.get(item.id) ?? [],
      createdAt: item.created_at,
      updatedAt: item.updated_at,
    }));
    const confirmationSnapshots: AiConstructionConfirmationSnapshot[] = confirmationRows.map((item) => ({
      id: item.id,
      milestoneId: item.milestone_id,
      graphRevision: Number(item.graph_revision),
      packageRevisionOid: item.package_revision_oid,
      actor: { username: item.actor_username, role: item.actor_role },
      note: item.note ?? undefined,
      createdAt: item.created_at,
    }));

    return {
      projectId: graph.project_id,
      revision: Number(graph.revision),
      status: graph.status,
      source: graph.source,
      createdAt: graph.created_at,
      updatedAt: graph.updated_at,
      milestones: milestoneSnapshots,
      tasks: taskSnapshots,
      confirmations: confirmationSnapshots,
      progress: deriveProgress(
        graph.status, milestoneSnapshots, taskSnapshots, this.measuredTaskDurations(),
      ),
    };
  }

  /**
   * 增量补充阶段。懒创建而不是随图初始化：
   *
   * - 存量项目不必迁移；
   * - 从没做过增量的项目不会凭空多出一个永远 pending 的阶段，进度和阶段数也就不会被它带偏。
   *
   * 序号排在 construction_completed 之后。这个阶段可以反复进入——每补一批资料一轮。
   */
  ensureIncrementMilestone(projectId: string): string {
    const existing = this.db.prepare(`SELECT milestone_id FROM ai_construction_milestones
      WHERE project_id = ? AND milestone_id = ?`)
      .get(projectId, INCREMENT_MILESTONE_ID) as { milestone_id: string } | undefined;
    if (existing) return INCREMENT_MILESTONE_ID;
    const max = this.db.prepare(`SELECT COALESCE(MAX(ordinal), 0) AS ordinal
      FROM ai_construction_milestones WHERE project_id = ?`).get(projectId) as { ordinal: number };
    this.db.prepare(`INSERT INTO ai_construction_milestones
      (project_id, milestone_id, ordinal, title, status, updated_at)
      VALUES (?, ?, ?, ?, 'pending', ?)`)
      .run(projectId, INCREMENT_MILESTONE_ID, Number(max.ordinal) + 1, INCREMENT_MILESTONE_TITLE, nowIso());
    return INCREMENT_MILESTONE_ID;
  }

  /**
   * 把某个阶段设为活动。只动这一个，其余阶段的确认状态一概不碰——
   * 增量补充的前提就是「已确认的部分继续有效」。
   */
  activateMilestone(projectId: string, milestoneId: string): void {
    this.db.prepare(`UPDATE ai_construction_milestones SET status = 'active', updated_at = ?
      WHERE project_id = ? AND milestone_id = ? AND status <> 'active'`)
      .run(nowIso(), projectId, milestoneId);
    this.db.prepare(`UPDATE ai_construction_graphs SET status = 'active', updated_at = ?
      WHERE project_id = ?`).run(nowIso(), projectId);
  }

  /**
   * 跨项目的同类任务实测耗时，用来把 ETA 从静态表拉到真实数据上。
   * 只看本项目没有意义：每种 kind 一个项目通常只跑一次，需要估算的时候必然还没有样本。
   *
   * 只取最近 200 条，老样本反映的是早期模型和资料规模，参考价值低；
   * 每种 kind 最多留 20 个，避免某一类任务把区间算法的极值拉偏。
   */
  private measuredTaskDurations(): MeasuredTaskDurations {
    const rows = this.db.prepare(`
      SELECT task_kind, created_at, updated_at FROM ai_construction_tasks
      WHERE status = 'completed' ORDER BY updated_at DESC LIMIT 200
    `).all() as Array<{ task_kind: string; created_at: string; updated_at: string }>;
    const measured: MeasuredTaskDurations = {};
    for (const row of rows) {
      const start = Date.parse(row.created_at);
      const end = Date.parse(row.updated_at);
      if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
      const minutes = (end - start) / MINUTE_MS;
      if (minutes < MIN_CREDIBLE_SAMPLE_MINUTES) continue;
      const kind = row.task_kind as AiConstructionTaskKind;
      const bucket = measured[kind] ?? (measured[kind] = []);
      if (bucket.length < 20) bucket.push(minutes);
    }
    return measured;
  }
}
