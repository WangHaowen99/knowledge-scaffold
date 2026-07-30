import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-cancellation-'));

const {
  cancelEvaluation,
  cancelEvaluationsForProject,
  startEvaluation,
} = await import('../src/evaluation/engine.js');
const { saveProjectDataSource } = await import('../src/evaluation/data-source-repo.js');
const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');
const { graphPackageSpec } = await import('./helpers/graph-import-fixtures.js');

function prepareProject(name: string) {
  const project = createProject({ owner: 'alice', name });
  m3repo.addTestcase(project.id, '真实评测问题', '真实参考答案', 'manual');
  saveProjectDataSource({
    projectId: project.id,
    mode: 'none',
    workspaceId: `mw-shadow-${project.id}`,
  });
  return project;
}

function packageSnapshot(revisionOid: string) {
  return {
    version: graphPackageSpec.version,
    spec: graphPackageSpec,
    files: {},
    revisionOid,
  };
}

test('取消评测会等待后台任务完成中断清理后再返回', async () => {
  const project = prepareProject('评测取消生命周期');
  let notifyAbort = () => {};
  const abortObserved = new Promise<void>((resolve) => { notifyAbort = resolve; });
  let releaseCleanup = () => {};
  const cleanupGate = new Promise<void>((resolve) => { releaseCleanup = resolve; });
  let notifyStarted = () => {};
  const executionStarted = new Promise<void>((resolve) => { notifyStarted = resolve; });
  let cleanupFinished = false;
  const started = startEvaluation({
    projectId: project.id,
    owner: 'alice',
    token: 'access-token',
    packageSnapshot: packageSnapshot('revision-cancel'),
  }, {
    runRound: async ({ signal }) => new Promise<void>((resolve) => {
      notifyStarted();
      signal.addEventListener('abort', async () => {
        notifyAbort();
        await cleanupGate;
        cleanupFinished = true;
        resolve();
      }, { once: true });
    }),
  });

  await executionStarted;
  let cancellationFinished = false;
  const cancellation = cancelEvaluation(project.id, started.roundId)
    .finally(() => { cancellationFinished = true; });
  await abortObserved;
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(cancellationFinished, false);
  assert.equal(cleanupFinished, false);

  releaseCleanup();
  const round = await cancellation;
  assert.equal(cleanupFinished, true);
  assert.equal(round.status, 'failed');
  assert.match(round.log, /停止本轮/);
});

test('删除项目前会等待评测的外部写入链路真正结束', async () => {
  const project = prepareProject('项目删除评测生命周期');
  let notifyAbort = () => {};
  const abortObserved = new Promise<void>((resolve) => { notifyAbort = resolve; });
  let releaseCleanup = () => {};
  const cleanupGate = new Promise<void>((resolve) => { releaseCleanup = resolve; });
  let notifyStarted = () => {};
  const executionStarted = new Promise<void>((resolve) => { notifyStarted = resolve; });
  let cleanupFinished = false;
  const started = startEvaluation({
    projectId: project.id,
    owner: 'alice',
    token: 'access-token',
    packageSnapshot: packageSnapshot('revision-project-delete'),
  }, {
    runRound: async ({ signal }) => new Promise<void>((resolve) => {
      notifyStarted();
      signal.addEventListener('abort', async () => {
        notifyAbort();
        await cleanupGate;
        cleanupFinished = true;
        resolve();
      }, { once: true });
    }),
  });

  await executionStarted;
  let cancellationFinished = false;
  const cancellation = cancelEvaluationsForProject(project.id)
    .finally(() => { cancellationFinished = true; });
  await abortObserved;
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(cancellationFinished, false);

  releaseCleanup();
  await cancellation;
  assert.equal(cleanupFinished, true);
  const round = m3repo.getRound(started.roundId)!;
  assert.equal(round.status, 'failed');
  assert.match(round.log, /项目删除/);
});

test('评测尚未进入后台执行时取消不会再启动外部写入', async () => {
  const project = prepareProject('评测排队取消');
  let executionCalls = 0;
  const started = startEvaluation({
    projectId: project.id,
    owner: 'alice',
    token: 'access-token',
    packageSnapshot: packageSnapshot('revision-cancel-before-start'),
  }, {
    runRound: async () => { executionCalls += 1; },
  });

  const round = await cancelEvaluation(project.id, started.roundId);
  assert.equal(executionCalls, 0);
  assert.equal(round.status, 'failed');
});
