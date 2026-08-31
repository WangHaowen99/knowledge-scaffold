import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { type JourneyPlan, validateJourneyPlan } from '../scripts/browser-journey.js';

const planUrl = new URL('../browser-acceptance/real-modeling-evaluation.json', import.meta.url);

test('真实项目建模评测计划只通过浏览器动作完成状态变更', async () => {
  const plan = JSON.parse(await readFile(planUrl, 'utf8')) as JourneyPlan;
  validateJourneyPlan(plan);

  const mutationActions = new Set(['click', 'fill', 'upload', 'guidedConstruction']);
  const usedMutationActions = new Set(plan.steps
    .filter((step) => mutationActions.has(step.action))
    .map((step) => step.action));

  assert.deepEqual([...usedMutationActions].sort(), [...mutationActions].sort());
  assert.equal(plan.steps.some((step) => ![
    'goto', 'click', 'fill', 'upload', 'waitFor', 'assertText', 'assertNotText',
    'assertCount', 'assertLayout', 'guidedConstruction', 'screenshot',
  ].includes(step.action)), false);
});

test('真实项目建模评测计划覆盖石油炼化 0828 参照、0831 建模和三条正式评测用例', async () => {
  const plan = JSON.parse(await readFile(planUrl, 'utf8')) as JourneyPlan;
  const clicks = plan.steps.filter((step) => step.action === 'click').map((step) => step.text);
  const caseAdds = clicks.filter((text) => text === '添加').length;

  assert.ok(plan.steps.some((step) => step.action === 'click' && step.text === '石油炼化0828' && step.exact));
  assert.ok(plan.steps.some((step) => step.action === 'fill' && step.selector === '#pn'
    && step.valueEnv === 'E2E_PROJECT_NAME'));
  assert.ok(plan.steps.some((step) => step.action === 'guidedConstruction'));
  assert.equal(caseAdds, 3);
  assert.ok(clicks.includes('开始正式评测'));
  assert.ok(plan.steps.some((step) => step.action === 'assertCount'
    && step.selector === '.wb-eval-result' && step.expectedCount === 3));
});
