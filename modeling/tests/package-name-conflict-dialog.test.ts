import assert from 'node:assert/strict';
import { test } from 'node:test';

import { packageNameSelectionError } from '../frontend/src/PackageNameConflictDialog.jsx';

test('知识包冲突改名要求非空、不同名且不超过长度上限', () => {
  assert.equal(packageNameSelectionError('', '示例知识包'), '请输入新的知识包名称');
  assert.equal(
    packageNameSelectionError('Ｓｈａｒｅｄ　Ｐａｃｋａｇｅ', 'Shared Package'),
    '新名称不能与原名称相同',
  );
  assert.equal(
    packageNameSelectionError('a'.repeat(201), '示例知识包'),
    '知识包名称不能超过 200 个字符',
  );
  assert.equal(packageNameSelectionError('示例知识包（2）', '示例知识包'), '');
});
