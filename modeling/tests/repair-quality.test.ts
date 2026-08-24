import assert from 'node:assert/strict';
import test from 'node:test';

import {
  evaluateRepairQualityGate,
  repairFindingFingerprint,
} from '../src/ai-construction/repair-quality.js';
import type { CheckFinding } from '../src/workflow/package-check.js';

function finding(overrides: Partial<CheckFinding>): CheckFinding {
  return {
    id: String(overrides.id ?? Math.random()),
    source: overrides.source ?? 'semantic',
    severity: overrides.severity ?? '错误',
    category: overrides.category ?? '口径',
    target: overrides.target ?? 'rule:R1',
    message: overrides.message ?? '规则口径不完整',
    ...overrides,
  };
}

test('目标错误消失且没有新增阻断时质量门禁通过', () => {
  const target = finding({ target: 'rule:R1' });
  const existing = finding({ target: 'rule:R2' });
  const gate = evaluateRepairQualityGate({
    before: [target, existing],
    after: [existing],
    targetFingerprints: [repairFindingFingerprint(target)],
    changedPaths: ['rules/R1.md', 'rules/index.md'],
    allowedPaths: ['rules/R1.md', 'rules/index.md'],
  });
  assert.equal(gate.passed, true);
  assert.equal(gate.outcome, 'improved');
  assert.deepEqual(gate.remainingFingerprints, [repairFindingFingerprint(existing)]);
  assert.deepEqual(gate.introducedFingerprints, []);
});

test('候选引入新的阻断错误时判定回归', () => {
  const target = finding({ target: 'rule:R1' });
  const introduced = finding({ target: 'metric:M2', category: '来源缺失' });
  const gate = evaluateRepairQualityGate({
    before: [target],
    after: [introduced],
    targetFingerprints: [repairFindingFingerprint(target)],
  });
  assert.equal(gate.passed, false);
  assert.equal(gate.outcome, 'regressed');
  assert.equal(gate.introducedFingerprints.length, 1);
  assert.match(gate.reasons.join('；'), /新的必须修复问题/);
});

test('候选修改计划外文件时不得提升', () => {
  const target = finding({ target: 'rule:R1' });
  const gate = evaluateRepairQualityGate({
    before: [target],
    after: [],
    targetFingerprints: [repairFindingFingerprint(target)],
    changedPaths: ['rules/R1.md', 'objects/Customer.md'],
    allowedPaths: ['rules/R1.md'],
  });
  assert.equal(gate.passed, false);
  assert.deepEqual(gate.unexpectedPaths, ['objects/Customer.md']);
});

test('候选检查没有完整执行时不得提升候选', () => {
  const target = finding({ target: 'rule:R1' });
  const gate = evaluateRepairQualityGate({
    before: [target],
    after: [],
    targetFingerprints: [repairFindingFingerprint(target)],
    validationFailures: ['HTTP 429 insufficient_quota'],
  });
  assert.equal(gate.passed, false);
  assert.equal(gate.outcome, 'unresolved');
  assert.match(gate.reasons.join('；'), /复检未完整执行/);
});

test('展示文案轻微变化不会把同一问题误判为新增错误', () => {
  const before = finding({ target: 'rule:R1', message: '业务规则口径没有说明' });
  const after = finding({ target: 'rule:R1', message: '请补充这条业务规则的口径说明' });
  assert.equal(repairFindingFingerprint(before), repairFindingFingerprint(after));
});
