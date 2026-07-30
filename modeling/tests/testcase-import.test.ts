import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { parseTestcaseImport } from '../frontend/src/testcase-import.js';
import { createRequire } from 'node:module';

const frontendRequire = createRequire(join(process.cwd(), 'frontend/package.json'));
const React = frontendRequire('react');
const { renderToStaticMarkup } = frontendRequire('react-dom/server');
const { TestcaseImportPreview } = await import('../frontend/src/M3Panels.jsx');

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'testcase-import-'));
const m3repo = await import('../src/workflow/m3-repo.js');
const { createProject } = await import('../src/projects/repo.js');

test('真实评测用例导入支持数组和 cases 包装并忽略额外元数据', () => {
  const wrapped = parseTestcaseImport(JSON.stringify({
    source: { exportedAt: '2026-07-24' },
    cases: [
      { question: '如何处理请求？', groundTruth: '先校验再执行', verdict: 'pass' },
      { question: '异常时怎么办？', ground_truth: '转人工复核' },
    ],
  }));
  const direct = parseTestcaseImport(JSON.stringify([
    { question: '如何收尾？', answer: '记录结果' },
  ]));

  assert.deepEqual(wrapped, [
    { question: '如何处理请求？', groundTruth: '先校验再执行' },
    { question: '异常时怎么办？', groundTruth: '转人工复核' },
  ]);
  assert.deepEqual(direct, [{ question: '如何收尾？', groundTruth: '记录结果' }]);
});

test('真实评测用例导入拒绝空问题、重复问题和超量文件', () => {
  assert.throws(() => parseTestcaseImport(JSON.stringify({ cases: [{ groundTruth: '无问题' }] })), /第 1 条缺少问题/);
  assert.throws(() => parseTestcaseImport(JSON.stringify({ cases: [
    { question: '同一个问题' }, { question: '  同一个问题  ' },
  ] })), /包含重复问题/);
  assert.throws(() => parseTestcaseImport(JSON.stringify({
    cases: Array.from({ length: 501 }, (_, index) => ({ question: `问题 ${index}` })),
  })), /最多导入 500 条/);
});

test('批量保存真实用例在一个事务中去重并报告结果', () => {
  const project = createProject({ owner: 'tester', name: '批量用例' });
  m3repo.addTestcase(project.id, '已有问题', '旧答案');

  const result = m3repo.addTestcases(project.id, [
    { question: '  已有问题  ', groundTruth: '不覆盖' },
    { question: '新增问题 A', groundTruth: '答案 A' },
    { question: '新增问题 B', groundTruth: '' },
  ]);

  assert.deepEqual({ added: result.added, skipped: result.skipped }, { added: 2, skipped: 1 });
  assert.deepEqual(m3repo.listTestcases(project.id).map((item) => [item.question, item.groundTruth]), [
    ['已有问题', '旧答案'],
    ['新增问题 A', '答案 A'],
    ['新增问题 B', ''],
  ]);
});

test('批量导入先展示问题数和参考答案覆盖，再由用户确认', () => {
  const html = renderToStaticMarkup(React.createElement(TestcaseImportPreview, {
    cases: [
      { question: '问题 A', groundTruth: '答案 A' },
      { question: '问题 B', groundTruth: '' },
    ],
    filename: '真实用例.json',
    onConfirm: () => undefined,
    onCancel: () => undefined,
  }));

  assert.match(html, /待导入 2 条真实用例/);
  assert.match(html, /1 条含参考答案/);
  assert.match(html, /确认导入/);
  assert.match(html, /取消/);
});
