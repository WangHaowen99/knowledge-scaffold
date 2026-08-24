import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AdaptiveGenerationGate,
  dependencyHash,
  dynamicBatchSize,
  mapWithConcurrency,
  planGenerationUnits,
} from '../src/workflow/generation-planner.js';
import { buildOperationTargets } from '../src/workflow/m2.js';
import { confirmedFlowchart } from './fixtures/confirmed-flowchart.js';

test('动态批次在大上下文中扩大对象批次且保持输出上限', () => {
  const items = Array.from({ length: 25 }, (_, index) => ({ id: index, name: `对象${index}` }));
  assert.equal(dynamicBatchSize('object', items, 1_000_000), 12);
  assert.deepEqual(planGenerationUnits('object', items, 1_000_000).map((unit) => unit.items.length), [12, 12, 1]);
});

test('生成单元依赖指纹只随本单元输入变化', () => {
  const first = planGenerationUnits('knowledge', Array.from({ length: 16 }, (_, index) => ({ id: index })), 1_000_000);
  const second = planGenerationUnits('knowledge', Array.from({ length: 16 }, (_, index) => ({ id: index === 15 ? 99 : index })), 1_000_000);
  assert.equal(first[0].dependencyHash, second[0].dependencyHash);
  assert.notEqual(first[1].dependencyHash, second[1].dependencyHash);
});

test('下游单元指纹包含上游计划，避免对象变化后复用旧规则操作', () => {
  const unit = planGenerationUnits('knowledge', [{ id: 1 }], 1_000_000)[0];
  const before = dependencyHash({ unit: unit.dependencyHash, objectPlan: ['objects-v1'] });
  const after = dependencyHash({ unit: unit.dependencyHash, objectPlan: ['objects-v2'] });
  assert.notEqual(before, after);
});

test('受控并发保持输入顺序并限制同时执行数', async () => {
  let active = 0;
  let peak = 0;
  const values = await mapWithConcurrency([1, 2, 3, 4, 5], 2, async (value) => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, value % 2 ? 8 : 1));
    active -= 1;
    return value * 10;
  });
  assert.equal(peak, 2);
  assert.deepEqual(values, [10, 20, 30, 40, 50]);
});

test('operation 动态批次遵守结构化提交每批最多三个流程', () => {
  const items = Array.from({ length: 8 }, (_, index) => ({ id: index }));
  assert.deepEqual(planGenerationUnits('operation', items, 1_000_000).map((unit) => unit.items.length), [3, 3, 2]);
});

test('模型限流后自适应门闩把后续并发减半', async () => {
  const gate = new AdaptiveGenerationGate(4);
  gate.rateLimited(1);
  assert.equal(gate.currentLimit(), 2);
  let active = 0;
  let peak = 0;
  await Promise.all(Array.from({ length: 5 }, () => gate.run(async () => {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 4));
    active -= 1;
  })));
  assert.equal(peak, 2);
});

test('多步骤流程生成总编排和步骤 operation，并标记规则指标覆盖要求', () => {
  const flowchart = confirmedFlowchart([{
    id: 'marketing', title: '营销流程',
    steps: ['归集客户数据', '识别业务场景', '评估需求强度', '输出营销建议'],
  }]);
  const targets = buildOperationTargets(flowchart.diagrams);
  assert.equal(targets.length, 5);
  assert.equal(targets[0].id, 'process_marketing');
  assert.equal(targets[2].requiresRule, true);
  assert.equal(targets[3].requiresMetric, true);
});
