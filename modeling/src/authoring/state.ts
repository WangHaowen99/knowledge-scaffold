import { randomUUID } from 'node:crypto';
import { EventEmitter } from 'node:events';
import type { DatabaseSync } from 'node:sqlite';
import { getDb, nowIso } from '../db/index.js';
import {
  AUTHORING_INTERACTION_TYPES,
  AuthoringStateError,
} from './types.js';
import type {
  AuthoringCommand,
  AuthoringCommandResult,
  AuthoringInteraction,
  AuthoringInteractionType,
  AuthoringRun,
  AuthoringRunStatus,
  AuthoringSnapshot,
  JsonObject,
  JsonValue,
  ProjectEvent,
  ProjectEventPage,
} from './types.js';

const ACTIVE_RUN_STATUSES: AuthoringRunStatus[] = ['queued', 'running', 'waiting'];
const TERMINAL_RUN_STATUSES: AuthoringRunStatus[] = ['completed', 'failed', 'cancelled'];
const RUN_STATUSES: AuthoringRunStatus[] = [...ACTIVE_RUN_STATUSES, ...TERMINAL_RUN_STATUSES];
const SYSTEM_EVENT_PREFIX = 'authoring.';
const EVENT_TYPE = /^[a-z][a-z0-9_.-]{0,79}$/;

const ALLOWED_RUN_TRANSITIONS: Record<AuthoringRunStatus, ReadonlySet<AuthoringRunStatus>> = {
  queued: new Set(['running', 'failed', 'cancelled']),
  running: new Set(['waiting', 'completed', 'failed', 'cancelled']),
  waiting: new Set(['running', 'completed', 'failed', 'cancelled']),
  completed: new Set(),
  failed: new Set(),
  cancelled: new Set(),
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function normalizeJson(value: unknown, seen = new Set<object>()): JsonValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (seen.has(value)) throw new AuthoringStateError('INVALID_INPUT', '内容不能循环引用');
    seen.add(value);
    const normalized = value.map((item) => normalizeJson(item, seen));
    seen.delete(value);
    return normalized;
  }
  if (isPlainObject(value)) {
    if (seen.has(value)) throw new AuthoringStateError('INVALID_INPUT', '内容不能循环引用');
    seen.add(value);
    const normalized: JsonObject = {};
    for (const key of Object.keys(value).sort()) {
      const item = value[key];
      if (item === undefined) throw new AuthoringStateError('INVALID_INPUT', '内容不能包含空字段');
      normalized[key] = normalizeJson(item, seen);
    }
    seen.delete(value);
    return normalized;
  }
  throw new AuthoringStateError('INVALID_INPUT', '内容必须是 JSON');
}

function encodeObject(value: unknown, label: string): string {
  if (!isPlainObject(value)) throw new AuthoringStateError('INVALID_INPUT', `${label}格式不正确`);
  return JSON.stringify(normalizeJson(value));
}

function decodeObject(value: string | null | undefined): JsonObject {
  if (!value) return {};
  const parsed = JSON.parse(value) as unknown;
  if (!isPlainObject(parsed)) throw new Error('数据库中的 JSON 格式不正确');
  return parsed as JsonObject;
}

function requiredText(value: string, label: string, maxLength: number): string {
  const text = String(value ?? '').trim();
  if (!text) throw new AuthoringStateError('INVALID_INPUT', `${label}不能为空`);
  if (text.length > maxLength) throw new AuthoringStateError('INVALID_INPUT', `${label}过长`);
  return text;
}

function optionalText(value: string | undefined, label: string, maxLength: number): string | undefined {
  if (value === undefined) return undefined;
  const text = value.trim();
  if (!text) return undefined;
  if (text.length > maxLength) throw new AuthoringStateError('INVALID_INPUT', `${label}过长`);
  return text;
}

function rowToRun(row: any): AuthoringRun {
  return {
    id: row.id,
    projectId: row.project_id,
    intent: row.intent,
    status: row.status,
    baseRevision: row.base_revision ?? undefined,
    state: decodeObject(row.state_json),
    result: row.result_json ? decodeObject(row.result_json) : undefined,
    error: row.error ?? undefined,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    finishedAt: row.finished_at ?? undefined,
  };
}

function normalizeRunState(state: JsonObject, status: AuthoringRunStatus, now: string): JsonObject {
  const rawProgress = Number(state.progress);
  const progress = Number.isFinite(rawProgress)
    ? Math.max(0, Math.min(100, Math.round(rawProgress)))
    : undefined;
  const rawRetryCount = Number(state.retryCount);
  const retryCount = Number.isFinite(rawRetryCount) && rawRetryCount >= 0
    ? Math.floor(rawRetryCount)
    : 0;
  return {
    ...state,
    ...(['completed', 'failed', 'cancelled'].includes(status)
      ? (state.phase ? {} : { phase: status })
      : state.phase ? {} : { phase: status === 'queued' ? 'queued' : 'running' }),
    ...(progress === undefined ? {} : { progress }),
    retryCount,
    lastHeartbeatAt: now,
  };
}

function runObservabilityPayload(state: JsonObject): JsonObject {
  const keys = [
    'phase', 'currentStep', 'progress', 'lastHeartbeatAt', 'retryCount', 'eta',
    'queuePosition', 'errorCode', 'recoveryAction',
  ];
  return Object.fromEntries(keys
    .filter((key) => state[key] !== undefined)
    .map((key) => [key, state[key]])) as JsonObject;
}

function rowToInteraction(row: any): AuthoringInteraction {
  return {
    id: row.id,
    projectId: row.project_id,
    runId: row.run_id,
    type: row.type,
    status: row.status,
    baseRevision: row.base_revision ?? undefined,
    payload: decodeObject(row.payload_json),
    responseId: row.response_id ?? undefined,
    response: row.response_json ? decodeObject(row.response_json) : undefined,
    createdAt: row.created_at,
    respondedAt: row.responded_at ?? undefined,
  };
}

function rowToEvent(row: any): ProjectEvent {
  return {
    id: row.id,
    projectId: row.project_id,
    seq: Number(row.seq),
    type: row.type,
    runId: row.run_id ?? undefined,
    interactionId: row.interaction_id ?? undefined,
    payload: decodeObject(row.payload_json),
    createdAt: row.created_at,
  };
}

function sameJson(left: string | null | undefined, right: string): boolean {
  if (!left) return false;
  return JSON.stringify(normalizeJson(JSON.parse(left))) === right;
}

// 事件写入后唤醒同进程内的 SSE 订阅者，省掉“等下一次轮询”的固定延迟。
// 只发信号不带内容：订阅方仍从 project_events 按 seq 读取，因此漏通知只会退化成轮询兜底。
const projectEventBus = new EventEmitter();
projectEventBus.setMaxListeners(0);

// 事务尚未提交时通知会让订阅方读空，因此最多顺延几个 tick 等待提交完成。
const MAX_NOTIFY_DEFERRALS = 5;

export function onProjectEventAppended(projectId: string, listener: () => void): () => void {
  const channel = `project:${projectId}`;
  projectEventBus.on(channel, listener);
  return () => { projectEventBus.off(channel, listener); };
}

/**
 * 持久化建模状态。公开面只有命令、快照和事件回放；事务、去重与状态转换均在内部完成。
 */
export class AuthoringState {
  private readonly pendingNotifications = new Set<string>();
  private notifyScheduled = false;

  constructor(private readonly db: DatabaseSync = getDb()) {}

  private scheduleNotification(projectId: string): void {
    this.pendingNotifications.add(projectId);
    if (this.notifyScheduled) return;
    this.notifyScheduled = true;
    setImmediate(() => this.flushNotifications(0));
  }

  private flushNotifications(deferrals: number): void {
    // 外层事务可能由任务图或编排器持有，此时提交尚未发生，订阅方读不到新事件。
    if (this.db.isTransaction && deferrals < MAX_NOTIFY_DEFERRALS) {
      setImmediate(() => this.flushNotifications(deferrals + 1));
      return;
    }
    this.notifyScheduled = false;
    const projectIds = [...this.pendingNotifications];
    this.pendingNotifications.clear();
    for (const projectId of projectIds) {
      try { projectEventBus.emit(`project:${projectId}`); } catch { /* 订阅方异常不影响写入 */ }
    }
  }

  apply(command: AuthoringCommand): AuthoringCommandResult {
    switch (command.kind) {
      case 'start_run': return this.startRun(command);
      case 'update_run': return this.updateRun(command);
      case 'open_interaction': return this.openInteraction(command);
      case 'respond_interaction': return this.respondInteraction(command);
      case 'publish_event': return this.publishEvent(command);
      default: throw new AuthoringStateError('INVALID_INPUT', '操作不支持');
    }
  }

  snapshot(projectId: string): AuthoringSnapshot {
    return this.readTransaction(() => {
      this.assertProject(projectId);
      const placeholders = ACTIVE_RUN_STATUSES.map(() => '?').join(',');
      const activeRuns = (this.db.prepare(`SELECT * FROM authoring_runs
        WHERE project_id = ? AND status IN (${placeholders}) ORDER BY created_at, rowid`)
        .all(projectId, ...ACTIVE_RUN_STATUSES) as any[]).map(rowToRun);
      const pendingInteractions = (this.db.prepare(`SELECT * FROM authoring_interactions
        WHERE project_id = ? AND status = 'pending' ORDER BY created_at, rowid`)
        .all(projectId) as any[]).map(rowToInteraction);
      const last = this.db.prepare(
        'SELECT COALESCE(MAX(seq), 0) AS seq FROM project_events WHERE project_id = ?',
      ).get(projectId) as { seq: number };
      return { projectId, activeRuns, pendingInteractions, lastEventSeq: Number(last.seq) };
    });
  }

  interaction(projectId: string, interactionId: string): AuthoringInteraction | null {
    return this.readTransaction(() => {
      this.assertProject(projectId);
      const row = this.db.prepare(`SELECT * FROM authoring_interactions
        WHERE project_id = ? AND id = ?`).get(projectId, interactionId);
      return row ? rowToInteraction(row) : null;
    });
  }

  replay(projectId: string, afterSeq = 0, limit = 200): ProjectEventPage {
    if (!Number.isInteger(afterSeq) || afterSeq < 0) {
      throw new AuthoringStateError('INVALID_INPUT', '事件位置不正确');
    }
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new AuthoringStateError('INVALID_INPUT', '每次最多读取 1000 条事件');
    }
    return this.readTransaction(() => {
      this.assertProject(projectId);
      const rows = this.db.prepare(`SELECT * FROM project_events
        WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT ?`)
        .all(projectId, afterSeq, limit + 1) as any[];
      const hasMore = rows.length > limit;
      const events = rows.slice(0, limit).map(rowToEvent);
      return {
        events,
        nextSeq: events.at(-1)?.seq ?? afterSeq,
        hasMore,
      };
    });
  }

  private startRun(command: Extract<AuthoringCommand, { kind: 'start_run' }>): AuthoringCommandResult {
    return this.transaction(() => {
      this.assertProject(command.projectId);
      const now = nowIso();
      const status = command.status ?? 'running';
      if (status !== 'queued' && status !== 'running') {
        throw new AuthoringStateError('INVALID_INPUT', '任务初始状态不支持');
      }
      const run: AuthoringRun = {
        id: `ar_${randomUUID().slice(0, 12)}`,
        projectId: command.projectId,
        intent: requiredText(command.intent, '任务名称', 100),
        status,
        baseRevision: optionalText(command.baseRevision, '修订号', 200),
        state: decodeObject(encodeObject(command.state ?? {}, '任务进度')),
        createdAt: now,
        updatedAt: now,
      };
      run.state = normalizeRunState({ ...run.state, jobId: run.id }, status, now);
      this.db.prepare(`INSERT INTO authoring_runs
        (id, project_id, intent, status, base_revision, state_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)`)
        .run(run.id, run.projectId, run.intent, run.status, run.baseRevision ?? null,
          encodeObject(run.state, '任务进度'), now, now);
      const event = this.appendEvent({
        projectId: run.projectId,
        type: 'authoring.run.started',
        runId: run.id,
        payload: {
          runId: run.id,
          intent: run.intent,
          status: run.status,
          ...runObservabilityPayload(run.state),
          ...(run.baseRevision ? { baseRevision: run.baseRevision } : {}),
        },
      });
      return { kind: 'run', run, events: [event] };
    });
  }

  private updateRun(command: Extract<AuthoringCommand, { kind: 'update_run' }>): AuthoringCommandResult {
    return this.transaction(() => {
      this.assertProject(command.projectId);
      const current = this.getRun(command.projectId, command.runId);
      if (!current) throw new AuthoringStateError('RUN_NOT_FOUND', '任务不存在');
      if (!RUN_STATUSES.includes(command.status)) {
        throw new AuthoringStateError('INVALID_INPUT', '任务状态不支持');
      }
      if (current.status !== command.status && !ALLOWED_RUN_TRANSITIONS[current.status].has(command.status)) {
        throw new AuthoringStateError('INVALID_TRANSITION', '任务状态不能这样更改');
      }
      const nextState = command.state === undefined
        ? current.state
        : { ...current.state, ...command.state };
      const stateJson = encodeObject(normalizeRunState(nextState, command.status, nowIso()), '任务进度');
      const resultJson = command.result === undefined
        ? (current.result ? encodeObject(current.result, '任务结果') : null)
        : encodeObject(command.result, '任务结果');
      const error = command.error === undefined ? current.error : optionalText(command.error, '失败原因', 2000);
      if (command.status === 'failed' && !error) {
        throw new AuthoringStateError('INVALID_INPUT', '请填写失败原因');
      }
      const unchanged = current.status === command.status
        && sameJson(encodeObject(current.state, '任务进度'), stateJson)
        && ((current.result === undefined && resultJson === null)
          || (current.result !== undefined && resultJson !== null
            && sameJson(encodeObject(current.result, '任务结果'), resultJson)))
        && current.error === error;
      if (unchanged) return { kind: 'run', run: current, events: [] };
      if (TERMINAL_RUN_STATUSES.includes(current.status)) {
        throw new AuthoringStateError('INVALID_TRANSITION', '已结束的任务不能再更改');
      }

      const now = nowIso();
      const finishedAt = TERMINAL_RUN_STATUSES.includes(command.status) ? now : undefined;
      this.db.prepare(`UPDATE authoring_runs SET status = ?, state_json = ?, result_json = ?,
        error = ?, updated_at = ?, finished_at = ? WHERE id = ? AND project_id = ?`)
        .run(command.status, stateJson, resultJson, error ?? null, now, finishedAt ?? null,
          command.runId, command.projectId);
      const run = this.getRun(command.projectId, command.runId)!;
      const events: ProjectEvent[] = [this.appendEvent({
        projectId: run.projectId,
        type: 'authoring.run.updated',
        runId: run.id,
          payload: {
            runId: run.id,
            status: run.status,
            ...runObservabilityPayload(run.state),
            ...(run.error ? { error: run.error } : {}),
        },
      })];

      if (finishedAt) {
        const pending = this.db.prepare(`SELECT id, type FROM authoring_interactions
          WHERE project_id = ? AND run_id = ? AND status = 'pending' ORDER BY created_at, rowid`)
          .all(run.projectId, run.id) as Array<{ id: string; type: AuthoringInteractionType }>;
        this.db.prepare(`UPDATE authoring_interactions SET status = 'cancelled'
          WHERE project_id = ? AND run_id = ? AND status = 'pending'`).run(run.projectId, run.id);
        for (const interaction of pending) {
          events.push(this.appendEvent({
            projectId: run.projectId,
            type: 'authoring.interaction.cancelled',
            runId: run.id,
            interactionId: interaction.id,
            payload: { interactionId: interaction.id, type: interaction.type },
          }));
        }
      }
      return { kind: 'run', run, events };
    });
  }

  private openInteraction(
    command: Extract<AuthoringCommand, { kind: 'open_interaction' }>,
  ): AuthoringCommandResult {
    return this.transaction(() => {
      this.assertProject(command.projectId);
      const run = this.getRun(command.projectId, command.runId);
      if (!run) throw new AuthoringStateError('RUN_NOT_FOUND', '任务不存在');
      if (run.status !== 'running' && run.status !== 'waiting') {
        throw new AuthoringStateError('INVALID_TRANSITION', '该任务当前不能新建待办');
      }
      if (!AUTHORING_INTERACTION_TYPES.includes(command.type as AuthoringInteractionType)) {
        throw new AuthoringStateError('INVALID_INPUT', '待办类型不支持');
      }
      const now = nowIso();
      const payloadJson = encodeObject(command.payload, '待办内容');
      const interaction: AuthoringInteraction = {
        id: `ai_${randomUUID().slice(0, 12)}`,
        projectId: command.projectId,
        runId: command.runId,
        type: command.type,
        status: 'pending',
        baseRevision: optionalText(command.baseRevision, '修订号', 200),
        payload: decodeObject(payloadJson),
        createdAt: now,
      };
      this.db.prepare(`INSERT INTO authoring_interactions
        (id, project_id, run_id, type, status, base_revision, payload_json, created_at)
        VALUES (?,?,?,?,?,?,?,?)`)
        .run(interaction.id, interaction.projectId, interaction.runId, interaction.type,
          interaction.status, interaction.baseRevision ?? null, payloadJson, now);

      const events: ProjectEvent[] = [];
      let nextRun = run;
      if (run.status === 'running') {
        const waitingState = normalizeRunState({
          ...run.state,
          phase: run.state.phase ?? '等待用户回答',
          currentStep: run.state.currentStep ?? '等待用户回答',
        }, 'waiting', now);
        this.db.prepare(`UPDATE authoring_runs SET status = 'waiting', state_json = ?, updated_at = ?
          WHERE id = ? AND project_id = ?`).run(
          encodeObject(waitingState, '任务进度'), now, run.id, run.projectId,
        );
        nextRun = this.getRun(run.projectId, run.id)!;
        events.push(this.appendEvent({
          projectId: run.projectId,
          type: 'authoring.run.updated',
          runId: run.id,
          payload: {
            runId: run.id,
            status: 'waiting',
            ...runObservabilityPayload(nextRun.state),
          },
        }));
      }
      events.push(this.appendEvent({
        projectId: interaction.projectId,
        type: 'authoring.interaction.opened',
        runId: interaction.runId,
        interactionId: interaction.id,
        payload: {
          interactionId: interaction.id,
          type: interaction.type,
          payload: interaction.payload,
          ...(interaction.baseRevision ? { baseRevision: interaction.baseRevision } : {}),
        },
      }));
      return { kind: 'interaction', interaction, run: nextRun, replayed: false, events };
    });
  }

  private respondInteraction(
    command: Extract<AuthoringCommand, { kind: 'respond_interaction' }>,
  ): AuthoringCommandResult {
    return this.transaction(() => {
      this.assertProject(command.projectId);
      const responseId = requiredText(command.responseId, '请求编号', 200);
      const responseJson = encodeObject(command.response, '回答');
      const prior = this.db.prepare(`SELECT * FROM authoring_interactions
        WHERE project_id = ? AND response_id = ?`).get(command.projectId, responseId) as any;
      if (prior) {
        if (prior.id !== command.interactionId || !sameJson(prior.response_json, responseJson)) {
          throw new AuthoringStateError('IDEMPOTENCY_CONFLICT', '请求编号已用于其他回答');
        }
        const interaction = rowToInteraction(prior);
        const run = this.getRun(command.projectId, interaction.runId);
        if (!run) throw new AuthoringStateError('RUN_NOT_FOUND', '任务不存在');
        return { kind: 'interaction', interaction, run, replayed: true, events: [] };
      }

      const raw = this.db.prepare(`SELECT * FROM authoring_interactions
        WHERE id = ? AND project_id = ?`).get(command.interactionId, command.projectId) as any;
      if (!raw) throw new AuthoringStateError('INTERACTION_NOT_FOUND', '待办不存在');
      if (raw.status !== 'pending') {
        throw new AuthoringStateError('INTERACTION_CLOSED', '该待办已处理');
      }
      const now = nowIso();
      this.db.prepare(`UPDATE authoring_interactions SET status = 'answered', response_id = ?,
        response_json = ?, responded_at = ? WHERE id = ? AND project_id = ? AND status = 'pending'`)
        .run(responseId, responseJson, now, command.interactionId, command.projectId);
      const interaction = rowToInteraction(this.db.prepare(
        'SELECT * FROM authoring_interactions WHERE id = ?',
      ).get(command.interactionId));
      const events: ProjectEvent[] = [this.appendEvent({
        projectId: interaction.projectId,
        type: 'authoring.interaction.answered',
        runId: interaction.runId,
        interactionId: interaction.id,
        payload: {
          interactionId: interaction.id,
          type: interaction.type,
          response: interaction.response ?? {},
        },
      })];

      let run = this.getRun(command.projectId, interaction.runId);
      if (!run) throw new AuthoringStateError('RUN_NOT_FOUND', '任务不存在');
      const pending = this.db.prepare(`SELECT COUNT(*) AS count FROM authoring_interactions
        WHERE project_id = ? AND run_id = ? AND status = 'pending'`)
        .get(command.projectId, interaction.runId) as { count: number };
      if (Number(pending.count) === 0 && run.status === 'waiting') {
        const resumedStateInput = { ...run.state };
        // 这是打开待办时补上的展示占位，不是后台恢复后的真实执行步骤。若继续保留，
        // 运行中的任务卡会优先显示“等待用户回答”，直到业务代码再次上报进度。
        if (resumedStateInput.currentStep === '等待用户回答') {
          delete resumedStateInput.currentStep;
        }
        const resumedState = normalizeRunState(resumedStateInput, 'running', now);
        this.db.prepare(`UPDATE authoring_runs SET status = 'running', state_json = ?, updated_at = ?
          WHERE id = ? AND project_id = ?`).run(
          encodeObject(resumedState, '任务进度'), now, run.id, run.projectId,
        );
        run = this.getRun(run.projectId, run.id)!;
        events.push(this.appendEvent({
          projectId: run.projectId,
          type: 'authoring.run.updated',
          runId: run.id,
          payload: {
            runId: run.id,
            status: 'running',
            ...runObservabilityPayload(run.state),
          },
        }));
      }
      return { kind: 'interaction', interaction, run, replayed: false, events };
    });
  }

  private publishEvent(command: Extract<AuthoringCommand, { kind: 'publish_event' }>): AuthoringCommandResult {
    return this.transaction(() => {
      this.assertProject(command.projectId);
      const type = requiredText(command.type, '事件类型', 80);
      if (!EVENT_TYPE.test(type) || type.startsWith(SYSTEM_EVENT_PREFIX)) {
        throw new AuthoringStateError('INVALID_INPUT', '事件类型不支持');
      }
      let runId = command.runId;
      if (runId && !this.getRun(command.projectId, runId)) {
        throw new AuthoringStateError('RUN_NOT_FOUND', '任务不存在');
      }
      if (command.interactionId) {
        const interaction = this.db.prepare(`SELECT id, run_id FROM authoring_interactions
          WHERE id = ? AND project_id = ?`).get(command.interactionId, command.projectId) as
          { id: string; run_id: string } | undefined;
        if (!interaction) throw new AuthoringStateError('INTERACTION_NOT_FOUND', '待办不存在');
        if (runId && runId !== interaction.run_id) {
          throw new AuthoringStateError('INVALID_INPUT', '任务与待办不匹配');
        }
        runId = interaction.run_id;
      }
      return { kind: 'event', event: this.appendEvent({
        projectId: command.projectId,
        type,
        runId,
        interactionId: command.interactionId,
        payload: command.payload ?? {},
      }) };
    });
  }

  private appendEvent(input: {
    projectId: string;
    type: string;
    runId?: string;
    interactionId?: string;
    payload: JsonObject;
  }): ProjectEvent {
    const next = this.db.prepare(
      'SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM project_events WHERE project_id = ?',
    ).get(input.projectId) as { seq: number };
    const createdAt = nowIso();
    const payloadJson = encodeObject(input.payload, '事件内容');
    const event: ProjectEvent = {
      id: `pe_${randomUUID().slice(0, 12)}`,
      projectId: input.projectId,
      seq: Number(next.seq),
      type: input.type,
      runId: input.runId,
      interactionId: input.interactionId,
      payload: decodeObject(payloadJson),
      createdAt,
    };
    this.db.prepare(`INSERT INTO project_events
      (id, project_id, seq, type, run_id, interaction_id, payload_json, created_at)
      VALUES (?,?,?,?,?,?,?,?)`)
      .run(event.id, event.projectId, event.seq, event.type, event.runId ?? null,
        event.interactionId ?? null, payloadJson, event.createdAt);
    this.scheduleNotification(event.projectId);
    return event;
  }

  private getRun(projectId: string, runId: string): AuthoringRun | null {
    const row = this.db.prepare('SELECT * FROM authoring_runs WHERE id = ? AND project_id = ?')
      .get(runId, projectId);
    return row ? rowToRun(row) : null;
  }

  private assertProject(projectId: string): void {
    const id = requiredText(projectId, '项目', 200);
    const project = this.db.prepare('SELECT id FROM projects WHERE id = ?').get(id);
    if (!project) throw new AuthoringStateError('PROJECT_NOT_FOUND', '项目不存在');
  }

  private transaction<T>(work: () => T): T {
    if (this.db.isTransaction) {
      const savepoint = `authoring_${randomUUID().replaceAll('-', '')}`;
      this.db.exec(`SAVEPOINT ${savepoint}`);
      try {
        const result = work();
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        return result;
      } catch (error) {
        this.db.exec(`ROLLBACK TO SAVEPOINT ${savepoint}`);
        this.db.exec(`RELEASE SAVEPOINT ${savepoint}`);
        throw error;
      }
    }
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = work();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private readTransaction<T>(work: () => T): T {
    if (this.db.isTransaction) return work();
    this.db.exec('BEGIN');
    try {
      const result = work();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      if (this.db.isTransaction) this.db.exec('ROLLBACK');
      throw error;
    }
  }
}

let defaultState: AuthoringState | undefined;

export function getAuthoringState(): AuthoringState {
  defaultState ??= new AuthoringState();
  return defaultState;
}
