import assert from 'node:assert/strict';
import test from 'node:test';
import type { PackageSpec } from '../src/types.js';
import { canonicalizePackageSpec } from '../src/workflow/package-spec.js';

test('operation 只保留脚本引用，完整实现以顶层 scripts 为唯一来源', () => {
  const spec = {
    id: 'demo-agent',
    name: '演示知识包',
    description: '演示',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    rules: [],
    metrics: [],
    operations: [{
      key: 'calculate',
      title: '计算',
      description: '执行计算',
      inputs: [],
      outputs: [],
      flow: ['执行 script:calculate_script'],
      relatedObjects: [],
      script: {
        key: 'calculate_script',
        required: true,
        sourceCode: 'print("embedded stale implementation")',
        entrypoint: 'embedded.py',
      },
    }],
    scripts: [{
      key: 'calculate_script',
      title: '计算脚本',
      description: '唯一实现',
      boundOperation: 'calculate',
      runtime: 'python',
      entrypoint: 'calculate.py',
      timeoutSeconds: 30,
      parameters: [],
      outputs: [],
      boundaries: [],
      sourceCode: 'print("canonical implementation")',
    }],
  } as unknown as PackageSpec;

  const canonical = canonicalizePackageSpec(spec);

  assert.deepEqual(canonical.operations[0].script, { key: 'calculate_script', required: true });
  assert.equal(canonical.scripts?.[0].sourceCode, 'print("canonical implementation")');
  assert.notEqual(canonical, spec);
  assert.notEqual(canonical.operations[0], spec.operations[0]);
});
