import assert from 'node:assert/strict';
import test from 'node:test';
import {
  answerPicks, decisionAnswer, draftFilled, hasStructuredAnswer, initialDraft,
} from '../src/pages/workbench/questionAnswers.js';

// 合成决策卡：两个正式选项加「其他，我来说」（契约 4.6 Question）
const OPTIONS = [{ id: '0.0', key: 'A', label: '甲,乙' }, { id: '0.1', key: 'B', label: '方案二' }, { id: '0.custom', key: 'C', label: '其他，我来说', custom: true }];
const single = { question_id: 'q-1', options: OPTIONS };
const multi = { question_id: 'q-2', options: OPTIONS, multi: true };

test('决策卡答复：单选传 option_id，多选题传 option_ids（原来多选题也只能选一项）', () => {
  assert.deepEqual(decisionAnswer(single, ['0.1'], ''), { option_id: '0.1', custom_text: undefined });
  assert.deepEqual(decisionAnswer(multi, ['0.0', '0.1'], ''), { option_ids: ['0.0', '0.1'], custom_text: undefined });
  assert.equal(decisionAnswer(multi, [], ''), null);
});

test('决策卡答复：选了「其他，我来说」要填文字才能提交', () => {
  assert.equal(decisionAnswer(multi, ['0.0', '0.custom'], '  '), null);
  assert.deepEqual(decisionAnswer(multi, ['0.0', '0.custom'], ' 先补字段 '), { option_ids: ['0.0', '0.custom'], custom_text: '先补字段' });
  assert.deepEqual(decisionAnswer(single, ['0.custom'], '自己说'), { option_id: '0.custom', custom_text: '自己说' });
});

test('还原勾选只认选项 ID：带逗号的选项保持一项，未知选项丢掉，只写文字时勾「其他」', () => {
  assert.deepEqual(answerPicks(multi, { option_ids: ['0.0', '0.1'] }), ['0.0', '0.1']);
  assert.deepEqual(answerPicks(single, { option_id: '0.1' }), ['0.1']);
  assert.deepEqual(answerPicks(single, { option_id: '9.9' }), []);
  assert.deepEqual(answerPicks(single, { custom_text: '方案二' }), ['0.custom']);
  assert.deepEqual(answerPicks(single, {}), []);
  assert.deepEqual(answerPicks(single, null), []);
});

test('结构化答案：选项或文字有一样才算，旧记录的空答案不算', () => {
  assert.equal(hasStructuredAnswer({ option_id: '0.0' }), true);
  assert.equal(hasStructuredAnswer({ option_ids: ['0.0'] }), true);
  assert.equal(hasStructuredAnswer({ custom_text: '合成回答' }), true);
  assert.equal(hasStructuredAnswer({}), false);
  assert.equal(hasStructuredAnswer({ option_ids: [] }), false);
  assert.equal(hasStructuredAnswer(null), false);
});

test('文字题（只剩「其他，我来说」）直接勾上，草稿没写字不算填过', () => {
  const text = { question_id: 'q-3', options: [OPTIONS[2]] };
  const draft = initialDraft(text);
  assert.deepEqual(draft, { picks: ['0.custom'], custom: '', expired: false });
  assert.equal(draftFilled(text, draft), false);
  assert.equal(draftFilled(text, { ...draft, custom: '合成回答' }), true);
  assert.deepEqual(initialDraft(single).picks, []);
  assert.equal(draftFilled(single, { picks: ['0.1'], custom: '', expired: false }), true);
  assert.equal(draftFilled(single, null), false);
});
