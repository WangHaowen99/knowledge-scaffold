import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'evaluation-effective-results-'));

const repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');

function completedRound(
  projectId: string,
  revision: string,
  cases: Array<{ id: string; question: string; groundTruth: string; verdict: 'pass' | 'partial' | 'fail' }>,
) {
  const round = repo.createRound(projectId, '1.0.0-rc.1', cases.length, revision);
  for (const item of cases) {
    repo.addResult(round.id, {
      testcaseId: item.id,
      question: item.question,
      groundTruth: item.groundTruth,
      finalAnswer: item.verdict === 'pass' ? item.groundTruth : '不符合预期',
      verdict: item.verdict,
      reason: item.verdict === 'pass' ? '符合预期' : '定向重评发现回归',
    });
  }
  repo.roundProgress(round.id, cases.length, cases.filter((item) => item.verdict === 'pass').length);
  repo.finishRound(round.id, 'done');
  return round;
}

function prepare(name: string) {
  const project = createProject({ owner: 'alice', name });
  const first = repo.addTestcase(project.id, '问题一', '答案一', 'manual');
  const second = repo.addTestcase(project.id, '问题二', '答案二', 'manual');
  return { project, cases: [first, second] };
}

test('完整评测的全部结果构成当前修订的有效结果视图', () => {
  const { project, cases } = prepare('完整评测结果视图');
  const round = completedRound(project.id, 'revision-a', cases.map((item) => ({ ...item, verdict: 'pass' })));

  const effective = repo.latestEffectiveEvaluation(project.id, 'revision-a', cases.map((item) => item.id));

  assert.equal(effective.latestRound?.id, round.id);
  assert.deepEqual(new Set(effective.results.map((item) => item.testcaseId)), new Set(cases.map((item) => item.id)));
  assert.ok(effective.results.every((item) => item.verdict === 'pass'));
});

test('定向重评覆盖同一用例并继承同修订下未重跑用例', () => {
  const { project, cases } = prepare('定向评测覆盖结果视图');
  const full = completedRound(project.id, 'revision-b', cases.map((item) => ({ ...item, verdict: 'pass' })));
  const targeted = completedRound(project.id, 'revision-b', [{ ...cases[0], verdict: 'fail' }]);

  const effective = repo.latestEffectiveEvaluation(project.id, 'revision-b', cases.map((item) => item.id));
  const byCase = new Map(effective.results.map((item) => [item.testcaseId, item]));

  assert.equal(effective.latestRound?.id, targeted.id);
  assert.equal(byCase.get(cases[0].id)?.roundId, targeted.id);
  assert.equal(byCase.get(cases[0].id)?.verdict, 'fail');
  assert.equal(byCase.get(cases[1].id)?.roundId, full.id);
  assert.equal(byCase.get(cases[1].id)?.verdict, 'pass');
});

test('知识包修订变化后不继承旧修订评测结果', () => {
  const { project, cases } = prepare('修订变化结果隔离');
  const oldRound = completedRound(project.id, 'revision-old', cases.map((item) => ({ ...item, verdict: 'pass' })));

  const effective = repo.latestEffectiveEvaluation(project.id, 'revision-new', cases.map((item) => item.id));

  assert.ok(oldRound.id);
  assert.equal(effective.latestRound, null);
  assert.deepEqual(effective.results, []);
});

test('失败或未完成的定向轮次不覆盖同修订下已完成的有效结果', () => {
  const { project, cases } = prepare('失败轮次不覆盖结果视图');
  const full = completedRound(project.id, 'revision-c', cases.map((item) => ({ ...item, verdict: 'pass' })));
  for (const status of ['failed', 'running'] as const) {
    const interrupted = repo.createRound(project.id, '1.0.0-rc.1', 1, 'revision-c');
    repo.addResult(interrupted.id, {
      testcaseId: cases[0].id,
      question: cases[0].question,
      groundTruth: cases[0].groundTruth,
      finalAnswer: '未完成的错误回答',
      verdict: 'fail',
      reason: '本轮没有完成',
    });
    if (status === 'failed') repo.finishRound(interrupted.id, status);
  }

  const effective = repo.latestEffectiveEvaluation(project.id, 'revision-c', cases.map((item) => item.id));

  assert.equal(effective.latestRound?.id, full.id);
  assert.equal(effective.results.length, 2);
  assert.ok(effective.results.every((item) => item.verdict === 'pass'));
});

test('融合来源回归有独立归属且不覆盖正式发布结果', () => {
  const { project, cases } = prepare('来源回归归属');
  const current = completedRound(project.id, 'revision-current', cases.map((item) => ({
    ...item, verdict: 'pass',
  })));
  const source = repo.createRound(project.id, '0.9.0', 1, 'fusion-source:fingerprint-a', {
    kind: 'fusion_source',
    sourceId: 'source-a',
    sourceName: '来源 A',
    fingerprint: 'fingerprint-a',
  });
  repo.addResult(source.id, {
    testcaseId: cases[0].id,
    question: cases[0].question,
    groundTruth: cases[0].groundTruth,
    finalAnswer: '来源回归失败',
    verdict: 'fail',
    reason: '仅属于来源 A',
  });
  repo.finishRound(source.id, 'done');

  assert.deepEqual(repo.getRound(source.id)?.scope, {
    kind: 'fusion_source',
    sourceId: 'source-a',
    sourceName: '来源 A',
    fingerprint: 'fingerprint-a',
  });
  const effective = repo.latestEffectiveEvaluation(
    project.id,
    'revision-current',
    cases.map((item) => item.id),
  );
  assert.equal(effective.latestRound?.id, current.id);
  assert.equal(effective.results.length, 2);
  assert.ok(effective.results.every((item) => item.verdict === 'pass'));
});
