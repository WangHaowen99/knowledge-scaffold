import assert from 'node:assert/strict';
import test from 'node:test';
import {
  INVENTORY_FACT_FIELDS,
  createProductInventoryCapture,
  normalizeProductInventory,
} from '../src/workflow/product-inventory-capture.js';

function factItem(overrides: Record<string, unknown> = {}) {
  return {
    canonicalItemId: 'product-a',
    name: '合成产品 A',
    ruleRefs: ['rule-a'],
    sourceRecordIds: ['source-a'],
    sourceLocator: 'manual#a',
    evidenceStatus: 'complete',
    applicableSubjects: ['合成主体'],
    triggerScenarios: ['合成场景'],
    businessNeeds: ['合成需求'],
    admissionConditions: ['合成准入条件'],
    exclusionConditions: ['合成排除条件'],
    structuredGaps: [],
    ...overrides,
  };
}

test('来源字段齐备的产品事实判为完整并标注完备性契约', () => {
  const [item] = normalizeProductInventory([factItem()]);
  assert.equal(item.completenessContract, 'source-fields-v1');
  assert.equal(item.evidenceStatus, 'complete');
  assert.equal(item.processingStatus, 'ready');
  assert.deepEqual(item.structuredGaps, []);
  assert.equal(item.insufficiencyReason, undefined);
});

test('任一来源字段缺失都由平台反推缺口，不采信模型自报的完整状态', () => {
  for (const field of INVENTORY_FACT_FIELDS) {
    const [item] = normalizeProductInventory([factItem({
      [field]: [], evidenceStatus: 'complete', structuredGaps: [],
    })]);
    assert.deepEqual(item.structuredGaps, [field], `${field} 缺失时应反推出结构化缺口`);
    assert.equal(item.evidenceStatus, 'insufficient', `${field} 缺失时不得判为资料完整`);
    assert.equal(item.processingStatus, 'blocked');
    assert.ok(item.insufficiencyReason?.includes(field));
  }
});

test('占位说明不算事实，会被清空并计入结构化缺口', () => {
  const [item] = normalizeProductInventory([factItem({
    triggerScenarios: ['资料未提供'], admissionConditions: ['N/A'],
  })]);
  assert.deepEqual(item.triggerScenarios, []);
  assert.deepEqual(item.admissionConditions, []);
  assert.deepEqual(item.structuredGaps, ['triggerScenarios', 'admissionConditions']);
  assert.equal(item.evidenceStatus, 'insufficient');
});

test('模型自报的非字段类缺口降级为语义警告，不冒充来源字段缺失', () => {
  const [item] = normalizeProductInventory([factItem({
    structuredGaps: ['阈值口径需业务确认'],
  })]);
  assert.deepEqual(item.structuredGaps, []);
  assert.deepEqual(item.semanticWarnings, ['阈值口径需业务确认']);
  assert.equal(item.evidenceStatus, 'complete');
});

test('缺少标识或来源的条目直接报错，不能靠丢弃该项让来源看起来完整', () => {
  assert.throws(
    () => normalizeProductInventory([factItem(), factItem({ canonicalItemId: '', name: '无标识项' })]),
    /不能以删除该项代替修正/u,
  );
  assert.throws(() => normalizeProductInventory([factItem({ sourceRecordIds: [] })]), /缺少标识/u);
  assert.throws(() => normalizeProductInventory('not-an-array'), /必须为数组/u);
});

test('把规范项 ID、规则 ID 或证据位置混进原始记录会被拒绝', () => {
  assert.throws(() => normalizeProductInventory([factItem({ sourceRecordIds: ['product-a'] })]), /混入sourceRecordIds/u);
  assert.throws(() => normalizeProductInventory([factItem({ sourceRecordIds: ['rule-a'] })]), /混入sourceRecordIds/u);
  assert.throws(() => normalizeProductInventory([factItem({ sourceRecordIds: ['manual#a'] })]), /混入sourceRecordIds/u);
});

test('多条原始记录归并时由平台补齐归并组标识', () => {
  const [merged] = normalizeProductInventory([factItem({ sourceRecordIds: ['source-a', 'source-b'] })]);
  assert.equal(merged.mergeGroupId, 'merge:product-a');
  const [single] = normalizeProductInventory([factItem()]);
  assert.equal(single.mergeGroupId, undefined);
});

test('分批提交的产品事实在收尾时汇总，同批号改写内容会被拒绝', async () => {
  const capture = createProductInventoryCapture();
  const first = await capture.tool.execute('call-1', { batchId: 'batch-1', items: [factItem()] });
  assert.deepEqual(first.details, { batchCount: 1, itemCount: 1 });
  await capture.tool.execute('call-2', {
    batchId: 'batch-2',
    items: [factItem({ canonicalItemId: 'product-b', name: '合成产品 B', sourceRecordIds: ['source-b'] })],
  });
  await assert.rejects(
    () => capture.tool.execute('call-3', { batchId: 'batch-1', items: [factItem({ name: '改名后的产品 A' })] }),
    /不得覆盖已接收批次/u,
  );
  const items = capture.finish('extracted', undefined);
  assert.deepEqual(items.map((item) => item.canonicalItemId), ['product-a', 'product-b']);
});

test('产品事实处置必须显式声明，且声明要与已提交内容一致', () => {
  const empty = createProductInventoryCapture();
  assert.throws(() => empty.finish(undefined, undefined), /必须声明 productInventoryDisposition/u);
  assert.throws(() => empty.finish('extracted', undefined), /尚未提交逐项产品数据/u);
  assert.deepEqual(empty.finish('not_applicable', undefined), []);

  const filled = createProductInventoryCapture();
  assert.throws(() => filled.finish('not_applicable', [factItem()]), /不能声明not_applicable/u);
});
