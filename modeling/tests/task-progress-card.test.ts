import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import TaskProgressCard from '../frontend/src/TaskProgressCard.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');

test('进度卡展示当前步骤、真实比例、耗时、ETA 和后续步骤', () => {
  const html = renderToStaticMarkup(React.createElement(TaskProgressCard, {
    progress: {
      currentStep: '运行第 3/5 条评测用例',
      completedUnits: 6,
      totalUnits: 9,
      percent: 67,
      countLabel: '2/5 条用例',
      elapsedLabel: '3 分钟',
      estimatedLabel: '约 2 分钟',
      nextSteps: ['完成当前用例', '汇总评测结果'],
    },
  }));

  assert.match(html, /运行第 3\/5 条评测用例/);
  assert.match(html, /aria-valuenow="67"/);
  assert.match(html, /2\/5 条用例 · 已用 3 分钟/);
  assert.match(html, /预计剩余/);
  assert.match(html, /约 2 分钟/);
  assert.match(html, /完成当前用例/);
});

test('等待模型的进度卡使用不确定进度而不伪造百分比', () => {
  const html = renderToStaticMarkup(React.createElement(TaskProgressCard, {
    progress: {
      currentStep: '分析诊断证据并生成优化建议',
      indeterminate: true,
      elapsedLabel: '1 分钟',
      estimatedLabel: '约 2 分钟',
      nextSteps: ['保存并展示优化建议'],
    },
  }));

  assert.match(html, /generation-progress-bar indeterminate/);
  assert.doesNotMatch(html, /aria-valuenow/);
  assert.match(html, /处理中 · 已用 1 分钟/);
});
