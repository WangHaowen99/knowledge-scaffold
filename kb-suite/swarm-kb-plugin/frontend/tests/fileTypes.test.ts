// Synthetic file names only.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { unsupportedType } from '../src/fileTypes.ts';

test('a declared type passes, case-insensitively', () => {
  assert.equal(unsupportedType('合成报告.PDF', ['pdf', 'docx']), null);
});

test('an undeclared type or a name without extension is refused with the allowed list', () => {
  assert.equal(unsupportedType('截图.png', ['pdf', 'docx']), '不支持 .png 文件，这个知识库支持 PDF、DOCX');
  assert.match(unsupportedType('README', ['md']) ?? '', /^文件没有扩展名/);
  assert.match(unsupportedType('.hidden', ['md']) ?? '', /^文件没有扩展名/);
});

test('nothing is refused when the backend declares no types', () => {
  assert.equal(unsupportedType('任意.xyz', undefined), null);
  assert.equal(unsupportedType('任意.xyz', []), null);
});
