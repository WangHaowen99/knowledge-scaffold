import assert from 'node:assert/strict';
import { test } from 'node:test';

import { detectNameConflict } from '../src/projects/name-conflict.js';

test('名称未被占用时不产生冲突', () => {
  assert.equal(detectNameConflict(['示例项目'], '示例项目（2）', '示例项目（2）'), null);
});

test('重名判定忽略大小写、首尾空白与全角半角差异', () => {
  assert.ok(detectNameConflict(['Shared Project'], '  ｓｈａｒｅｄ　ｐｒｏｊｅｃｔ  ', 'Shared Project'));
});

test('候选名称基于原始名称顺延并跳过已占用名称', () => {
  const conflict = detectNameConflict(
    ['示例项目', '示例项目（2）', '示例项目（4）'],
    '示例项目',
    '示例项目',
  );
  assert.deepEqual(conflict, {
    originalName: '示例项目',
    attemptedName: '示例项目',
    candidates: ['示例项目（3）', '示例项目（5）', '示例项目（6）'],
  });
});

test('改名后再次冲突时候选仍以原始名称为基准', () => {
  const conflict = detectNameConflict(['示例项目', '示例项目（2）'], '示例项目（2）', '示例项目');
  assert.deepEqual(conflict?.candidates, ['示例项目（3）', '示例项目（4）', '示例项目（5）']);
  assert.equal(conflict?.attemptedName, '示例项目（2）');
});

test('超长名称生成候选时截断到 200 个字符以内', () => {
  const longName = '项'.repeat(240);
  const conflict = detectNameConflict([longName], longName, longName);
  for (const candidate of conflict!.candidates) {
    assert.ok([...candidate].length <= 200, candidate);
  }
});
