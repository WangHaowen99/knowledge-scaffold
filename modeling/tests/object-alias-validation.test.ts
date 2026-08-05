import assert from 'node:assert/strict';
import test from 'node:test';

import { runRuleChecks } from '../src/workflow/package-check.js';
import {
  validateObjectBatch,
  validateSpecIntegrity,
} from '../src/workflow/spec-validation.js';
import type { PackageSpec, SpecObject } from '../src/types.js';

function object(key: string, title: string, aliases: string[]): SpecObject {
  return {
    key,
    title,
    aliases,
    description: `${title}的完整业务对象定义`,
    fields: [{ key: 'id', cnName: '编号', type: 'string' }],
  };
}

function aliasFindings(objects: SpecObject[]) {
  const spec: PackageSpec = {
    id: 'object-alias-check',
    name: '对象别名校验',
    description: '验证对象别名在知识包内保持唯一',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects,
    operations: [],
  };
  return runRuleChecks(spec, [], [], { validateDataMappings: false })
    .filter((finding) => finding.category === '对象别名冲突');
}

test('对象别名校验：阻断同一 alias 跨对象重复并按 Consumer 规则归一化', () => {
  const findings = aliasFindings([
    object('客户价值评估', '客户价值评估', ['ERM评级', 'CustomerRating']),
    object('客户评级', '客户评级', ['ERM评级', 'customerrating']),
  ]);

  assert.equal(findings.length, 2);
  assert.ok(findings.every((finding) => finding.severity === '错误'));
  assert.ok(findings.some((finding) => (
    finding.message.includes('ERM评级')
    && finding.message.includes('客户价值评估')
    && finding.message.includes('客户评级')
  )));
  assert.ok(findings.some((finding) => (
    finding.message.includes('customerrating')
    && finding.message.includes('客户价值评估')
    && finding.message.includes('客户评级')
  )));
});

test('对象别名校验：拆分 Consumer 支持的分隔符后检查冲突', () => {
  const findings = aliasFindings([
    object('CustomerValue', '客户价值', ['ERM评级/ValueRating']),
    object('CustomerRating', '客户等级', ['ERM评级', 'valuerating']),
  ]);

  assert.equal(findings.length, 2);
  assert.ok(findings.some((finding) => finding.message.includes('ERM评级')));
  assert.ok(findings.some((finding) => finding.message.includes('valuerating')));
});

test('对象别名校验：阻断 alias 与其他对象 key 或 title 冲突', () => {
  const findings = aliasFindings([
    object('CustomerProfile', '客户画像', ['客户评级', 'RATING']),
    object('客户评级', 'Rating', []),
  ]);

  assert.equal(findings.length, 2);
  assert.ok(findings.some((finding) => /客户评级.*key.*客户评级/u.test(finding.message)));
  assert.ok(findings.some((finding) => /rating.*title.*客户评级/iu.test(finding.message)));
});

test('对象别名校验：同一对象内部重复或与自身标识相同不产生跨对象冲突', () => {
  const findings = aliasFindings([
    object('CustomerRating', '客户评级', [
      'CustomerRating', 'customerrating', '客户评级', '客户评级',
    ]),
    object('CustomerProfile', '客户画像', ['客户档案']),
  ]);

  assert.deepEqual(findings, []);
});

test('对象批次校验：新生成对象与已有对象 alias 冲突时要求 AI 重新提交', () => {
  const issues = validateObjectBatch(
    [],
    [object('客户评级', '客户评级', ['customerrating'])],
    [object('客户价值评估', '客户价值评估', ['CustomerRating'])],
  );

  assert.equal(issues.length, 1);
  assert.match(issues[0], /customerrating.*客户价值评估.*客户评级/u);
});

test('规格完整性校验：阻止后续 AI 修改重新引入对象 alias 冲突', () => {
  const objects = [
    object('客户价值评估', '客户价值评估', ['CustomerRating']),
    object('客户评级', '客户评级', ['customerrating']),
  ];
  const spec: PackageSpec = {
    id: 'object-alias-integrity',
    name: '对象别名完整性校验',
    description: '阻止后续修改引入对象别名冲突',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects,
    operations: [],
  };

  assert.throws(
    () => validateSpecIntegrity(spec),
    /对象别名「customerrating」同时指向多个对象：客户价值评估、客户评级/u,
  );
});
