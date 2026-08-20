import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-pi-authoring-'));

const [{ createProject }, { getDb }, authoring] = await Promise.all([
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/authoring/index.js'),
]);

const {
  AuthoringState,
  AuthoringStateError,
  PiAuthoringAdapter,
} = authoring;

function setup() {
  const project = createProject({ owner: 'test', name: `Pi 任务-${Math.random()}` });
  const state = new AuthoringState(getDb());
  const adapter = new PiAuthoringAdapter(state);
  return { projectId: project.id, state, adapter };
}

test('Pi 任务可以启动、更新进度并完成', () => {
  const { projectId, state, adapter } = setup();
  const started = adapter.start(projectId, '提取业务对象', 'R3', { step: '准备' });

  assert.equal(started.kind, 'run');
  assert.equal(started.run.status, 'running');
  assert.equal(started.run.baseRevision, 'R3');
  assert.equal(started.run.state.step, '准备');
  assert.equal(started.run.state.jobId, started.run.id);
  assert.deepEqual(started.events.map((event) => event.type), ['authoring.run.started']);

  const progressed = adapter.progress(
    projectId,
    started.run.id,
    { step: '提取对象', current: 2, total: 5 },
    { type: 'run.progress', payload: { message: '已处理 2 批' } },
  );
  assert.equal(progressed.run.status, 'running');
  assert.equal(progressed.run.state.step, '提取对象');
  assert.equal(progressed.run.state.current, 2);
  assert.equal(progressed.run.state.total, 5);
  assert.deepEqual(progressed.events.map((event) => event.type), [
    'authoring.run.updated',
    'run.progress',
  ]);

  const completed = adapter.complete(projectId, started.run.id, {
    objectCount: 12,
    revision: 'R4',
  });
  assert.equal(completed.run.status, 'completed');
  assert.deepEqual(completed.run.result, { objectCount: 12, revision: 'R4' });
  assert.ok(completed.run.finishedAt);
  assert.deepEqual(state.snapshot(projectId).activeRuns, []);
});

test('待办会暂停任务，全部回答后恢复', () => {
  const { projectId, state, adapter } = setup();
  const run = adapter.start(projectId, '确认产品边界').run;
  const first = adapter.request(
    projectId,
    run.id,
    'question_batch',
    { title: '请确认产品别名', questions: ['资金池是否为同一产品？'] },
    'R4',
  );
  const second = adapter.request(
    projectId,
    run.id,
    'artifact_review',
    { title: '请审阅对象', keys: ['Product'] },
  );

  assert.equal(first.run.status, 'waiting');
  assert.equal(second.run.status, 'waiting');
  assert.deepEqual(first.events.map((event) => event.type), [
    'authoring.run.updated',
    'authoring.interaction.opened',
  ]);
  assert.deepEqual(second.events.map((event) => event.type), ['authoring.interaction.opened']);

  const firstAnswer = adapter.respond(
    projectId,
    first.interaction.id,
    'answer-1',
    { accepted: true },
  );
  assert.equal(firstAnswer.run.status, 'waiting');
  assert.deepEqual(firstAnswer.events.map((event) => event.type), [
    'authoring.interaction.answered',
  ]);

  const secondAnswer = adapter.respond(
    projectId,
    second.interaction.id,
    'answer-2',
    { accepted: true, note: '已核对' },
  );
  assert.equal(secondAnswer.run.status, 'running');
  assert.deepEqual(secondAnswer.events.map((event) => event.type), [
    'authoring.interaction.answered',
    'authoring.run.updated',
  ]);
  assert.deepEqual(state.snapshot(projectId).pendingInteractions, []);

  const replayed = adapter.respond(
    projectId,
    second.interaction.id,
    'answer-2',
    { note: '已核对', accepted: true },
  );
  assert.equal(replayed.replayed, true);
  assert.deepEqual(replayed.events, []);
});

test('waiting 任务的后台心跳不会误恢复成 running', () => {
  const { projectId, state, adapter } = setup();
  const run = adapter.start(projectId, '等待专家确认').run;
  const pending = adapter.request(projectId, run.id, 'decision', { title: '请确认' });
  const heartbeat = adapter.heartbeat(projectId, run.id, {});

  assert.equal(pending.run.status, 'waiting');
  assert.equal(heartbeat.run.status, 'waiting');
  assert.deepEqual(heartbeat.events, []);
  assert.equal(state.snapshot(projectId).activeRuns[0]?.status, 'waiting');
});

test('progress 会先恢复 waiting 任务，再写入新进度', () => {
  const { projectId, state, adapter } = setup();
  const run = adapter.start(projectId, '生成知识包', undefined, { step: '待确认' }).run;
  state.apply({
    kind: 'update_run',
    projectId,
    runId: run.id,
    status: 'waiting',
  });

  const progressed = adapter.progress(projectId, run.id, { step: '生成文件' });

  assert.equal(progressed.run.status, 'running');
  assert.equal(progressed.run.state.step, '生成文件');
  assert.deepEqual(progressed.events.map((event) => event.type), [
    'authoring.run.updated',
    'authoring.run.updated',
  ]);
  const events = state.replay(projectId).events;
  assert.deepEqual(events.slice(-2).map((event) => event.payload.status), ['running', 'running']);
});

test('Pi 任务可以保留进度后失败', () => {
  const { projectId, state, adapter } = setup();
  const run = adapter.start(projectId, '检查知识包').run;

  const failed = adapter.fail(
    projectId,
    run.id,
    '产品来源缺失',
    { step: '语义检查', checked: 8 },
  );

  assert.equal(failed.run.status, 'failed');
  assert.equal(failed.run.error, '产品来源缺失');
  assert.equal(failed.run.state.step, '语义检查');
  assert.equal(failed.run.state.checked, 8);
  assert.ok(failed.run.finishedAt);
  assert.deepEqual(state.snapshot(projectId).activeRuns, []);
});

test('适配器不会绕过已结束任务的状态限制', () => {
  const { projectId, adapter } = setup();
  const run = adapter.start(projectId, '生成测试数据').run;
  adapter.complete(projectId, run.id);

  assert.throws(
    () => adapter.progress(projectId, run.id, { step: '重新生成' }),
    (error) => error instanceof AuthoringStateError
      && error.code === 'INVALID_TRANSITION'
      && error.message === '任务状态不能这样更改',
  );
  assert.throws(
    () => adapter.request(projectId, run.id, 'decision', { title: '再次确认' }),
    (error) => error instanceof AuthoringStateError
      && error.code === 'INVALID_TRANSITION',
  );
  assert.throws(
    () => adapter.fail(projectId, run.id, '不应覆盖完成状态'),
    (error) => error instanceof AuthoringStateError
      && error.code === 'INVALID_TRANSITION',
  );
});

test('适配器产生的任务、待办和进度事件可按顺序回放', () => {
  const { projectId, state, adapter } = setup();
  const run = adapter.start(projectId, '评审产品模型').run;
  const pending = adapter.request(
    projectId,
    run.id,
    'decision',
    { title: '选择处理方式', options: ['保留', '修改'] },
  );
  adapter.respond(projectId, pending.interaction.id, 'decision-1', { choice: '保留' });
  adapter.progress(
    projectId,
    run.id,
    { step: '复审' },
    { type: 'run.progress', payload: { percent: 80 } },
  );
  adapter.complete(projectId, run.id, { revision: 'R8' });

  const replay = state.replay(projectId, 0, 20);
  assert.deepEqual(replay.events.map((event) => event.type), [
    'authoring.run.started',
    'authoring.run.updated',
    'authoring.interaction.opened',
    'authoring.interaction.answered',
    'authoring.run.updated',
    'authoring.run.updated',
    'run.progress',
    'authoring.run.updated',
  ]);
  assert.deepEqual(replay.events.map((event) => event.seq), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.equal(replay.events.every((event) => event.runId === run.id), true);
  assert.equal(replay.nextSeq, 8);
  assert.equal(replay.hasMore, false);
});
