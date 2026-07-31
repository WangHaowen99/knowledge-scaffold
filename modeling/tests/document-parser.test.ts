import { test } from 'node:test';
import assert from 'node:assert/strict';
import { kindOf, parseDocument } from '../src/documents/parser.js';

test('上传 CSV 后会解析为可落库的项目数据表', async () => {
  const csv = Buffer.from('yard_id,capacity\nY01,100\nY02,200\n', 'utf8');

  assert.equal(kindOf('warehouse_location.csv'), 'csv');
  const parsed = await parseDocument('csv', csv, 'warehouse_location.csv');

  assert.equal(parsed.error, undefined);
  assert.equal(parsed.tables?.length, 1);
  assert.deepEqual(parsed.tables?.[0], {
    name: 'warehouse_location',
    fields: [
      { name: 'yard_id', note: undefined, inferredType: 'string', sampleValues: ['Y01', 'Y02'], distinctRatio: 1 },
      { name: 'capacity', note: undefined, inferredType: 'number', sampleValues: ['100', '200'], distinctRatio: 1 },
    ],
    rowCount: 2,
    rows: [['Y01', '100'], ['Y02', '200']],
  });
});
