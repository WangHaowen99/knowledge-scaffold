import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as XLSX from 'xlsx';
import { kindOf, parseDocument } from '../src/documents/parser.js';
import { buildTools } from '../src/workflow/tools.js';

function excelDateTimeSerial(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
  second = 0,
): number {
  return Date.UTC(year, month - 1, day, hour, minute, second) / 86_400_000 + 25_569;
}

test('上传 CSV 后会解析为可落库的项目数据表', async () => {
  const csv = Buffer.from('yard_id,capacity\nY01,100\nY02,200\n', 'utf8');

  assert.equal(kindOf('warehouse_location.csv'), 'csv');
  const parsed = await parseDocument('csv', csv, 'warehouse_location.csv');

  assert.equal(parsed.error, undefined);
  assert.equal(parsed.tables?.length, 1);
  assert.deepEqual(parsed.tables?.[0], {
    name: 'warehouse_location',
    fields: [
      { name: 'yard_id', sourceColumn: 'A', note: undefined, inferredType: 'string', sampleValues: ['Y01', 'Y02'], distinctRatio: 1, nonEmptyRatio: 1 },
      { name: 'capacity', sourceColumn: 'B', note: undefined, inferredType: 'number', sampleValues: ['100', '200'], distinctRatio: 1, nonEmptyRatio: 1 },
    ],
    rowCount: 2,
    rows: [['Y01', '100'], ['Y02', '200']],
  });
});

test('普通 Excel 上传和资料分析保留本地日期时间且不做 UTC 截断', async () => {
  const worksheet = XLSX.utils.aoa_to_sheet([
    ['business_date', 'occurred_at', 'midnight_at', 'empty_at'],
    [
      excelDateTimeSerial(2026, 8, 6),
      excelDateTimeSerial(2026, 8, 6, 15, 4, 5),
      excelDateTimeSerial(2026, 8, 7),
      '',
    ],
  ]);
  worksheet.A2.z = 'yyyy-mm-dd';
  worksheet.B2.z = 'yyyy-mm-dd hh:mm:ss';
  worksheet.C2.z = 'yyyy-mm-dd hh:mm:ss';
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'events');
  const buffer = Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));

  const parsed = await parseDocument('excel', buffer, 'events.xlsx');
  const table = parsed.tables?.[0];
  assert.ok(table);
  assert.deepEqual(table.rows, [[
    '2026-08-06',
    '2026-08-06T15:04:05',
    '2026-08-07T00:00:00',
    '',
  ]]);
  assert.deepEqual(table.fields.map((field) => field.inferredType), [
    'date', 'date', 'date', 'unknown',
  ]);

  const readRows = buildTools({ tables: [table], documents: [], captured: {} })
    .find((tool) => tool.name === 'read_table_rows');
  assert.ok(readRows);
  const result = await readRows.execute('call-datetime', { table: 'events' } as never);
  assert.deepEqual(JSON.parse(result.content[0].text).rows, [{
    business_date: '2026-08-06',
    occurred_at: '2026-08-06T15:04:05',
    midnight_at: '2026-08-07T00:00:00',
    empty_at: '',
  }]);
});
