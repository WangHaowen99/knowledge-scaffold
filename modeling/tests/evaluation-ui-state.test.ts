import { createRequire } from 'node:module';
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  EvaluationRoundChips,
  mergeEvaluationRoundDetail,
  pickSelectedEvaluationRoundId,
  recoverStartedEvaluationRound,
  recoverStoppedEvaluationRound,
  resolveLoadedCollectionItems,
  summarizeEvaluationRoundView,
  loadEvaluationRoundsData,
  loadEvaluationRoundDetail,
  loadTestcasesData,
  pollEvaluationRound,
  upsertEvaluationRound,
} from '../frontend/src/M3Panels.jsx';
import { summarizeEvaluationCases } from '../frontend/src/evaluationCaseScope.js';
import { DataSourceSummary } from '../frontend/src/DataSourcePanel.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('空白正式用例会阻止前端进入正式评测并统计待修正数量', () => {
  const summary = summarizeEvaluationCases([
    { id: 'manual-blank', source: 'manual', question: '   ', groundTruth: '' },
    { id: 'manual-ready', source: 'manual', question: '真实问题', groundTruth: '答案' },
    { id: 'package-1', source: 'package', question: '建议问题', groundTruth: '' },
  ]);

  assert.equal(summary.invalidOfficialQuestionTotal, 1);
  assert.equal(summary.canRun, false);
  assert.equal(summary.officialTotal, 2);
});

test('评测用例首屏加载失败时返回空列表和错误，避免无限加载', async () => {
  const result = await loadTestcasesData('project-1', async () => {
    throw new Error('testcases unavailable');
  });

  assert.deepEqual(result, {
    testcases: [],
    error: 'testcases unavailable',
  });
});

test('刷新失败时保留上一版已加载数据，首屏失败时仍允许返回空列表', () => {
  assert.deepEqual(
    resolveLoadedCollectionItems([{ id: 'existing-case' }], [], 'temporary refresh failure', true),
    [{ id: 'existing-case' }],
  );
  assert.deepEqual(
    resolveLoadedCollectionItems([{ id: 'existing-case' }], [], 'initial load failure', false),
    [],
  );
});

test('评测轮次首屏加载失败时返回空列表和错误，避免无限加载', async () => {
  const result = await loadEvaluationRoundsData('project-1', async () => {
    throw new Error('rounds unavailable');
  });

  assert.deepEqual(result, {
    rounds: [],
    error: 'rounds unavailable',
  });
});

test('评测轮次详情加载失败时返回空详情和错误', async () => {
  const result = await loadEvaluationRoundDetail('project-1', 'round-1', async () => {
    throw new Error('round detail unavailable');
  });

  assert.deepEqual(result, {
    detail: null,
    error: 'round detail unavailable',
  });
});

test('评测轮询单次失败后仍要求继续重试', async () => {
  const result = await pollEvaluationRound('project-1', 'round-1', async () => {
    throw new Error('temporary network error');
  });

  assert.equal(result.detail, null);
  assert.equal(result.error, 'temporary network error');
  assert.equal(result.retry, true);
});

test('评测轮询成功后按轮次状态决定是否继续刷新', async () => {
  const running = await pollEvaluationRound('project-1', 'round-running', async () => ({
    round: { id: 'round-running', status: 'running' },
  }));
  const done = await pollEvaluationRound('project-1', 'round-done', async () => ({
    round: { id: 'round-done', status: 'done' },
  }));

  assert.equal(running.retry, true);
  assert.equal(running.error, '');
  assert.equal(running.detail?.round.id, 'round-running');

  assert.equal(done.retry, false);
  assert.equal(done.error, '');
  assert.equal(done.detail?.round.id, 'round-done');
});

test('评测运行态跟随真实活跃轮次，而不是当前查看的历史轮次', () => {
  const summary = summarizeEvaluationRoundView([
    { id: 'round-running', status: 'running', createdAt: '2026-08-24T10:00:00.000Z' },
    { id: 'round-old', status: 'done', createdAt: '2026-08-24T09:00:00.000Z' },
  ], 'round-old');

  assert.equal(summary.selectedRoundId, 'round-old');
  assert.equal(summary.activeRoundId, 'round-running');
  assert.equal(summary.running, true);
});

test('启动评测成功后即使轮次列表刷新失败，也能用 roundId 恢复运行态', async () => {
  const recovered = await recoverStartedEvaluationRound(
    'project-1',
    'round-running',
    [],
    async () => ({
      round: {
        id: 'round-running',
        status: 'running',
        packageVersion: 'v3',
        progressDone: 0,
        progressTotal: 4,
        passCount: 0,
        createdAt: '2026-08-24T10:00:00.000Z',
      },
      results: [],
    }),
  );
  const summary = summarizeEvaluationRoundView(recovered.rounds, 'round-running');

  assert.equal(recovered.detail?.round.id, 'round-running');
  assert.equal(summary.selectedRoundId, 'round-running');
  assert.equal(summary.activeRoundId, 'round-running');
  assert.equal(summary.running, true);
});

test('启动恢复会把新轮次合并回历史列表并保持倒序', () => {
  const merged = upsertEvaluationRound([
    { id: 'round-old', status: 'done', createdAt: '2026-08-24T09:00:00.000Z' },
    { id: 'round-older', status: 'failed', createdAt: '2026-08-24T08:00:00.000Z' },
  ], {
    id: 'round-running',
    status: 'running',
    createdAt: '2026-08-24T10:00:00.000Z',
  });

  assert.deepEqual(merged.map((item) => item.id), ['round-running', 'round-old', 'round-older']);
});

test('停止详情刷新失败时会用轮次摘要兜底终态，避免继续显示运行中', () => {
  const merged = mergeEvaluationRoundDetail(
    {
      round: {
        id: 'round-running',
        status: 'running',
        packageVersion: 'v3',
        progressDone: 1,
        progressTotal: 4,
        passCount: 0,
      },
      results: [{ id: 'result-1' }],
    },
    {
      id: 'round-running',
      status: 'failed',
      packageVersion: 'v3',
      progressDone: 4,
      progressTotal: 4,
      passCount: 2,
    },
  );

  assert.equal(merged.round.status, 'failed');
  assert.equal(merged.round.progressDone, 4);
  assert.equal(merged.results.length, 1);
});

test('停止当前查看的活跃轮次后会刷新详情为终态', async () => {
  const recovered = await recoverStoppedEvaluationRound(
    'project-1',
    'round-running',
    'round-running',
    [{ id: 'round-running', status: 'running', createdAt: '2026-08-24T10:00:00.000Z' }],
    async () => ({
      round: {
        id: 'round-running',
        status: 'failed',
        packageVersion: 'v3',
        progressDone: 4,
        progressTotal: 4,
        passCount: 2,
        createdAt: '2026-08-24T10:00:00.000Z',
      },
      results: [],
    }),
  );
  const summary = summarizeEvaluationRoundView(recovered.rounds, 'round-running');

  assert.equal(recovered.refreshed, true);
  assert.equal(recovered.detail?.round.status, 'failed');
  assert.equal(summary.selectedRoundId, 'round-running');
  assert.equal(summary.activeRoundId, null);
  assert.equal(summary.running, false);
});

test('停止活跃轮次时如果用户正在看历史轮次，不会强行切换当前详情', async () => {
  const recovered = await recoverStoppedEvaluationRound(
    'project-1',
    'round-running',
    'round-history',
    [
      { id: 'round-running', status: 'running', createdAt: '2026-08-24T10:00:00.000Z' },
      { id: 'round-history', status: 'done', createdAt: '2026-08-24T09:00:00.000Z' },
    ],
    async () => {
      throw new Error('should not fetch stopped round detail');
    },
  );

  assert.equal(recovered.refreshed, false);
  assert.equal(recovered.detail, null);
  assert.deepEqual(recovered.rounds.map((item) => item.id), ['round-running', 'round-history']);
});

test('停止成功后即使轮次刷新失败，历史视图下也会先清掉 running 摘要', () => {
  const nextRounds = upsertEvaluationRound([
    { id: 'round-running', status: 'running', createdAt: '2026-08-24T10:00:00.000Z' },
    { id: 'round-history', status: 'done', createdAt: '2026-08-24T09:00:00.000Z' },
  ], {
    id: 'round-running',
    status: 'failed',
    createdAt: '2026-08-24T10:00:00.000Z',
  });
  const summary = summarizeEvaluationRoundView(nextRounds, 'round-history');

  assert.equal(summary.selectedRoundId, 'round-history');
  assert.equal(summary.activeRoundId, null);
  assert.equal(summary.running, false);
});

test('轮次切换按钮在忙碌态会禁用，避免停止过程切换视图', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationRoundChips, {
    rounds: [
      { id: 'round-running', status: 'running', packageVersion: 'v3', createdAt: '2026-08-24T10:00:00.000Z', progressTotal: 4, passCount: 0 },
      { id: 'round-history', status: 'done', packageVersion: 'v2', createdAt: '2026-08-24T09:00:00.000Z', progressTotal: 4, passCount: 4 },
    ],
    selectedRoundId: 'round-running',
    busy: true,
    onSelect: () => {},
  }));

  assert.match(html, /disabled=""/);
});

test('数据源摘要在请求失败时展示错误，而不是误判成未配置', () => {
  const html = renderToStaticMarkup(React.createElement(DataSourceSummary, {
    dataSource: null,
    error: '数据源请求失败',
  }));

  assert.match(html, /数据源请求失败/);
  assert.doesNotMatch(html, /尚未保存数据源配置/);
});

test('当前查看轮次不存在时回退到最新轮次', () => {
  assert.equal(pickSelectedEvaluationRoundId([
    { id: 'round-latest', status: 'done' },
    { id: 'round-history', status: 'failed' },
  ], 'round-missing'), 'round-latest');
  assert.equal(pickSelectedEvaluationRoundId([], 'round-missing'), null);
});
