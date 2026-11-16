import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { runInThisContext } from 'node:vm';
import { test } from 'node:test';
import { transform } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import * as questionAnswers from '../src/lib/questionAnswers.js';

const source = await readFile(new URL('../src/components/QuestionCard.jsx', import.meta.url), 'utf8');
const compiled = await transform(source, { loader: 'jsx', format: 'cjs', jsx: 'automatic' });
const require = createRequire(import.meta.url);
const componentModule = { exports: {} };
runInThisContext(`(function(module, exports, require) { ${compiled.code}\n})`)(
  componentModule, componentModule.exports,
  (specifier) => specifier === '../lib/questionAnswers.js' ? questionAnswers : require(specifier),
);
const QuestionCard = componentModule.exports.default;

// 合成题目与回答，仅验证当前卡片展示，不推断历史选中状态。
const questions = [
  { question: '合成单选', options: [{ label: '方案一' }, { label: '方案二' }] },
  { question: '合成文字一', options: [{ label: '占位示例' }] },
  { question: '合成文字二', options: [] },
];
const render = (props) => renderToStaticMarkup(createElement(QuestionCard, {
  req: { request_id: 'request_demo', tool_input: { questions } },
  onAnswer: async () => {}, ...props,
}));

test('已答复卡保留正式选项和只读状态，同时展示每题答案', () => {
  const html = render({
    resolved: true,
    resolution: { behavior: 'allow', answers: {
      合成单选: '方案二', 合成文字一: '人工合成回答一', 合成文字二: '人工合成回答二',
    } },
  });
  const options = [...html.matchAll(/<button\b[^>]*class="question-option[^>]*>/g)].map(([tag]) => tag);
  assert.equal(options.length, 2);
  assert.ok(options.every((tag) => tag.includes('disabled=""')));
  assert.match(html, /方案一/);
  assert.match(html, /方案二/);
  assert.match(html, /人工合成回答一/);
  assert.match(html, /人工合成回答二/);
  assert.doesNotMatch(html, /占位示例|<textarea|type="submit"/);
});

test('未答复卡保持选项及逐题文字输入，允许统一提交', () => {
  const html = render({ resolved: false });
  assert.equal([...html.matchAll(/<textarea\b/g)].length, 3);
  assert.equal([...html.matchAll(/aria-pressed="false"/g)].length, 2);
  assert.doesNotMatch(html, /disabled=""/);
  assert.match(html, /type="submit"/);
});

test('缺失历史答案时保留选项与缺失提示，不从原型属性取答案', () => {
  const html = render({ resolved: true, resolution: { behavior: 'allow' } });
  assert.equal([...html.matchAll(/aria-pressed="false"/g)].length, 2);
  assert.equal([...html.matchAll(/此历史记录未保存答案/g)].length, 3);
  const special = render({
    req: { request_id: 'request_demo', tool_input: { questions: [{ question: '__proto__' }] } },
    resolved: true, resolution: { behavior: 'allow', answers: {} },
  });
  assert.match(special, /此历史记录未保存答案/);
});
