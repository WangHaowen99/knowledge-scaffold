import assert from 'node:assert/strict';
import test from 'node:test';

const { decodeLegacyArchiveName, displayPackageEntryName } = await import(
  '../frontend/src/legacyArchiveFilename.js'
);

test('Workbench 展示层兼容已导入的 GBK 文件名', () => {
  const legacyName = String.fromCharCode(0xb2, 0xe2, 0xca, 0xd4);
  assert.equal(decodeLegacyArchiveName(`objects/${legacyName}.md`), 'objects/测试.md');
  assert.equal(displayPackageEntryName(`objects/${legacyName}.md`), '测试.md');
});

test('Workbench 不改写正常 UTF-8 和 ASCII 文件名', () => {
  assert.equal(decodeLegacyArchiveName('objects/正常条目.md'), 'objects/正常条目.md');
  assert.equal(displayPackageEntryName('operations/index.md'), 'operations/index.md');
  assert.equal(displayPackageEntryName('objects/record.md'), 'record.md');
  const legacyLatinName = `caf${String.fromCharCode(0xe9)}.md`;
  assert.equal(decodeLegacyArchiveName(legacyLatinName), legacyLatinName);
});
