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

test('get_schema_overview 一次返回结构统计而不返回业务行', async () => {
  const tools = buildTools({
    tables: [{ name: 'synthetic', rowCount: 10_000, fields: [
      { name: 'id', sourceColumn: 'A', inferredType: 'string', sampleValues: ['1', '2', '3'], distinctRatio: 1, nonEmptyRatio: 1 },
    ], rows: Array.from({ length: 10_000 }, (_, i) => [String(i)]) }],
    documents: [], captured: {},
  });
  const tool = tools.find((item) => item.name === 'get_schema_overview');
  assert.ok(tool);
  const payload = JSON.parse((await tool.execute('overview', {} as never)).content[0].text);
  assert.equal(payload.tableCount, 1);
  assert.equal(payload.tables[0].rowCount, 10_000);
  assert.equal('rows' in payload.tables[0], false);
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

test('连接型评测诊断提供运行时数据查询并标记上传表格为样例', async () => {
  const calls: Array<{ entity: string; query: string }> = [];
  const tool = buildTools({
    tables: [{
      name: 'sample_records',
      rowCount: 2,
      rows: [['synthetic-1']],
      fields: [{ name: 'record_id', inferredType: 'string', sampleValues: ['synthetic-1'], distinctRatio: 1 }],
    }],
    documents: [],
    runtimeDataSearch: async (input) => {
      calls.push(input);
      return '{"result_count":3,"scope":"evaluation_runtime"}';
    },
    captured: {},
  });

  const runtimeSearch = tool.find((item) => item.name === 'business_data_search');
  assert.ok(runtimeSearch);
  const runtimeOutput = await runtimeSearch.execute('call-runtime', {
    entity: 'record',
    query: '查找全部记录',
  } as never);
  assert.match(runtimeOutput.content[0].text, /evaluation_runtime/);
  assert.deepEqual(calls, [{ entity: 'record', query: '查找全部记录' }]);

  assert.equal(tool.some((item) => item.name === 'read_table_rows'), false);
  const tableList = await tool.find((item) => item.name === 'list_tables')?.execute('call-list', {} as never);
  assert.match(tableList?.content[0].text ?? '', /结构\/样例/);
});
