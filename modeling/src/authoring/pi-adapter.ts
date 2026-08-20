import { AuthoringState, getAuthoringState } from './state.js';
import type {
  AuthoringCommandResult,
  AuthoringInteraction,
  AuthoringInteractionType,
  AuthoringRun,
  JsonObject,
  ProjectEvent,
} from './types.js';

export interface PiRunResult {
  kind: 'run';
  run: AuthoringRun;
  events: ProjectEvent[];
}

export interface PiInteractionResult {
  kind: 'interaction';
  interaction: AuthoringInteraction;
  run: AuthoringRun;
  replayed: boolean;
  events: ProjectEvent[];
}

export interface PiProgressEvent {
  type: string;
  payload?: JsonObject;
}

function runResult(result: AuthoringCommandResult): PiRunResult {
  if (result.kind !== 'run') throw new Error('任务结果不正确');
  return result;
}

function interactionResult(result: AuthoringCommandResult): PiInteractionResult {
  if (result.kind !== 'interaction') throw new Error('待办结果不正确');
  return result;
}

/** Pi 的统一任务入口；状态转换、事务和事件均交由 AuthoringState 处理。 */
export class PiAuthoringAdapter {
  constructor(private readonly authoringState: AuthoringState = getAuthoringState()) {}

  start(
    projectId: string,
    intent: string,
    baseRevision?: string,
    state?: JsonObject,
  ): PiRunResult {
    return runResult(this.authoringState.apply({
      kind: 'start_run',
      projectId,
      intent,
      ...(baseRevision === undefined ? {} : { baseRevision }),
      ...(state === undefined ? {} : { state }),
    }));
  }

  progress(
    projectId: string,
    runId: string,
    state: JsonObject,
    event?: PiProgressEvent,
  ): PiRunResult {
    const events: ProjectEvent[] = [];
    const activeRun = this.authoringState.snapshot(projectId).activeRuns
      .find((run) => run.id === runId);

    if (activeRun && activeRun.status !== 'running') {
      const resumed = runResult(this.authoringState.apply({
        kind: 'update_run',
        projectId,
        runId,
        status: 'running',
      }));
      events.push(...resumed.events);
    }

    const updated = runResult(this.authoringState.apply({
      kind: 'update_run',
      projectId,
      runId,
      status: 'running',
      state,
    }));
    events.push(...updated.events);

    if (event) {
      const published = this.authoringState.apply({
        kind: 'publish_event',
        projectId,
        runId,
        type: event.type,
        ...(event.payload === undefined ? {} : { payload: event.payload }),
      });
      if (published.kind !== 'event') throw new Error('事件结果不正确');
      events.push(published.event);
    }

    return { kind: 'run', run: updated.run, events };
  }

  /** 更新统一 Job 的阶段心跳；不会改变业务结果，只记录当前可观测状态。 */
  heartbeat(projectId: string, runId: string, state: JsonObject): PiRunResult {
    const current = this.authoringState.snapshot(projectId).activeRuns.find((run) => run.id === runId);
    if (!current) throw new Error('任务不存在或已结束');
    // 待办打开后任务会进入 waiting。后台定时器不能把它误恢复为 running，
    // 否则前端会丢掉“等待用户操作”的真实状态。
    if (current.status === 'waiting') return { kind: 'run', run: current, events: [] };
    return runResult(this.authoringState.apply({
      kind: 'update_run',
      projectId,
      runId,
      status: 'running',
      state: {
        ...state,
        lastHeartbeatAt: new Date().toISOString(),
      },
    }));
  }

  request(
    projectId: string,
    runId: string,
    type: AuthoringInteractionType,
    payload: JsonObject,
    baseRevision?: string,
  ): PiInteractionResult {
    return interactionResult(this.authoringState.apply({
      kind: 'open_interaction',
      projectId,
      runId,
      type,
      payload,
      ...(baseRevision === undefined ? {} : { baseRevision }),
    }));
  }

  respond(
    projectId: string,
    interactionId: string,
    responseId: string,
    response: JsonObject,
  ): PiInteractionResult {
    return interactionResult(this.authoringState.apply({
      kind: 'respond_interaction',
      projectId,
      interactionId,
      responseId,
      response,
    }));
  }

  complete(projectId: string, runId: string, result?: JsonObject): PiRunResult {
    return runResult(this.authoringState.apply({
      kind: 'update_run',
      projectId,
      runId,
      status: 'completed',
      ...(result === undefined ? {} : { result }),
    }));
  }

  fail(
    projectId: string,
    runId: string,
    error: string,
    state?: JsonObject,
  ): PiRunResult {
    return runResult(this.authoringState.apply({
      kind: 'update_run',
      projectId,
      runId,
      status: 'failed',
      error,
      state: {
        phase: '失败',
        currentStep: '任务未完成',
        errorCode: 'AUTHORING_RUN_FAILED',
        recoveryAction: 'retry_last_step',
        ...(state ?? {}),
      },
    }));
  }
}

let defaultAdapter: PiAuthoringAdapter | undefined;

export function getPiAuthoringAdapter(): PiAuthoringAdapter {
  defaultAdapter ??= new PiAuthoringAdapter();
  return defaultAdapter;
}
