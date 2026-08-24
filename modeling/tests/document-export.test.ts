import assert from 'node:assert/strict';
import test from 'node:test';

import { buildDocumentExport } from '../src/documents/export.js';
import { parseExcel } from '../src/documents/parser.js';
import type { DocumentRecord } from '../src/types.js';

function document(patch: Partial<DocumentRecord> = {}): DocumentRecord {
  return {
    id: 'doc-export', projectId: 'project-export', filename: '生产计划.xlsx', kind: 'excel',
    parseStatus: 'parsed', createdAt: '2026-08-21T00:00:00.000Z',
    ...patch,
  };
}

test('新上传资料优先下载原始字节', () => {
  const raw = Buffer.from('original-material');
  const exported = buildDocumentExport(document(), raw)!;

  assert.equal(exported.mode, 'original');
  assert.equal(exported.filename, '生产计划.xlsx');
  assert.deepEqual(exported.buffer, raw);
});

test('历史表格没有原始字节时导出可重新上传的规范化工作簿', () => {
  const exported = buildDocumentExport(document({
    tables: [{
      name: '批次计划', rowCount: 2,
      fields: [
        { name: 'batch_id', note: '批次编号', inferredType: 'string', sampleValues: ['B1'], distinctRatio: 1 },
        { name: 'volume', note: '加工量', inferredType: 'number', sampleValues: ['100'], distinctRatio: 1 },
      ],
      rows: [['B1', '100'], ['B2', '200']],
    }],
  }), null)!;

  assert.equal(exported.mode, 'normalized');
  assert.equal(exported.filename, '生产计划.xlsx');
  const [table] = parseExcel(exported.buffer);
  assert.equal(table.name, '批次计划');
  assert.deepEqual(table.fields.map((field) => [field.name, field.note]), [
    ['batch_id', '批次编号'], ['volume', '加工量'],
  ]);
  assert.deepEqual(table.rows, [['B1', '100'], ['B2', '200']]);
});

test('历史 Word 或 PDF 只导出明确标识的提取正文', () => {
  const exported = buildDocumentExport(document({
    filename: '操作规程.pdf', kind: 'pdf', textContent: '正文证据',
  }), null)!;

  assert.equal(exported.mode, 'normalized');
  assert.equal(exported.filename, '操作规程.pdf.extracted.txt');
  assert.equal(exported.buffer.toString('utf8'), '正文证据');
});
