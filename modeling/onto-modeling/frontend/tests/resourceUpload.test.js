// 资料上传的白名单与大小上限（移植自 develop 的同名测试）：资料区、新建项目、工作台、对话附件共用一份
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  MAX_RESOURCE_BYTES,
  checkResourceFile,
  legacyOfficeHint,
  splitResourceFiles,
  uploadRules,
} from '../src/lib/resourceUpload.js';

test('支持的扩展名不分大小写，正好 20 MB 能传', () => {
  assert.equal(checkResourceFile('资料.PDF', MAX_RESOURCE_BYTES), null);
  assert.equal(checkResourceFile('数据.csv', 1), null);
  assert.equal(checkResourceFile('截图.PNG', 1), null);
});

test('超限和不支持的类型给出原因', () => {
  assert.match(checkResourceFile('资料.docx', MAX_RESOURCE_BYTES + 1), /超过 20 MB/);
  assert.match(checkResourceFile('程序.exe', 1), /不支持这种格式/);
  assert.match(checkResourceFile('图形.svg', 1), /PDF 或 drawio/);
  assert.match(checkResourceFile('没有扩展名', 1), /不支持这种格式/);
});

test('一批里有被挡的，其余照常传', () => {
  const result = splitResourceFiles([{ name: '合法.md', size: 1 }, { name: '非法.exe', size: 1 }]);
  assert.deepEqual(result.accepted.map((file) => file.name), ['合法.md']);
  assert.deepEqual(result.rejected.map((item) => item.file.name), ['非法.exe']);
});

test('规则以 C-01 下发的为准，没取到时用兜底', () => {
  const rules = uploadRules({ material_types: ['.MD', '.csv'], limits: { material_upload_bytes: 1024 } });
  assert.deepEqual(rules.types, ['.md', '.csv']);
  assert.equal(rules.accept, '.md,.csv');
  assert.equal(checkResourceFile('a.pdf', 1, rules) !== null, true);
  assert.match(checkResourceFile('a.md', 2048, rules), /超过/);
  assert.equal(uploadRules(null).maxBytes, MAX_RESOURCE_BYTES);
  assert.ok(uploadRules(undefined).types.includes('.xlsx'));
});

test('旧版 .doc / .xls 传完提示另存', () => {
  assert.match(legacyOfficeHint('旧版规则.doc'), /另存为 \.docx/);
  assert.match(legacyOfficeHint('旧版表.XLS'), /另存为 \.xlsx/);
  assert.equal(legacyOfficeHint('新版规则.docx'), '');
});
