import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MAX_RESOURCE_FILE_SIZE,
  validateResourceFile,
  validateResourceFiles,
} from '../src/lib/resourceUpload.js';

test('resource upload accepts supported extensions case-insensitively at the size boundary', () => {
  assert.equal(
    validateResourceFile({ name: '资料.PDF', size: MAX_RESOURCE_FILE_SIZE }).valid,
    true,
  );
  assert.equal(validateResourceFile({ name: '数据.csv', size: 1 }).valid, true);
  assert.equal(validateResourceFile({ name: '截图.PNG', size: 1 }).valid, true);
});

test('resource upload rejects oversized and unsupported files with stable codes', () => {
  assert.equal(
    validateResourceFile({ name: '资料.docx', size: MAX_RESOURCE_FILE_SIZE + 1 }).code,
    'resource_file_too_large',
  );
  assert.equal(
    validateResourceFile({ name: '程序.exe', size: 1 }).code,
    'unsupported_resource_file_type',
  );
});

test('batch validation keeps valid files when another file is rejected', () => {
  const result = validateResourceFiles([
    { name: '合法.md', size: 1 },
    { name: '非法.exe', size: 1 },
  ]);
  assert.deepEqual(result.accepted.map((file) => file.name), ['合法.md']);
  assert.deepEqual(result.rejected.map((item) => item.file.name), ['非法.exe']);
});
