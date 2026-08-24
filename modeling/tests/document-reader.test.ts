import assert from 'node:assert/strict';
import test from 'node:test';
import { readDocumentPage } from '../src/workflow/document-read-session.js';

test('按需原文查询返回稳定分页位置，不承担全文分析状态', () => {
  const document = { filename: 'synthetic-manual.md', textContent: '0123456789' };
  assert.deepEqual(readDocumentPage(document, 3, 4), {
    filename: 'synthetic-manual.md',
    offset: 3,
    endOffset: 7,
    totalChars: 10,
    nextOffset: 7,
    eof: false,
    content: '3456',
  });
  assert.equal(readDocumentPage(document, 7, 4).eof, true);
});
