import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';

import EvaluationResultCard, {
  EvaluationResultsList,
  shouldExpandEvaluationResult,
} from '../frontend/src/EvaluationResultCard.jsx';

const frontendRequire = createRequire(new URL('../frontend/package.json', import.meta.url));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const stylesSource = readFileSync(new URL('../frontend/src/styles.css', import.meta.url), 'utf8');
const cardSource = readFileSync(new URL('../frontend/src/EvaluationResultCard.jsx', import.meta.url), 'utf8');

const result = {
  id: 'result-1',
  verdict: 'fail',
  question: '请说明本月生产计划的关键约束、计算过程与最终结论。',
  groundTruth: '标准答案正文',
  finalAnswer: '评测回答正文',
  reason: '关键约束遗漏，因此判定失败。',
  overridden: false,
};

test('失败结果默认展开，通过结果默认折叠', () => {
  assert.equal(shouldExpandEvaluationResult('fail'), true);
  assert.equal(shouldExpandEvaluationResult('partial'), true);
  assert.equal(shouldExpandEvaluationResult('error'), true);
  assert.equal(shouldExpandEvaluationResult('pass'), false);
  assert.equal(shouldExpandEvaluationResult(undefined), false);
});

test('评测结果使用可访问的折叠按钮并分开展示答案、判分和工具轨迹', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationResultCard, {
    result,
    trace: {
      status: 'complete',
      toolCalls: ['business_data_search', 'execute_script'],
    },
    open: true,
    onToggle: () => {},
    onOverride: () => {},
  }));

  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /ev-question-summary/);
  assert.match(html, /aria-label="标准答案"/);
  assert.match(html, /aria-label="评测回答"/);
  assert.match(html, /aria-label="判分说明"/);
  assert.match(html, /标准答案/);
  assert.match(html, /评测回答/);
  assert.match(html, /判分说明/);
  assert.match(html, /工具执行轨迹/);
  assert.match(html, /业务数据查询/);
  assert.match(html, /脚本执行/);
  assert.doesNotMatch(html, /思维链/);
});

test('结果列表默认展开异常项并提供轮次级展开和收起操作', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationResultsList, {
    results: [
      result,
      { ...result, id: 'result-2', verdict: 'pass', question: '通过的问题' },
    ],
    traces: {},
    onOverride: () => {},
  }));

  assert.match(html, /展开全部/);
  assert.match(html, /收起全部/);
  assert.equal((html.match(/aria-expanded="true"/g) ?? []).length, 1);
  assert.equal((html.match(/aria-expanded="false"/g) ?? []).length, 1);
});

test('不可用轨迹只展示安全提示，不回显内部错误详情', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationResultCard, {
    result,
    trace: {
      status: 'unavailable',
      error: 'http://internal-service/trace?token=secret',
      toolCalls: [],
    },
    open: true,
    onToggle: () => {},
    onOverride: () => {},
  }));

  assert.match(html, /工具执行轨迹暂不可用/);
  assert.doesNotMatch(html, /internal-service|secret/);
});

test('评测答案正文保持正常字重并在窄屏切换为单列对照', () => {
  assert.match(stylesSource, /\.ev-question-summary\s*\{[^}]*font-weight:\s*400/s);
  assert.match(stylesSource, /\.ev-answer-content[^}]*font-weight:\s*400/s);
  assert.match(stylesSource, /\.ev-answer-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,/s);
  assert.match(stylesSource, /@media \(max-width:\s*900px\)[\s\S]*?\.ev-answer-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(stylesSource, /\.ev-answer-standard\s*\{[^}]*#f8fbff/s);
  assert.match(stylesSource, /\.ev-answer-evaluated\s*\{[^}]*#f8fcfa/s);
});

test('人工改判要求理由并展示完整审计信息', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationResultCard, {
    result: {
      ...result, verdict: 'pass', overridden: true, originalVerdict: 'fail',
      overrideReason: '标准答案允许同义表达', overrideActor: 'alice',
      overriddenAt: '2026-07-24T08:00:00.000Z',
    },
    open: true, onToggle: () => {}, onOverride: () => {},
  }));
  assert.match(html, /人工改判记录/);
  assert.match(html, /原判定：失败/);
  assert.match(html, /标准答案允许同义表达/);
  assert.match(html, /alice/);
  assert.match(cardSource, /说明改判理由/);
  assert.match(cardSource, /overrideReason\.trim\(\)/);
});

test('运行错误不展示人工改判操作', () => {
  const html = renderToStaticMarkup(React.createElement(EvaluationResultCard, {
    result: { ...result, verdict: 'error' }, open: true, onToggle: () => {}, onOverride: () => {},
  }));
  assert.match(html, /运行错误需要先修复环境并重试/);
  assert.doesNotMatch(html, />确认改判</);
});
