import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildTools } from '../src/workflow/tools.js';

test('read_table_rows 按字段名返回有边界的代表行', async () => {
  const tools = buildTools({
    tables: [{
      name: 'sample_records',
      rowCount: 3,
      fields: [
        { name: 'record_id', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 },
        { name: 'derived_value', inferredType: 'number', sampleValues: ['2.5'], distinctRatio: 1 },
      ],
      rows: [['1', '2.5'], ['2', '4.5'], ['3', '6.5']],
    }],
    documents: [],
    captured: {},
  });
  const tool = tools.find((item) => item.name === 'read_table_rows');
  assert.ok(tool);

  const output = await tool.execute('call-1', { table: 'sample_records', offset: 1, limit: 1 } as never);
  const payload = JSON.parse(output.content[0].text);
  assert.deepEqual(payload.columns, ['record_id', 'derived_value']);
  assert.deepEqual(payload.rows, [{ record_id: '2', derived_value: '4.5' }]);
  assert.deepEqual(payload.page, { offset: 1, limit: 1, returned: 1, total: 3, nextOffset: 2 });
});

test('read_table_rows 强制限制单次读取行数', async () => {
  const rows = Array.from({ length: 80 }, (_, index) => [String(index)]);
  const tool = buildTools({
    tables: [{
      name: 'bounded_records', rowCount: rows.length, rows,
      fields: [{ name: 'id', inferredType: 'number', sampleValues: ['0'], distinctRatio: 1 }],
    }],
    documents: [], captured: {},
  }).find((item) => item.name === 'read_table_rows');
  assert.ok(tool);

  const output = await tool.execute('call-2', { table: 'bounded_records', limit: 10_000 } as never);
  const payload = JSON.parse(output.content[0].text);
  assert.equal(payload.rows.length, 50);
  assert.equal(payload.page.limit, 50);
});
