import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildScriptGenerationRequirement,
  identifyScriptCandidates,
  parseScriptContractResponse,
  parseScriptDecisionResponse,
} from '../src/ai-construction/deterministic-capability.js';
import type { SpecOperation } from '../src/types.js';

const operation = (
  key: string,
  title: string,
  flow: string[],
  extra: Partial<SpecOperation> = {},
): SpecOperation => ({
  key, title, description: title, inputs: [], outputs: [], flow, relatedObjects: [], ...extra,
});

test('只把需要稳定重复执行的 operation 建议为脚本候选', () => {
  const candidates = identifyScriptCandidates([
    operation('CalculateYield', '计算产品收率', ['读取投入量', '按公式计算收率'], {
      outputs: [{ field: 'yield_rate', cnName: '收率', type: 'number' }],
    }),
    operation('AllocateVolume', '分配批次加工量', ['按产能排序', '分配各装置加工量']),
    operation('ExpertReview', '专家综合研判', ['结合行业经验判断风险', '给出开放式建议']),
    operation('ExistingScript', '已有确定性脚本', ['按公式计算'], {
      script: { key: 'ExistingScriptRunner', required: true },
    }),
  ], []);

  assert.deepEqual(candidates.map((item) => [item.operationKey, item.confidence]), [
    ['CalculateYield', 'high'],
    ['AllocateVolume', 'medium'],
  ]);
  assert.match(candidates[0].reason, /数值输出|计算/);
  assert.match(candidates[0].suggestedContract ?? '', /输入：/);
  assert.match(candidates[0].suggestedContract ?? '', /输出：yield_rate\(number\)/);
  assert.match(candidates[0].suggestedContract ?? '', /同一合法输入重复执行结果一致/);
  assert.doesNotMatch(candidates.map((item) => item.operationKey).join(','), /ExpertReview/);
  assert.doesNotMatch(candidates.map((item) => item.operationKey).join(','), /ExistingScript/);
});

test('脚本契约优先接受系统候选，只有候选不合适时才要求补充输入', () => {
  const selected = identifyScriptCandidates([
    operation('CalculateScore', '计算评分', ['读取权重', '按规则汇总'], {
      inputs: [
        { param: 'items', cnName: '明细', type: 'array', required: true },
        { param: 'weight', cnName: '权重', type: 'number', required: false },
      ],
      outputs: [{ field: 'score', cnName: '评分', type: 'number' }],
    }),
  ], []);
  const suggestion = selected[0]?.suggestedContract;
  assert.ok(suggestion);

  const contracts = parseScriptContractResponse({
    answers: [{ questionId: 'script_contract_1', selected: [suggestion] }],
  }, selected);

  assert.equal(contracts[0]?.contract, suggestion);
  assert.match(suggestion, /items\(array，必填\)/);
  assert.match(suggestion, /weight\(number，可选\)/);
});

test('脚本决策只接受候选 operation，且暂不生成不能与具体项同时选择', () => {
  const candidates = identifyScriptCandidates([
    operation('CalculateYield', '计算产品收率', ['按公式计算收率']),
  ], []);

  assert.deepEqual(parseScriptDecisionResponse({
    answers: [{ questionId: 'script_candidates', selected: ['CalculateYield'] }],
  }, candidates).selectedOperationKeys, ['CalculateYield']);
  assert.deepEqual(parseScriptDecisionResponse({
    answers: [{ questionId: 'script_candidates', selected: ['__none__'] }],
  }, candidates).selectedOperationKeys, []);
  assert.throws(() => parseScriptDecisionResponse({
    answers: [{ questionId: 'script_candidates', selected: ['CalculateYield', '__none__'] }],
  }, candidates), /不能同时/);
  assert.throws(() => parseScriptDecisionResponse({
    answers: [{ questionId: 'script_candidates', selected: ['UnknownOperation'] }],
  }, candidates), /不属于当前候选/);
});

test('脚本生成前必须为每个 operation 明确输入输出契约和测试案例', () => {
  const selected = [
    { operationKey: 'CalculateYield', title: '计算产品收率', confidence: 'high' as const, reason: '包含数值输出' },
    { operationKey: 'AllocateVolume', title: '分配批次加工量', confidence: 'medium' as const, reason: '包含分配步骤' },
  ];
  const contracts = parseScriptContractResponse({
    answers: [
      {
        questionId: 'script_contract_1', selected: [],
        other: '输入：投入量、产品量；输出：收率；测试：投入100、产品90，期望0.9',
      },
      {
        questionId: 'script_contract_2', selected: [],
        other: '输入：批次量、装置产能；输出：各装置分配量；测试：总量100，期望分配和为100',
      },
    ],
  }, selected);

  assert.deepEqual(contracts.map((item) => item.operationKey), ['CalculateYield', 'AllocateVolume']);
  const requirement = buildScriptGenerationRequirement(selected, contracts);
  assert.match(requirement, /CalculateYield/);
  assert.match(requirement, /投入100、产品90，期望0\.9/);
  assert.match(requirement, /不得访问网络/);
  assert.throws(() => parseScriptContractResponse({
    answers: [{ questionId: 'script_contract_1', other: '只有第一项' }],
  }, selected), /AllocateVolume/);
});
