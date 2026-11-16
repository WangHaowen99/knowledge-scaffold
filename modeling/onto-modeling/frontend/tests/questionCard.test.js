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

test('缺失历史答案时保留选项，仅在卡片级提示一次，不从原型属性取答案', () => {
  const html = render({ resolved: true, resolution: { behavior: 'allow' } });
  assert.equal([...html.matchAll(/aria-pressed="false"/g)].length, 2);
  assert.equal([...html.matchAll(/此历史记录未保存完整答案/g)].length, 1);
  assert.doesNotMatch(html, /class="question-answer"/);
  const special = render({
    req: { request_id: 'request_demo', tool_input: { questions: [{ question: '__proto__' }] } },
    resolved: true, resolution: { behavior: 'allow', answers: {} },
  });
  assert.match(special, /此历史记录未保存完整答案/);
});

test('取消、超时和失效分别展示，逐题不重复渲染无答案提示', () => {
  for (const [resolution, label] of [
    [{ behavior: 'deny' }, '已取消'],
    [{ behavior: 'deny', reason: 'timeout' }, '已超时'],
    [{ behavior: 'expired' }, '已失效'],
  ]) {
    const html = render({ resolved: true, resolution });
    assert.match(html, new RegExp(label));
    assert.doesNotMatch(html, /class="question-answer"|未提交答案|此历史记录/);
    assert.equal([...html.matchAll(/该问题已失效/g)].length, resolution.behavior === 'expired' ? 1 : 0);
  }
});

test('补发后的历史卡显示已补发和保存答案，不再提示失效', () => {
  const html = render({ resolved: true, resolution: { behavior: 'forwarded', answers: { 合成文字一: '合成补发答案' } } });
  assert.match(html, /已补发/);
  assert.match(html, /合成补发答案/);
  assert.doesNotMatch(html, /已失效|未保存完整答案/);
});

test('异常问题和选项提供重新提问按钮，已结束的异常卡不提供操作', () => {
  for (const malformed of [undefined, [], [null], [{ question: '合成问题', options: ['A', 'B'] }],
    [{ question: '合成重复' }, { question: '合成重复' }]]) {
    const req = { request_id: 'request_demo', tool_input: { questions: malformed } };
    const html = render({ req, onReask: async () => {} });
    assert.match(html, /让助手重新提问/);
    assert.doesNotMatch(html, /<textarea|type="submit"/);
    assert.doesNotMatch(render({ req, resolved: true }), /<button/);
  }
});

test('正常卡保留显式提交按钮和快捷键提示', () => {
  const html = render({ resolved: false });
  assert.match(html, /Ctrl\/⌘\+Enter/);
  assert.match(html, /aria-keyshortcuts="Control\+Enter Meta\+Enter"/);
  assert.match(html, /type="submit"/);
});

function interactiveHarness(onAnswer) {
  const slots = [];
  let cursor = 0;
  const useSlot = (initial) => {
    const index = cursor++;
    if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
    return [slots[index], (value) => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
  };
  const interactiveModule = { exports: {} };
  runInThisContext(`(function(module, exports, require) { ${compiled.code}\n})`)(
    interactiveModule, interactiveModule.exports,
    (specifier) => specifier === 'react' ? {
      useState: useSlot, useRef: (initial) => useSlot(() => ({ current: initial }))[0], useId: () => 'synthetic-card',
    } : specifier === '../lib/questionAnswers.js' ? questionAnswers : require(specifier),
  );
  const props = {
    req: { request_id: 'request_demo', tool_input: { questions: [
      { question: '合成单选', options: [{ label: '合成选项' }] },
      { question: '合成文字甲' }, { question: '合成文字乙' },
    ] } },
    onAnswer,
  };
  const draw = () => { cursor = 0; return interactiveModule.exports.default(props); };
  const find = (element, predicate) => {
    if (!element || typeof element !== 'object') return null;
    if (predicate(element)) return element;
    return [element.props?.children].flat(Infinity).map((child) => find(child, predicate)).find(Boolean);
  };
  const fill = () => {
    find(draw(), (node) => node.type === 'button' && node.props?.className?.startsWith('question-option')).props.onClick();
    for (const [index, value] of [[1, '合成回答甲'], [2, '合成回答乙']]) {
      find(draw(), (node) => node.props?.name === `answer-${index}`).props.onChange({ target: { value } });
    }
  };
  return { props, draw, find, fill };
}

for (const status of [410, 404]) {
  for (const expiredEventFirst of [false, true]) {
    test(`${status} 失效后保留两题文字和所选项，关闭提交；失效事件先到=${expiredEventFirst}`, async () => {
      let reject, attempts = 0;
      const h = interactiveHarness(async () => {
        attempts++;
        await new Promise((_resolve, fail) => { reject = fail; });
      });
      h.fill();
      const pending = h.draw().props.onSubmit({ preventDefault() {} });
      if (expiredEventFirst) {
        h.props.resolved = true;
        h.props.resolution = { behavior: 'expired' };
        assert.equal(h.find(h.draw(), (node) => node.props?.name === 'answer-1').props.value, '合成回答甲');
      }
      reject(Object.assign(new Error('合成问题失效'), { status }));
      await pending;
      const expired = h.draw();
      for (const [index, value] of [[1, '合成回答甲'], [2, '合成回答乙']]) {
        const input = h.find(expired, (node) => node.props?.name === `answer-${index}`);
        assert.equal(input.props.value, value);
        assert.equal(input.props.readOnly, true);
        assert.equal(input.props.disabled, false);
      }
      const choice = h.find(expired, (node) => node.type === 'button' && node.props?.className?.startsWith('question-option'));
      assert.equal(choice.props['aria-pressed'], true);
      assert.equal(choice.props.disabled, true);
      assert.ok(h.find(expired, (node) => node.props?.role === 'status' &&
        typeof node.props.children === 'string' && node.props.children.includes('该问题已失效，请让助手重新提问')));
      assert.equal(h.find(expired, (node) => node.props?.htmlType === 'submit'), undefined);
      await expired.props.onSubmit({ preventDefault() {} });
      assert.equal(attempts, 1);
    });
  }
}

test('主动收到失效事件也保留当前答案，不依赖提交失败才能保留', () => {
  const h = interactiveHarness(async () => assert.fail('失效卡不得提交'));
  h.fill();
  h.props.resolved = true;
  h.props.resolution = { behavior: 'expired' };
  const expired = h.draw();
  assert.equal(h.find(expired, (node) => node.props?.name === 'answer-1').props.value, '合成回答甲');
  assert.equal(h.find(expired, (node) => node.props?.name === 'answer-2').props.value, '合成回答乙');
  assert.equal(h.find(expired, (node) => node.props?.htmlType === 'submit'), undefined);
});

test('非失效错误保留可编辑答案并允许手动重试，成功后展示已保存答案', async () => {
  let attempts = 0;
  const h = interactiveHarness(async (_id, answers) => {
    attempts++;
    if (attempts === 1) throw Object.assign(new Error('合成 409 冲突'), { status: 409 });
    return { behavior: 'allow', answers };
  });
  h.fill();
  await h.draw().props.onSubmit({ preventDefault() {} });
  const retry = h.draw();
  const input = h.find(retry, (node) => node.props?.name === 'answer-1');
  assert.equal(input.props.value, '合成回答甲');
  assert.equal(input.props.disabled, false);
  assert.equal(input.props.readOnly, false);
  assert.ok(h.find(retry, (node) => node.props?.role === 'alert' && node.props.children === '合成 409 冲突'));
  await retry.props.onSubmit({ preventDefault() {} });
  const done = h.draw();
  assert.ok(h.find(done, (node) => node.props?.className === 'question-answer' && node.props.children === '合成回答甲'));
  assert.ok(h.find(done, (node) => node.props?.role === 'status' && node.props.children.includes('已答复')));
  assert.equal(attempts, 2);
});
