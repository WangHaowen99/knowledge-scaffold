import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  createFauxCore,
  fauxAssistantMessage,
  fauxText,
  fauxToolCall,
} from '@earendil-works/pi-ai/providers/faux';
import { diagnoseEvaluationFailures } from '../src/evaluation/failure-diagnosis.js';
import {
  hasConfirmedOptimizableDiagnoses,
  listFailureDiagnoses,
  markFailureDiagnosesForRepair,
  replaceFailureDiagnoses,
  resolveFailureDiagnosis,
} from '../src/evaluation/failure-diagnosis-repo.js';
import {
  decideScriptOptimization,
  normalizeOptimizationSuggestionTargets,
  OPTIMIZATION_APPLY_MAX_OUTPUT_TOKENS,
  OPTIMIZATION_APPLY_TIMEOUT_MS,
  optimizationSuggestionDedupeKey,
  OPTIMIZATION_PROPOSAL_MAX_OUTPUT_TOKENS,
  OPTIMIZATION_PROPOSAL_TIMEOUT_MS,
  proposeOptimizations,
  validateOptimizationSuggestions,
} from '../src/evaluation/optimizer.js';
import type { LlmDeps } from '../src/llm/provider.js';
import { createProject } from '../src/projects/repo.js';
import type { EvalResult } from '../src/workflow/m3-repo.js';
import { addResult, createRound, getResultTrace } from '../src/workflow/m3-repo.js';
import { insertSuggestions, listSuggestions } from '../src/workflow/m4-repo.js';

function result(id: string, question: string, groundTruth: string): EvalResult {
  return {
    id,
    roundId: 'round-1',
    testcaseId: `tc-${id}`,
    question,
    groundTruth,
    finalAnswer: '回答不正确',
    verdict: 'fail',
    reason: '关键数值不一致',
    overridden: false,
    createdAt: '2026-07-16T00:00:00.000Z',
  };
}

test('优化任务限制模型输出和单次等待时间', () => {
  assert.equal(OPTIMIZATION_PROPOSAL_MAX_OUTPUT_TOKENS, 12_000);
  assert.equal(OPTIMIZATION_APPLY_MAX_OUTPUT_TOKENS, 32_000);
  assert.equal(OPTIMIZATION_PROPOSAL_TIMEOUT_MS, 180_000);
  assert.equal(OPTIMIZATION_APPLY_TIMEOUT_MS, 600_000);
});

test('同一根因和目标的优化建议不因标题改写而重复', () => {
  const base = {
    title: '修复统一计算入口', detail: '使用确定性脚本。', kind: 'modify_script', target: 'calculate_result',
    diagnosisIds: ['dg-b', 'dg-a'], evidence: [], generalizationPlan: '', validationPlan: '',
  };
  assert.equal(optimizationSuggestionDedupeKey(base), optimizationSuggestionDedupeKey({
    ...base, title: '调整计算脚本的权威执行路径', diagnosisIds: ['dg-a', 'dg-b'],
  }));
  assert.notEqual(optimizationSuggestionDedupeKey(base), optimizationSuggestionDedupeKey({
    ...base, target: 'validate_result',
  }));
});

const scriptedSpec = () => ({
  id: 'scripted-demo', name: 'scripted-demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
  defaultProviderAlias: 'data', objects: [], rules: [], metrics: [],
  operations: [{
    key: '加氢装置负荷校验', title: '加氢装置负荷校验', description: '校验装置负荷。',
    inputs: [], outputs: [], flow: ['执行 script:OperationScript_6c818957d1a1'], relatedObjects: [],
    competencyQuestion: '如何校验加氢装置负荷？',
    script: { key: 'OperationScript_6c818957d1a1', required: true },
  }],
  scripts: [{
    key: 'OperationScript_6c818957d1a1', title: '加氢装置负荷计算', description: '执行确定性负荷计算。',
    boundOperation: '加氢装置负荷校验', runtime: 'python' as const,
    entrypoint: 'calculate_load.py', timeoutSeconds: 30, parameters: [], outputs: [], boundaries: [],
    sourceCode: 'print("{}")',
  }],
});

const calculationScriptSpec = () => {
  const spec = scriptedSpec();
  spec.operations[0] = {
    ...spec.operations[0], key: 'calculate_load_operation', title: '负荷计算',
    competencyQuestion: '如何按运行时字段汇总？', script: { key: 'calculate_load', required: true },
  };
  spec.scripts[0] = {
    ...spec.scripts[0], key: 'calculate_load', title: '负荷计算脚本',
    boundOperation: 'calculate_load_operation', entrypoint: 'calculate_load.py',
  };
  return spec;
};

test('优化建议目标将 key:title 和唯一标题规范化为真实资源 key', () => {
  const spec = scriptedSpec();
  const suggestions = normalizeOptimizationSuggestionTargets([{
    title: '修复负荷校验路由', detail: '让已给定输入进入脚本。', kind: 'modify_operation',
    target: '加氢装置负荷校验:加氢装置负荷校验',
  }, {
    title: '修复脚本计算', detail: '补齐运行时输入解析。', kind: 'modify_script',
    target: '加氢装置负荷计算',
  }], spec);

  assert.equal(suggestions[0].target, '加氢装置负荷校验');
  assert.equal(suggestions[1].target, 'OperationScript_6c818957d1a1');
});

test('修改建议的目标无法定位时在进入模型修订前拒绝', () => {
  assert.throws(() => normalizeOptimizationSuggestionTargets([{
    title: '修复不存在的操作', detail: '修复。', kind: 'modify_operation', target: 'missing:missing',
  }], scriptedSpec()), /无法定位|missing/);
});

test('已有脚本但执行轨迹未调用时优先修复 operation 路由', () => {
  const diagnosis = {
    category: 'needs_script' as const,
    title: '复杂计算未执行确定性脚本',
    summary: '加氢装置负荷校验已绑定脚本，但回答过程没有调用。',
    evidence: ['execute_script 调用次数 = 0'],
    relatedQuestions: ['如何校验加氢装置负荷？'],
    suggestedAction: '修复执行路由。',
  } as any;

  assert.deepEqual(decideScriptOptimization(scriptedSpec(), diagnosis), {
    action: 'fix_invocation',
    operationKey: '加氢装置负荷校验',
    scriptKey: 'OperationScript_6c818957d1a1',
    reason: '已绑定脚本，但执行轨迹显示未调用，应先修复 operation 路由和参数传递。',
  });
});

test('已有脚本能力不足时优先修改原脚本，职责独立时才新增', () => {
  const base = {
    category: 'needs_script' as const,
    title: '加氢装置负荷校验计算不完整',
    summary: '已有脚本没有处理缺失值。',
    evidence: ['execute_script 已调用，但返回字段校验错误'],
    relatedQuestions: ['如何校验加氢装置负荷？'],
    suggestedAction: '补齐原脚本输入解析。',
  } as any;
  assert.equal(decideScriptOptimization(scriptedSpec(), base)?.action, 'modify_existing');
  assert.equal(decideScriptOptimization(scriptedSpec(), base)?.scriptKey, 'OperationScript_6c818957d1a1');

  const independent = {
    ...base,
    summary: '新能力属于独立职责，与原脚本输入输出契约不兼容，需隔离执行。',
  };
  assert.equal(decideScriptOptimization(scriptedSpec(), independent)?.action, 'add_new');
});

test('建议模型将未调用的已有脚本误判为新增时会获得精确决策后重交', async () => {
  const failed = result('script-not-invoked', '如何校验加氢装置负荷？', '返回确定性校验结果');
  const diagnosis = {
    id: 'dg-script-not-invoked', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'needs_script' as const, title: '加氢装置负荷校验未调用脚本',
    summary: '已有脚本已绑定，但运行轨迹中未执行。', evidence: ['execute_script 调用次数 = 0'],
    confidence: 'high' as const, blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '先修复 operation 路由。',
    createdAt: '2026-07-26T00:00:00.000Z',
  };
  const suggestion = (kind: string, target: string) => ({
    title: '修复负荷校验脚本路径', detail: '保证结构化输入进入确定性脚本。', kind, target,
    diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
    generalizationPlan: '所有负荷校验都走同一脚本路径。',
    validationPlan: '使用缺失字段、字段顺序变化和边界输入验证调用与阻断行为。',
  });
  const core = createFauxCore({ provider: 'script-decision-retry', models: [{ id: 'script-decision-retry' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [
      suggestion('add_script', '加氢装置负荷校验'),
    ] })]),
    fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [
      suggestion('modify_operation', '加氢装置负荷校验:加氢装置负荷校验'),
    ] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const suggestions = await proposeOptimizations({
    id: 'project-1', owner: 'tester', name: '通用流程建模', domain: '生产', background: '', stage: 'optimize',
    createdAt: '2026-07-26T00:00:00.000Z', updatedAt: '2026-07-26T00:00:00.000Z',
  }, scriptedSpec(), [failed], llm, [], [diagnosis]);

  assert.equal(core.state.callCount, 2);
  assert.equal(suggestions[0].kind, 'modify_operation');
  assert.equal(suggestions[0].target, '加氢装置负荷校验');
});

test('优化建议模型只输出正文时会被强制恢复为结构化提交', async () => {
  const failed = result('prose-first', '如何校验加氢装置负荷？', '返回确定性校验结果');
  const diagnosis = {
    id: 'dg-prose-first', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '脚本负荷计算口径错误',
    summary: '现有脚本没有按装置能力聚合结构化输入。', evidence: ['执行轨迹显示脚本负荷聚合口径不完整'],
    confidence: 'high' as const, blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '修复现有脚本聚合口径。',
    createdAt: '2026-09-01T00:00:00.000Z',
  };
  const core = createFauxCore({ provider: 'optimization-prose-first', models: [{ id: 'optimization-prose-first' }] });
  core.setResponses([
    fauxAssistantMessage([fauxText('我先分析根因和修改范围。')]),
    fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [{
      title: '修复装置负荷聚合脚本', detail: '按结构化批次输入和装置能力统一聚合并输出校验结果。',
      kind: 'modify_script', target: 'OperationScript_6c818957d1a1', diagnosisIds: [diagnosis.id],
      evidence: diagnosis.evidence, generalizationPlan: '支持任意批次数量和装置能力输入。',
      validationPlan: '使用空输入、字段顺序变化和满量程场景验证聚合性质与明确阻断。',
    }] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const suggestions = await proposeOptimizations({
    id: 'project-1', owner: 'tester', name: '通用流程建模', domain: '生产', background: '', stage: 'optimize',
    createdAt: '2026-09-01T00:00:00.000Z', updatedAt: '2026-09-01T00:00:00.000Z',
  }, scriptedSpec(), [failed], llm, [], [diagnosis]);

  assert.equal(core.state.callCount, 2);
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].kind, 'modify_script');
  assert.equal(suggestions[0].target, 'OperationScript_6c818957d1a1');
});

test('相同问题的标准答案冲突时阻止生成知识包优化建议', async () => {
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [
      result('r1', '装置负荷是多少？', '负荷为 41.25%'),
      result('r2', ' 装置负荷 是多少？\n', '负荷为 63.75%'),
    ],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
    },
  });

  assert.equal(report.canProposeOptimizations, false);
  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'testcase_conflict');
  assert.equal(report.diagnoses[0].blocking, true);
  assert.deepEqual(report.diagnoses[0].resultIds.sort(), ['r1', 'r2']);
  assert.match(report.diagnoses[0].evidence.join('\n'), /41\.25%/);
  assert.match(report.diagnoses[0].evidence.join('\n'), /63\.75%/);
});

test('运行错误单独定位且不生成知识包优化建议', async () => {
  const unauthorized = result('runtime', '查询装置负荷', '负荷为 41.25%');
  unauthorized.finalAnswer = '';
  unauthorized.verdict = 'error';
  unauthorized.reason = '运行失败：Unauthorized';

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [unauthorized],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
    },
  });

  assert.equal(report.canProposeOptimizations, false);
  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'runtime_error');
  assert.equal(report.diagnoses[0].repairScope, 'runtime');
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.match(report.diagnoses[0].evidence.join('\n'), /Unauthorized/);
});

test('知识包脚本运行错误直接生成可优化的脚本诊断', async () => {
  const failed = result('script-runtime', '计算多批次生产计划的装置负荷', '返回精确负荷');
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [failed],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
    },
    traces: {
      'script-runtime': {
        toolCalls: ['execute_script'],
        toolFailures: [{
          toolName: 'execute_script',
          error: "TypeError: query() got an unexpected keyword argument 'filters'",
        }],
      },
    },
  });

  const diagnosis = report.diagnoses.find((item) => item.resultIds.includes('script-runtime'));
  assert.ok(diagnosis);
  assert.equal(diagnosis.category, 'runtime_error');
  assert.equal(diagnosis.repairScope, 'script');
  assert.equal(diagnosis.packageOptimizable, true);
  assert.equal(diagnosis.blocking, false);
  assert.match(diagnosis.evidence.join('\n'), /unexpected keyword argument 'filters'/);
  assert.equal(report.canProposeOptimizations, true);
});

test('评测等待用户输入时交给语义诊断判断是否为知识包路由问题', async () => {
  const needsInput = result('needs-input', '请按已给定的处理模式计算装置负荷', '输出装置负荷和过载结论');
  needsInput.finalAnswer = '';
  needsInput.verdict = 'error';
  needsInput.reason = '运行失败：评测用例缺少运行所需输入：请再次选择处理模式';

  const core = createFauxCore({ provider: 'input-diagnosis', models: [{ id: 'input-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'routing_issue', title: '已给定输入未从问题中传入操作',
    summary: '用例已包含处理模式，运行时仍重复请求用户选择。',
    evidence: ['问题中已给定处理模式', '运行结果再次请求选择处理模式'], confidence: 'high',
    blocking: false, repairScope: 'package', packageOptimizable: true, resultIds: ['needs-input'],
    relatedQuestions: ['请按已给定的处理模式计算装置负荷'],
    suggestedAction: '调整 operation 输入绑定，优先从用户问题提取已给定值。',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [needsInput],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'routing_issue');
  assert.equal(report.diagnoses[0].repairScope, 'package');
  assert.equal(report.diagnoses[0].packageOptimizable, true);
  assert.equal(report.canProposeOptimizations, true);
});

test('失败诊断首次只输出正文时会补交结构化诊断', async () => {
  const failed = result('protocol-recovery', '请计算处理单元整体负荷', '整体负荷为 95%');
  const core = createFauxCore({ provider: 'diagnosis-protocol-recovery', models: [{ id: 'diagnosis-protocol-recovery' }] });
  core.setResponses([
    fauxAssistantMessage('已完成失败原因分析，主要问题是计算口径不完整。'),
    fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
      category: 'caliber_issue', title: '整体负荷口径不完整',
      summary: '回答只计算了部分输入分量。', evidence: ['未汇总全部输入分量'], confidence: 'high',
      blocking: false, repairScope: 'package', packageOptimizable: true,
      resultIds: ['protocol-recovery'], relatedQuestions: ['请计算处理单元整体负荷'],
      suggestedAction: '统一整体负荷的汇总口径。',
    }] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(core.state.callCount, 2);
  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'caliber_issue');
  assert.equal(report.canProposeOptimizations, true);
});

test('依赖评测答案反推的语义诊断不得进入知识包优化', async () => {
  const failed = result('oracle-derived-diagnosis', '如何计算通用汇总指标？', '按确认的业务口径计算。');
  const core = createFauxCore({ provider: 'oracle-derived-diagnosis', models: [{ id: 'oracle-derived-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'caliber_issue', title: '汇总公式与标准答案口径不一致',
    summary: '现有公式与 ground truth 给出的结果不同。',
    evidence: ['全部用例的标准答案都使用另一公式'], confidence: 'high',
    blocking: false, repairScope: 'package', packageOptimizable: true,
    resultIds: ['oracle-derived-diagnosis'], relatedQuestions: ['如何计算通用汇总指标？'],
    suggestedAction: '改成与标准答案一致的公式。',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'judge_issue');
  assert.equal(report.diagnoses[0].repairScope, 'judge');
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.equal(report.canProposeOptimizations, false);
  assert.match(report.diagnoses[0].summary, /独立证据/);
});

test('诊断有独立执行证据时只移除附带的用例结果描述', async () => {
  const failed = result('independent-trace-diagnosis', '如何执行通用计算脚本？', '按脚本契约执行。');
  const core = createFauxCore({ provider: 'independent-trace-diagnosis', models: [{ id: 'independent-trace-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'routing_issue', title: '脚本读取字段契约不匹配',
    summary: '执行轨迹显示脚本读取了不存在的字段。该问题导致全部4个用例失败。',
    evidence: ['script:calculate_result 读取字段 legacy_name，但 ProviderResult.data 只提供 canonical_name', '全部4个用例均未通过'],
    confidence: 'high', blocking: false, repairScope: 'script', packageOptimizable: true,
    resultIds: ['independent-trace-diagnosis'], relatedQuestions: ['如何执行通用计算脚本？'],
    suggestedAction: '按 ProviderResult.data 的字段契约修复脚本读取逻辑。',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses[0].repairScope, 'script');
  assert.equal(report.diagnoses[0].packageOptimizable, true);
  assert.deepEqual(report.diagnoses[0].evidence, [
    'script:calculate_result 读取字段 legacy_name，但 ProviderResult.data 只提供 canonical_name',
  ]);
  assert.doesNotMatch(report.diagnoses[0].summary, /用例|失败/);
  assert.equal(report.canProposeOptimizations, true);
});

test('评测问题确实缺少必填决策时标记为用例缺口而不修改知识包', async () => {
  const needsInput = result('testcase-input-gap', '请计算本月装置负荷', '按用户选择的收率场景计算');
  needsInput.finalAnswer = '';
  needsInput.verdict = 'error';
  needsInput.reason = '运行失败：评测用例缺少运行所需输入：请选择收率场景';

  const core = createFauxCore({ provider: 'testcase-gap-diagnosis', models: [{ id: 'testcase-gap-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'testcase_gap', title: '评测用例未给出必需场景',
    summary: '问题未指定收率场景，无法唯一计算。',
    evidence: ['问题原文未包含收率场景', '运行时请求用户选择收率场景'], confidence: 'high',
    blocking: false, repairScope: 'package', packageOptimizable: true, resultIds: ['testcase-input-gap'],
    relatedQuestions: ['请计算本月装置负荷'], suggestedAction: '补充用例中的收率场景后重新评测。',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [needsInput],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'testcase_gap');
  assert.equal(report.diagnoses[0].repairScope, 'testcase');
  assert.equal(report.diagnoses[0].blocking, true);
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.equal(report.canProposeOptimizations, false);
});

test('资料评估中的相关缺口进入失败诊断并阻止编造公式', async () => {
  const failed = result('economic', '整体月度经济效益如何？', '经济效益为 209925317 元');
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [failed],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
    },
    qualityGaps: [{
      kind: '口径不明',
      target: '经济效益计算口径',
      message: '缺少材料采购单价和处理成本定义，无法计算经济效益。',
      how: '补充材料价格字段并确认目标函数。',
    }],
  });

  const gap = report.diagnoses.find((item) => item.category === 'data_gap');
  assert.ok(gap);
  assert.equal(gap.blocking, true);
  assert.equal(gap.repairScope, 'data');
  assert.equal(gap.packageOptimizable, false);
  assert.match(gap.evidence.join('\n'), /材料采购单价/);
  assert.equal(report.canProposeOptimizations, false);
});

test('多批次精确计算且没有脚本执行时要求生成确定性脚本', async () => {
  const failed = result(
    'complex',
    '第1批2万吨，第2批3万吨，第3批4万吨，请统计三套装置负荷和整体经济效益。',
    '负荷分别为62.50%、48.25%、75.00%，经济效益123456元',
  );
  failed.finalAnswer = '负荷分别为61.00%、19.50%、72.00%，经济效益65432元';

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [failed],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
    },
    traces: {
      complex: {
        runId: 'run-1', sessionId: 'session-1',
        toolCalls: ['ontology_context', 'business_data_search', 'business_data_search', 'business_data_search'],
      },
    },
  });

  const diagnosis = report.diagnoses.find((item) => item.category === 'needs_script');
  assert.ok(diagnosis);
  assert.equal(diagnosis.repairScope, 'script');
  assert.equal(diagnosis.packageOptimizable, true);
  assert.equal(diagnosis.blocking, false);
  assert.match(diagnosis.evidence.join('\n'), /business_data_search.*3/);
  assert.equal(report.canProposeOptimizations, true);
});

test('Trace 不可用时不得推断 execute_script 调用次数为零', async () => {
  const failed = result('trace-unavailable', '统计第1批、第2批、第3批的装置负荷和经济效益', '精确计算结果');
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
    traces: {
      'trace-unavailable': {
        status: 'unavailable',
        error: 'trace endpoint returned 503',
        toolCalls: [],
      },
    },
  });

  assert.equal(report.diagnoses.some((item) => item.category === 'needs_script'), false);
});

test('必须修复的问题只隔离关联用例，不阻止其他可优化失败', async () => {
  const conflictA = result('conflict-a', '库存口径是什么？', '包含在库');
  const conflictB = result('conflict-b', '库存口径是什么？', '包含在库和在制');
  const calculable = result('scriptable', '第1批、第2批、第3批的装置负荷如何统计？', '按装置能力计算');
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [conflictA, conflictB, calculable],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
    traces: { scriptable: { toolCalls: ['business_data_search', 'business_data_search', 'business_data_search'] } },
  });

  assert.equal(report.diagnoses.some((item) => item.category === 'testcase_conflict'), true);
  assert.equal(report.diagnoses.some((item) => item.category === 'needs_script'), true);
  assert.equal(report.canProposeOptimizations, true);
});

test('同一用例缺数据时仍允许先生成可复用计算脚本建议', async () => {
  const failed = result(
    'mixed-cause',
    '第1批、第2批、第3批的整体月度经济效益如何计算？',
    '按材料价格、处理成本和产出价值计算',
  );
  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
    qualityGaps: [{
      kind: '缺字段', target: '经济效益计算口径',
      message: '缺少材料价格字段，当前无法得出经济效益结果。', how: '补充材料价格数据。',
    }],
    traces: { 'mixed-cause': { toolCalls: ['business_data_search', 'business_data_search', 'business_data_search'] } },
  });

  assert.equal(report.diagnoses.some((item) => item.category === 'data_gap'), true);
  assert.equal(report.diagnoses.some((item) => item.category === 'needs_script'), true);
  assert.equal(report.canProposeOptimizations, true);
});

test('数据缺口不得吞掉已执行脚本的独立公式诊断', async () => {
  const failed = result(
    'script-caliber-with-gap',
    '第1批2吨、第2批3吨，请计算处理单元负荷。',
    '按每批的派生流量和处理能力计算。',
  );
  failed.finalAnswer = '直接使用了每批总量。';
  const core = createFauxCore({ provider: 'mixed-diagnosis', models: [{ id: 'mixed-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', {
    diagnoses: [{
      category: 'caliber_issue',
      title: '脚本使用了错误的输入口径',
      summary: '脚本将父级数量当成了派生流量。',
      evidence: ['Trace 显示 execute_script 已执行，回答说明直接使用了每批总量'],
      confidence: 'high',
      blocking: false,
      repairScope: 'script',
      packageOptimizable: true,
      resultIds: ['script-caliber-with-gap'],
      relatedQuestions: ['计算处理单元负荷'],
      suggestedAction: '核对脚本的派生流量公式。',
    }],
  })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
    qualityGaps: [{
      kind: '缺字段', target: '处理单元负荷',
      message: '缺少部分中间流量字段。', how: '补充缺失的中间流量。',
    }],
    traces: { 'script-caliber-with-gap': { toolCalls: ['ontology_context', 'execute_script'] } },
  }, llm);

  assert.equal(report.diagnoses.some((item) => item.category === 'data_gap'), true);
  assert.equal(report.diagnoses.some((item) => item.category === 'caliber_issue'), true);
  assert.equal(report.canProposeOptimizations, true);
});

test('语义失败诊断可以分页读取上传表格证据', async () => {
  const failed = result('grounded-caliber', '请计算处理单元的负荷。', '按派生量计算。');
  const core = createFauxCore({ provider: 'grounded-diagnosis', models: [{ id: 'grounded-diagnosis' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('read_table_rows', { table: 'material_factors', offset: 0, limit: 20 })]),
    fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
      category: 'caliber_issue', title: '公式输入粒度错误', summary: '父级数量不等于派生量。',
      evidence: ['material_factors 表的 derived_ratio 字段给出了派生比例'], confidence: 'high',
      blocking: false, repairScope: 'script', packageOptimizable: true, resultIds: ['grounded-caliber'],
      relatedQuestions: ['计算处理单元负荷'], suggestedAction: '使用资料中的派生比例。',
    }] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
    traces: { 'grounded-caliber': { toolCalls: ['execute_script'] } },
    tables: [{
      name: 'material_factors', rowCount: 1, rows: [['A', '0.25']],
      fields: [
        { name: 'material', inferredType: 'string', sampleValues: ['A'], distinctRatio: 1 },
        { name: 'derived_ratio', inferredType: 'number', sampleValues: ['0.25'], distinctRatio: 1 },
      ],
    }],
  }, llm);

  assert.equal(report.diagnoses.some((item) => item.category === 'caliber_issue'), true);
  assert.match(report.diagnoses[0].evidence.join('\n'), /derived_ratio/);
});

test('语义失败诊断按输入预算分批并覆盖大量长回答', async () => {
  const failures = ['batch-a', 'batch-b', 'batch-c'].map((id, index) => {
    const failed = result(id, `计算方案 ${index + 1} 的整体负荷`, '按全部输入分量汇总计算');
    failed.finalAnswer = `未完整计算-${id}-`.repeat(4_000);
    return failed;
  });
  const core = createFauxCore({ provider: 'batched-diagnosis', models: [{ id: 'batched-diagnosis' }] });
  core.setResponses(failures.map((failed) => fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', {
    diagnoses: [{
      category: 'caliber_issue', title: `输入口径不完整 ${failed.id}`,
      summary: '回答未覆盖全部必需输入分量。', evidence: ['回答与完整口径不一致'],
      confidence: 'high', blocking: false, repairScope: 'package', packageOptimizable: true,
      resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '补齐公式输入口径。',
    }],
  })])));
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: failures,
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.deepEqual(
    report.diagnoses.flatMap((diagnosis) => diagnosis.resultIds).sort(),
    failures.map((failed) => failed.id).sort(),
  );
  assert.equal(report.canProposeOptimizations, true);
});

test('语义诊断部分批次失败时保留成功结果并给出可重试诊断', async () => {
  const failures = ['batch-ok', 'batch-timeout-a', 'batch-timeout-b'].map((id) => {
    const failed = result(id, `核对 ${id} 的整体负荷`, '按全部输入分量汇总计算');
    failed.finalAnswer = `长回答-${id}-`.repeat(5_000);
    return failed;
  });
  const core = createFauxCore({ provider: 'partial-batch-diagnosis', models: [{ id: 'partial-batch-diagnosis' }] });
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const input = JSON.stringify(context);
      if (input.includes('batch-timeout-a') || input.includes('batch-timeout-b')) {
        throw Object.assign(new Error('synthetic timeout'), { code: 'MODEL_REQUEST_TIMEOUT' });
      }
      core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', {
        diagnoses: [{
          category: 'caliber_issue', title: '成功批次口径不完整',
          summary: '回答未覆盖全部必需输入分量。', evidence: ['来源公式包含多个必需输入分量'],
          confidence: 'high', blocking: false, repairScope: 'package', packageOptimizable: true,
          resultIds: ['batch-ok'], relatedQuestions: ['核对 batch-ok 的整体负荷'],
          suggestedAction: '补齐公式输入口径。',
        }],
      })])]);
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: failures,
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  const successful = report.diagnoses.find((diagnosis) => diagnosis.title === '成功批次口径不完整');
  const retryable = report.diagnoses.find((diagnosis) => diagnosis.title === '部分失败原因分析未完成');
  assert.deepEqual(successful?.resultIds, ['batch-ok']);
  assert.deepEqual(retryable?.resultIds.sort(), ['batch-timeout-a', 'batch-timeout-b']);
  assert.equal(retryable?.packageOptimizable, false);
  assert.equal(retryable?.repairScope, 'runtime');
  assert.match(retryable?.evidence.join('\n') ?? '', /MODEL_REQUEST_TIMEOUT/);
  assert.equal(report.canProposeOptimizations, true);
});

test('语义诊断发现源数据缺值时不得转成知识包修改', async () => {
  const failed = result('source-value-missing', '请按转换系数计算结果。', '按来源数据计算。');
  const core = createFauxCore({ provider: 'data-gap-diagnosis', models: [{ id: 'data-gap-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'data_gap', title: '来源表缺少转换系数', summary: '计算必需的源字段为空。',
    evidence: ['source_records.factor 为空'], confidence: 'high', blocking: false,
    repairScope: 'package', packageOptimizable: true, resultIds: ['source-value-missing'],
    relatedQuestions: ['按转换系数计算'], suggestedAction: '补充源数据。',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses[0].category, 'data_gap');
  assert.equal(report.diagnoses[0].repairScope, 'data');
  assert.equal(report.diagnoses[0].blocking, true);
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.equal(report.canProposeOptimizations, false);
});

test('优化建议拒绝照抄用例答案和硬编码批次范围', () => {
  const failed = result(
    'overfit',
    '第1批2万吨，第2批3万吨，第3批4万吨，请统计装置负荷。',
    '负荷分别为62.50%、48.25%、75.00%',
  );
  const diagnosis = {
    id: 'dg-script', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'needs_script' as const, title: '需要脚本', summary: '多批次精确计算不稳定',
    evidence: ['business_data_search 调用 3 次，execute_script 调用 0 次'], confidence: 'high' as const,
    blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '生成通用计算脚本',
    createdAt: '2026-07-16T00:00:00.000Z',
  };

  assert.throws(() => validateOptimizationSuggestions([{
    title: '固定九个批次的计算',
    detail: '第1-4批按一套计算，第5-9批按另一套计算，3月为31天。标准结果是负荷分别为62.50%、48.25%、75.00%。',
    kind: 'add_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
    generalizationPlan: '处理该用例', validationPlan: '重跑该用例',
  }], [diagnosis], [failed], {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  }), /过拟合|标准答案|批次范围/);

  assert.doesNotThrow(() => validateOptimizationSuggestions([{
    title: '生成通用装置负荷计算脚本',
    detail: '从结构化输入读取任意数量的批次和装置能力，按规则和指标口径逐批计算。',
    kind: 'add_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
    generalizationPlan: '批次数量、日历天数和装置能力全部作为输入，不固定某个用例。',
    validationPlan: '使用至少两组未见批次组合验证输入校验、计算和输出。',
  }], [diagnosis], [failed], {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  }));
});

test('优化建议拒绝摘抄标准答案中的长片段', () => {
  const failed = result(
    'partial-ground-truth-copy',
    '如何按通用口径计算并输出结果？',
    '先校验全部结构化输入，再按统一口径计算每个分量，最后聚合输出总量与边界说明。',
  );
  const diagnosis = {
    id: 'dg-partial-copy', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '口径不完整', summary: '现有规则缺少通用聚合口径。',
    evidence: ['资料定义了分量与总量的字段口径'], confidence: 'high' as const,
    blocking: false, repairScope: 'rule' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '补充通用口径。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const spec = {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  };

  assert.throws(() => validateOptimizationSuggestions([{
    title: '补充聚合计算规则',
    detail: '补充由运行时输入驱动的通用聚合规则。',
    kind: 'modify_rule', target: 'aggregate_result', diagnosisIds: [diagnosis.id],
    evidence: ['评测摘录：按统一口径计算每个分量，最后聚合输出总量与边界说明。'],
    generalizationPlan: '适用于任意数量的分量。',
    validationPlan: '用未见的分量数量与缺失输入验证。',
  }], [diagnosis], [failed], spec), /标准答案|过拟合|长片段/);

  assert.doesNotThrow(() => validateOptimizationSuggestions([{
    title: '补充聚合计算规则',
    detail: '补充由运行时输入驱动的通用聚合规则。',
    kind: 'modify_rule', target: 'aggregate_result', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence,
    generalizationPlan: '按统一口径计算，适用于任意数量的分量。',
    validationPlan: '用未见的分量数量与缺失输入验证。',
  }], [diagnosis], [failed], spec));
});

test('优化建议继续拒绝完整复制较短的标准答案', () => {
  const failed = result('short-ground-truth-copy', '余额如何计算？', '余额等于可用余额加冻结余额');
  const diagnosis = {
    id: 'dg-short-copy', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '余额口径缺失', summary: '现有口径缺少组成项。',
    evidence: ['资料定义了余额组成字段'], confidence: 'high' as const,
    blocking: false, repairScope: 'metric' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '补充通用口径。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };

  assert.throws(() => validateOptimizationSuggestions([{
    title: '补充余额口径', detail: '余额等于可用余额加冻结余额',
    kind: 'modify_metric', target: 'balance', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence, generalizationPlan: '按字段口径计算任意账户余额。',
    validationPlan: '使用未见的冻结和解冻场景验证。',
  }], [diagnosis], [failed], {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  }), /标准答案|过拟合/);
});

test('优化建议拒绝携带评测裁判原文并接受来源与轨迹证据', () => {
  const failed = result('oracle-language', '如何计算月度汇总？', '按来源字段定义执行参数化聚合');
  const diagnosis = {
    id: 'dg-oracle-language', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '聚合口径缺失', summary: '规则遗漏来源字段定义。',
    evidence: ['资料字段 monthly_capacity 定义月度能力'], confidence: 'high' as const,
    blocking: false, repairScope: 'rule' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '补齐来源字段口径。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const spec = {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  };
  const base = {
    title: '补齐参数化聚合口径', detail: '从结构化输入读取数量与能力字段并按统一公式聚合。',
    kind: 'modify_rule', target: 'monthly_aggregate', diagnosisIds: [diagnosis.id],
    generalizationPlan: '数量和能力均由运行时字段提供。', validationPlan: '使用边界值和未见输入组合验证公式性质。',
  };

  assert.throws(() => validateOptimizationSuggestions([{
    ...base, evidence: ['地面真值显示智能体回答偏低'],
  }], [diagnosis], [failed], spec), /评测裁判|答案|评测结果/);

  assert.doesNotThrow(() => validateOptimizationSuggestions([{
    ...base, evidence: ['资料字段 monthly_capacity 定义月度能力', '执行轨迹未读取该字段'],
  }], [diagnosis], [failed], spec));
});

test('优化建议验证方案拒绝复用失败轮次特征数值并允许通用边界', () => {
  const failed = result(
    'numeric-replay',
    '如何计算本次月度汇总？',
    '月度总量为515420吨，装置负荷率为97.00%。',
  );
  failed.finalAnswer = '装置负荷率为92.37%。';
  const diagnosis = {
    id: 'dg-numeric-replay', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '汇总公式错误', summary: '执行结果未遵循来源字段口径。',
    evidence: ['执行轨迹使用了错误的能力字段'], confidence: 'high' as const,
    blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '修复参数化公式。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const spec = {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  };
  const base = {
    title: '修复参数化汇总公式', detail: '由运行时总量和能力字段计算结果。',
    kind: 'modify_script', target: 'calculate_monthly', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence, generalizationPlan: '适用于任意非负总量和正能力输入。',
  };

  assert.throws(() => validateOptimizationSuggestions([{
    ...base, validationPlan: '使用总量515420验证结果为97.00%，并对比旧结果92.37%。',
  }], [diagnosis], [failed], spec), /特征数值|验证方案|边界组合/);

  assert.doesNotThrow(() => validateOptimizationSuggestions([{
    ...base,
    validationPlan: '使用符号变量 X 与正能力 Y 验证线性；使用零总量和零能力边界验证结果与明确错误行为。',
  }], [diagnosis], [failed], spec));
});

test('确定性脚本优化建议禁止在执行失败后回退到模型或人工计算', () => {
  const failed = result('script-fallback', '请按结构化输入计算结果。', '返回脚本的确定性计算结果');
  const diagnosis = {
    id: 'dg-script-fallback', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'runtime_error' as const, title: '确定性脚本执行失败', summary: '脚本接口不兼容。',
    evidence: ['execute_script 返回参数错误'], confidence: 'high' as const,
    blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '修复脚本接口。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const spec = {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  };

  assert.throws(() => validateOptimizationSuggestions([{
    title: '修复确定性脚本并增加兜底',
    detail: '修复脚本接口；如果 execute_script 仍然失败，则改用 business_data_search 并由模型手工汇总结果。',
    kind: 'modify_script', target: 'calculate_result', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence,
    generalizationPlan: '脚本不可用时允许人工计算，保证始终给出结果。',
    validationPlan: '使用未见输入验证脚本和人工兜底路径。',
  }], [diagnosis], [failed], spec), /权威执行路径|手工计算|回退/);

  assert.doesNotThrow(() => validateOptimizationSuggestions([{
    title: '修复确定性脚本接口',
    detail: '按 SDK 契约修复脚本接口；execute_script 失败时停止并报告阻断，不生成推算结果。',
    kind: 'modify_script', target: 'calculate_result', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence,
    generalizationPlan: '所有结构化输入都通过同一确定性脚本执行，不引入第二条计算路径。',
    validationPlan: '使用未见输入、缺失参数和执行异常验证成功结果与阻断行为。',
  }], [diagnosis], [failed], spec));
});

test('优化建议首次触发防过拟合门禁时会根据校验原因自动改写', async () => {
  const failed = result('overfit-retry', '请统计任意批次数量下的装置整体负荷。', '按全部输入分量和装置能力计算。');
  const diagnosis = {
    id: 'dg-overfit-retry', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'needs_script' as const, title: '需要确定性计算脚本', summary: '多批次计算不稳定。',
    evidence: ['执行过程未调用确定性脚本'], confidence: 'high' as const, blocking: false,
    repairScope: 'script' as const, packageOptimizable: true, resultIds: [failed.id],
    relatedQuestions: [failed.question], suggestedAction: '生成参数化计算脚本。',
    createdAt: '2026-07-16T00:00:00.000Z',
  };
  const core = createFauxCore({ provider: 'optimization-validation-retry', models: [{ id: 'optimization-validation-retry' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [{
      title: '按固定批次范围调整计算', detail: '第1-4批走第一条分支，第5-9批走第二条分支。',
      kind: 'add_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
      generalizationPlan: '覆盖当前批次组合。', validationPlan: '重跑当前用例。',
    }] })]),
    fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [{
      title: '新增参数化装置负荷计算脚本',
      detail: '从结构化输入读取任意数量的批次、组成项和装置能力，按统一口径逐批聚合并输出。',
      kind: 'add_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
      generalizationPlan: '批次数量、输入组成和装置能力均由运行时参数提供，不固定案例范围。',
      validationPlan: '使用未见过的批次数量、空批次和边界能力组合验证输入校验与计算性质。',
    }] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const suggestions = await proposeOptimizations({
    id: 'project-1', owner: 'tester', name: '通用流程建模', domain: '通用生产流程', background: '处理任意批次计划',
    sensitivityLevel: 'internal', workflowProfile: { enabledSteps: [], requiredSteps: [], releaseRequested: false, externalResearch: false },
    stage: 'optimize', createdAt: '2026-07-16T00:00:00.000Z', updatedAt: '2026-07-16T00:00:00.000Z',
  }, {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  }, [failed], llm, [], [diagnosis]);

  assert.equal(core.state.callCount, 2);
  assert.equal(suggestions.length, 1);
  assert.equal(suggestions[0].title, '新增参数化装置负荷计算脚本');
});

test('已有稳定诊断时优化器上下文不再携带原始评测答案', async () => {
  const failed = result(
    'diagnosis-only-context',
    '包含 QUESTION_SENTINEL_3141592653 的真实问题',
    '包含 GROUND_TRUTH_SENTINEL_2718281828 的参考内容',
  );
  failed.finalAnswer = '包含 FINAL_ANSWER_SENTINEL_1618033988 的模型输出';
  const diagnosis = {
    id: 'dg-diagnosis-only', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'routing_issue' as const, title: 'Stage2Handler 字段读取契约不匹配', summary: '脚本未按 ProviderResult 结构读取字段。',
    evidence: ['执行轨迹显示记录ID=10的字段解析失败'], confidence: 'high' as const, blocking: false,
    repairScope: 'script' as const, packageOptimizable: true, resultIds: [failed.id],
    relatedQuestions: [failed.question], suggestedAction: '按数据提供方契约修复字段读取。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const core = createFauxCore({ provider: 'diagnosis-only-context', models: [{ id: 'diagnosis-only-context' }] });
  let requestContext = '';
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContext = JSON.stringify(context);
      core.setResponses([fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [{
        title: '修复脚本字段读取契约', detail: '按 ProviderResult 的成功状态和数据载荷统一解析字段。',
        kind: 'modify_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
        generalizationPlan: '通过字段别名归一与结构校验支持不同数据提供方返回。',
        validationPlan: '使用符号字段集合与缺失字段场景验证成功解析和明确阻断。',
      }] })])]);
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  await proposeOptimizations({
    id: 'project-1', owner: 'tester', name: '通用流程建模', domain: '通用数据处理', background: '按数据契约执行',
    sensitivityLevel: 'internal', workflowProfile: { enabledSteps: [], requiredSteps: [], releaseRequested: false, externalResearch: false },
    stage: 'optimize', createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  }, calculationScriptSpec(), [failed], llm, [], [diagnosis]);

  assert.match(requestContext, /dg-diagnosis-only/);
  assert.match(requestContext, /Stage2Handler/);
  assert.doesNotMatch(requestContext, /记录ID=10/);
  assert.doesNotMatch(requestContext, /QUESTION_SENTINEL_3141592653/);
  assert.doesNotMatch(requestContext, /GROUND_TRUTH_SENTINEL_2718281828/);
  assert.doesNotMatch(requestContext, /FINAL_ANSWER_SENTINEL_1618033988/);
});

test('优化建议连续两次复用评测数值后仍可在第三次收敛为性质验证', async () => {
  const failed = result('numeric-retry-three', '如何按运行时字段汇总？', '总量为515420，比例为97.00%。');
  failed.finalAnswer = '比例为92.37%。';
  const diagnosis = {
    id: 'dg-numeric-retry-three', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'routing_issue' as const, title: '脚本字段读取契约不匹配', summary: '字段解析未遵循数据提供方契约。',
    evidence: ['执行轨迹显示字段解析失败'], confidence: 'high' as const, blocking: false,
    repairScope: 'script' as const, packageOptimizable: true, resultIds: [failed.id],
    relatedQuestions: [failed.question], suggestedAction: '修复字段解析。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const bad = (validationPlan: string) => fauxAssistantMessage([fauxToolCall('submit_suggestions', { suggestions: [{
    title: '修复脚本字段读取契约', detail: '按数据提供方结构解析字段。',
    kind: 'modify_script', target: 'calculate_load', diagnosisIds: [diagnosis.id], evidence: diagnosis.evidence,
    generalizationPlan: '统一处理字段别名和缺失字段。', validationPlan,
  }] })]);
  const core = createFauxCore({ provider: 'numeric-retry-three', models: [{ id: 'numeric-retry-three' }] });
  core.setResponses([
    bad('使用总量515420和比例97.00%回归。'),
    bad('对比比例92.37%并确认结果。'),
    bad('使用符号输入集合验证字段别名等价性、缺失字段阻断和顺序无关性。'),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const suggestions = await proposeOptimizations({
    id: 'project-1', owner: 'tester', name: '通用流程建模', domain: '通用数据处理', background: '按数据契约执行',
    sensitivityLevel: 'internal', workflowProfile: { enabledSteps: [], requiredSteps: [], releaseRequested: false, externalResearch: false },
    stage: 'optimize', createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  }, calculationScriptSpec(), [failed], llm, [], [diagnosis]);

  assert.equal(core.state.callCount, 3);
  assert.match(suggestions[0].validationPlan ?? '', /符号输入集合/);
});

test('优化建议拒绝明知遗漏必要组成项的分阶段修复', () => {
  const failed = result('partial-repair', '按所有输入分量计算负荷。', '完整聚合结果');
  const diagnosis = {
    id: 'dg-complete-formula', projectId: 'project-1', roundId: 'round-1', basedVersion: '1.0.0',
    category: 'caliber_issue' as const, title: '公式遗漏组成项', summary: '来源定义了多个必需输入分量。',
    evidence: ['source_definition = component_a + component_b + component_c'], confidence: 'high' as const,
    blocking: false, repairScope: 'script' as const, packageOptimizable: true,
    resultIds: [failed.id], relatedQuestions: [failed.question], suggestedAction: '修复完整公式',
    createdAt: '2026-07-16T00:00:00.000Z',
  };

  assert.throws(() => validateOptimizationSuggestions([{
    title: '先修复主要分量',
    detail: '当前阶段可先计算 component_a，其他必要组成项后续再补充。',
    kind: 'modify_script', target: 'calculate_result', diagnosisIds: [diagnosis.id],
    evidence: diagnosis.evidence, generalizationPlan: '适用于多组输入', validationPlan: '用未见组合验证',
  }], [diagnosis], [failed], {
    id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
    defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [],
  }), /不完整|分阶段/);
});

test('语义诊断阶段只返回根因和证据，不直接返回修改建议', async () => {
  const failed = result('caliber', '客户余额如何计算？', '余额=可用余额+冻结余额');
  failed.finalAnswer = '余额等于可用余额';
  const core = createFauxCore({ provider: 'diagnosis', models: [{ id: 'diagnosis-1' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', {
    diagnoses: [{
      category: 'caliber_issue',
      title: '余额指标遗漏冻结余额',
      summary: '现有指标口径只包含可用余额。',
      evidence: ['metric:balance 当前口径只包含可用余额字段，未引用冻结余额字段'],
      confidence: 'high',
      blocking: false,
      repairScope: 'package',
      packageOptimizable: true,
      resultIds: ['caliber'],
      relatedQuestions: ['客户余额如何计算？'],
      suggestedAction: '修正余额指标口径。',
    }],
  })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1',
    results: [failed],
    spec: {
      id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN',
      defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [{
        key: 'balance', title: '余额', caliber: '余额=可用余额', boundaries: ['当前账户'],
      }],
    },
  }, llm);

  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'caliber_issue');
  assert.equal(report.diagnoses[0].repairScope, 'package');
  assert.equal(report.canProposeOptimizations, true);
  assert.equal('kind' in report.diagnoses[0], false);
  assert.equal('target' in report.diagnoses[0], false);
});

test('Judge 问题即使被模型标成可优化也不得修改知识包', async () => {
  const failed = result('judge', '推荐什么产品？', '产品 A');
  failed.finalAnswer = '产品 A';
  failed.reason = '判分未命中格式';
  const core = createFauxCore({ provider: 'judge-diagnosis', models: [{ id: 'judge-diagnosis' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_failure_diagnoses', { diagnoses: [{
    category: 'judge_issue', title: '判分依据不稳定', summary: '回答与标准答案实质一致',
    evidence: ['回答和标准答案都包含产品 A'], confidence: 'high', blocking: false,
    repairScope: 'package', packageOptimizable: true, resultIds: ['judge'], relatedQuestions: ['推荐什么产品？'],
    suggestedAction: '修改知识包',
  }] })])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  }, llm);

  assert.equal(report.diagnoses[0].category, 'judge_issue');
  assert.equal(report.diagnoses[0].repairScope, 'judge');
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.equal(report.canProposeOptimizations, false);
});

test('Judge 未产出结论应直接诊断为判分服务问题且不再调用语义模型', async () => {
  const failed = result('judge-protocol', '请核对装置负荷。', '装置未过载。');
  failed.finalAnswer = '';
  failed.reason = 'judge 未产出结论';

  const report = await diagnoseEvaluationFailures({
    projectId: 'project-1', results: [failed],
    spec: { id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data', objects: [], operations: [], rules: [], metrics: [] },
  });

  assert.equal(report.diagnoses.length, 1);
  assert.equal(report.diagnoses[0].category, 'judge_issue');
  assert.equal(report.diagnoses[0].repairScope, 'judge');
  assert.equal(report.diagnoses[0].packageOptimizable, false);
  assert.equal(report.canProposeOptimizations, false);
});

test('评测结果持久化 Consumer 运行标识和工具时间线', () => {
  const project = createProject({ owner: 'trace-user', name: 'trace-project', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  const saved = addResult(round.id, {
    testcaseId: 'tc-trace', question: '查询余额', groundTruth: '100', finalAnswer: '90', verdict: 'fail',
    trace: {
      runId: 'run-123', sessionId: 'session-123',
      toolCalls: ['ontology_context', 'business_data_search'],
    },
  });

  assert.deepEqual(getResultTrace(saved.id), {
    runId: 'run-123', sessionId: 'session-123',
    status: 'complete',
    toolCalls: ['ontology_context', 'business_data_search'],
  });
});

test('失败诊断按评测轮次持久化并可重新读取', () => {
  const project = createProject({ owner: 'diagnosis-user', name: 'diagnosis-project', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'runtime_error', title: '认证过期', summary: 'Token 已过期', evidence: ['401 Unauthorized'],
    confidence: 'high', blocking: false, repairScope: 'runtime', packageOptimizable: false,
    resultIds: ['result-1'], relatedQuestions: ['查询余额'], suggestedAction: '刷新 Token 后重试',
  }]);

  const loaded = listFailureDiagnoses(project.id, round.id);
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].roundId, round.id);
  assert.equal(loaded[0].basedRevisionOid, 'revision-1');
  assert.equal(loaded[0].category, 'runtime_error');
  assert.equal(loaded[0].packageRepairEligible, false);
  assert.deepEqual(loaded[0].evidence, ['401 Unauthorized']);
});

test('专家可将数据阻断诊断转为可验证的知识包修复并保留审计信息', () => {
  const project = createProject({ owner: 'diagnosis-expert', name: 'diagnosis-resolution', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  const [diagnosis] = replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'data_gap', title: '字段语义缺失', summary: '价格字段口径不明确', evidence: ['资料中没有口径'],
    confidence: 'high', blocking: true, repairScope: 'data', packageOptimizable: false,
    resultIds: ['result-1'], relatedQuestions: ['如何计算收益？'], suggestedAction: '请专家澄清。',
  }]);
  assert.equal(hasConfirmedOptimizableDiagnoses([diagnosis]), false);

  const resolved = resolveFailureDiagnosis({
    projectId: project.id, diagnosisId: diagnosis.id, action: 'clarify_semantics',
    note: '价格指不含税吨价，以计划批次生效日口径计算。', actor: 'expert-user',
    payload: { nextSteps: ['generate_optimization_suggestions', 'rerun_evaluation'] },
  });

  assert.equal(resolved.resolutionStatus, 'confirmed');
  assert.equal(resolved.resolutionAction, 'clarify_semantics');
  assert.equal(resolved.resolutionActor, 'expert-user');
  assert.equal(resolved.blocking, false);
  assert.equal(resolved.packageOptimizable, true);
  assert.equal(resolved.repairScope, 'package');
  assert.match(resolved.suggestedAction, /不含税吨价/);
  assert.deepEqual(resolved.resolutionPayload, {
    nextSteps: ['generate_optimization_suggestions', 'rerun_evaluation'],
  });
  assert.equal(hasConfirmedOptimizableDiagnoses([resolved]), true);
});

test('用户转为建模修缮任务会确认可优化诊断并保留修缮来源', () => {
  const project = createProject({ owner: 'diagnosis-plan', name: 'diagnosis-plan', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  const [diagnosis] = replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'knowledge_gap', title: '缺少适用条件', summary: '规则没有覆盖该场景', evidence: ['用例证据'],
    confidence: 'high', blocking: false, repairScope: 'package', packageOptimizable: false,
    resultIds: ['result-1'], relatedQuestions: ['何时适用'], suggestedAction: '补充规则',
  }]);

  const [planned] = markFailureDiagnosesForRepair({
    projectId: project.id,
    diagnosisIds: [diagnosis.id],
    actor: 'repair-user',
    note: '用户选择转为建模修缮任务',
    payload: { source: 'evaluation_repair', roundId: round.id },
  });

  assert.equal(planned.resolutionStatus, 'confirmed');
  assert.equal(planned.resolutionAction, 'plan_repair');
  assert.equal(planned.packageOptimizable, true);
  assert.equal(planned.resolutionActor, 'repair-user');
  assert.equal(planned.resolutionNote, '用户选择转为建模修缮任务');
  assert.deepEqual(planned.resolutionPayload, {
    source: 'evaluation_repair', roundId: round.id,
  });
  assert.equal(hasConfirmedOptimizableDiagnoses([planned]), true);
});

test('脚本缺陷可以直接确认进入知识包优化链路', () => {
  const project = createProject({ owner: 'script-repair', name: 'script-repair', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  const [diagnosis] = replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'runtime_error', title: '确定性脚本执行失败', summary: '脚本参数接口不兼容',
    evidence: ['execute_script：unrecognized arguments'], confidence: 'high', blocking: false,
    repairScope: 'script', packageOptimizable: true,
    resultIds: ['result-1'], relatedQuestions: ['校验生产计划'], suggestedAction: '修复脚本参数接口',
  }]);

  assert.equal(diagnosis.packageRepairEligible, true);
  const [planned] = markFailureDiagnosesForRepair({
    projectId: project.id,
    diagnosisIds: [diagnosis.id],
    actor: 'repair-user',
  });

  assert.equal(planned.resolutionStatus, 'confirmed');
  assert.equal(planned.resolutionAction, 'plan_repair');
  assert.equal(planned.repairScope, 'script');
  assert.equal(planned.packageOptimizable, true);
  assert.equal(hasConfirmedOptimizableDiagnoses([planned]), true);
});

test('暂缓处理不会清除阻断状态或把诊断提升为可优化知识包缺陷', () => {
  const project = createProject({ owner: 'diagnosis-defer', name: 'diagnosis-defer', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  const [diagnosis] = replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'data_gap', title: '字段口径待确认', summary: '当前资料缺少权威语义', evidence: ['资料未说明口径'],
    confidence: 'high', blocking: true, repairScope: 'package', packageOptimizable: false,
    resultIds: ['result-1'], relatedQuestions: ['如何计算收益？'], suggestedAction: '请专家先确认字段口径。',
  }]);

  const deferred = resolveFailureDiagnosis({
    projectId: project.id,
    diagnosisId: diagnosis.id,
    action: 'defer',
    note: '先暂缓，等待业务确认',
    actor: 'expert-user',
    payload: { nextSteps: ['rerun_evaluation'] },
  });

  assert.equal(deferred.resolutionStatus, 'deferred');
  assert.equal(deferred.resolutionAction, 'defer');
  assert.equal(deferred.blocking, true);
  assert.equal(deferred.packageOptimizable, false);
  assert.equal(deferred.repairScope, 'package');
  assert.equal(hasConfirmedOptimizableDiagnoses([deferred]), false);
});

test('优化建议持久化根因、证据、通用化和验证方案', () => {
  const project = createProject({ owner: 'suggestion-user', name: 'suggestion-project', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1);
  insertSuggestions(project.id, round.id, '1.0.0', 'revision-1', [{
    title: '生成通用计算脚本', detail: '将复杂计算下沉为脚本', kind: 'add_script', target: 'calculate_load',
    diagnosisIds: ['dg-1'], evidence: ['没有调用 execute_script'],
    generalizationPlan: '批次和日期作为输入', validationPlan: '用新的批次组合验证',
  }]);

  const [saved] = listSuggestions(project.id, round.id);
  assert.deepEqual(saved.diagnosisIds, ['dg-1']);
  assert.deepEqual(saved.evidence, ['没有调用 execute_script']);
  assert.equal(saved.generalizationPlan, '批次和日期作为输入');
  assert.equal(saved.validationPlan, '用新的批次组合验证');
  assert.equal(saved.basedRevisionOid, 'revision-1');
});

test('失败诊断重跑写入失败时回滚删除并保留上一批诊断', () => {
  const project = createProject({ owner: 'diagnosis-atomic', name: 'diagnosis-atomic', domain: 'demo', background: '' });
  const round = createRound(project.id, '1.0.0', 1, 'revision-1');
  replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'runtime_error', title: '原诊断', summary: '原诊断仍应保留', evidence: ['原证据'],
    confidence: 'high', blocking: false, repairScope: 'runtime', packageOptimizable: false,
    resultIds: ['result-1'], relatedQuestions: ['问题'], suggestedAction: '修复运行环境',
  }]);
  const circular: any[] = [];
  circular.push(circular);
  assert.throws(() => replaceFailureDiagnoses(project.id, round.id, '1.0.0', 'revision-1', [{
    category: 'runtime_error', title: '新诊断', summary: '序列化会失败', evidence: circular,
    confidence: 'high', blocking: false, repairScope: 'runtime', packageOptimizable: false,
    resultIds: [], relatedQuestions: [], suggestedAction: '无',
  }]), /circular|cyclic/i);
  const loaded = listFailureDiagnoses(project.id, round.id);
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].title, '原诊断');
});
