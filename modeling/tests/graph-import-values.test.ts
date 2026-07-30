import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as XLSX from 'xlsx';
import neo4j from 'neo4j-driver';
import { parseExcel } from '../src/documents/parser.js';
import { writeGraphImport } from '../src/graph-import/neo4j-writer.js';
import type { GraphImportPackageSchema } from '../src/graph-import/types.js';
import { validateGraphImportMapping } from '../src/graph-import/validator.js';
import { neo4jConnection, recordingDriver } from './helpers/graph-import-fixtures.js';

const typedSchema: GraphImportPackageSchema = {
  packageVersion: '1.0.0',
  objects: [{
    key: 'Typed',
    title: '类型测试',
    label: 'Typed',
    defaultSheet: 'Typed',
    propertyMappings: [
      { property: 'code', sourceColumn: 'code' },
      { property: 'quantity', sourceColumn: 'quantity' },
      { property: 'metric', sourceColumn: 'metric' },
      { property: 'active', sourceColumn: 'active' },
      { property: 'event_date', sourceColumn: 'event_date' },
      { property: 'occurred_at', sourceColumn: 'occurred_at' },
      { property: 'mixed', sourceColumn: 'mixed' },
      { property: 'large_id', sourceColumn: 'large_id' },
      { property: 'overflow_id', sourceColumn: 'overflow_id' },
      { property: 'precise_value', sourceColumn: 'precise_value' },
      { property: 'empty_value', sourceColumn: 'empty_value' },
      { property: 'midnight_at', sourceColumn: 'midnight_at' },
    ],
  }],
  relations: [],
};

function excelDateTimeSerial(
  year: number,
  month: number,
  day: number,
  hour = 0,
  minute = 0,
  second = 0,
  millisecond = 0,
): number {
  return Date.UTC(year, month - 1, day, hour, minute, second, millisecond) / 86_400_000 + 25_569;
}

function typedWorkbookBuffer(): Buffer {
  const worksheet = XLSX.utils.aoa_to_sheet([
    [
      'code', 'quantity', 'metric', 'active', 'event_date', 'occurred_at',
      'mixed', 'large_id', 'overflow_id', 'precise_value', 'empty_value',
      'midnight_at',
    ],
    [
      12, 7, 1, true,
      excelDateTimeSerial(2026, 7, 29),
      excelDateTimeSerial(2024, 12, 19, 15, 1, 52),
      '1', '9223372036854775807', '9223372036854775808', '123456789012345.67', '',
      excelDateTimeSerial(2026, 7, 29),
    ],
    [13, '', 2.5, '', '', '', '2', '42', '42', '1.25', ''],
    [
      14, 9, '', false,
      excelDateTimeSerial(2026, 7, 30),
      excelDateTimeSerial(2026, 7, 30, 8, 5, 6, 123),
      'N/A', '7', '7', '2.5', '', '',
    ],
  ]);
  for (const address of ['A2', 'A3', 'A4']) worksheet[address].z = '0000';
  for (const address of ['E2', 'E4']) worksheet[address].z = 'yyyy-mm-dd';
  for (const address of ['F2', 'F4', 'L2']) worksheet[address].z = 'yyyy-mm-dd hh:mm:ss';
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, 'Typed');
  return Buffer.from(XLSX.write(workbook, { type: 'buffer', bookType: 'xlsx' }));
}

function typedPlan() {
  const tables = parseExcel(typedWorkbookBuffer(), {
    maxRows: null,
    includeCellMetadata: true,
  });
  return {
    table: tables[0],
    plan: validateGraphImportMapping(typedSchema, tables, { relations: [] }),
  };
}

test('graph import value inference：保留 Excel 单元格类型并按整列非空值保守推断', () => {
  const { table, plan } = typedPlan();
  assert.deepEqual(table.cellRows?.[0][0], {
    kind: 'number', value: '12', formatted: '0012',
  });
  assert.equal(table.cellRows?.[0][5].kind, 'date');
  assert.equal(table.cellRows?.[0][5].value, '2024-12-19T15:01:52');

  assert.deepEqual(plan.entities[0].propertyTypes, {
    code: 'string',
    quantity: 'integer',
    metric: 'float',
    active: 'boolean',
    event_date: 'date',
    occurred_at: 'local_datetime',
    mixed: 'string',
    large_id: 'integer',
    overflow_id: 'string',
    precise_value: 'string',
    empty_value: 'string',
    midnight_at: 'local_datetime',
  });
  assert.deepEqual(plan.entities[0].rows[0].properties, {
    code: '0012',
    quantity: '7',
    metric: '1',
    active: 'true',
    event_date: '2026-07-29',
    occurred_at: '2024-12-19T15:01:52',
    mixed: '1',
    large_id: '9223372036854775807',
    overflow_id: '9223372036854775808',
    precise_value: '123456789012345.67',
    empty_value: '',
    midnight_at: '2026-07-29T00:00:00',
  });
  assert.deepEqual(plan.entities[0].rows[1].properties, {
    code: '0013',
    quantity: '',
    metric: '2.5',
    active: '',
    event_date: '',
    occurred_at: '',
    mixed: '2',
    large_id: '42',
    overflow_id: '42',
    precise_value: '1.25',
    empty_value: '',
    midnight_at: '',
  });
  assert.match(plan.warnings.join('；'), /mixed.*混合类型.*字符串/);
  assert.match(plan.warnings.join('；'), /overflow_id.*64 位整数范围.*字符串/);
  assert.match(plan.warnings.join('；'), /precise_value.*FLOAT 精度.*字符串/);
});

test('graph import Neo4j values：非空值使用对应驱动类型且空属性保留空字符串', async () => {
  const { plan } = typedPlan();
  const driver = recordingDriver();
  await writeGraphImport(plan, neo4jConnection(), { userName: 'alice' }, driver.factory);

  const write = driver.calls.find((call) => /MERGE \(node:`Typed`/.test(call.query));
  assert.ok(write);
  const [first, second, third] = write.params.rows as Array<{ properties: Record<string, any> }>;
  assert.equal(first.properties.code, '0012');
  assert.equal(neo4j.isInt(first.properties.quantity), true);
  assert.equal(first.properties.quantity.toString(), '7');
  assert.equal(first.properties.metric, 1);
  assert.equal(typeof first.properties.metric, 'number');
  assert.equal(first.properties.active, true);
  assert.equal(first.properties.event_date.constructor.name, 'Date');
  assert.equal(first.properties.event_date.toString(), '2026-07-29');
  assert.equal(first.properties.occurred_at.constructor.name, 'LocalDateTime');
  assert.equal(first.properties.occurred_at.toString(), '2024-12-19T15:01:52');
  assert.equal(first.properties.midnight_at.constructor.name, 'LocalDateTime');
  assert.equal(first.properties.midnight_at.toString(), '2026-07-29T00:00:00');
  assert.equal(neo4j.isInt(first.properties.large_id), true);
  assert.equal(first.properties.large_id.toString(), '9223372036854775807');
  assert.equal(first.properties.overflow_id, '9223372036854775808');
  assert.equal(first.properties.precise_value, '123456789012345.67');

  assert.equal(second.properties.quantity, '');
  assert.equal(second.properties.active, '');
  assert.equal(second.properties.event_date, '');
  assert.equal(second.properties.occurred_at, '');
  assert.equal(second.properties.empty_value, '');
  assert.equal(third.properties.occurred_at.toString(), '2026-07-30T08:05:06');
});
