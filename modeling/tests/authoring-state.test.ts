import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-authoring-state-'));

const [{ createProject }, { getDb }, authoring] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/authoring/index.js'),
]);

const {
  AUTHORING_INTERACTION_TYPES,
  AuthoringState,
  AuthoringStateError,
} = authoring;

function projectId(): string {
  return createProject({ owner: 'test', name: `项目-${Math.random()}` }).id;
}

function startRun(state: InstanceType<typeof AuthoringState>, project: string): string {
  const result = state.apply({
    kind: 'start_run',
    projectId: project,
    intent: '整理业务对象',
    baseRevision: 'R1',
    state: { step: 1 },
  });
  assert.equal(result.kind, 'run');
  if (result.kind !== 'run') throw new Error('未创建任务');
  return result.run.id;
}

function openInteraction(
  state: InstanceType<typeof AuthoringState>,
  project: string,
  runId: string,
  type: typeof AUTHORING_INTERACTION_TYPES[number] = 'decision',
): string {
  const result = state.apply({
    kind: 'open_interaction',
    projectId: project,
    runId,
    type,
    baseRevision: 'R1',
    payload: { title: '请确认', options: ['保留', '修改'] },
  });
  assert.equal(result.kind, 'interaction');
  if (result.kind !== 'interaction') throw new Error('未创建待办');
  return result.interaction.id;
}

test('任务和八类待办可在重新读取后继续', () => {
  const project = projectId();
  const first = new AuthoringState(getDb());
  const runId = startRun(first, project);

  for (const type of AUTHORING_INTERACTION_TYPES) {
    openInteraction(first, project, runId, type);
  }

  const restored = new AuthoringState(getDb()).snapshot(project);
  assert.equal(restored.activeRuns.length, 1);
  assert.equal(restored.activeRuns[0]?.status, 'waiting');
  assert.equal(restored.activeRuns[0]?.state.step, 1);
  assert.equal(restored.activeRuns[0]?.state.jobId, runId);
  assert.equal(restored.activeRuns[0]?.state.retryCount, 0);
  assert.equal(restored.activeRuns[0]?.state.phase, 'running');
  assert.ok(Date.parse(String(restored.activeRuns[0]?.state.lastHeartbeatAt)) > 0);
  assert.deepEqual(restored.pendingInteractions.map((item) => item.type), AUTHORING_INTERACTION_TYPES);
  assert.equal(restored.lastEventSeq, 10);

  assert.throws(() => first.apply({
    kind: 'open_interaction',
    projectId: project,
    runId,
    type: 'other' as any,
    payload: {},
  }), (error) => error instanceof AuthoringStateError && error.code === 'INVALID_INPUT');
});

test('进度更新合并原状态并发布统一 Job 心跳字段', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const runId = startRun(state, project);
  const updated = state.apply({
    kind: 'update_run', projectId: project, runId, status: 'running',
    state: { phase: '规则抽取', currentStep: '生成第 2/5 批', progress: 40, retryCount: 1 },
  });
  assert.equal(updated.kind, 'run');
  if (updated.kind !== 'run') throw new Error('未更新任务');
  assert.equal(updated.run.state.step, 1, '局部心跳不能覆盖既有状态');
  assert.equal(updated.run.state.phase, '规则抽取');
  assert.equal(updated.run.state.progress, 40);
  assert.equal(updated.run.state.retryCount, 1);
  assert.ok(Date.parse(String(updated.run.state.lastHeartbeatAt)) > 0);
  assert.deepEqual(updated.events[0]?.payload, {
    runId,
    status: 'running',
    phase: '规则抽取',
    currentStep: '生成第 2/5 批',
    progress: 40,
    lastHeartbeatAt: updated.run.state.lastHeartbeatAt,
    retryCount: 1,
  });
});

test('重复回答只处理一次', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const runId = startRun(state, project);
  const interactionId = openInteraction(state, project, runId);

  const first = state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'answer-1',
    response: { choice: '保留', note: '已核对' },
  });
  assert.equal(first.kind, 'interaction');
  if (first.kind !== 'interaction') throw new Error('未保存回答');
  assert.equal(first.replayed, false);
  assert.equal(first.run.status, 'running');
  assert.equal(first.events.length, 2);

  const retry = state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'answer-1',
    response: { note: '已核对', choice: '保留' },
  });
  assert.equal(retry.kind, 'interaction');
  if (retry.kind !== 'interaction') throw new Error('未读取回答');
  assert.equal(retry.replayed, true);
  assert.deepEqual(retry.events, []);
  assert.equal(state.snapshot(project).lastEventSeq, 5);

  assert.throws(() => state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'answer-1',
    response: { choice: '修改' },
  }), (error) => error instanceof AuthoringStateError && error.code === 'IDEMPOTENCY_CONFLICT');

  assert.throws(() => state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'answer-2',
    response: { choice: '保留' },
  }), (error) => error instanceof AuthoringStateError && error.code === 'INTERACTION_CLOSED');
});

test('回答最后一个待办后清除等待用户回答的占位步骤', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const runId = startRun(state, project);
  const interactionId = openInteraction(state, project, runId, 'question_batch');
  assert.equal(state.snapshot(project).activeRuns[0]?.state.currentStep, '等待用户回答');

  const answered = state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'resume-after-answer',
    response: { answers: ['已确认'] },
  });

  assert.equal(answered.kind, 'interaction');
  if (answered.kind !== 'interaction') throw new Error('未保存回答');
  assert.equal(answered.run.status, 'running');
  assert.equal(answered.run.state.currentStep, undefined);
  assert.equal(answered.events.at(-1)?.payload.currentStep, undefined);
});

test('全部待办答完后任务才继续', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const runId = startRun(state, project);
  const firstId = openInteraction(state, project, runId, 'question_batch');
  const secondId = openInteraction(state, project, runId, 'artifact_review');

  const first = state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId: firstId,
    responseId: 'batch-answer',
    response: { answers: ['已确认'] },
  });
  assert.equal(first.kind, 'interaction');
  if (first.kind !== 'interaction') throw new Error('未保存回答');
  assert.equal(first.run.status, 'waiting');
  assert.equal(first.events.length, 1);

  const second = state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId: secondId,
    responseId: 'review-answer',
    response: { accepted: true },
  });
  assert.equal(second.kind, 'interaction');
  if (second.kind !== 'interaction') throw new Error('未保存回答');
  assert.equal(second.run.status, 'running');
  assert.equal(second.events.length, 2);
  assert.equal(state.snapshot(project).pendingInteractions.length, 0);
});

test('项目事件按顺序分页回放且互不串项', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const otherProject = projectId();
  const runId = startRun(state, project);
  startRun(state, otherProject);

  for (let step = 1; step <= 3; step += 1) {
    state.apply({
      kind: 'publish_event',
      projectId: project,
      runId,
      type: 'run.progress',
      payload: { step },
    });
  }

  const firstPage = state.replay(project, 1, 2);
  assert.deepEqual(firstPage.events.map((event) => event.seq), [2, 3]);
  assert.deepEqual(firstPage.events.map((event) => event.payload.step), [1, 2]);
  assert.equal(firstPage.nextSeq, 3);
  assert.equal(firstPage.hasMore, true);

  const secondPage = state.replay(project, firstPage.nextSeq, 2);
  assert.deepEqual(secondPage.events.map((event) => event.seq), [4]);
  assert.equal(secondPage.hasMore, false);
  assert.deepEqual(state.replay(otherProject).events.map((event) => event.seq), [1]);

  assert.throws(() => state.apply({
    kind: 'publish_event',
    projectId: project,
    type: 'authoring.run.updated',
    payload: {},
  }), (error) => error instanceof AuthoringStateError && error.code === 'INVALID_INPUT');
});

test('任务结束时关闭未回答的待办', () => {
  const state = new AuthoringState(getDb());
  const project = projectId();
  const runId = startRun(state, project);
  const interactionId = openInteraction(state, project, runId, 'finding_review');

  const failed = state.apply({
    kind: 'update_run',
    projectId: project,
    runId,
    status: 'failed',
    error: '资料读取失败',
  });
  assert.equal(failed.kind, 'run');
  if (failed.kind !== 'run') throw new Error('未更新任务');
  assert.deepEqual(failed.events.map((event) => event.type), [
    'authoring.run.updated',
    'authoring.interaction.cancelled',
  ]);
  assert.deepEqual(state.snapshot(project).activeRuns, []);
  assert.deepEqual(state.snapshot(project).pendingInteractions, []);

  assert.throws(() => state.apply({
    kind: 'respond_interaction',
    projectId: project,
    interactionId,
    responseId: 'late-answer',
    response: { accepted: true },
  }), (error) => error instanceof AuthoringStateError && error.code === 'INTERACTION_CLOSED');
  assert.throws(() => state.apply({
    kind: 'update_run',
    projectId: project,
    runId,
    status: 'completed',
  }), (error) => error instanceof AuthoringStateError && error.code === 'INVALID_TRANSITION');
  assert.throws(() => state.apply({
    kind: 'update_run',
    projectId: project,
    runId,
    status: 'failed',
    error: '另一个原因',
  }), (error) => error instanceof AuthoringStateError && error.code === 'INVALID_TRANSITION');
});

test('旧项目库可直接增加新表', () => {
  const directory = mkdtempSync(join(tmpdir(), 'modeling-authoring-legacy-'));
  const legacy = new DatabaseSync(join(directory, 'legacy.db'));
  legacy.exec(`
    PRAGMA foreign_keys = ON;
    CREATE TABLE projects (id TEXT PRIMARY KEY, owner TEXT NOT NULL);
    INSERT INTO projects (id, owner) VALUES ('old-project', 'test');
  `);
  const schema = readFileSync(new URL('../src/db/schema.sql', import.meta.url), 'utf8');
  legacy.exec(schema);

  const oldProject = legacy.prepare('SELECT id, owner FROM projects').get() as {
    id: string;
    owner: string;
  };
  assert.equal(oldProject.id, 'old-project');
  assert.equal(oldProject.owner, 'test');
  const tables = legacy.prepare(`SELECT name FROM sqlite_master
    WHERE type = 'table' AND name IN ('authoring_runs', 'authoring_interactions', 'project_events')
    ORDER BY name`).all() as Array<{ name: string }>;
  assert.deepEqual(tables.map((row) => row.name), [
    'authoring_interactions',
    'authoring_runs',
    'project_events',
  ]);
  legacy.close();
});
