import assert from 'node:assert/strict';
import test from 'node:test';

import { packageDisplayLabel } from './packagePresentation.js';

test('package selector labels distinguish display names from stable ids', () => {
  assert.equal(
    packageDisplayLabel({ name: '知识包示例', id: 'knowledge_package_demo' }),
    '知识包示例（knowledge_package_demo）',
  );
  assert.equal(
    packageDisplayLabel({
      name: 'knowledge_package_demo',
      id: 'knowledge_package_demo',
    }),
    'knowledge_package_demo',
  );
  assert.equal(packageDisplayLabel({ id: 'knowledge_package_demo' }), 'knowledge_package_demo');
});
