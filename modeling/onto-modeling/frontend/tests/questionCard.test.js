import assert from 'node:assert/strict';
import test from 'node:test';
import { decisionView, initialDraft } from '../src/pages/workbench/questionAnswers.js';

// 决策卡（questions.jsx DecisionCard）显示哪种样子：回答过的只读回显、失效的留着草稿、提交后锁住。合成题目
const OPTIONS = [
  { id: '0.0', key: 'A', label: '方案一' }, { id: '0.1', key: 'B', label: '方案二' }, { id: '0.2', key: 'C', label: '方案三' },
  { id: '0.custom', key: 'D', label: '其他，我来说', custom: true },
];
const single = { question_id: 'q-1', options: OPTIONS };
const multi = { question_id: 'q-2', options: OPTIONS, multi: true };

test('回答过的多选卡重新挂载后勾着当时选的两项，其余不勾，全部只读、没有提交', () => {
  const view = decisionView(multi, { answer: { option_ids: ['0.0', '0.2'] } });
  assert.equal(view.mode, 'answered');
  assert.deepEqual(view.picks, ['0.0', '0.2']);
  assert.equal(view.locked, true);
  assert.equal(view.body, null);
});

test('回答过的单选卡勾实际选的那项，不默认第一项；只写了文字时勾「其他」并显示原文，不伪造正式选项', () => {
  assert.deepEqual(decisionView(single, { answer: { option_id: '0.1' } }).picks, ['0.1']);
  const text = decisionView(single, { answer: { option_id: '0.custom', custom_text: '方案二' } });
  assert.deepEqual(text.picks, ['0.custom']);
  assert.equal(text.custom, '方案二');
  assert.deepEqual(decisionView(single, { answer: { custom_text: '方案一' } }).picks, ['0.custom']);
});

test('新卡可作答：没选不能提交，选了得到答复体', () => {
  const open = decisionView(single, { draft: initialDraft(single) });
  assert.equal(open.mode, 'open');
  assert.equal(open.locked, false);
  assert.equal(open.body, null);
  const picked = decisionView(single, { draft: { picks: ['0.2'], custom: '', expired: false } });
  assert.deepEqual(picked.body, { option_id: '0.2', custom_text: undefined });
});

test('提交成功等回执期间锁住，不能再点', () => {
  const view = decisionView(single, { draft: { picks: ['0.0'], custom: '', expired: false }, sent: true });
  assert.equal(view.mode, 'sent');
  assert.equal(view.locked, true);
});

test('提交时问题已失效（410）：留着选的和写的，只读，说明没提交，不再给提交', () => {
  const draft = { picks: ['0.0', '0.custom'], custom: '合成补充', expired: true };
  const view = decisionView(multi, { draft });
  assert.equal(view.mode, 'expired');
  assert.deepEqual(view.picks, ['0.0', '0.custom']);
  assert.equal(view.custom, '合成补充');
  assert.equal(view.locked, true);
  assert.equal(view.body, null);
  assert.match(view.notice, /没有提交/);
});

test('收到失效事件（超时、拒绝）也留着草稿，按原因说明', () => {
  const view = decisionView(single, { draft: { picks: ['0.1'], custom: '', expired: false }, withdrawn: true, reason: '等待回答超时' });
  assert.equal(view.mode, 'expired');
  assert.deepEqual(view.picks, ['0.1']);
  assert.match(view.notice, /^等待回答超时：填写的内容没有提交/);
  assert.match(decisionView(single, { withdrawn: true }).notice, /^这个问题已失效/);
});

test('有回答记录时以回答为准，不看本地草稿', () => {
  const view = decisionView(single, { answer: { option_id: '0.2' }, draft: { picks: ['0.0'], custom: '', expired: true } });
  assert.equal(view.mode, 'answered');
  assert.deepEqual(view.picks, ['0.2']);
});
