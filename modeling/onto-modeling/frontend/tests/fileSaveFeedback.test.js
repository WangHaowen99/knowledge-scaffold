import assert from 'node:assert/strict';
import { test } from 'node:test';
import { showFileSaveFeedback } from '../src/lib/fileSaveFeedback.js';

test('保存告警包含位置且不会显示无告警成功提示', () => {
  const calls = [];
  showFileSaveFeedback({ warnings: [{ path: 'ontology/scripts/script_demo.py', line: 8, message: '存在 TODO 未完成标记' }] }, {
    success: () => assert.fail('warning must remain visible'),
    warning: (value) => calls.push(value),
  });
  assert.match(calls[0].content, /已保存.*script_demo.py 第 8 行.*TODO/);
  assert.equal(calls[0].duration, 10);
});

test('兼容旧返回且限制单次展示的告警数量', () => {
  let feedback;
  const messages = { success: (text) => { feedback = text; }, warning: (value) => { feedback = value.content; } };
  showFileSaveFeedback({}, messages);
  assert.equal(feedback, '已保存并记录迭代');
  showFileSaveFeedback({ warnings: Array.from({ length: 8 }, (_, i) => ({ path: 'script_demo.py', line: i + 1, message: 'TODO' })) }, messages);
  assert.match(feedback, /另有 3 条告警/);
  assert.doesNotMatch(feedback, /第 6 行/);
});
