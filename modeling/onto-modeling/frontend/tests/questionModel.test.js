import assert from 'node:assert/strict';
import test from 'node:test';
import { decisionAnswer } from '../src/pages/workbench/questionModel.js';

const OPTIONS = [{ id: '0.0', key: 'A' }, { id: '0.1', key: 'B' }, { id: '0.c', key: 'C', custom: true }];

test('决策卡答复：单选传 option_id，多选题传 option_ids（原来多选题也只能选一项）', () => {
  assert.deepEqual(decisionAnswer({ options: OPTIONS }, ['0.1'], ''), { option_id: '0.1', custom_text: undefined });
  assert.deepEqual(decisionAnswer({ options: OPTIONS, multi: true }, ['0.0', '0.1'], ''),
    { option_ids: ['0.0', '0.1'], custom_text: undefined });
  assert.equal(decisionAnswer({ options: OPTIONS, multi: true }, [], ''), null);
});

test('决策卡答复：选了「其他，我来说」要填文字才能提交', () => {
  assert.equal(decisionAnswer({ options: OPTIONS, multi: true }, ['0.0', '0.c'], '  '), null);
  assert.deepEqual(decisionAnswer({ options: OPTIONS, multi: true }, ['0.0', '0.c'], ' 先补字段 '),
    { option_ids: ['0.0', '0.c'], custom_text: '先补字段' });
  assert.deepEqual(decisionAnswer({ options: OPTIONS }, ['0.c'], '自己说'), { option_id: '0.c', custom_text: '自己说' });
});
