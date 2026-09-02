import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import {
  validateKnowledgeBatch,
  validateOperationBatch,
  validateObjectBatch,
  validateSpecIntegrity,
} from '../src/workflow/spec-validation.js';
import { listNodes, runAnalyze, upsertNode } from '../src/workflow/engine.js';
import { generatePackage } from '../src/workflow/m2.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { relationNameProblem } from '../src/workflow/relation-name.js';
import { applyOptimizations, OptimizationExpertReviewRequiredError } from '../src/evaluation/optimizer.js';
import { createProject } from '../src/projects/repo.js';
import { getPackageArtifactStore } from '../src/artifacts/runtime.js';
import {
  confirmedFlowchart,
  operationTargetId,
  operationTargetQuestion,
} from './fixtures/confirmed-flowchart.js';
import { buildFauxModeler, type LlmDeps } from '../src/llm/provider.js';
import { candidateId } from '../src/workflow/candidate-disposition.js';
import type { AnalysisResult, PackageSpec, Project } from '../src/types.js';
import type { OptSuggestion } from '../src/workflow/m4-repo.js';
import type { EvalResult } from '../src/workflow/m3-repo.js';

async function initializeGenerationDraft(project: Project): Promise<void> {
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id.replace(/^pj_/, '')}`,
    name: project.name,
    description: project.background || `${project.name} 的知识包生成测试草稿`,
  });
}

function faux(toolName: string, params: unknown): LlmDeps {
  const core = createFauxCore({ provider: 'faux', models: [{ id: 'faux-1' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall(toolName, params)])]);
  return { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };
}

const baseSpec = (): PackageSpec => ({
  id: 'demo', name: 'demo', description: 'demo', version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
  objects: [{
    key: 'customer', title: '客户', description: '对公客户业务对象', sourceTable: 'customer',
    fields: [{ key: 'id', cnName: '客户编号', sourceColumn: 'id' }],
  }],
  operations: [{
    key: 'recommend', title: '产品推荐', description: '根据客户特征推荐产品',
    inputs: [], outputs: [], flow: ['查询 object:customer', '应用 rule:product_match'],
    relatedObjects: ['customer'], usesRules: ['product_match'], competencyQuestion: '应该推荐什么产品？',
    evidence: ['diagram:recommendation'],
  }],
  rules: [{
    key: 'product_match', title: '产品匹配', content: '满足准入条件时推荐。',
    appliesToObjects: ['customer'], evidence: ['material:产品手册.docx'],
  }],
  metrics: [{
    key: 'wallet_share', title: '钱包份额', caliber: '本行资产/全部金融资产',
    boundaries: ['统计期内'], usesObjects: ['customer'], evidence: ['material:指标口径.md'],
  }],
});

test('规则指标批次在整包保存前拒绝重复 key 和无效对象引用', () => {
  const spec = baseSpec();
  const result = validateKnowledgeBatch({
    rules: [{
      key: 'product_match', title: '重复规则', content: '重复内容',
      appliesToObjects: ['missing_customer'],
    }],
    metrics: [{
      key: 'wallet_share', title: '重复指标', caliber: '金额之比', boundaries: ['统计期'],
      usesObjects: ['missing_customer'],
    }],
  }, {
    objects: spec.objects,
    rules: spec.rules ?? [],
    metrics: spec.metrics ?? [],
  });

  assert.deepEqual(result.issues, [
    '规则 key 重复：product_match',
    '规则 product_match 引用了不存在的对象 missing_customer',
    '指标 key 重复：wallet_share',
    '指标 wallet_share 引用了不存在的对象 missing_customer',
  ]);
  assert.equal(result.invalidReferenceCount, 2);
});

test('规则指标批次允许引用已经生成的对象', () => {
  const spec = baseSpec();
  const result = validateKnowledgeBatch({
    rules: [{
      key: 'customer_active', title: '活跃客户判断', content: '客户状态为活跃',
      appliesToObjects: ['customer'],
    }],
    metrics: [{
      key: 'customer_count', title: '客户数', caliber: '客户去重计数', boundaries: ['统计期'],
      usesObjects: ['customer'],
    }],
  }, { objects: spec.objects, rules: [], metrics: [] });

  assert.deepEqual(result, { issues: [], invalidReferenceCount: 0 });
});

test('规则指标和 operation 批次在保存前拒绝空壳资源', () => {
  const ruleMetric = validateKnowledgeBatch({
    rules: [{ key: 'empty_rule', title: '待访谈规则', content: '' }],
    metrics: [{ key: 'empty_metric', title: '待访谈指标', caliber: '', boundaries: [] }],
  }, { objects: [], rules: [], metrics: [] });
  assert.ok(ruleMetric.issues.some((item) => item.includes('empty_rule') && item.includes('完整规则内容')));
  assert.ok(ruleMetric.issues.some((item) => item.includes('empty_metric') && item.includes('业务口径')));
  assert.ok(ruleMetric.issues.some((item) => item.includes('empty_metric') && item.includes('计算边界')));

  const operation = validateOperationBatch([{ id: 'process_step', question: '完成流程步骤' }], [{
    key: 'empty_operation', title: '空操作', description: '', inputs: [], outputs: [], flow: [], relatedObjects: [],
    competencyQuestionId: 'process_step', competencyQuestion: '完成流程步骤',
  }], { objects: [], rules: [], metrics: [] });
  assert.ok(operation.issues.some((item) => item.includes('empty_operation') && item.includes('业务描述')));
  assert.ok(operation.issues.some((item) => item.includes('empty_operation') && item.includes('可执行流程步骤')));
});

test('知识包渲染统一生成 operation 流程序号且不重复展示模型自带编号', () => {
  const spec = baseSpec();
  spec.operations[0].flow = ['1. 查询 object:customer', '2. 2. 应用 rule:product_match'];

  const markdown = renderPackage(spec)['operations/recommend.md'];

  assert.match(markdown, /# 流程\n\n1\. 查询 object:customer\n2\. 应用 rule:product_match/u);
  assert.doesNotMatch(markdown, /^\d+\.\s+\d+[.)、]\s+/mu);
});

test('知识包渲染受控脚本说明、实现文件和 operation 绑定', () => {
  const spec = baseSpec();
  spec.operations[0].script = { key: 'calculate_wallet_share', required: true };
  spec.scripts = [{
    key: 'calculate_wallet_share',
    title: '钱包份额计算脚本',
    description: '根据结构化输入稳定计算钱包份额。',
    boundOperation: 'recommend',
    runtime: 'python',
    entrypoint: 'calculate_wallet_share.py',
    timeoutSeconds: 30,
    parameters: [
      { name: 'input_json', flag: '--input-json', type: 'string', required: true, description: '结构化输入' },
      { name: 'format', flag: '--format', type: 'string', required: false, defaultValue: 'compact', description: '输出格式' },
    ],
    outputs: [{ key: 'wallet_share', type: 'number', description: '钱包份额' }],
    boundaries: ['只处理调用方提供的结构化 JSON，不访问文件或网络。'],
    sourceCode: [
      '#!/usr/bin/env python3',
      'import argparse',
      'import json',
      'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)',
      'parser.add_argument("--format", default="compact")',
      'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": payload["bank_assets"] / payload["total_assets"]}, ensure_ascii=False))',
    ].join('\n'),
  }];

  const files = renderPackage(spec);
  assert.match(files['scripts/index.md'], /calculate_wallet_share/);
  assert.match(files['scripts/calculate_wallet_share.md'], /bound_operation: recommend/);
  assert.match(files['scripts/calculate_wallet_share.md'], /--input-json/);
  assert.match(files['scripts/calculate_wallet_share.md'], /\| format \| --format \| string \| 否 \| compact \| 输出格式 \|/);
  assert.match(files['scripts/calculate_wallet_share.py'], /wallet_share/);
  assert.match(files['operations/recommend.md'], /script:/);
});

test('知识包拒绝包含子进程、动态执行或网络访问的生成脚本', () => {
  const spec = baseSpec();
  spec.operations[0].script = { key: 'unsafe_script', required: true };
  spec.scripts = [{
    key: 'unsafe_script', title: '不安全脚本', description: '不安全示例', boundOperation: 'recommend',
    runtime: 'python', entrypoint: 'unsafe.py', timeoutSeconds: 30, parameters: [], outputs: [], boundaries: [],
    sourceCode: 'import subprocess\nsubprocess.run(["sh", "-c", "echo unsafe"])',
  }];

  assert.throws(() => renderPackage(spec), /脚本安全检查失败.*subprocess/);

  spec.scripts![0] = { ...spec.scripts![0], sourceCode: 'import requests\nrequests.get("https://example.com")' };
  assert.throws(() => renderPackage(spec), /脚本安全检查失败.*网络访问/);
});

test('资料分段事实保持通用，领域汇总和最终输出受已确认建模目标约束', async () => {
  const project = createProject({ owner: 'tester', name: '合成目标定向分析', domain: '生产', background: '' });
  const brief = {
    problem: '生成减一线分流生产方案',
    targetUsers: '生产计划人员',
    scenarios: '编制日度生产方案',
    successCriteria: '方案可执行且可追溯',
  };
  const prompts: string[] = [];
  const base = buildFauxModeler();
  const llm: LlmDeps = {
    ...base,
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      prompts.push(system);
      return base.streamFn(model, context, options);
    }) as LlmDeps['streamFn'],
  };

  const result = await runAnalyze(
    project.id,
    [],
    [{ filename: '生产手册.md', textContent: '根据原油性质编制分流方案。' }],
    llm,
    undefined,
    brief,
  );

  assert.ok(prompts.some((prompt) => prompt.includes('submit_document_chunk_analysis')));
  const domainPrompt = prompts.find((prompt) => prompt.includes('submit_material_analysis_result')) ?? '';
  assert.match(domainPrompt, new RegExp(brief.problem));
  assert.match(domainPrompt, new RegExp(brief.successCriteria));
  assert.deepEqual(result.analysis.modelingBrief, brief);
  assert.equal(result.analysis.candidateProcesses?.[0].title, brief.scenarios);
  assert.deepEqual(result.analysis.documentReadCoverage, [{
    filename: '生产手册.md',
    revisionFingerprint: `sha256:${'c00259f87b8033da9c41de0140f6cb039cb201811ae961280a18cda51e0a305e'}`,
    totalChars: 13,
    ranges: [[0, 13]],
    contiguousEnd: 13,
    eofSeen: true,
    complete: true,
  }]);
  const node = listNodes(project.id).find((item) => item.type === 'analyze');
  assert.deepEqual((node?.output as AnalysisResult).documentReadCoverage, result.analysis.documentReadCoverage);
});

test('规格硬校验保留引用完整性，但未覆盖问题和分析候选不再阻断', () => {
  const dangling = baseSpec();
  dangling.rules = [];
  assert.throws(() => validateSpecIntegrity(dangling, ['应该推荐什么产品？']), /不存在的规则 product_match/);

  const uncovered = baseSpec();
  uncovered.operations[0].competencyQuestion = '另一个问题';
  assert.deepEqual(validateSpecIntegrity(uncovered, ['应该推荐什么产品？']).uncovered, ['应该推荐什么产品？']);

  const substring = baseSpec();
  substring.operations[0].competencyQuestion = '产品';
  assert.deepEqual(validateSpecIntegrity(substring, ['应该推荐什么产品？']).uncovered, ['应该推荐什么产品？']);

  const analysis: AnalysisResult = {
    domainOverview: '金融营销', candidateObjects: [], openQuestions: [], materialAssessments: [],
    candidateArtifacts: [{
      kind: 'rule', name: '产品匹配规则', rationale: '手册给出适用条件', sources: ['产品手册.docx'], readiness: 'extractable',
    }],
  };
  const noRules = baseSpec();
  noRules.rules = [];
  noRules.operations[0].usesRules = [];
  assert.doesNotThrow(() => validateSpecIntegrity(noRules, ['应该推荐什么产品？'], analysis));

  const twoRuleCandidates: AnalysisResult = {
    ...analysis,
    candidateArtifacts: [
      analysis.candidateArtifacts![0],
      { kind: 'rule', name: '准入规则', rationale: '手册另有准入条件', sources: ['产品手册.docx'], readiness: 'extractable' },
    ],
  };
  assert.doesNotThrow(() => validateSpecIntegrity(baseSpec(), ['应该推荐什么产品？'], twoRuleCandidates));

  const duplicate = baseSpec();
  duplicate.rules = [...duplicate.rules!, { ...duplicate.rules![0], key: ' product_match ', title: '重复规则' }];
  assert.throws(() => validateSpecIntegrity(duplicate), /规则 key 重复：product_match/);

  assert.doesNotThrow(() => validateSpecIntegrity(baseSpec(), ['应该推荐什么产品？'], analysis));
});

test('逻辑对象候选不得静默遗漏', () => {
  const candidates = [
    { id: 'candidate_1', name: '客户', rationale: '客户主数据', sourceTables: ['customer'] },
    { id: 'candidate_2', name: '外部环境', rationale: '运行时输入', sourceTables: [] },
  ];
  const submitted = [{
    candidateId: 'candidate_1', key: 'customer', title: '客户', description: '客户主数据',
    sourceTable: 'customer', fields: [{ key: 'id', cnName: '客户号', sourceColumn: 'id' }],
  }];

  assert.deepEqual(validateObjectBatch(candidates, submitted), [
    '对象候选「外部环境」未生成，也没有进入专家处理',
  ]);
});

test('对象关系名称拒绝纯基数和重复端点名称并保留模型生成的中英文谓词', () => {
  for (const cardinality of [
    '1:1', '1:N', 'N:M', 'Ｘ：Ｘ', '1..*', '一对多', 'one-to-many',
  ]) {
    assert.match(relationNameProblem(cardinality) ?? '', /关系名称不能只填写关系基数/);
  }
  for (const relation of [
    '属于', '包含', '生成', '引用', '依赖', '适用于', '对应', 'belongs_to',
  ]) {
    assert.equal(relationNameProblem(relation), undefined);
  }

  const context = {
    source: { key: 'application_item', title: '申请项' },
    target: { key: 'application', title: '申请', aliases: ['业务申请'] },
  };
  for (const redundant of [
    '属于申请', '对应业务申请', '申请项属于', 'belongs_to_application',
  ]) {
    assert.match(relationNameProblem(redundant, context) ?? '', /不应包含(?:源|目标)对象名称/);
  }
  for (const relation of ['属于', '对应', '隶属于', 'belongs_to']) {
    assert.equal(relationNameProblem(relation, context), undefined);
  }

  const invalid = baseSpec();
  invalid.objects[0].relations = [{ relation: '1:N', target: 'customer' }];
  assert.ok(validateObjectBatch([], invalid.objects).some((issue) => (
    issue.includes('relations[0].relation') && issue.includes('关系基数')
  )));
  assert.throws(() => validateSpecIntegrity(invalid), /relations\[0\]\.relation.*关系基数/);

  const selfRelation = baseSpec();
  selfRelation.objects[0].relations = [{ relation: '属于', target: selfRelation.objects[0].key }];
  assert.ok(validateObjectBatch([], selfRelation.objects).some((issue) => issue.includes('不允许自关联')));
  assert.throws(() => validateSpecIntegrity(selfRelation), /不允许自关联/);

  const duplicateRelation = baseSpec();
  duplicateRelation.objects[0].relations = [
    { relation: '属于', target: 'customer' },
    { relation: ' 属于 ', target: ' customer ' },
  ];
  assert.ok(validateObjectBatch([], duplicateRelation.objects).some((issue) => issue.includes('source-target-relation 重复')));
  assert.throws(() => validateSpecIntegrity(duplicateRelation), /source-target-relation 重复/);

  const redundant = baseSpec();
  redundant.objects[0].title = '申请项';
  redundant.objects.push({
    key: 'application', title: '申请', description: '用于关系连读校验的合成申请对象',
    aliases: ['业务申请'], fields: [{ key: 'id', cnName: '申请编号' }],
  });
  redundant.objects[0].relations = [{ relation: '属于申请', target: 'application' }];
  assert.ok(validateObjectBatch([], redundant.objects).some((issue) => issue.includes('目标对象名称')));
  assert.throws(() => validateSpecIntegrity(redundant), /目标对象名称.*申请/);

  const valid = structuredClone(redundant);
  valid.objects[0].relations = [{ relation: '属于', target: 'application' }];
  assert.deepEqual(validateObjectBatch([], valid.objects), []);
  assert.doesNotThrow(() => validateSpecIntegrity(valid));
});

test('规则指标候选可以逐项生成或延期，但不得静默遗漏', async () => {
  const project = createProject({ owner: 'tester', name: '指标补全回归', domain: '金融', background: '' });
  await initializeGenerationDraft(project);
  const process = { id: 'customer_metrics_lookup', title: '查询客户核心指标' };
  const flowchart = confirmedFlowchart([process]);

  const analysis: AnalysisResult = {
    domainOverview: '银行营销',
    candidateObjects: [{ name: '客户', rationale: '客户表', sourceTables: ['customer_table'] }],
    openQuestions: [],
    materialAssessments: [{
      source: '产品手册.docx', sourceType: 'document', suitableFor: ['metric'], rationale: '给出两个明确计算口径',
    }],
    candidateArtifacts: [
      { kind: 'metric', name: '客户资产规模', rationale: '手册给出资产合计口径', sources: ['产品手册.docx'], readiness: 'extractable' },
      { kind: 'metric', name: '客户产品覆盖率', rationale: '手册给出覆盖率公式', sources: ['产品手册.docx'], readiness: 'extractable' },
    ],
  };
  upsertNode({ projectId: project.id, type: 'analyze', status: 'done', ordinal: 0, output: analysis });

  const metric1 = {
    key: 'asset_scale', title: '客户资产规模', caliber: '各类资产余额合计', boundaries: ['统计期末'], usesObjects: ['customer_table'],
  };
  const operation = {
    key: 'query_customer_metrics', title: '查询客户核心指标', description: '返回两个核心指标',
    inputs: [], outputs: [], flow: ['查询 object:customer_table'], relatedObjects: ['customer_table'],
    usesMetrics: ['asset_scale'],
    competencyQuestionId: operationTargetId(process.id),
  };

  const core = createFauxCore({ provider: 'faux-knowledge-retry', models: [{ id: 'faux-knowledge-retry-1' }] });
  let knowledgeCalls = 0;
  const llm: LlmDeps = {
    model: core.getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const systemPrompt = String(context?.systemPrompt ?? '');
      if (systemPrompt.includes('submit_rules_metrics')) {
        knowledgeCalls += 1;
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_rules_metrics', {
          rules: [], metrics: [metric1],
          dispositions: [
            {
              candidateId: candidateId(analysis.candidateArtifacts![0], 0),
              kind: 'metric', name: '客户资产规模', status: 'generated', resourceKeys: ['asset_scale'],
              reason: '首版查询流程直接使用该指标。',
            },
            {
              candidateId: candidateId(analysis.candidateArtifacts![1], 1),
              kind: 'metric', name: '客户产品覆盖率', status: 'deferred',
              reason: '当前确认流程只输出客户资产规模，不包含产品持有对象或覆盖率计算步骤，无法绑定现有流程输出。',
            },
          ],
        })])]);
      } else if (systemPrompt.includes('submit_operations')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })])]);
      } else {
        throw new Error(`未处理的测试 prompt: ${systemPrompt.slice(0, 80)}`);
      }
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer_table', rowCount: 1, materialPurpose: 'schema',
      fields: [{ name: 'customer_id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }],
    documents: [{ id: 'product-manual', filename: '产品手册.docx', textContent: '资产规模与产品覆盖率计算口径。' }],
  }, llm, flowchart);

  assert.equal(knowledgeCalls, 1);
  assert.equal(result.spec.metrics?.length, 1);
  assert.equal(result.spec.operations[0].competencyQuestion, operationTargetQuestion(process.title));
  assert.equal(result.coverage.uncovered.length, 0);
});

test('operation 用来源表名引用对象时转换为对象 key', async () => {
  const project = createProject({ owner: 'tester', name: '对象引用规范化', domain: '金融', background: '' });
  await initializeGenerationDraft(project);
  const process = { id: 'customer_lookup', title: '查询客户信息' };
  const flowchart = confirmedFlowchart([process]);
  const analysis: AnalysisResult = {
    domainOverview: '银行客户',
    candidateObjects: [{ name: '客户', rationale: '客户表', sourceTables: ['customer_table'] }],
    openQuestions: [], materialAssessments: [], candidateArtifacts: [],
  };
  upsertNode({ projectId: project.id, type: 'analyze', status: 'done', ordinal: 0, output: analysis });

  const core = createFauxCore({ provider: 'faux-object-ref', models: [{ id: 'faux-object-ref-1' }] });
  const llm: LlmDeps = {
    model: core.getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const systemPrompt = String(context?.systemPrompt ?? '');
      if (systemPrompt.includes('submit_operations')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [{
          key: 'query_customer', title: '查询客户', description: '查询客户信息', inputs: [], outputs: [],
          flow: ['查询 object:customer'], relatedObjects: ['customer_table'],
          competencyQuestionId: operationTargetId(process.id),
        }] })])]);
      } else {
        throw new Error(`未处理的测试 prompt: ${systemPrompt.slice(0, 80)}`);
      }
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer_table', rowCount: 1, materialPurpose: 'schema',
      fields: [{ name: 'customer_id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }],
    documents: [],
  }, llm, flowchart);

  assert.deepEqual(result.spec.operations[0].relatedObjects, ['customer_table']);
  assert.equal(result.spec.operations[0].competencyQuestion, operationTargetQuestion(process.title));
});

test('优化器可以新增规则并保留既有指标', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p1', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-14T00:00:00.000Z', updatedAt: '2026-07-14T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's1', projectId: 'p1', roundId: 'r1', basedVersion: '1.0.0',
    title: '新增重点客户规则', detail: '新增重点客户判断条件', kind: 'add_rule',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  };
  const rules = [...(spec.rules ?? []), {
    key: 'priority_customer', title: '重点客户', content: '资产规模超过阈值时标记为重点客户。', appliesToObjects: ['customer'],
  }];

  const revised = await applyOptimizations(project, spec, [suggestion], faux('submit_revised_spec', {
    objects: spec.objects,
    operations: spec.operations,
    rules,
    metrics: spec.metrics,
    scripts: spec.scripts ?? [],
  }));

  assert.equal(revised.rules.length, 2);
  assert.equal(revised.metrics.length, 1);
  assert.equal(revised.rules[1].key, 'priority_customer');
});

test('优化器支持只提交变更资源补丁并由服务端保留完整规格', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-resource-patch', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-resource-patch', projectId: project.id, roundId: 'r-resource-patch', basedVersion: '1.0.0',
    title: '完善通用边界', detail: '补充运行时输入为空时的处理。', kind: 'modify_rule',
    target: 'product_match', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changedRule = {
    ...spec.rules![0],
    content: '满足准入条件时推荐；运行时输入为空或字段非法时返回明确错误。',
  };

  const revised = await applyOptimizations(project, spec, [suggestion], faux('submit_optimization_patch', {
    rules: [changedRule],
  }));

  assert.deepEqual(revised.objects, spec.objects);
  assert.deepEqual(revised.operations, spec.operations);
  assert.deepEqual(revised.metrics, spec.metrics);
  assert.deepEqual(revised.scripts, spec.scripts ?? []);
  assert.match(revised.rules.find((rule) => rule.key === 'product_match')?.content ?? '', /输入为空/);
});

test('优化候选先输出分析正文时会强制改用结构化补丁提交', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-candidate-capture', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-09-01T00:00:00.000Z', updatedAt: '2026-09-01T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-candidate-capture', projectId: project.id, roundId: 'r-candidate-capture', basedVersion: '1.0.0',
    title: '完善通用边界', detail: '补充运行时输入为空时的处理。', kind: 'modify_rule',
    target: 'product_match', status: 'pending', createdAt: '2026-09-01T00:00:00.000Z',
  };
  const changedRule = {
    ...spec.rules![0],
    content: '满足准入条件时推荐；运行时输入为空或字段非法时返回明确错误。',
  };
  const core = createFauxCore({ provider: 'candidate-capture', models: [{ id: 'candidate-capture' }] });
  core.setResponses([
    fauxAssistantMessage('我先分析需要修改的资源。'),
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { rules: [changedRule] })]),
  ]);
  const requestContexts: string[] = [];
  let patchToolPropertyNames: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      const tools = context?.tools ?? (options as any)?.tools ?? [];
      const patchTool = tools.find((tool: any) => tool?.name === 'submit_optimization_patch');
      const properties = patchTool?.parameters?.properties ?? patchTool?.inputSchema?.properties;
      if (properties) patchToolPropertyNames = Object.keys(properties);
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.deepEqual(patchToolPropertyNames, ['rules']);
  assert.match(requestContexts[1], /上一次未调用目标提交工具/);
  assert.match(requestContexts[1], /submit_optimization_patch/);
  assert.match(revised.rules.find((rule) => rule.key === 'product_match')?.content ?? '', /输入为空/);
});

test('优化器可以一次落实同一资源上的多条兼容修改建议', async () => {
  const spec = baseSpec();
  spec.rules![0].evidence = [
    '保留的资料证据',
    '标准答案原文不应保留',
    'diagnosis:标准答案中的原始结果文本',
    'diagnosis:dg_existing-stable',
  ];
  const project: Project = {
    id: 'p-multi-target', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestions: OptSuggestion[] = [{
    id: 's-caliber', projectId: project.id, roundId: 'r-multi-target', basedVersion: '1.0.0',
    title: '统一计算口径', detail: '明确使用月度总量除以月度产能。', kind: 'modify_rule',
    target: 'product_match', diagnosisIds: ['dg_caliber'], evidence: ['标准答案原文不应入包'],
    status: 'pending', createdAt: '2026-07-24T00:00:00.000Z',
  }, {
    id: 's-boundary', projectId: project.id, roundId: 'r-multi-target', basedVersion: '1.0.0',
    title: '补充边界条件', detail: '明确零输入和阈值边界。', kind: 'modify_rule',
    target: 'product_match', diagnosisIds: ['dg_boundary'], evidence: ['智能体回答原文不应入包'],
    status: 'pending', createdAt: '2026-07-24T00:00:00.000Z',
  }];
  const rules = (spec.rules ?? []).map((rule) => rule.key === 'product_match' ? {
    ...rule,
    content: '使用月度总量除以月度产能计算；输入为零时返回零，达到阈值时按边界规则判定。',
  } : rule);

  const revised = await applyOptimizations(project, spec, suggestions, faux('submit_revised_spec', {
    objects: spec.objects,
    operations: spec.operations,
    rules,
    metrics: spec.metrics,
    scripts: spec.scripts ?? [],
  }));

  assert.match(revised.rules.find((rule) => rule.key === 'product_match')?.content ?? '', /月度总量/);
  assert.match(revised.rules.find((rule) => rule.key === 'product_match')?.content ?? '', /输入为零/);
  assert.deepEqual(
    revised.rules.find((rule) => rule.key === 'product_match')?.evidence?.filter((item) => item.startsWith('optimization-decision:')),
    ['optimization-decision:统一计算口径', 'optimization-decision:补充边界条件'],
  );
  assert.deepEqual(
    revised.rules.find((rule) => rule.key === 'product_match')?.evidence,
    [
      '保留的资料证据',
      'diagnosis:dg_existing-stable',
      'diagnosis:dg_caliber',
      'optimization-decision:统一计算口径',
      'diagnosis:dg_boundary',
      'optimization-decision:补充边界条件',
    ],
  );
});

test('优化候选生成不向修订模型传递评测证据原文', async () => {
  const spec = baseSpec();
  spec.rules![0].evidence = [
    'material:产品手册.docx',
    '标准答案：SPEC_ORACLE_SENTINEL_42424242',
    'diagnosis:评测结果原文不应入模',
  ];
  const project: Project = {
    id: 'p-sanitized-evidence', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-sanitized-evidence', projectId: project.id, roundId: 'r-sanitized-evidence', basedVersion: '1.0.0',
    title: '补充通用边界', detail: '根据参数状态明确边界处理。', kind: 'modify_rule',
    target: 'product_match', diagnosisIds: ['dg_safe-reference'],
    evidence: ['LEAKED_STANDARD_ANSWER_987654321'], status: 'pending',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const rules = spec.rules!.map((rule) => rule.key === 'product_match'
    ? { ...rule, content: '满足准入条件时推荐，临界值按输入字段状态判定。' }
    : rule);
  const submitted = {
    objects: spec.objects, operations: spec.operations, rules,
    metrics: spec.metrics, scripts: spec.scripts ?? [],
  };
  const core = createFauxCore({ provider: 'sanitized-evidence', models: [{ id: 'sanitized-evidence' }] });
  let requestContext = '';
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContext = JSON.stringify(context);
      core.setResponses([fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted)])]);
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  await applyOptimizations(project, spec, [suggestion], llm);

  assert.doesNotMatch(requestContext, /LEAKED_STANDARD_ANSWER_987654321/);
  assert.doesNotMatch(requestContext, /SPEC_ORACLE_SENTINEL_42424242/);
  assert.doesNotMatch(requestContext, /diagnosis:评测结果原文不应入模/);
  assert.match(requestContext, /dg_safe-reference/);
});

test('优化候选逐次拒绝评测裁判表述和真实问题片段后才接受通用修订', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-candidate-evaluation-leak', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-candidate-evaluation-leak', projectId: project.id, roundId: 'r-candidate-evaluation-leak', basedVersion: '1.0.0',
    title: '补充通用输入边界', detail: '按运行时输入状态明确边界。', kind: 'modify_rule',
    target: 'product_match', status: 'pending', createdAt: '2026-07-24T00:00:00.000Z',
  };
  const failed: EvalResult = {
    id: 'result-candidate-evaluation-leak', roundId: suggestion.roundId, testcaseId: 'case-candidate-evaluation-leak',
    question: '请根据任意数量的客户记录、动态阈值、缺失字段和不同字段顺序判断是否满足产品准入条件，并说明异常输入如何阻断处理。',
    groundTruth: '应按运行时阈值逐条检查客户记录，缺少必填字段时明确阻断，不得复用某一条样例的固定结论。',
    finalAnswer: '未正确处理动态阈值。', verdict: 'fail', reason: '边界处理不完整', overridden: false,
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const revisedRules = (content: string) => spec.rules!.map((rule) => rule.key === 'product_match'
    ? { ...rule, content }
    : rule);
  const response = (content: string) => fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
    objects: spec.objects, operations: spec.operations, rules: revisedRules(content),
    metrics: spec.metrics, scripts: spec.scripts ?? [],
  })]);
  const core = createFauxCore({ provider: 'candidate-evaluation-leak', models: [{ id: 'candidate-evaluation-leak' }] });
  core.setResponses([
    response('根据标准答案修正准入规则，并保留评测结果作为规则依据。'),
    response('按运行时阈值逐条校验必填字段；字段缺失时明确阻断，字段顺序变化不影响判断。'),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm, undefined, [failed]);

  assert.equal(core.state.callCount, 2);
  assert.doesNotMatch(requestContexts[1], /上次未通过校验的候选补丁/);
  assert.match(revised.rules.find((rule) => rule.key === 'product_match')?.content ?? '', /运行时阈值/);
  assert.doesNotMatch(JSON.stringify(revised), /标准答案|评测结果|示例问题/);
});

test('优化候选修改资源时会清理继承的真实评测样例数值', async () => {
  const spec = baseSpec();
  spec.operations[0] = {
    ...spec.operations[0],
    script: { key: 'calculate_plan', required: true },
  };
  const sourceWithEvaluationExample = [
    'import argparse',
    'import json',
    'parser = argparse.ArgumentParser()',
    'parser.add_argument("--input-json", required=True, help="示例格式: batchAmount=9876.54321")',
    'args = parser.parse_args()',
    'payload = json.loads(args.input_json)',
    'amount_tons = payload["batchAmount"] * 10000',
    'print(json.dumps({"result": amount_tons}))',
  ].join('\n');
  spec.scripts = [{
    key: 'calculate_plan', title: '计划计算', description: '按批次输入计算。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'calculate_plan.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'result', type: 'object' }], boundaries: ['只处理结构化输入。'],
    sourceCode: sourceWithEvaluationExample,
  }];
  const project: Project = {
    id: 'p-clean-inherited-example', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-clean-inherited-example', projectId: project.id, roundId: 'r-clean-inherited-example', basedVersion: '1.0.0',
    title: '完善计划脚本边界', detail: '改为参数化批次输入并使用抽象示例。', kind: 'modify_script',
    target: 'calculate_plan', status: 'pending', createdAt: '2026-07-24T00:00:00.000Z',
  };
  const failed: EvalResult = {
    id: 'result-clean-inherited-example', roundId: suggestion.roundId, testcaseId: 'case-clean-inherited-example',
    question: '输入若干批次，其中一个批次金额为 9876.54321，单位换算中会出现 10000，要求按运行时参数统一计算。',
    groundTruth: '应读取任意批次数量和金额，不依赖固定样例。', finalAnswer: '', verdict: 'fail',
    reason: '脚本示例被误当成固定输入。', overridden: false, createdAt: '2026-07-24T00:00:00.000Z',
  };
  const script = (sourceCode: string) => ({
    ...spec.scripts![0], description: '按任意数量的结构化批次输入计算。', sourceCode,
  });
  const response = (sourceCode: string) => fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [script(sourceCode)],
  })]);
  const core = createFauxCore({ provider: 'clean-inherited-example', models: [{ id: 'clean-inherited-example' }] });
  core.setResponses([
    response(sourceWithEvaluationExample),
    response(sourceWithEvaluationExample.replace('示例格式: batchAmount=9876.54321', '格式: batchAmount=<运行时金额>')),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm, undefined, [failed]);

  assert.equal(core.state.callCount, 1);
  assert.doesNotMatch(requestContexts[0], /9876\.54321/);
  assert.match(requestContexts[0], /<运行时数值>/);
  assert.match(requestContexts[0], /\* 10000/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /9876\.54321/);
  assert.match(revised.scripts[0].sourceCode, /\* 10000/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /\*\s*<运行时数值>/);
});

test('优化候选增量补丁会统一清理未触及资源里继承的真实评测样例数值', async () => {
  const spec = baseSpec();
  const untouchedDescription = '保留原始格式（温度：350℃，体积：12cm³），不得被优化器顺带改写。';
  spec.objects[0] = { ...spec.objects[0], description: untouchedDescription };
  spec.rules![0].content = '满足准入条件时推荐；单位换算采用输入量乘以10000。';
  spec.operations[0] = {
    ...spec.operations[0],
    description: '输入格式示例：batchAmount=9876.54321；实际运行时必须读取调用参数。',
  };
  const project: Project = {
    id: 'p-clean-untouched-example', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-clean-untouched-example', projectId: project.id, roundId: 'r-clean-untouched-example', basedVersion: '1.0.0',
    title: '补充输入边界', detail: '字段缺失时明确阻断。', kind: 'modify_rule',
    target: 'product_match', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const failed: EvalResult = {
    id: 'result-clean-untouched-example', roundId: suggestion.roundId, testcaseId: 'case-clean-untouched-example',
    question: '某批次金额为 9876.54321，输入单位换算需乘以10000，如何按运行时输入计算？',
    groundTruth: '应读取运行时输入，不能复用固定样例。', finalAnswer: '', verdict: 'fail',
    reason: '存在固定样例输入。', overridden: false, createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changedRule = {
    ...spec.rules![0],
    content: '满足准入条件时推荐；单位换算采用输入量乘以10000；字段缺失或非法时明确阻断。',
  };

  const revised = await applyOptimizations(project, spec, [suggestion], faux('submit_optimization_patch', {
    rules: [changedRule],
  }), undefined, [failed]);

  assert.doesNotMatch(JSON.stringify(revised), /9876\.54321/);
  assert.match(revised.operations[0].description, /<运行时数值>/);
  assert.match(revised.rules[0].content, /10000/);
  assert.match(revised.rules[0].content, /明确阻断/);
  assert.equal(revised.objects[0].description, untouchedDescription);
  assert.equal(revised.rules[0].content, changedRule.content);
});

test('优化候选也会拒绝仅出现在标准答案或模型答案中的真实样例数值', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-answer-only-number', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-answer-only-number', projectId: project.id, roundId: 'r-answer-only-number', basedVersion: '1.0.0',
    title: '补充通用计算边界', detail: '按运行时参数计算，不保留评测样例。', kind: 'modify_rule',
    target: 'product_match', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const failed: EvalResult = {
    id: 'result-answer-only-number', roundId: suggestion.roundId, testcaseId: 'case-answer-only-number',
    question: '如何按运行时输入完成汇总计算？',
    groundTruth: '该次评测的核验结果为 8765.4321。', finalAnswer: '计算结果为 8765.4321。',
    verdict: 'fail', reason: '结果与评测口径不一致。', overridden: false,
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const revisedRules = (content: string) => spec.rules!.map((rule) => rule.key === 'product_match'
    ? { ...rule, content }
    : rule);
  const response = (content: string) => fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
    objects: spec.objects, operations: spec.operations, rules: revisedRules(content),
    metrics: spec.metrics, scripts: spec.scripts ?? [],
  })]);
  const core = createFauxCore({ provider: 'answer-only-number', models: [{ id: 'answer-only-number' }] });
  core.setResponses([
    response('示例：汇总结果为 8765.4321。'),
    response('按运行时输入逐项汇总，并对缺失值和非法值返回明确错误。'),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm, undefined, [failed]);

  assert.equal(core.state.callCount, 2);
  assert.doesNotMatch(JSON.stringify(revised), /8765\.4321/);
});

test('DataProvider 脚本候选必须跳过空键并显式归一数值类型', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'calculate_score', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--entity-id", required=True)',
    'args = parser.parse_args()', 'client = DataProviderClient()', 'result = client.query("entity")',
    'if not result.success: print(json.dumps({"error": result.error or result.message})); raise SystemExit(1)',
    'rows = result.data', 'print(json.dumps({"score": len(rows)}))',
  ].join('\n');
  spec.scripts = [{
    key: 'calculate_score', title: '评分计算', description: '读取数据并计算。', boundOperation: 'recommend',
    runtime: 'python', entrypoint: 'calculate_score.py', timeoutSeconds: 30,
    parameters: [{ name: 'entity_id', flag: '--entity-id', type: 'string', required: true }],
    outputs: [{ key: 'score', type: 'number' }], boundaries: ['字段缺失时明确报错。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-provider-row-contract', owner: 'tester', name: '测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-provider-row-contract', projectId: project.id, roundId: 'r-provider-row-contract', basedVersion: '1.0.0',
    title: '将数据读取从命名键改为位置索引',
    detail: '命名键可能为空字符串，实际值可能位于来源列键；读取后必须兼容字符串和数字混合类型。',
    kind: 'modify_script', target: 'calculate_score', status: 'pending',
    generalizationPlan: '集中定义候选键，跳过空值并把数值字段显式转换后再比较和计算。',
    validationPlan: '覆盖命名键为空、来源列有值、字符串数字和真实数字混合的情况。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const unsafeSource = [
    'import argparse', 'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--entity-id", required=True)',
    'args = parser.parse_args()', 'client = DataProviderClient()', 'result = client.query("entity")',
    'if not result.success: print(json.dumps({"error": result.error or result.message})); raise SystemExit(1)',
    'rows = result.data',
    'def get_field(row, field):', '    return row.get(field)',
    'target = [row for row in rows if get_field(row, "entity_id") == 10][0]',
    'print(json.dumps({"score": get_field(target, "amount") + 1}))',
  ].join('\n');
  const safeSource = [
    'import argparse', 'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--entity-id", required=True)',
    'args = parser.parse_args()', 'client = DataProviderClient()', 'result = client.query("entity")',
    'if not result.success: print(json.dumps({"error": result.error or result.message})); raise SystemExit(1)',
    'rows = result.data',
    'def first_non_empty(row, *candidate_keys):',
    '    for key in candidate_keys:',
    '        value = row.get(key)',
    '        if value is None or (isinstance(value, str) and not value.strip()):',
    '            continue',
    '        return value',
    '    return None',
    'def to_number(value, field):',
    '    if value in (None, ""): raise ValueError(f"missing {field}")',
    '    return float(value)',
    'target_id = to_number(args.entity_id, "entity_id")',
    'target = [row for row in rows if to_number(first_non_empty(row, "entity_id", "0"), "entity_id") == target_id][0]',
    'score = to_number(first_non_empty(target, "amount", "1"), "amount") + 1',
    'print(json.dumps({"score": score}))',
  ].join('\n');
  const positionFirstSource = safeSource
    .replace('first_non_empty(row, "entity_id", "0")', 'first_non_empty(row, "0", "entity_id")')
    .replace('first_non_empty(target, "amount", "1")', 'first_non_empty(target, "1", "amount")');
  const wrappedPositionFirstSource = [
    safeSource,
    'def get_mapped_field(row, semantic_key, source_column):',
    '    if row:',
    '        return first_non_empty(row, source_column, semantic_key)',
    '    return first_non_empty(row, semantic_key, source_column)',
  ].join('\n');
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], description: '按数据行契约读取并计算。', sourceCode }],
  });
  const core = createFauxCore({ provider: 'provider-row-contract', models: [{ id: 'provider-row-contract' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.doesNotMatch(requestContexts[0], /从命名键改为位置索引/);
  assert.match(requestContexts[0], /本体语义字段优先/);
  assert.match(requestContexts[1], /上次未通过校验的候选补丁/);
  assert.match(requestContexts[1], /定点修复上下文/);
  assert.doesNotMatch(requestContexts[1], /【当前规格】/);
  assert.match(requestContexts[1], /get_field/);
  assert.match(revised.scripts[0].sourceCode, /first_non_empty/);
  assert.match(revised.scripts[0].sourceCode, /float\(value\)/);
  assert.doesNotMatch(JSON.stringify(revised.scripts[0].evidence ?? []), /从命名键改为位置索引/);
  assert.match(JSON.stringify(revised.scripts[0].evidence ?? []), /本体语义字段优先/);
});

test('结构化输入的数值字段必须在比较前逐项归一', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'validate_records', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)',
    'parser.add_argument("--threshold", required=True)',
    'args = parser.parse_args()',
    'print(json.dumps({"valid": True}))',
  ].join('\n');
  spec.scripts = [{
    key: 'validate_records', title: '结构化输入校验', description: '校验运行时记录。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'validate_records.py', timeoutSeconds: 30,
    parameters: [
      { name: 'records', flag: '--records', type: 'string', required: true },
      { name: 'threshold', flag: '--threshold', type: 'string', required: true },
    ],
    outputs: [{ key: 'valid', type: 'boolean' }], boundaries: ['非法数值必须明确拒绝。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-structured-numeric-normalization', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-structured-numeric-normalization', projectId: project.id, roundId: 'r-structured-numeric-normalization', basedVersion: '1.0.0',
    title: '归一结构化数值输入', detail: 'CLI JSON 中每个数值字段都可能是字符串或数字，必须在比较和计算前逐项显式转换。',
    kind: 'modify_script', target: 'validate_records', status: 'pending',
    generalizationPlan: '所有结构化数值字段统一走转换函数。', validationPlan: '覆盖字符串数字、原生数字、空值和非法文本。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const unsafeSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)',
    'parser.add_argument("--threshold", required=True)',
    'args = parser.parse_args()',
    'threshold = float(args.threshold)',
    'records = json.loads(args.records)',
    'amount = records[0].get("amount")',
    'valid = amount > threshold',
    'print(json.dumps({"valid": valid}))',
  ].join('\n');
  const safeSource = unsafeSource
    .replace('amount = records[0].get("amount")', 'amount = float(records[0].get("amount"))')
    .replace(
      'valid = amount > threshold',
      'normalized = {"amount": amount}\nvalid = normalized["amount"] > threshold',
    );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'structured-numeric-normalization', models: [{ id: 'structured-numeric-normalization' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /结构化输入变量 amount/);
  assert.match(revised.scripts[0].sourceCode, /amount = float\(/);
});

test('enumerate 解包出的结构化记录数值也必须逐项归一', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'validate_batches', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()',
    'parser.add_argument("--batches", required=True)',
    'args = parser.parse_args()',
    'batches = json.loads(args.batches)',
    'print(json.dumps({"valid": True}))',
  ].join('\n');
  spec.scripts = [{
    key: 'validate_batches', title: '批次输入校验', description: '校验结构化批次输入。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'validate_batches.py', timeoutSeconds: 30,
    parameters: [{ name: 'batches', flag: '--batches', type: 'string', required: true }],
    outputs: [{ key: 'valid', type: 'boolean' }], boundaries: ['字符串数字和原生数字必须采用同一口径。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-enumerated-structured-numeric', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-enumerated-structured-numeric', projectId: project.id, roundId: 'r-enumerated-structured-numeric', basedVersion: '1.0.0',
    title: '归一循环中的结构化数值', detail: '结构化输入中的数值可能是字符串或数字，循环解包后的每条记录也必须在比较和计算前显式转换。',
    kind: 'modify_script', target: 'validate_batches', status: 'pending',
    generalizationPlan: '结构化记录无论如何迭代都先做类型归一。', validationPlan: '覆盖 enumerate 元组解包、字符串数字和原生数字。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const unsafeSource = baseSource.replace(
    'print(json.dumps({"valid": True}))',
    [
      'valid = True',
      'raw_limit = "1"',
      'default_limit = float(raw_limit)',
      'for index, batch in enumerate(batches):',
      '    amount = batch.get("amount", 0)',
      '    days = batch.get("days", 0)',
      '    if amount <= 0 or days <= 0:',
      '        valid = False',
      '    projected = batch["amount"] * batch["days"]',
      'print(json.dumps({"valid": valid}))',
    ].join('\n'),
  );
  const safeSource = unsafeSource
    .replace('amount = batch.get("amount", 0)', 'amount = float(batch.get("amount", 0))')
    .replace('days = batch.get("days", 0)', 'days = float(batch.get("days", 0))')
    .replace('projected = batch["amount"] * batch["days"]', 'projected = amount * days');
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'enumerated-structured-numeric', models: [{ id: 'enumerated-structured-numeric' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /amount/);
  assert.match(requestContexts[1], /days/);
  assert.match(revised.scripts[0].sourceCode, /amount = float\(/);
  assert.match(revised.scripts[0].sourceCode, /projected = amount \* days/);
});

test('可选数值字段必须先判空再转换', async () => {
  const spec = baseSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    fields: [
      ...spec.objects[0].fields,
      { key: 'optionalScore', cnName: '可选评分', type: 'number', required: false, sourceColumn: 'optional_score' },
    ],
  };
  spec.operations[0] = { ...spec.operations[0], script: { key: 'read_optional_score', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--records", required=True)',
    'args = parser.parse_args()', 'records = json.loads(args.records)',
    'print(json.dumps({"scores": []}))',
  ].join('\n');
  spec.scripts = [{
    key: 'read_optional_score', title: '读取可选评分', description: '读取数据源中的可选数值字段。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'read_optional_score.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'scores', type: 'array<number>' }], boundaries: ['缺失可选字段不能中断整批处理。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-optional-numeric', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-optional-numeric', projectId: project.id, roundId: 'r-optional-numeric', basedVersion: '1.0.0',
    title: '保护可选数值字段', detail: '数据源中的可选数值字段必须先读取并判断非空，再进行数值转换。',
    kind: 'modify_script', target: 'read_optional_score', status: 'pending',
    generalizationPlan: '所有 optional 数值字段采用先判空后转换。', validationPlan: '覆盖 null、空字符串、字符串数字和原生数字。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const helper = [
    'def normalize_number(raw, field_name):',
    '    if raw is None or raw == "":',
    '        raise ValueError(f"missing {field_name}")',
    '    return float(raw)',
    '',
    'def get_numeric(row, field_name, source_column):',
    '    raw = row.get(field_name, row.get(source_column))',
    '    return normalize_number(raw, field_name)',
  ].join('\n');
  const unsafeSource = baseSource.replace(
    'print(json.dumps({"scores": []}))',
    `${helper}\nscores = [get_numeric(record, "optionalScore", "optional_score") for record in records]\nprint(json.dumps({"scores": scores}))`,
  );
  const safeSource = baseSource.replace(
    'print(json.dumps({"scores": []}))',
    `${helper}\nscores = []\nfor record in records:\n    raw_score = record.get("optionalScore", record.get("optional_score"))\n    if raw_score not in (None, ""):\n        scores.append(normalize_number(raw_score, "optionalScore"))\nprint(json.dumps({"scores": scores}))`,
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'optional-numeric', models: [{ id: 'optional-numeric' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /raw_score not in \(None, ""\)/);
});

test('互斥分配脚本候选必须先按稳定业务键去重', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'allocate_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"count": len(records)}))',
  ].join('\n');
  spec.scripts = [{
    key: 'allocate_records', title: '记录分配', description: '按路由分配逻辑贡献。', boundOperation: 'recommend',
    runtime: 'python', entrypoint: 'allocate_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'count', type: 'number' }], boundaries: ['互斥目标只能计入一次。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-deduplicate-routing', owner: 'tester', name: '测试', domain: '通用分配', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-deduplicate-routing', projectId: project.id, roundId: 'r-deduplicate-routing', basedVersion: '1.0.0',
    title: '修复同一逻辑贡献的互斥分配',
    detail: '两条物理记录可能表示同一逻辑贡献，必须先合并后再按运行时路由只分配到一个目标，且总量只计一次；路由输入不在允许值中时必须明确拒绝。',
    kind: 'modify_script', target: 'allocate_records', status: 'pending',
    generalizationPlan: '根据能代表同一贡献的业务属性合并记录，再根据运行时路由只分配到一个目标。',
    validationPlan: '同一逻辑贡献存在多条物理记录、不同路由和单条记录时均验证总量守恒。',
    createdAt: '2026-07-24T00:00:00.000Z',
  };
  const deduplicatedOnlySource = baseSource.replace(
    'print(json.dumps({"count": len(records)}))',
    'seen_business_keys = set()\ncount = 0\nfor record in records:\n    business_key = record["business_key"]\n    if business_key in seen_business_keys:\n        continue\n    seen_business_keys.add(business_key)\n    count += record["value"]\nprint(json.dumps({"count": count}))',
  );
  const misleadingSetSource = baseSource.replace(
    'print(json.dumps({"count": len(records)}))',
    [
      'allowed_routes = {"left", "right"}',
      'logical_values = []',
      'unrelated_category_ids = set()',
      'for record in records:',
      '    route = record.get("route")',
      '    if route not in allowed_routes:',
      '        raise ValueError(f"unsupported route: {route}")',
      '    business_key = record["business_key"]  # 稳定业务键去重',
      '    unrelated_category_ids.add(record.get("category_id"))',
      '    logical_values.append(float(record["value"]))',
      'count = sum(logical_values)',
      'expected_total = sum(logical_values)',
      'if abs(count - expected_total) > 1e-9:',
      '    raise RuntimeError("allocation conservation failed")',
      'print(json.dumps({"count": count}))',
    ].join('\n'),
  );
  const commentedDedupSource = baseSource.replace(
    'print(json.dumps({"count": len(records)}))',
    [
      'allowed_routes = {"left", "right"}',
      'seen_business_keys = set()',
      'logical_values = []',
      'for record in records:',
      '    route = record.get("route")',
      '    if route not in allowed_routes:',
      '        raise ValueError(f"unsupported route: {route}")',
      '    business_key = record["business_key"]',
      '    # if business_key in seen_business_keys:',
      '    #     continue',
      '    # seen_business_keys.add(business_key)',
      '    logical_values.append(float(record["value"]))',
      'count = sum(logical_values)',
      'expected_total = sum(logical_values)',
      'if abs(count - expected_total) > 1e-9:',
      '    raise RuntimeError("allocation conservation failed")',
      'print(json.dumps({"count": count}))',
    ].join('\n'),
  );
  const safeSource = baseSource.replace(
    'print(json.dumps({"count": len(records)}))',
    [
      'allowed_routes = {"left", "right"}',
      'logical_values = {}',
      'for record in records:',
      '    route = record.get("route")',
      '    if route not in allowed_routes:',
      '        raise ValueError(f"unsupported route: {route}")',
      '    business_key = record["business_key"]',
      '    logical_values.setdefault(business_key, float(record["value"]))',
      'seen_business_keys = set()',
      'count = 0.0',
      'for business_key, value in logical_values.items():',
      '    if business_key in seen_business_keys:',
      '        continue',
      '    seen_business_keys.add(business_key)',
      '    count += value',
      'expected_total = sum(logical_values.values())',
      'if abs(count - expected_total) > 1e-9:',
      '    raise RuntimeError("allocation conservation failed")',
      'print(json.dumps({"count": count}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], description: '按稳定业务键去重并互斥分配。', sourceCode }],
  });
  const core = createFauxCore({ provider: 'deduplicate-routing', models: [{ id: 'deduplicate-routing' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(misleadingSetSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /seen_business_keys/);
  assert.match(revised.scripts[0].sourceCode, /allowed_routes/);
  assert.match(revised.scripts[0].sourceCode, /allocation conservation failed/);
});

test('特殊类别成员资格不能只由任意关联字段非空推断', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'classify_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"special": []}))',
  ].join('\n');
  spec.scripts = [{
    key: 'classify_records', title: '记录分类', description: '根据显式业务分类识别特殊记录。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'classify_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'special', type: 'array<string>' }], boundaries: ['成员资格必须可验证。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-explicit-classification', owner: 'tester', name: '测试', domain: '通用分类', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-explicit-classification', projectId: project.id, roundId: 'r-explicit-classification', basedVersion: '1.0.0',
    title: '修复特殊类别识别',
    detail: '特殊类别或流向的成员资格必须来自明确的数据字段或可验证规则，不得仅以任意关联字段非空来推断。',
    kind: 'modify_script', target: 'classify_records', status: 'pending',
    generalizationPlan: '使用显式分类字段识别成员资格。', validationPlan: '覆盖关联字段存在但分类不匹配的记录。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const presenceOnlySource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'special_ids = set()',
      'for record in records:',
      '    left_id = record.get("left_id")',
      '    right_id = record.get("right_id")',
      '    if left_id is not None or right_id is not None:',
      '        special_ids.add(record["id"])',
      'print(json.dumps({"special": sorted(special_ids)}))',
    ].join('\n'),
  );
  const hardCodedSource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'SPECIAL_RECORD_IDS = {10, 20}',
      'special_ids = set()',
      'for record in records:',
      '    if record["id"] in SPECIAL_RECORD_IDS:',
      '        special_ids.add(record["id"])',
      'print(json.dumps({"special": sorted(special_ids)}))',
    ].join('\n'),
  );
  const hardCodedNamesSource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'special_ids = set()',
      'for record in records:',
      '    group_name = record.get("group_name")',
      '    if group_name in ("category_a", "category_b"):',
      '        special_ids.add(record["id"])',
      'print(json.dumps({"special": sorted(special_ids)}))',
    ].join('\n'),
  );
  const explicitSource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'special_ids = set()',
      'for record in records:',
      '    if record.get("classification") == "special":',
      '        special_ids.add(record["id"])',
      'print(json.dumps({"special": sorted(special_ids)}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'explicit-classification', models: [{ id: 'explicit-classification' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(hardCodedSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(explicitSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /classification/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /left_id is not None or right_id is not None/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /group_name in/);
});

test('特殊类别不能用目标对象名称的子串猜测', async () => {
  const spec = baseSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    fields: [
      ...spec.objects[0].fields,
      { key: 'sourceName', cnName: '来源名称', type: 'string', required: true, sourceColumn: 'source_name' },
      { key: 'targetGroupName', cnName: '目标分组名称', type: 'string', required: true, sourceColumn: 'target_group_name' },
    ],
  };
  spec.operations[0] = { ...spec.operations[0], script: { key: 'classify_runtime_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"special": []}))',
  ].join('\n');
  spec.scripts = [{
    key: 'classify_runtime_records', title: '运行时记录分类', description: '按显式来源分类字段识别记录。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'classify_runtime_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'special', type: 'array<string>' }], boundaries: ['分类来源必须可追溯。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-substring-classification', owner: 'tester', name: '测试', domain: '通用分类', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-substring-classification', projectId: project.id, roundId: 'r-substring-classification', basedVersion: '1.0.0',
    title: '使用显式来源分类', detail: '分类与路由必须来自明确的 sourceName（来源名称）字段或关系，不能从无关目标对象名称推断。',
    kind: 'modify_script', target: 'classify_runtime_records', status: 'pending',
    generalizationPlan: '按来源记录的显式分类字段判断。', validationPlan: '覆盖目标名称含相似词但来源分类不同的记录。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const substringSource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'special_ids = []',
      'for record in records:',
      '    target_group_name = record.get("target_group_name")',
      '    if "priority" in str(target_group_name):',
      '        special_ids.append(record["id"])',
      'print(json.dumps({"special": special_ids}))',
    ].join('\n'),
  );
  const explicitSource = baseSource.replace(
    'print(json.dumps({"special": []}))',
    [
      'special_ids = []',
      'for record in records:',
      '    raw_source_name = record.get("sourceName")',
      '    source_name = str(raw_source_name).strip() if raw_source_name is not None else ""',
      '    if "priority" in source_name:',
      '        special_ids.append(record["id"])',
      'print(json.dumps({"special": special_ids}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'substring-classification', models: [{ id: 'substring-classification' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(substringSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(explicitSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /source_name/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /target_group_name/);
});

test('特殊类别可以使用显式来源名称字段的受控匹配', async () => {
  const spec = baseSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    fields: [
      ...spec.objects[0].fields,
      { key: 'sourceProductName', cnName: '来源产品名称', type: 'string', required: true, sourceColumn: 'source_product_name' },
    ],
  };
  spec.operations[0] = { ...spec.operations[0], script: { key: 'classify_source_records', required: true } };
  const sourceCode = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'special_ids = []',
    'for record in records:',
    '    source_product_name = str(record.get("sourceProductName", "")).strip()',
    '    if "priority" in source_product_name:',
    '        special_ids.append(record["id"])',
    'print(json.dumps({"special": special_ids}))',
  ].join('\n');
  spec.scripts = [{
    key: 'classify_source_records', title: '来源记录分类', description: '从明确的来源产品名称字段识别分类。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'classify_source_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'special', type: 'array<string>' }], boundaries: ['不得使用目标对象名称反推来源分类。'], sourceCode: 'print("base")',
  }];
  const project: Project = {
    id: 'p-source-name-classification', owner: 'tester', name: '测试', domain: '通用分类', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-source-name-classification', projectId: project.id, roundId: 'r-source-name-classification', basedVersion: '1.0.0',
    title: '使用显式来源字段', detail: '特殊类别成员资格必须来自明确的 sourceProductName（来源产品名称）运行时字段，不能从无关目标对象名称推断。',
    kind: 'modify_script', target: 'classify_source_records', status: 'pending',
    generalizationPlan: '只使用来源记录自身的显式名称或分类字段。', validationPlan: '覆盖来源名称匹配和目标名称相似但来源不同的记录。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const companionSuggestion: OptSuggestion = {
    ...suggestion,
    id: 's-source-name-companion',
    title: '修正同一脚本的其他输入处理',
    detail: '修正同一脚本的结构化输入处理，并保留已确认的分类来源。',
    candidateFeedback: '分类必须来自明确的运行时来源记录字段，不能从无关目标对象名称推断。',
  };
  const submitted = {
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts[0], sourceCode }],
  };
  const core = createFauxCore({ provider: 'source-name-classification', models: [{ id: 'source-name-classification' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted)])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion, companionSuggestion], llm);

  assert.equal(core.state.callCount, 1);
  assert.match(revised.scripts[0].sourceCode, /source_product_name/);
});

test('命令行结构化示例必须使用占位值', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'run_plan', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--payload", required=True)', 'args = parser.parse_args()',
    'print(json.dumps({"ok": True}))',
  ].join('\n');
  spec.scripts = [{
    key: 'run_plan', title: '执行计划', description: '处理结构化计划输入。', boundOperation: 'recommend',
    runtime: 'python', entrypoint: 'run_plan.py', timeoutSeconds: 30,
    parameters: [{ name: 'payload', flag: '--payload', type: 'string', required: true }],
    outputs: [{ key: 'ok', type: 'boolean' }], boundaries: ['帮助文本不得固化当前业务记录。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-cli-placeholder', owner: 'tester', name: '测试', domain: '通用输入', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-cli-placeholder', projectId: project.id, roundId: 'r-cli-placeholder', basedVersion: '1.0.0',
    title: '移除当前记录示例', detail: '命令行帮助和示例必须使用符号占位或独立合成值，不能固化当前业务记录。',
    kind: 'modify_script', target: 'run_plan', status: 'pending',
    generalizationPlan: '结构化样例只展示字段形状。', validationPlan: '扫描帮助文本中的具体业务名称和数值。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const concreteSource = baseSource.replace(
    'parser.add_argument("--payload", required=True)',
    [
      'parser.add_argument(',
      '    "--payload", required=True,',
      '    help=\'格式：[{"itemName":"现有业务项",\'',
      '         \'"amount":1.5}]\'',
      ')',
    ].join('\n'),
  );
  const placeholderSource = baseSource.replace(
    'parser.add_argument("--payload", required=True)',
    [
      'parser.add_argument(',
      '    "--payload", required=True,',
      '    help=\'格式：[{"itemName":"<业务项>",\'',
      '         \'"amount":"<数值>"}]；允许类别："route_a" 或 "route_b"\'',
      ')',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'cli-placeholder', models: [{ id: 'cli-placeholder' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(concreteSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(placeholderSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /<业务项>/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /现有业务项/);
});

test('规则不得保留未决的模糊或精确匹配二选一', async () => {
  const spec = baseSpec();
  const targetRuleKey = spec.rules![0].key;
  spec.rules = [{
    ...spec.rules![0], key: targetRuleKey, title: '运行时映射', description: '按运行时数据建立映射。',
    content: '输入名称必须在运行时数据中存在。', conditions: [], exceptions: [],
  }];
  const project: Project = {
    id: 'p-match-strategy', owner: 'tester', name: '测试', domain: '通用映射', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-match-strategy', projectId: project.id, roundId: 'r-match-strategy', basedVersion: '1.0.0',
    title: '明确映射匹配策略', detail: '规则文档与脚本的匹配策略必须一致，不能保留模糊匹配或精确匹配的未决二选一。',
    kind: 'modify_rule', target: targetRuleKey, status: 'pending',
    generalizationPlan: '选择单一可验证的匹配策略。', validationPlan: '覆盖近似名称和规范化后的精确名称。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const submitted = (rule: NonNullable<PackageSpec['rules']>[number]) => ({
    objects: spec.objects, operations: spec.operations, rules: [rule], metrics: spec.metrics, scripts: spec.scripts ?? [],
  });
  const ambiguousRule = {
    ...spec.rules[0],
    content: '从运行时数据建立名称映射。',
    exceptions: ['名称略有差异时，查询需考虑模糊匹配或精确匹配策略。'],
  };
  const exactRule = {
    ...spec.rules[0],
    content: '输入名称先做 Unicode 规范化和首尾空白清理，再执行精确匹配。',
    exceptions: ['禁止未经确认的模糊匹配；无精确结果时返回未找到。'],
  };
  const core = createFauxCore({ provider: 'match-strategy', models: [{ id: 'match-strategy' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(ambiguousRule))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(exactRule))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.rules[0].content, /精确匹配/);
  assert.doesNotMatch(revised.rules[0].exceptions?.join('\n') ?? '', /模糊匹配或精确匹配/);
});

test('比例和百分数混合输入必须先统一量纲再比较', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'compare_threshold', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--payload", required=True)', 'args = parser.parse_args()',
    'payload = json.loads(args.payload)', 'print(json.dumps({"exceeded": False}))',
  ].join('\n');
  spec.scripts = [{
    key: 'compare_threshold', title: '阈值比较', description: '比较运行值和阈值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'compare_threshold.py', timeoutSeconds: 30,
    parameters: [{ name: 'payload', flag: '--payload', type: 'string', required: true }],
    outputs: [{ key: 'exceeded', type: 'boolean' }], boundaries: ['比例量纲必须一致。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-percentage-scale', owner: 'tester', name: '测试', domain: '通用阈值', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-percentage-scale', projectId: project.id, roundId: 'r-percentage-scale', basedVersion: '1.0.0',
    title: '修正阈值脚本', detail: '修正 compare_threshold 脚本的运行时阈值比较实现。',
    kind: 'modify_script', target: 'compare_threshold', status: 'pending',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const ruleSuggestion: OptSuggestion = {
    id: 's-percentage-rule', projectId: project.id, roundId: 'r-percentage-scale', basedVersion: '1.0.0',
    title: '统一阈值量纲',
    detail: '阈值可能是 0.9 这类小数比例，也可能是 90 这类百分数；compare_threshold 脚本必须统一到百分比量纲后再比较。',
    kind: 'modify_rule', target: spec.rules![0].key, status: 'pending',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const revisedRule = { ...spec.rules![0], description: '阈值先统一量纲后再判定。' };
  const rawComparison = baseSource.replace(
    'print(json.dumps({"exceeded": False}))',
    [
      'rate_percent = float(payload.get("rate_percent"))',
      'threshold = float(payload.get("threshold"))',
      'print(json.dumps({"exceeded": rate_percent > threshold}))',
    ].join('\n'),
  );
  const normalizedComparison = baseSource.replace(
    'print(json.dumps({"exceeded": False}))',
    [
      'def normalize_percent(value):',
      '    number = float(value)',
      '    return number * 100.0 if abs(number) <= 1.0 else number',
      'rate_percent = normalize_percent(payload.get("rate_percent"))',
      'threshold_percent = normalize_percent(payload.get("threshold"))',
      'print(json.dumps({"exceeded": rate_percent > threshold_percent}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: [revisedRule], metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'percentage-scale', models: [{ id: 'percentage-scale' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(rawComparison))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(normalizedComparison))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion, ruleSuggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /normalize_percent/);
  assert.match(revised.scripts[0].sourceCode, /\* 100\.0/);
});

test('比例和百分数也可以统一归一为小数比例', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'compare_ratio_threshold', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--payload", required=True)', 'args = parser.parse_args()',
    'payload = json.loads(args.payload)', 'print(json.dumps({"exceeded": False}))',
  ].join('\n');
  spec.scripts = [{
    key: 'compare_ratio_threshold', title: '比例阈值比较', description: '比较运行比例和阈值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'compare_ratio_threshold.py', timeoutSeconds: 30,
    parameters: [{ name: 'payload', flag: '--payload', type: 'string', required: true }],
    outputs: [{ key: 'exceeded', type: 'boolean' }], boundaries: ['比例量纲必须一致。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-ratio-scale', owner: 'tester', name: '测试', domain: '通用阈值', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-ratio-scale', projectId: project.id, roundId: 'r-ratio-scale', basedVersion: '1.0.0',
    title: '统一阈值量纲', detail: '比例与百分比字段比较前必须统一量纲。',
    kind: 'modify_script', target: 'compare_ratio_threshold', status: 'pending',
    candidateFeedback: '输入可能使用小数比例或百分数表示，必须兼容两种形式后再比较。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const normalizedComparison = baseSource.replace(
    'print(json.dumps({"exceeded": False}))',
    [
      'def normalize_ratio(value):',
      '    number = float(value)',
      '    if number < 0 or number > 100:',
      '        raise ValueError("ratio is outside the supported range")',
      '    return number / 100.0 if number > 1.0 else number',
      'rate_ratio = normalize_ratio(payload.get("rate"))',
      'threshold_ratio = normalize_ratio(payload.get("threshold"))',
      'print(json.dumps({"exceeded": rate_ratio > threshold_ratio}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'ratio-scale', models: [{ id: 'ratio-scale' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(normalizedComparison))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 1);
  assert.match(revised.scripts[0].sourceCode, /normalize_ratio/);
  assert.match(revised.scripts[0].sourceCode, /\/ 100\.0/);
});

test('脚本参数声明枚举路由时必须拒绝未知值', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'route_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"left": 0, "right": 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'route_records', title: '记录路由', description: '把记录互斥分配到目标。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'route_records.py', timeoutSeconds: 30,
    parameters: [{
      name: 'records', flag: '--records', type: 'string', required: true,
      description: 'JSON 数组，每条记录的 direction 只能为 left 或 right。',
    }],
    outputs: [{ key: 'left', type: 'number' }, { key: 'right', type: 'number' }],
    boundaries: ['所有记录只能进入一个目标。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-enumerated-route-contract', owner: 'tester', name: '测试', domain: '通用路由', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-enumerated-route-contract', projectId: project.id, roundId: 'r-enumerated-route-contract', basedVersion: '1.0.0',
    title: '修正路由汇总', detail: '按运行时字段完成互斥汇总。', kind: 'modify_script', target: 'route_records',
    status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const implicitElseSource = baseSource.replace(
    'print(json.dumps({"left": 0, "right": 0}))',
    [
      'left = 0', 'right = 0',
      'for record in records:',
      '    direction = record.get("direction")',
      '    if direction == "left":', '        left += 1',
      '    else:', '        right += 1',
      'print(json.dumps({"left": left, "right": right}))',
    ].join('\n'),
  );
  const guardedSource = baseSource.replace(
    'print(json.dumps({"left": 0, "right": 0}))',
    [
      'allowed_directions = {"left", "right"}',
      'left = 0', 'right = 0',
      'for record in records:',
      '    direction = record.get("direction")',
      '    if direction not in allowed_directions:',
      '        raise ValueError(f"unsupported direction: {direction}")',
      '    if direction == "left":', '        left += 1',
      '    else:', '        right += 1',
      'print(json.dumps({"left": left, "right": right}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'enumerated-route-contract', models: [{ id: 'enumerated-route-contract' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(implicitElseSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(guardedSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /not in allowed_directions/);
});

test('结构化输入数值不能被无关的数值转换掩盖', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'sum_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"total": 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'sum_records', title: '数值汇总', description: '汇总结构化输入中的数值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'sum_records.py', timeoutSeconds: 30,
    parameters: [{
      name: 'records', flag: '--records', type: 'string', required: true,
      description: 'JSON 数组，amount 在参与比较和计算前必须显式转换或严格校验类型。',
    }],
    outputs: [{ key: 'total', type: 'number' }], boundaries: ['非法数值必须拒绝。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-structured-number-normalization', owner: 'tester', name: '测试', domain: '通用汇总', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-structured-number-normalization', projectId: project.id, roundId: 'r-structured-number-normalization', basedVersion: '1.0.0',
    title: '修正输入汇总', detail: '结构化输入中的数值在比较和运算前必须显式转换或严格校验类型。',
    kind: 'modify_script', target: 'sum_records', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const unsafeSource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'def safe_float(value):', '    return float(value)',
      'unrelated = safe_float("1")',
      'total = 0',
      'for record in records:',
      '    total += record["amount"]',
      'print(json.dumps({"total": total, "unrelated": unrelated}))',
    ].join('\n'),
  );
  const safeSource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'total = 0.0',
      'for record in records:',
      '    amount = float(record.get("amount"))',
      '    if amount <= 0:', '        continue',
      '    total += amount',
      'print(json.dumps({"total": total}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'structured-number-normalization', models: [{ id: 'structured-number-normalization' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context).replaceAll('\\', ''));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /下标表达式 record\["amount"\]/);
  assert.match(revised.scripts[0].sourceCode, /amount = float/);
});

test('优化候选一次返回全部确定性校验问题供下一轮统一修复', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'normalize_and_allocate', required: true } };
  spec.scripts = [{
    key: 'normalize_and_allocate', title: '数据归一与分配', description: '读取数据并按路由互斥分配。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'normalize_and_allocate.py', timeoutSeconds: 30,
    parameters: [{ name: 'route', flag: '--route', type: 'string', required: true }],
    outputs: [{ key: 'total', type: 'number' }], boundaries: ['未知路由必须拒绝。'],
    sourceCode: [
      'import argparse', 'import json',
      'parser = argparse.ArgumentParser()', 'parser.add_argument("--route", required=True)',
      'args = parser.parse_args()', 'print(json.dumps({"total": 0}))',
    ].join('\n'),
  }];
  const project: Project = {
    id: 'p-all-validation-errors', owner: 'tester', name: '测试', domain: '通用分配', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-all-validation-errors', projectId: project.id, roundId: 'r-all-validation-errors', basedVersion: '1.0.0',
    title: '统一数据契约与互斥分配',
    detail: '命名字段可能为空字符串，实际值位于来源列；两条重复物理记录表示同一逻辑贡献，必须按稳定业务键去重并只分配一次；路由不在允许值时明确拒绝，并校验总量守恒。',
    kind: 'modify_script', target: 'normalize_and_allocate', status: 'pending',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const unsafeSource = [
    'import argparse', 'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--route", required=True)',
    'args = parser.parse_args()', 'client = DataProviderClient()', 'rows = client.query("customer").data',
    'total = sum(row.get("amount", 0) for row in rows)',
    'print(json.dumps({"total": total}))',
  ].join('\n');
  const safeSource = [
    'import argparse', 'import json',
    'from shared_capabilities.ontology_script_sdk import DataProviderClient',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--route", required=True)',
    'args = parser.parse_args()',
    'allowed_routes = {"left", "right"}',
    'if args.route not in allowed_routes:',
    '    raise ValueError(f"unsupported route: {args.route}")',
    'client = DataProviderClient()',
    'result = client.query("customer")',
    'if not result.success:',
    '    raise RuntimeError(result.error or result.message)',
    'def first_non_empty(row, *candidate_keys):',
    '    for key in candidate_keys:',
    '        value = row.get(key)',
    '        if value not in (None, ""):',
    '            return value',
    '    return None',
    'logical_values = {}',
    'for row in result.data:',
    '    business_key = first_non_empty(row, "customer_id", "customerId", "0")',
    '    value = float(first_non_empty(row, "amount", "source_amount", "1"))',
    '    logical_values.setdefault(business_key, value)',
    'seen_business_keys = set()',
    'allocated_total = 0.0',
    'for business_key, value in logical_values.items():',
    '    if business_key in seen_business_keys:',
    '        continue',
    '    seen_business_keys.add(business_key)',
    '    allocated_total += value',
    'expected_total = sum(logical_values.values())',
    'if abs(allocated_total - expected_total) > 1e-9:',
    '    raise RuntimeError("allocation conservation failed")',
    'print(json.dumps({"total": allocated_total}))',
  ].join('\n');
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'all-validation-errors', models: [{ id: 'all-validation-errors' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /空值回退/);
  assert.match(requestContexts[1], /稳定业务键去重/);
  assert.match(requestContexts[1], /路由输入/);
  assert.match(requestContexts[1], /总量守恒/);
  assert.match(revised.scripts[0].sourceCode, /first_non_empty/);
});

test('优化续修补丁会叠加在上一版安全候选上', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'validate_amount', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--payload", required=True)',
    'args = parser.parse_args()', 'print(json.dumps({"valid": True}))',
  ].join('\n');
  spec.scripts = [{
    key: 'validate_amount', title: '数值校验', description: '校验结构化输入。', boundOperation: 'recommend',
    runtime: 'python', entrypoint: 'validate_amount.py', timeoutSeconds: 30,
    parameters: [{ name: 'payload', flag: '--payload', type: 'string', required: true }],
    outputs: [{ key: 'valid', type: 'boolean' }], boundaries: ['非法数值必须拒绝。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-cumulative-repair-patch', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const addRule: OptSuggestion = {
    id: 's-cumulative-rule', projectId: project.id, roundId: 'r-cumulative-repair', basedVersion: '1.0.0',
    title: '新增运行时映射规则', detail: '映射必须从运行时数据源读取。', kind: 'add_rule', target: 'runtime_mapping',
    status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const modifyScript: OptSuggestion = {
    id: 's-cumulative-script', projectId: project.id, roundId: 'r-cumulative-repair', basedVersion: '1.0.0',
    title: '归一运行时数值', detail: '结构化输入中的数值可能是字符串或数字，比较前必须显式转换。',
    kind: 'modify_script', target: 'validate_amount', status: 'pending',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const runtimeRule = {
    ...spec.rules![0], key: 'runtime_mapping', title: '运行时映射规则',
    description: '根据当前数据源记录解析映射。', content: '运行时查询数据源并按稳定业务键解析映射。',
  };
  const unsafeSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--payload", required=True)',
    'args = parser.parse_args()', 'payload = json.loads(args.payload)',
    'amount = payload.get("amount")', 'valid = amount > 0',
    'print(json.dumps({"valid": valid}))',
  ].join('\n');
  const safeSource = unsafeSource.replace('amount = payload.get("amount")', 'amount = float(payload.get("amount"))');
  const script = (sourceCode: string) => ({ ...spec.scripts![0], sourceCode });
  const core = createFauxCore({ provider: 'cumulative-repair', models: [{ id: 'cumulative-repair' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { rules: [runtimeRule], scripts: [script(unsafeSource)] })]),
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { scripts: [script(safeSource)] })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [addRule, modifyScript], llm);

  assert.equal(core.state.callCount, 2);
  assert.ok(revised.rules.some((rule) => rule.key === 'runtime_mapping'));
  assert.match(revised.scripts[0].sourceCode, /float\(payload\.get/);
});

test('优化续修在第二次尝试中带回首次确定性错误', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'validate_payload', required: true } };
  const baseSource = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--payload", required=True)',
    'args = parser.parse_args()', 'payload = json.loads(args.payload)',
    'print(json.dumps({"valid": True}))',
  ].join('\n');
  spec.scripts = [{
    key: 'validate_payload', title: '载荷校验', description: '校验结构化输入中的数值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'validate_payload.py', timeoutSeconds: 30,
    parameters: [{
      name: 'payload', flag: '--payload', type: 'string', required: true,
      description: 'JSON 对象，amount 可能是字符串或数字，比较前必须显式转换。',
    }],
    outputs: [{ key: 'valid', type: 'boolean' }], boundaries: ['非法数值必须拒绝。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-accumulated-validation-errors', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-accumulated-validation-errors', projectId: project.id, roundId: 'r-accumulated-validation-errors', basedVersion: '1.0.0',
    title: '归一结构化数值', detail: '结构化输入中的数值可能是字符串或数字，比较前必须显式转换。',
    kind: 'modify_script', target: 'validate_payload', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const assignedRawSource = baseSource.replace(
    'print(json.dumps({"valid": True}))',
    'amount = payload.get("amount")\nvalid = amount > 0\nprint(json.dumps({"valid": valid}))',
  );
  const directRawSource = baseSource.replace(
    'print(json.dumps({"valid": True}))',
    'valid = payload["amount"] > 0\nprint(json.dumps({"valid": valid}))',
  );
  const bothRawSource = baseSource.replace(
    'print(json.dumps({"valid": True}))',
    'amount = payload.get("amount")\ndirect = payload["amount"]\nvalid = amount > 0 and payload["amount"] > 0\nprint(json.dumps({"valid": valid}))',
  );
  const safeSource = baseSource.replace(
    'print(json.dumps({"valid": True}))',
    'amount = float(payload.get("amount"))\nvalid = amount > 0\nprint(json.dumps({"valid": valid}))',
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'accumulated-validation-errors', models: [{ id: 'accumulated-validation-errors' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(bothRawSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context).replaceAll('\\', ''));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /结构化输入变量 amount/);
  assert.match(revised.scripts[0].sourceCode, /amount = float/);
});

test('修改既有脚本时必须保留运行时绑定和入口文件名', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'validate_payload', required: true } };
  const sourceCode = [
    'import argparse', 'import json',
    'parser = argparse.ArgumentParser()', 'parser.add_argument("--payload", required=True)',
    'args = parser.parse_args()', 'payload = json.loads(args.payload)',
    'amount = float(payload.get("amount"))',
    'print(json.dumps({"valid": amount > 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'validate_payload', title: '载荷校验', description: '校验结构化输入中的数值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'validate_payload.py', timeoutSeconds: 30,
    parameters: [{
      name: 'payload', flag: '--payload', type: 'string', required: true,
      description: 'JSON 对象，amount 可能是字符串或数字，比较前必须显式转换。',
    }],
    outputs: [{ key: 'valid', type: 'boolean' }], boundaries: ['非法数值必须拒绝。'], sourceCode,
  }];
  const project: Project = {
    id: 'p-script-runtime-identity', owner: 'tester', name: '测试', domain: '通用校验', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-script-runtime-identity', projectId: project.id, roundId: 'r-script-runtime-identity', basedVersion: '1.0.0',
    title: '完善载荷校验', detail: '补充结构化数值输入校验。', kind: 'modify_script', target: 'validate_payload',
    status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const revisedSource = sourceCode.replace(
    'amount = float(payload.get("amount"))',
    'raw_amount = payload.get("amount")\namount = float(raw_amount)',
  );
  const renamed = { ...spec.scripts[0], entrypoint: 'payload_validator.py', sourceCode: revisedSource };
  const emptied = { ...spec.scripts[0], sourceCode: '' };
  const preserved = { ...spec.scripts[0], sourceCode: revisedSource };
  const submitted = (script: NonNullable<PackageSpec['scripts']>[number]) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics, scripts: [script],
  });
  const core = createFauxCore({ provider: 'script-runtime-identity', models: [{ id: 'script-runtime-identity' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted({ ...renamed, sourceCode: '' }))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(preserved))]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /入口文件名/);
  assert.match(requestContexts[1], /源码不能为空/);
  assert.equal(revised.scripts[0].entrypoint, 'validate_payload.py');
  assert.equal(revised.scripts[0].boundOperation, 'recommend');
  assert.equal(revised.scripts[0].runtime, 'python');
});

test('人工反馈要求记录身份时不能用目标分类键覆盖不同记录', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'aggregate_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"total": 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'aggregate_records', title: '记录汇总', description: '按运行时记录汇总数值。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'aggregate_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'total', type: 'number' }], boundaries: ['不同业务记录不得因目标相同而被覆盖。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-record-identity-deduplication', owner: 'tester', name: '测试', domain: '通用汇总', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-record-identity-deduplication', projectId: project.id, roundId: 'r-record-identity-deduplication', basedVersion: '1.0.0',
    title: '修正记录聚合', detail: '按运行时数据完成汇总。', kind: 'modify_script', target: 'aggregate_records', status: 'pending',
    candidateFeedback: '稳定业务键必须优先采用资源声明的主键或记录标识；不能仅因分类或目标相同就合并不同业务记录。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const targetKeySource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'values_by_target = {}',
      'for record in records:',
      '    target_id = record.get("target_id")',
      '    values_by_target[target_id] = float(record.get("value"))',
      'print(json.dumps({"total": sum(values_by_target.values())}))',
    ].join('\n'),
  );
  const recordIdentitySource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'deduplicated_records = {}',
      'for record in records:',
      '    record_id = record.get("record_id")',
      '    if record_id in deduplicated_records:',
      '        continue',
      '    deduplicated_records[record_id] = float(record.get("value"))',
      'print(json.dumps({"total": sum(deduplicated_records.values())}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'record-identity-deduplication', models: [{ id: 'record-identity-deduplication' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(targetKeySource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(recordIdentitySource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /record_id/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /values_by_target/);
});

test('多条来源记录要求求和时不能用目标对象键覆盖前一条记录', async () => {
  const spec = baseSpec();
  spec.operations[0] = { ...spec.operations[0], script: { key: 'aggregate_multiple_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"total": 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'aggregate_multiple_records', title: '多记录汇总', description: '汇总运行时返回的多条独立来源记录。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'aggregate_multiple_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'total', type: 'number' }], boundaries: ['每条独立来源记录最多计入一次。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-multiple-record-aggregation', owner: 'tester', name: '测试', domain: '通用汇总', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-multiple-record-aggregation', projectId: project.id, roundId: 'r-multiple-record-aggregation', basedVersion: '1.0.0',
    title: '正确汇总多条来源记录',
    detail: '同一逻辑流可能返回两条独立来源记录，必须将两条记录的数值之和分配到运行时选择的目标。',
    kind: 'modify_script', target: 'aggregate_multiple_records', status: 'pending',
    generalizationPlan: '未来出现更多来源记录时仍逐条保留并累计，不依赖目标对象标识覆盖记录。',
    validationPlan: '两条记录分别为合成值 4 和 6 时总量为 10；单条记录和新增记录同样正确。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const targetKeyOverwriteSource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'values_by_target = {}',
      'for record in records:',
      '    target_id = record.get("target_id")',
      '    values_by_target[target_id] = float(record.get("value"))',
      'print(json.dumps({"total": sum(values_by_target.values())}))',
    ].join('\n'),
  );
  const recordIdentitySource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'records_by_id = {}',
      'for record in records:',
      '    record_id = record.get("record_id")',
      '    if record_id in records_by_id:',
      '        continue',
      '    records_by_id[record_id] = float(record.get("value"))',
      'print(json.dumps({"total": sum(records_by_id.values())}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'multiple-record-aggregation', models: [{ id: 'multiple-record-aggregation' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(targetKeyOverwriteSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(recordIdentitySource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /record_id/);
  assert.doesNotMatch(revised.scripts[0].sourceCode, /values_by_target/);
});

test('互补关联的多条记录不能先按单一可选关联预过滤', async () => {
  const spec = baseSpec();
  spec.objects[0] = {
    ...spec.objects[0],
    fields: [
      ...spec.objects[0].fields,
      { key: 'leftLink', cnName: '左侧可选关联', type: 'number', required: false, sourceColumn: 'left_link' },
      { key: 'rightLink', cnName: '右侧可选关联', type: 'number', required: false, sourceColumn: 'right_link' },
      { key: 'recordId', cnName: '记录标识', type: 'string', required: true, sourceColumn: 'record_id' },
      { key: 'value', cnName: '数值', type: 'number', required: true, sourceColumn: 'value' },
    ],
  };
  spec.operations[0] = { ...spec.operations[0], script: { key: 'aggregate_linked_records', required: true } };
  const baseSource = [
    'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
    'parser.add_argument("--records", required=True)', 'args = parser.parse_args()',
    'records = json.loads(args.records)', 'print(json.dumps({"total": 0}))',
  ].join('\n');
  spec.scripts = [{
    key: 'aggregate_linked_records', title: '互补关联记录汇总', description: '保留并汇总互补关联的来源记录。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'aggregate_linked_records.py', timeoutSeconds: 30,
    parameters: [{ name: 'records', flag: '--records', type: 'string', required: true }],
    outputs: [{ key: 'total', type: 'number' }], boundaries: ['任一可选关联缺失都不能丢弃记录。'], sourceCode: baseSource,
  }];
  const project: Project = {
    id: 'p-complementary-links', owner: 'tester', name: '测试', domain: '通用汇总', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-complementary-links', projectId: project.id, roundId: 'r-complementary-links', basedVersion: '1.0.0',
    title: '汇总互补关联记录',
    detail: '两条互补关联记录中，一条只有 leftLink 且 rightLink 缺失，另一条只有 rightLink 且 leftLink 缺失；必须保留两条记录并求和，不得先按单一可选关联字段预过滤。',
    kind: 'modify_script', target: 'aggregate_linked_records', status: 'pending',
    generalizationPlan: '先按稳定记录标识保留记录，再根据两个可选关联的并集分类。',
    validationPlan: '覆盖仅左关联、仅右关联和两者均有的合成记录。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const unsafeSource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'selected = []',
      'for record in records:',
      '    raw_left = record.get("leftLink")',
      '    if raw_left is not None:',
      '        selected.append(record)',
      'records_by_id = {}',
      'for record in selected:',
      '    record_id = record["recordId"]',
      '    if record_id in records_by_id:',
      '        continue',
      '    records_by_id[record_id] = float(record["value"])',
      'print(json.dumps({"total": sum(records_by_id.values())}))',
    ].join('\n'),
  );
  const safeSource = baseSource.replace(
    'print(json.dumps({"total": 0}))',
    [
      'records_by_id = {}',
      'for record in records:',
      '    raw_left = record.get("leftLink")',
      '    raw_right = record.get("rightLink")',
      '    if raw_left is None and raw_right is None:',
      '        continue',
      '    record_id = record["recordId"]',
      '    if record_id in records_by_id:',
      '        continue',
      '    records_by_id[record_id] = float(record["value"])',
      'print(json.dumps({"total": sum(records_by_id.values())}))',
    ].join('\n'),
  );
  const submitted = (sourceCode: string) => ({
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics,
    scripts: [{ ...spec.scripts![0], sourceCode }],
  });
  const core = createFauxCore({ provider: 'complementary-links', models: [{ id: 'complementary-links' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(unsafeSource))]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted(safeSource))]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.scripts[0].sourceCode, /raw_left is None and raw_right is None/);
});

test('优化候选规则必须消除阈值冲突并避免位置键硬编码映射', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-rule-consistency', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-rule-consistency', projectId: project.id, roundId: 'r-rule-consistency', basedVersion: '1.0.0',
    title: '统一运行阈值和映射口径', detail: '规则必须使用一致阈值，并通过数据契约动态读取映射。',
    kind: 'modify_rule', target: 'product_match', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const rule = (overrides: Record<string, unknown>) => ({ ...spec.rules![0], ...overrides });
  const contradictory = rule({
    description: '利用率 > 80% 时判定过载。',
    content: '按运行时利用率判断是否过载。',
    exceptions: ['过载应按动态阈值判定，而非 > 80%。'],
  });
  const hardcodedMapping = rule({
    description: '必须使用位置索引读取映射，不得使用命名键。',
    content: '映射关系：甲 → 1；乙 → 2；更新数据表后规则自动适配。',
    exceptions: [],
  });
  const equalsMapping = rule({
    description: '按运行时字段契约读取映射。',
    content: '当前映射关系：1=甲，2=乙，3=丙；数据表新增记录后规则自动适配。',
    exceptions: [],
  });
  const consistent = rule({
    description: '按运行时阈值判断，并通过对象字段契约动态读取映射。',
    content: '优先读取语义字段，缺失时回退来源列字段；阈值来自当前运行参数。',
    conditions: [{ condition: '利用率超过运行时阈值', conclusion: '判定过载' }],
    exceptions: ['字段缺失或阈值非法时停止并返回明确错误。'],
  });
  const response = (changedRule: unknown) => fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
    rules: [changedRule],
  })]);
  const core = createFauxCore({ provider: 'rule-consistency', models: [{ id: 'rule-consistency' }] });
  core.setResponses([response(contradictory), response(consistent)]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /冲突比较 > 80%/);
  assert.match(requestContexts[1], /删除被废弃阈值的否定对照/);
  assert.match(revised.rules[0].content, /语义字段/);
  assert.doesNotMatch(JSON.stringify(revised.rules[0]), /位置索引读取映射|甲 → 1/);
});

test('运行时动态映射不能被当前静态记录封闭', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-dynamic-mapping', owner: 'tester', name: '测试', domain: '通用映射', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-dynamic-mapping', projectId: project.id, roundId: 'r-dynamic-mapping', basedVersion: '1.0.0',
    title: '补充动态映射规则', detail: '映射必须在运行时查询数据源。',
    kind: 'modify_rule', target: 'product_match', status: 'pending',
    candidateFeedback: '动态映射规则不得把当前记录固化成封闭名单，新增记录必须自动可用。',
    createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changed = (content: string, conditions: NonNullable<PackageSpec['rules']>[number]['conditions']) => ({
    ...spec.rules![0], description: '映射关系必须在运行时查询分类字典表。', content, conditions, exceptions: [],
  });
  const closedWorld = changed(
    '已知映射：甲 → 1；乙 → 2。输入不在上述映射中时直接返回错误。',
    [{ condition: '名称未出现在上述映射中', conclusion: '拒绝处理' }],
  );
  const dynamic = changed(
    '每次按输入名称查询分类字典表，以数据源返回的标识为准；表中无匹配记录时返回明确错误。',
    [{ condition: '运行时数据源存在名称匹配记录', conclusion: '采用该记录的标识' }],
  );
  const response = (rule: unknown) => fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { rules: [rule] })]);
  const core = createFauxCore({ provider: 'dynamic-mapping', models: [{ id: 'dynamic-mapping' }] });
  core.setResponses([response(closedWorld), response(dynamic)]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(revised.rules[0].content, /数据源返回/);
  assert.doesNotMatch(revised.rules[0].content, /已知映射|甲 → 1/);
});

test('优化器全量修订遗漏未涉及资源时由服务端保留原资源', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-preserve', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-14T00:00:00.000Z', updatedAt: '2026-07-14T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-preserve', projectId: project.id, roundId: 'r-preserve', basedVersion: '1.0.0',
    title: '新增输入完整性规则', detail: '新增通用输入校验', kind: 'add_rule', target: 'input_completeness',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  };
  const submitted = {
    objects: [],
    operations: spec.operations,
    rules: [...(spec.rules ?? []), {
      key: 'input_completeness', title: '输入完整性', content: '必填输入缺失时停止计算。', appliesToObjects: ['customer'],
    }],
    metrics: spec.metrics,
    scripts: [],
  };
  const core = createFauxCore({ provider: 'preserve-untouched', models: [{ id: 'preserve-untouched' }] });
  core.setResponses(Array.from({ length: 3 }, () => fauxAssistantMessage([
    fauxToolCall('submit_revised_spec', submitted),
  ])));
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 1);
  assert.deepEqual(revised.objects, spec.objects);
  assert.ok(revised.rules.some((rule) => rule.key === 'input_completeness'));
});

test('优化器可以新增受控脚本并绑定目标 operation', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-script', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-14T00:00:00.000Z', updatedAt: '2026-07-14T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-script', projectId: project.id, roundId: 'r-script', basedVersion: '1.0.0',
    title: '用脚本稳定计算钱包份额', detail: '将多步骤计算下沉为受控 Python 脚本', kind: 'add_script',
    target: 'calculate_wallet_share', relatedQuestion: '应该推荐什么产品？',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  };
  const operations = spec.operations.map((operation) => operation.key === 'recommend'
    ? { ...operation, script: { key: 'calculate_wallet_share', required: true } }
    : operation);
  const scripts = [{
    key: 'calculate_wallet_share', title: '钱包份额计算脚本', description: '稳定计算钱包份额。',
    boundOperation: 'recommend', runtime: 'python' as const, entrypoint: 'calculate_wallet_share.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'wallet_share', type: 'number' }], boundaries: ['仅使用调用方传入的结构化数据。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": payload["bank_assets"] / payload["total_assets"]}))',
    ].join('\n'),
  }];

  const revised = await applyOptimizations(project, spec, [suggestion], faux('submit_revised_spec', {
    objects: spec.objects, operations, rules: spec.rules, metrics: spec.metrics, scripts,
  }));

  assert.equal(revised.scripts.length, 1);
  assert.equal(revised.operations[0].script?.key, 'calculate_wallet_share');
  const files = renderPackage({ ...spec, ...revised });
  assert.match(files['scripts/calculate_wallet_share.py'], /wallet_share/);
});

test('修改既有脚本时忽略未授权的 operation 人工计算兜底', async () => {
  const spec = baseSpec();
  spec.operations[0] = {
    ...spec.operations[0],
    description: '使用确定性脚本计算钱包份额。',
    script: { key: 'calculate_wallet_share', required: true },
  };
  spec.scripts = [{
    key: 'calculate_wallet_share', title: '钱包份额计算脚本', description: '按统一口径计算钱包份额。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'calculate_wallet_share.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'wallet_share', type: 'number' }], boundaries: ['只处理结构化输入。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": payload["bank_assets"] / payload["total_assets"]}))',
    ].join('\n'),
  }];
  const project: Project = {
    id: 'p-script-fallback', owner: 'tester', name: '测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-07-24T00:00:00.000Z', updatedAt: '2026-07-24T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-script-fallback', projectId: project.id, roundId: 'r-script-fallback', basedVersion: '1.0.0',
    title: '修复脚本 SDK 调用', detail: '按当前 SDK 契约修复查询和结果读取。', kind: 'modify_script',
    target: 'calculate_wallet_share', status: 'pending', createdAt: '2026-07-24T00:00:00.000Z',
  };
  const operations = spec.operations.map((operation) => operation.key === 'recommend' ? {
    ...operation,
    description: '优先执行确定性脚本；脚本失败后由模型根据规则手工计算并返回结果。',
  } : operation);
  const scripts = spec.scripts.map((script) => ({
    ...script,
    description: '按当前 SDK 契约查询并计算钱包份额。',
  }));
  const submitted = { objects: spec.objects, operations, rules: spec.rules, metrics: spec.metrics, scripts };
  const core = createFauxCore({ provider: 'reject-script-fallback', models: [{ id: 'reject-script-fallback' }] });
  core.setResponses([fauxAssistantMessage([fauxToolCall('submit_revised_spec', submitted)])]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.operations[0].description, '使用确定性脚本计算钱包份额。');
  assert.equal(revised.operations[0].script?.key, 'calculate_wallet_share');
  assert.equal(revised.scripts[0].description, '按当前 SDK 契约查询并计算钱包份额。');
});

test('修改既有脚本时误增同名 operation 会反馈后清理重试基线', async () => {
  const spec = baseSpec();
  spec.operations[0] = {
    ...spec.operations[0],
    description: '使用确定性脚本计算钱包份额。',
    script: { key: 'calculate_wallet_share', required: true },
  };
  spec.scripts = [{
    key: 'calculate_wallet_share', title: '钱包份额计算脚本', description: '按统一口径计算钱包份额。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'calculate_wallet_share.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'wallet_share', type: 'number' }], boundaries: ['只处理结构化输入。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": payload["bank_assets"] / payload["total_assets"]}))',
    ].join('\n'),
  }];
  const project: Project = {
    id: 'p-script-phantom-operation', owner: 'tester', name: '测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-script-phantom-operation', projectId: project.id, roundId: 'r-script-phantom-operation', basedVersion: '1.0.0',
    title: '修复脚本 SDK 调用', detail: '按当前 SDK 契约修复查询和结果读取。', kind: 'modify_script',
    target: 'calculate_wallet_share', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changedScript = { ...spec.scripts[0], description: '按当前 SDK 契约查询并计算钱包份额。' };
  const phantomOperation = {
    ...spec.operations[0],
    key: 'calculate_wallet_share',
    title: '钱包份额计算脚本',
    description: '执行脚本失败后由模型按规则手工计算并返回结果。',
  };
  const core = createFauxCore({ provider: 'phantom-operation', models: [{ id: 'phantom-operation' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
      operations: [phantomOperation], scripts: [changedScript],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { scripts: [changedScript] })]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /operation 新增数量与建议不一致/);
  assert.match(requestContexts[1], /权威执行路径/);
  assert.deepEqual(revised.operations.map((operation) => operation.key), ['recommend']);
  assert.equal(revised.scripts[0].description, changedScript.description);
});

test('服务端补齐脚本绑定后产生的 operation 校验错误会把完整候选带回重试', async () => {
  const spec = baseSpec();
  spec.operations[0] = {
    ...spec.operations[0],
    description: '尚未绑定确定性脚本；脚本失败后由人工计算并返回结果。',
    script: undefined,
  };
  spec.scripts = [{
    key: 'calculate_wallet_share', title: '钱包份额计算脚本', description: '按统一口径计算钱包份额。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'calculate_wallet_share.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'wallet_share', type: 'number' }], boundaries: ['只处理结构化输入。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": float(payload["value"])}))',
    ].join('\n'),
  }];
  const project: Project = {
    id: 'p-auto-binding-repair-context', owner: 'tester', name: '测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-auto-binding-repair-context', projectId: project.id, roundId: 'r-auto-binding-repair-context', basedVersion: '1.0.0',
    title: '修复脚本实现', detail: '修复既有确定性脚本并保持原绑定。', kind: 'modify_script',
    target: 'calculate_wallet_share', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changedScript = { ...spec.scripts[0], description: '按当前运行契约计算钱包份额。' };
  const cleanedOperation = {
    ...spec.operations[0],
    description: '执行确定性脚本；脚本失败时停止并返回必须修复的错误。',
    script: { key: 'calculate_wallet_share', required: true },
  };
  const core = createFauxCore({ provider: 'auto-binding-repair-context', models: [{ id: 'auto-binding-repair-context' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { scripts: [changedScript] })]),
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', { operations: [cleanedOperation] })]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /脚本失败后由人工计算并返回结果/);
  assert.match(requestContexts[1], /calculate_wallet_share/);
  assert.equal(revised.operations[0].description, cleanedOperation.description);
  assert.equal(revised.operations[0].script?.key, 'calculate_wallet_share');
});

test('计划新增 operation 用错 key 且带人工兜底时一次反馈两类错误', async () => {
  const spec = baseSpec();
  spec.operations[0] = {
    ...spec.operations[0],
    description: '执行确定性脚本并返回计算结果。',
    script: { key: 'calculate_wallet_share', required: true },
  };
  spec.scripts = [{
    key: 'calculate_wallet_share', title: '钱包份额计算脚本', description: '按统一口径计算钱包份额。',
    boundOperation: 'recommend', runtime: 'python', entrypoint: 'calculate_wallet_share.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'wallet_share', type: 'number' }], boundaries: ['只处理结构化输入。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)',
      'print(json.dumps({"wallet_share": float(payload["value"])}))',
    ].join('\n'),
  }];
  const project: Project = {
    id: 'p-script-scope-feedback', owner: 'tester', name: '测试', domain: '通用计算', background: '', stage: 'optimize',
    createdAt: '2026-07-25T00:00:00.000Z', updatedAt: '2026-07-25T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-script-scope-feedback', projectId: project.id, roundId: 'r-script-scope-feedback', basedVersion: '1.0.0',
    title: '修复脚本实现', detail: '修复既有确定性脚本并保持原资源边界。', kind: 'modify_script',
    target: 'calculate_wallet_share', status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const addOperationSuggestion: OptSuggestion = {
    id: 's-add-operation-feedback', projectId: project.id, roundId: 'r-script-scope-feedback', basedVersion: '1.0.0',
    title: '新增汇总操作', detail: '新增独立的结构化汇总操作。', kind: 'add_operation',
    target: 'approved_operation', relatedQuestion: '如何执行结构化汇总？',
    status: 'pending', createdAt: '2026-07-25T00:00:00.000Z',
  };
  const changedScript = { ...spec.scripts[0], description: '按当前运行契约计算钱包份额。' };
  const unauthorizedOperation = {
    ...spec.operations[0],
    key: 'calculate_wallet_share',
    title: '钱包份额脚本替代操作',
    description: '脚本失败后改由人工计算并返回结果。',
    script: { key: 'calculate_wallet_share', required: true },
  };
  const approvedOperation = {
    ...spec.operations[0],
    key: 'approved_operation',
    title: '结构化汇总',
    description: '根据结构化输入执行汇总并返回结果。',
    competencyQuestion: '如何执行结构化汇总？',
    script: undefined,
  };
  const core = createFauxCore({ provider: 'script-scope-feedback', models: [{ id: 'script-scope-feedback' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_optimization_patch', {
      scripts: [changedScript], operations: [unauthorizedOperation],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
      objects: spec.objects,
      operations: [...spec.operations, approvedOperation],
      rules: spec.rules,
      metrics: spec.metrics,
      scripts: [changedScript],
    })]),
  ]);
  const requestContexts: string[] = [];
  const llm: LlmDeps = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      requestContexts.push(JSON.stringify(context));
      return core.streamSimple(model as any, context, options as any);
    }) as LlmDeps['streamFn'],
  };

  const revised = await applyOptimizations(project, spec, [suggestion, addOperationSuggestion], llm);

  assert.equal(core.state.callCount, 2);
  assert.match(requestContexts[1], /未新增 operation:approved_operation/);
  assert.match(requestContexts[1], /权威执行路径/);
  assert.equal(revised.operations.some((item) => item.key === unauthorizedOperation.key), false);
  assert.equal(revised.operations.some((item) => item.key === approvedOperation.key), true);
  assert.equal(revised.scripts[0].description, changedScript.description);
});

test('新增脚本已唯一声明绑定操作时由服务端补齐操作侧引用', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-bind-script', owner: 'tester', name: '测试', domain: '通用流程', background: '', stage: 'optimize',
    createdAt: '2026-07-14T00:00:00.000Z', updatedAt: '2026-07-14T00:00:00.000Z',
  };
  const suggestions: OptSuggestion[] = [{
    id: 's-add-operation', projectId: project.id, roundId: 'r-bind', basedVersion: '1.0.0',
    title: '新增计划分析操作', detail: '新增参数化计划分析入口', kind: 'add_operation', target: 'analyze_plan',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  }, {
    id: 's-add-script', projectId: project.id, roundId: 'r-bind', basedVersion: '1.0.0',
    title: '新增计划计算脚本', detail: '新增确定性计划计算脚本', kind: 'add_script', target: 'calculate_plan',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  }];
  const operation = {
    key: 'analyze_plan', title: '计划分析', description: '按结构化批次输入执行通用计划分析。',
    inputs: [], outputs: [], flow: ['校验输入', '执行 script:calculate_plan'], relatedObjects: ['customer'],
    competencyQuestion: '如何分析计划？',
  };
  const script = {
    key: 'calculate_plan', title: '计划计算', description: '对任意数量的结构化批次执行确定性计算。',
    boundOperation: 'analyze_plan', runtime: 'python' as const, entrypoint: 'calculate_plan.py', timeoutSeconds: 30,
    parameters: [{ name: 'input_json', flag: '--input-json', type: 'string', required: true }],
    outputs: [{ key: 'result', type: 'object' }], boundaries: ['只处理结构化输入。'],
    sourceCode: [
      'import argparse', 'import json', 'parser = argparse.ArgumentParser()',
      'parser.add_argument("--input-json", required=True)', 'args = parser.parse_args()',
      'payload = json.loads(args.input_json)', 'print(json.dumps({"result": payload}))',
    ].join('\n'),
  };
  const submitted = {
    objects: spec.objects, operations: [...spec.operations, operation], rules: spec.rules,
    metrics: spec.metrics, scripts: [script],
  };
  const core = createFauxCore({ provider: 'bind-script', models: [{ id: 'bind-script' }] });
  core.setResponses(Array.from({ length: 3 }, () => fauxAssistantMessage([
    fauxToolCall('submit_revised_spec', submitted),
  ])));
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  const revised = await applyOptimizations(project, spec, suggestions, llm);

  assert.equal(core.state.callCount, 1);
  assert.equal(revised.operations.find((item) => item.key === 'analyze_plan')?.script?.key, 'calculate_plan');
  assert.equal(revised.scripts.find((item) => item.key === 'calculate_plan')?.boundOperation, 'analyze_plan');
});

test('优化器新增 operation 用错 key 时携带校验错误自动重交', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-operation-repair', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-16T00:00:00.000Z', updatedAt: '2026-07-16T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-operation-repair', projectId: project.id, roundId: 'r-operation-repair', basedVersion: '1.0.0',
    title: '新增月度生产规划分析', detail: '新增通用生产规划分析操作', kind: 'add_operation',
    target: 'MonthlyProductionPlanAnalysis', relatedQuestion: '月度生产规划如何分析？',
    status: 'pending', createdAt: '2026-07-16T00:00:00.000Z',
  };
  const operation = (key: string) => ({
    key, title: '月度生产规划分析', description: '按结构化输入分析。', inputs: [], outputs: [],
    flow: ['读取输入', '按口径分析'], relatedObjects: ['customer'], competencyQuestion: '月度生产规划如何分析？',
  });
  const core = createFauxCore({ provider: 'repair-operation', models: [{ id: 'repair-operation' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
      objects: spec.objects, operations: [...spec.operations, operation('WrongOperationKey')],
      rules: spec.rules, metrics: spec.metrics, scripts: [],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', {
      objects: spec.objects, operations: [...spec.operations, operation('MonthlyProductionPlanAnalysis')],
      rules: spec.rules, metrics: spec.metrics, scripts: [],
    })]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };
  const progress: { phase: string; attempt: number }[] = [];

  const revised = await applyOptimizations(project, spec, [suggestion], llm, (event) => {
    progress.push({ phase: event.phase, attempt: event.attempt });
  });
  assert.ok(revised.operations.some((item) => item.key === 'MonthlyProductionPlanAnalysis'));
  assert.equal(revised.operations.some((item) => item.key === 'WrongOperationKey'), false);
  assert.deepEqual(progress, [
    { phase: 'revision_started', attempt: 1 },
    { phase: 'validation_failed', attempt: 1 },
    { phase: 'revision_started', attempt: 2 },
    { phase: 'validation_passed', attempt: 2 },
  ]);
});

test('历史建议把 key:title 当 target 时仍能落实到真实操作 key', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-legacy-target', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-26T00:00:00.000Z', updatedAt: '2026-07-26T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-legacy-target', projectId: project.id, roundId: 'r-legacy-target', basedVersion: '1.0.0',
    title: '修复产品推荐路由', detail: '明确调用顺序', kind: 'modify_operation',
    target: 'recommend:产品推荐', status: 'pending', createdAt: '2026-07-26T00:00:00.000Z',
  };
  const changedOperation = {
    ...spec.operations[0],
    description: '根据客户特征并按权威规则路由推荐产品。',
  };

  const revised = await applyOptimizations(project, spec, [suggestion], faux('submit_optimization_patch', {
    operations: [changedOperation],
  }));

  assert.equal(revised.operations.find((item) => item.key === 'recommend')?.description, changedOperation.description);
});

test('优化器连续两次未通过时转为专家决策而不继续空转', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p-expert-review', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-26T00:00:00.000Z', updatedAt: '2026-07-26T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's-expert-review', projectId: project.id, roundId: 'r-expert-review', basedVersion: '1.0.0',
    title: '修复产品推荐路由', detail: '调整操作路由', kind: 'modify_operation', target: 'recommend',
    status: 'pending', createdAt: '2026-07-26T00:00:00.000Z',
  };
  const unchanged = {
    objects: spec.objects, operations: spec.operations, rules: spec.rules, metrics: spec.metrics, scripts: [],
  };
  const core = createFauxCore({ provider: 'expert-review', models: [{ id: 'expert-review' }] });
  core.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', unchanged)]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', unchanged)]),
  ]);
  const llm: LlmDeps = { model: core.getModel(), streamFn: core.streamSimple, getApiKey: () => 'faux' };

  await assert.rejects(
    applyOptimizations(project, spec, [suggestion], llm),
    (error: unknown) => {
      assert.ok(error instanceof OptimizationExpertReviewRequiredError);
      assert.equal(error.validationErrors.length, 2);
      assert.match(error.message, /专家确认|跳过/);
      return true;
    },
  );
  assert.equal(core.state.callCount, 2);
});

test('优化器拒绝用新 key 偷换未涉及的既有指标', async () => {
  const spec = baseSpec();
  const project: Project = {
    id: 'p2', owner: 'tester', name: '测试', domain: '金融', background: '', stage: 'optimize',
    createdAt: '2026-07-14T00:00:00.000Z', updatedAt: '2026-07-14T00:00:00.000Z',
  };
  const suggestion: OptSuggestion = {
    id: 's2', projectId: 'p2', roundId: 'r2', basedVersion: '1.0.0',
    title: '新增重点客户规则', detail: '新增重点客户判断条件', kind: 'add_rule',
    status: 'pending', createdAt: '2026-07-14T00:00:00.000Z',
  };

  const metricReplacement = {
    objects: spec.objects,
    operations: spec.operations,
    rules: [...spec.rules!, { key: 'priority_customer', title: '重点客户', content: '满足条件时标记。' }],
    metrics: [{ ...spec.metrics![0], key: 'replacement_metric' }],
    scripts: spec.scripts ?? [],
  };
  const metricCore = createFauxCore({ provider: 'unplanned-metric', models: [{ id: 'unplanned-metric' }] });
  metricCore.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', metricReplacement)]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', metricReplacement)]),
  ]);
  await assert.rejects(applyOptimizations(project, spec, [suggestion], {
    model: metricCore.getModel(), streamFn: metricCore.streamSimple, getApiKey: () => 'faux',
  }), (error: unknown) => {
    assert.ok(error instanceof OptimizationExpertReviewRequiredError);
    assert.match(error.validationErrors.join('\n'), /metric 新增数量与建议不一致/);
    return true;
  });

  const extraRule = {
    objects: spec.objects,
    operations: spec.operations,
    rules: [
      ...spec.rules!,
      { key: 'priority_customer', title: '重点客户', content: '满足条件时标记。' },
      { key: 'unplanned_rule', title: '计划外规则', content: '不应静默新增。' },
    ],
    metrics: spec.metrics,
    scripts: spec.scripts ?? [],
  };
  const ruleCore = createFauxCore({ provider: 'unplanned-rule', models: [{ id: 'unplanned-rule' }] });
  ruleCore.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', extraRule)]),
    fauxAssistantMessage([fauxToolCall('submit_revised_spec', extraRule)]),
  ]);
  await assert.rejects(applyOptimizations(project, spec, [suggestion], {
    model: ruleCore.getModel(), streamFn: ruleCore.streamSimple, getApiKey: () => 'faux',
  }), (error: unknown) => {
    assert.ok(error instanceof OptimizationExpertReviewRequiredError);
    assert.match(error.validationErrors.join('\n'), /rule 新增数量与建议不一致/);
    return true;
  });
});
