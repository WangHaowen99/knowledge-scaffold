import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MAX_RESOURCE_FILE_SIZE,
  legacyOfficeHint,
  rejectedResourceMessage,
  uploadRules,
  validateResourceFile,
  validateResourceFiles,
} from '../src/lib/resourceUpload.js';

// ---- develop 原测试（只改了导入） ----

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

// ---- 本分支：文字、C-01 下发的规则、旧版 Office 提示（develop 在 sourceLocation 测试里） ----

test('拒收文字与 develop 一致，多份用「；」连成一条', () => {
  const { rejected } = validateResourceFiles([
    { name: '程序.exe', size: 1 },
    { name: '资料.docx', size: MAX_RESOURCE_FILE_SIZE + 1 },
  ]);
  assert.equal(rejectedResourceMessage(rejected), '不支持文件“程序.exe”的类型；文件“资料.docx”超过 20 MB，无法上传');
  assert.equal(validateResourceFile({ name: '截图.webp', size: 1 }).valid, true);
});

test('规则以 C-01 下发的为准，没取到时用兜底', () => {
  const rules = uploadRules({ material_types: ['.MD', '.csv'], limits: { material_upload_bytes: 1024 } });
  assert.equal(rules.accept, '.md,.csv');
  assert.equal(validateResourceFile({ name: 'a.pdf', size: 1 }, rules).valid, false);
  assert.equal(validateResourceFile({ name: 'a.md', size: 2048 }, rules).code, 'resource_file_too_large');
  assert.equal(uploadRules(null).maxBytes, MAX_RESOURCE_FILE_SIZE);
});

test('旧版 .doc / .xls 传完提示另存', () => {
  assert.match(legacyOfficeHint('旧版规则.doc'), /另存为 \.docx/);
  assert.match(legacyOfficeHint('旧版表.XLS'), /另存为 \.xlsx/);
  assert.equal(legacyOfficeHint('新版规则.docx'), '');
});
