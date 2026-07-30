import assert from 'node:assert/strict';
import test from 'node:test';
import type { PackageSpec } from '../src/types.js';
import { renderPackage } from '../src/workflow/package-render.js';
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

test('operation 流程步骤在规格边界移除模型重复写入的序号', () => {
  const spec = {
    id: 'numbered-flow',
    name: '流程序号',
    description: '流程序号',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [{
      key: 'plan', title: '制定计划', description: '制定计划', inputs: [], outputs: [],
      flow: ['1. 1. 接收计划数据', '2、校验计划数据', '第 3 步 制定排产方案', '按 rule:FlexCheck 校验'],
      relatedObjects: [],
    }],
  } as PackageSpec;

  const canonical = canonicalizePackageSpec(spec);
  const markdown = renderPackage(spec)['operations/plan.md'];

  assert.deepEqual(canonical.operations[0].flow, [
    '接收计划数据', '校验计划数据', '制定排产方案', '按 rule:FlexCheck 校验',
  ]);
  assert.deepEqual(spec.operations[0].flow, [
    '1. 1. 接收计划数据', '2、校验计划数据', '第 3 步 制定排产方案', '按 rule:FlexCheck 校验',
  ]);
  assert.match(markdown, /# 流程\n\n1\. 接收计划数据\n2\. 校验计划数据/u);
  assert.doesNotMatch(markdown, /^\d+\.\s+\d+[.)、]\s+/mu);
});
