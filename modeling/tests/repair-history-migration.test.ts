import assert from 'node:assert/strict';
import test from 'node:test';

import { assessHistoricalRepair } from '../src/ai-construction/repair-history-migration.js';
import type { CheckFinding } from '../src/workflow/package-check.js';

const finding = (target: string): CheckFinding => ({
  id: target,
  source: 'semantic',
  severity: '错误',
  category: '口径',
  target,
  message: `${target} 口径不完整`,
});

test('历史记录只在 before/after 事实齐全时回算质量', () => {
  assert.equal(assessHistoricalRepair([finding('rule:R1')], null).qualityOutcome, 'unknown_legacy');
  assert.equal(assessHistoricalRepair([finding('rule:R1')], []).qualityOutcome, 'resolved');
  assert.equal(assessHistoricalRepair(
    [finding('rule:R1'), finding('rule:R2')],
    [finding('rule:R2')],
  ).qualityOutcome, 'improved');
  assert.equal(assessHistoricalRepair(
    [finding('rule:R1')],
    [finding('rule:R1'), finding('rule:R3')],
  ).qualityOutcome, 'regressed');
});
