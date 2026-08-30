import assert from 'node:assert/strict';
import test from 'node:test';

import { validateJourneyPlan } from '../scripts/browser-journey.js';
import {
  browserAcceptanceCases,
  requiredBrowserAcceptanceIds,
} from '../browser-acceptance/cases.js';

test('浏览器黑盒套件逐一覆盖文档冻结的 61 个用例编号', () => {
  const actual = browserAcceptanceCases.map((item) => item.id);
  assert.equal(actual.length, 61);
  assert.equal(new Set(actual).size, 61);
  assert.deepEqual([...actual].sort(), [...requiredBrowserAcceptanceIds].sort());
});

test('61 条用例只包含声明式浏览器动作且都能通过计划校验', () => {
  for (const acceptanceCase of browserAcceptanceCases) {
    assert.doesNotThrow(() => validateJourneyPlan(acceptanceCase.plan), acceptanceCase.id);
    const serialized = JSON.stringify(acceptanceCase.plan);
    assert.doesNotMatch(serialized, /(?:\/api\/|fetch\(|page\.evaluate|localStorage|sessionStorage|cookie|database|docker|ssh)/iu, acceptanceCase.id);
  }
});

test('恢复类用例在严格模式下至少连续执行三次', () => {
  const recovery = browserAcceptanceCases.filter((item) => item.id.startsWith('E2E-REL-'));
  for (const item of recovery) {
    if (['E2E-REL-06'].includes(item.id)) continue;
    assert.ok((item.strictRepeats ?? 1) >= 3, item.id);
  }
});
