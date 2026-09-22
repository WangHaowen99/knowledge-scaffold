import assert from 'node:assert/strict';
import { test } from 'node:test';

import { nameSelectionError } from '../frontend/src/NameConflictDialog.jsx';

test('知识包冲突改名要求非空、不同名且不超过长度上限', () => {
  assert.equal(nameSelectionError('', '示例知识包'), '请输入新的知识包名称');
  assert.equal(
    nameSelectionError('Ｓｈａｒｅｄ　Ｐａｃｋａｇｅ', 'Shared Package'),
    '新名称不能与原名称相同',
  );
  assert.equal(
    nameSelectionError('a'.repeat(201), '示例知识包'),
    '知识包名称不能超过 200 个字符',
  );
  assert.equal(nameSelectionError('示例知识包（2）', '示例知识包'), '');
});

test('项目冲突改名复用同一套校验并使用项目文案', () => {
  assert.equal(nameSelectionError('', '示例项目', '项目'), '请输入新的项目名称');
  assert.equal(nameSelectionError('  示例项目  ', '示例项目', '项目'), '新名称不能与原名称相同');
  assert.equal(
    nameSelectionError('a'.repeat(201), '示例项目', '项目'),
    '项目名称不能超过 200 个字符',
  );
  assert.equal(nameSelectionError('示例项目（2）', '示例项目', '项目'), '');
});
