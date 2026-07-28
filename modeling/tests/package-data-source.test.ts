import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PACKAGE_DATA_SOURCE_OPTIONS } from '../frontend/src/package-data-source.js';
import { parsePackageDataSourceType } from '../src/workflow/package-data-source.js';

test('知识包数据源：前端暂不展示 MindsDB 且可见选项均为后端允许值', () => {
  const values = PACKAGE_DATA_SOURCE_OPTIONS.map((option) => option.value);
  assert.deepEqual(values, ['nl2sql', 'neo4j']);
  assert.equal(PACKAGE_DATA_SOURCE_OPTIONS[0].label, 'NL2SQL HTTP/Template');
  assert.deepEqual(values.map(parsePackageDataSourceType), values);
  assert.equal(parsePackageDataSourceType('mindsdb'), 'mindsdb');
  assert.throws(() => parsePackageDataSourceType(''), /请选择后续使用的数据源/);
  assert.throws(() => parsePackageDataSourceType('oracle'), /请选择后续使用的数据源/);
});
