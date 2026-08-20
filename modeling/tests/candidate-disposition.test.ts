import assert from 'node:assert/strict';
import test from 'node:test';
import type { ArtifactCandidate } from '../src/types.js';
import {
  dispositionForObjectAndOperationCandidates,
  planArtifactCandidates,
  validateKnowledgeCandidateDispositions,
} from '../src/workflow/candidate-disposition.js';

const candidate = (overrides: Partial<ArtifactCandidate> = {}): ArtifactCandidate => ({
  kind: 'rule', name: '装置负荷上限', rationale: '材料证据', sources: ['Device'], readiness: 'extractable',
  ...overrides,
});

test('候选计划会合并重复项并把待澄清项留出首版生成', () => {
  const plan = planArtifactCandidates([
    candidate(),
    candidate({ name: '装置-负荷上限', sources: ['manual.md'] }),
    candidate({ kind: 'metric', name: '月度利润', readiness: 'needs_clarification' }),
  ]);
  assert.equal(plan.selected.length, 1);
  assert.equal(plan.dispositions.filter((item) => item.status === 'merged').length, 1);
  assert.equal(plan.dispositions.filter((item) => item.status === 'needs_clarification').length, 1);
});

test('未进入首版范围的全文候选保留为延期处置而不是丢弃', () => {
  const selected = candidate();
  const later = candidate({ name: '局部温度指标', kind: 'metric', sources: ['manual.md'] });
  const plan = planArtifactCandidates([selected, later], [selected]);
  assert.deepEqual(plan.selected.map((item) => item.name), ['装置负荷上限']);
  assert.equal(plan.dispositions.find((item) => item.name === '局部温度指标')?.status, 'deferred');
});

test('入选候选必须提交可验证的处置记录', () => {
  const plan = planArtifactCandidates([candidate()]);
  assert.ok(validateKnowledgeCandidateDispositions(plan.selected, [], [], []).some((item) => /没有生成/.test(item)));

  const rule = { key: 'rule_load_limit', title: '负荷上限', content: '不得超过上限' };
  assert.deepEqual(validateKnowledgeCandidateDispositions(plan.selected, [rule], [], [{
    candidateId: plan.selected[0].id,
    kind: 'rule',
    name: plan.selected[0].name,
    status: 'generated',
    resourceKeys: [rule.key],
    reason: '已根据表格字段生成规则。',
  }]), []);
});

test('对象和 operation 候选由确定性资源或确认流程生成结果覆盖', () => {
  const dispositions = dispositionForObjectAndOperationCandidates([
    { kind: 'object', name: '客户', rationale: '表格', sources: ['customers'], readiness: 'extractable' },
    { kind: 'operation', name: '查询客户', rationale: '流程', sources: ['流程图'], readiness: 'extractable' },
  ], [
    {
      key: 'customer', title: '客户', description: '客户', fields: [],
      sourceRefs: [{ kind: 'table', table: 'customers' }],
    },
  ], [{
    key: 'query_customer', title: '查询客户', description: '查询客户', inputs: [], outputs: [], flow: [], relatedObjects: [],
  }]);
  assert.deepEqual(dispositions.map((item) => [item.status, item.resourceKeys]), [
    ['generated', ['customer']],
    ['generated', ['query_customer']],
  ]);
});
