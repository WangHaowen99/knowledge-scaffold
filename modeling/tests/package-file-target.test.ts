import assert from 'node:assert/strict';
import test from 'node:test';

import { resolvePackageFileTarget } from '../frontend/src/packageFileTarget.js';

const files = [
  'index.md',
  'objects/survey_scheme.md',
  'operations/review.md',
  'rules/eligibility.md',
  'metrics/index.md',
  'mappings/power_source_nature_code_mapping.yaml',
  'diagrams/order.mmd',
];

test('文件定位优先精确路径并兼容资源 key', () => {
  assert.equal(resolvePackageFileTarget(files, 'diagrams/order.mmd'), 'diagrams/order.mmd');
  assert.equal(resolvePackageFileTarget(files, 'review'), 'operations/review.md');
  assert.equal(resolvePackageFileTarget(files, 'eligibility.md'), 'rules/eligibility.md');
  assert.equal(
    resolvePackageFileTarget(files, 'survey_scheme.fields.special_load_cn'),
    'objects/survey_scheme.md',
  );
  assert.equal(
    resolvePackageFileTarget(files, 'mapping:power_source_nature_code_mapping'),
    'mappings/power_source_nature_code_mapping.yaml',
  );
  assert.equal(resolvePackageFileTarget(files, 'metrics'), 'metrics/index.md');
  assert.equal(resolvePackageFileTarget(files, 'missing'), '');
});
