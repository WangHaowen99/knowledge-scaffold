import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';
import {
  confirmedFlowchart,
  operationTargetId,
  operationTargetQuestion,
} from './fixtures/confirmed-flowchart.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'generation-trace-'));

const { createProject } = await import('../src/projects/repo.js');
const { upsertNode, listNodes } = await import('../src/workflow/engine.js');
const { generatePackage } = await import('../src/workflow/m2.js');
const { listTraceEvents } = await import('../src/workflow/trace.js');
const { getAuthoringState } = await import('../src/authoring/state.js');
const { getPiAuthoringAdapter } = await import('../src/authoring/pi-adapter.js');
const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');

async function createGenerationProject(input: Parameters<typeof createProject>[0]) {
  const project = createProject(input);
  await getPackageArtifactStore().initializeDraftPackage(project.id, {
    id: `project-${project.id.replace(/^pj_/, '')}`,
    name: project.name,
    description: project.background || `${project.name} 的知识包生成测试草稿`,
  });
  return project;
}

test('知识包分阶段生成过程持久化', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '生成轨迹', domain: '银行营销', background: '企业客户产品推荐' });
  const processes = [
    { id: 'identify_customer', title: '识别企业客户' },
    { id: 'check_eligibility', title: '校验客户资格' },
    { id: 'analyze_needs', title: '分析客户需求' },
    { id: 'match_product', title: '匹配金融产品' },
    { id: 'deliver_recommendation', title: '交付产品建议' },
  ];
  const flowchart = confirmedFlowchart(processes);
  upsertNode({
    projectId: project.id,
    type: 'analyze',
    status: 'done',
    ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '企业客户', rationale: '客户主数据', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });

  const object = {
    candidateId: 'candidate_1',
    key: 'customer', title: '企业客户', description: '对公企业客户', sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const operations = processes.map((process) => ({
    key: process.id,
    title: process.title,
    description: `执行${process.title}`,
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:customer', '返回结果'],
    relatedObjects: ['customer'],
    competencyQuestionId: operationTargetId(process.id),
  }));
  const faux = createFauxCore({ provider: 'generation-trace', models: [{ id: 'generation-trace' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: operations.slice(0, 3) })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: operations.slice(3) })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }],
    documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart, {
    dataSourceType: 'mindsdb',
  });

  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.operations.length, 5);
  assert.equal(result.spec.dataSourceType, 'mindsdb');
  assert.deepEqual(
    result.spec.operations.map((operation) => operation.competencyQuestion),
    processes.map((process) => operationTargetQuestion(process.title)),
  );
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  assert.equal(node?.status, 'done');
  const events = listTraceEvents(node!.id);
  const prepare = events.find((event) => event.phase === 'prepare' && event.kind === 'phase_started');
  assert.deepEqual({
    objectBatchCount: prepare?.details?.objectBatchCount,
    knowledgeBatchCount: prepare?.details?.knowledgeBatchCount,
    operationBatchCount: prepare?.details?.operationBatchCount,
    totalStepCount: prepare?.details?.totalStepCount,
  }, { objectBatchCount: 1, knowledgeBatchCount: 1, operationBatchCount: 2, totalStepCount: 5 });
  assert.ok(events.some((event) => event.title.includes('生成业务对象')));
  assert.ok(events.some((event) => event.title.includes('生成规则和指标')));
  assert.ok(events.some((event) => event.title.includes('生成业务操作')));
  assert.ok(events.some((event) => event.title === '提交业务对象' && event.details?.objectCount === 1));
  assert.ok(events.some((event) => event.title === '提交规则和指标' && event.details?.ruleCount === 0));
  assert.ok(events.some((event) => event.title === '提交业务操作' && event.details?.operationCount === 3));
  assert.ok(events.some((event) => event.kind === 'generation_completed'));
});

test('AI 辅助构建按人工确认流程生成业务操作', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '流程驱动生成', domain: '审批', background: '业务审批流程' });
  const process = {
    id: 'approval', title: '申请审批', trigger: '收到申请', actors: ['经办人', '审批人'],
    inputs: ['申请单'], steps: ['审批判断'], exceptions: ['资料不全时退回'],
    outputs: ['审批结果'], handoffs: ['通知经办人'],
  };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '审批',
      candidateObjects: [{ name: '申请单', rationale: '流程输入', sourceTables: ['application'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1', key: 'application', title: '申请单', description: '业务申请单',
    sourceTable: 'application', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '申请编号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const operation = {
    key: 'approve_application', title: '审批申请', description: '按照确认流程审批申请',
    inputs: [], outputs: [{ field: 'decision', type: 'string' }],
    flow: ['读取 object:application', '审批判断', '返回结果'], relatedObjects: ['application'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'process-generation', models: [{ id: 'process-generation' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'application', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['A001'], distinctRatio: 1 }],
    }],
    documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.equal(result.spec.operations.length, 1);
  assert.equal(result.spec.operations[0].competencyQuestionId, operationTargetId(process.id));
  assert.equal(result.spec.operations[0].competencyQuestion, operationTargetQuestion(process.title));
  assert.equal(result.coverage.uncovered.length, 0);
});

test('对象批次只提交部分分析候选时直接接受有依据的对象', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '对象补齐', domain: '供应链运营', background: '订单履约查询' });
  const process = { id: 'warehouse_location_lookup', title: '查询仓储位置数据' };
  const flowchart = confirmedFlowchart([process]);
  const candidates = [
    { name: 'WarehouseLocation', rationale: '仓储位置', sourceTables: ['warehouse_location'] },
    { name: 'ProcessingUnit', rationale: '处理单元', sourceTables: ['processing_unit'] },
    { name: 'DistributionCenter', rationale: '配送中心', sourceTables: ['distribution_center'] },
    { name: 'Product', rationale: '产品', sourceTables: ['product'] },
  ];
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '供应链运营', candidateObjects: candidates,
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });

  const objects = candidates.map((candidate, index) => ({
    candidateId: `candidate_${index + 1}`,
    key: candidate.name,
    title: candidate.rationale,
    description: candidate.rationale,
    sourceTable: candidate.sourceTables[0],
    primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '编号', type: 'string', required: true, sourceColumn: 'id' }],
  }));
  const operation = {
    key: 'query_warehouse_location', title: '查询仓储位置', description: '查询仓储位置数据',
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:WarehouseLocation', '返回结果'], relatedObjects: ['WarehouseLocation'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-object-repair', models: [{ id: 'generation-object-repair' }] });
  faux.setResponses([
    // 分析候选是建议，模型可以只提交当前有依据的对象。
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [objects[0]] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: candidates.map((candidate) => ({
      name: candidate.sourceTables[0], rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1 }],
    })),
    documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.deepEqual(result.spec.objects.map((item) => item.key), ['WarehouseLocation']);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  const events = listTraceEvents(node!.id);
  assert.equal(events.some((event) => event.kind === 'repair_started'), false);
});

test('操作批次引用不存在对象或漏掉确认流程时会自动重交', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '操作补齐', domain: '供应链运营', background: '仓储位置查询' });
  const processes = [
    { id: 'yard_base', title: '查询仓储位置基础信息' },
    { id: 'yard_inventory', title: '查询仓储位置库存' },
  ];
  const flowchart = confirmedFlowchart(processes);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '供应链运营',
      candidateObjects: [{ name: 'WarehouseLocation', rationale: '仓储位置', sourceTables: ['warehouse_location'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1',
    key: 'WarehouseLocation', title: '仓储位置', description: '仓储位置', sourceTable: 'warehouse_location', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '编号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const operationFor = (key, processTitle, processTargetId, relatedObjects = ['WarehouseLocation']) => ({
    key, title: processTitle, description: processTitle,
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: [`查询 object:${relatedObjects[0]}`, '返回结果'], relatedObjects,
    competencyQuestionId: processTargetId,
  });
  const repaired = [
    operationFor('query_yard_base', processes[0].title, operationTargetId(processes[0].id)),
    operationFor('query_yard_inventory', processes[1].title, operationTargetId(processes[1].id)),
  ];
  const faux = createFauxCore({ provider: 'generation-operation-repair', models: [{ id: 'generation-operation-repair' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    // 首次提交同时存在悬空对象引用和未精确认领的问题。
    fauxAssistantMessage([fauxToolCall('submit_operations', {
      operations: [
        operationFor('query_yard_base', processes[0].title, operationTargetId(processes[0].id), ['MissingObject']),
      ],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: repaired })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'warehouse_location', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.deepEqual(result.spec.operations.map((item) => item.key), ['query_yard_base', 'query_yard_inventory']);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  const events = listTraceEvents(node!.id);
  assert.ok(events.some((event) => event.kind === 'validation_warning' && event.summary?.includes('未覆盖 1')));
  assert.ok(events.some((event) => event.kind === 'repair_started' && event.phase === 'operation_batch_1'));
  assert.ok(events.some((event) => event.kind === 'repair_completed' && event.phase === 'operation_batch_1'));
});

test('操作批次引用尚未生成的脚本时自动补交并移除幽灵脚本', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '脚本引用补齐', domain: '金融营销', background: '客户需求识别与产品推荐' });
  const process = { id: 'identify_needs', title: '识别客户需求并推荐产品' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '金融营销',
      candidateObjects: [{ name: 'Customer', rationale: '客户信息', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1', key: 'customer', title: '客户信息', description: '客户信息',
    sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户编号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const operation = {
    key: 'identify_customer_needs', title: '识别客户需求', description: '识别客户需求并推荐产品',
    inputs: [], outputs: [{ field: 'recommendation', type: 'array' }],
    flow: ['读取 object:customer', '输出推荐结果'], relatedObjects: ['customer'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-operation-script-repair', models: [{ id: 'generation-operation-script-repair' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', {
      operations: [{
        ...operation,
        flow: ['读取 object:customer', '执行 script:phantom_customer_recommendation', '输出推荐结果'],
        script: { key: 'phantom_customer_recommendation', required: true },
      }],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.equal(result.spec.operations.length, 1);
  assert.equal(result.spec.operations[0]?.script, undefined);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  const events = listTraceEvents(node!.id);
  assert.ok(events.some((event) => (
    event.kind === 'validation_warning'
      && event.details?.issues?.some((issue: string) => issue.includes('尚未生成的脚本'))
  )));
  assert.ok(events.some((event) => event.kind === 'repair_started' && event.phase === 'operation_batch_1'));
  assert.ok(events.some((event) => event.kind === 'repair_completed' && event.phase === 'operation_batch_1'));
});

test('操作通过确认流程 ID 绑定并由服务端回填流程目标文本', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '流程 ID 绑定', domain: '银行营销', background: '客户产品推荐' });
  const process = { id: 'deposit_recommendation', title: '企业客户存款产品推荐' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '企业客户', rationale: '客户主数据', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1',
    key: 'customer', title: '企业客户', description: '对公企业客户', sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const operation = {
    key: 'recommend_deposit_product', title: '推荐存款产品', description: '按客户条件推荐产品',
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:customer', '匹配产品'], relatedObjects: ['customer'],
    competencyQuestionId: operationTargetId(process.id),
    competencyQuestion: '给企业推荐合适的产品',
  };
  const faux = createFauxCore({ provider: 'generation-question-id', models: [{ id: 'generation-question-id' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.equal(result.spec.operations[0].competencyQuestionId, operationTargetId(process.id));
  assert.equal(result.spec.operations[0].competencyQuestion, operationTargetQuestion(process.title));
});

test('知识包自动补交耗尽后等待专家决定是否跳过', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '生成失败轨迹', domain: '银行营销', background: '' });
  const processes = ['流程一', '流程二', '流程三', '流程四', '流程五']
    .map((title, index) => ({ id: `failure_${index + 1}`, title }));
  const flowchart = confirmedFlowchart(processes);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '企业客户', rationale: '客户主数据', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1',
    key: 'customer', title: '企业客户', description: '对公企业客户', sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户号', type: 'string', required: true, sourceColumn: 'id' }],
  };
  const invalidOperation = {
    key: 'invalid_operation', title: '错误操作', description: '引用不存在对象',
    inputs: [], outputs: [], flow: ['读取不存在对象'], relatedObjects: ['missing_object'],
    competencyQuestionId: operationTargetId(processes[0].id),
  };
  const faux = createFauxCore({ provider: 'generation-trace-fail', models: [{ id: 'generation-trace-fail' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
  ]);

  await assert.rejects(generatePackage(project, {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C001'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart), /等待专家确认是否跳过/);

  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  assert.equal(node?.status, 'waiting_user');
  assert.match(node?.reason ?? '', /操作.*仍未通过/);
  assert.ok(listTraceEvents(node!.id).some((event) => (
    event.kind === 'validation_warning' && event.title === '模型自动补交已耗尽，等待专家决定'
  )));
  const interaction = getAuthoringState().snapshot(project.id).pendingInteractions[0];
  assert.equal(interaction?.type, 'decision');
  assert.equal(interaction?.payload.purpose, 'generation_gate_skip');
});

test('专家确认后沿用原任务跳过失败批次并生成其余知识草案', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '专家跳过生成批次', domain: '银行营销', background: '' });
  const process = { id: 'handle_customer', title: '处理客户' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '客户', rationale: '客户主数据', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1', key: 'customer', title: '客户', description: '客户业务对象',
    sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户号', sourceColumn: 'id' }],
  };
  const invalidOperation = {
    key: 'handle_customer', title: '处理客户', description: '处理客户',
    inputs: [], outputs: [], flow: ['读取不存在的对象'], relatedObjects: ['missing_object'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-expert-skip', models: [{ id: 'generation-expert-skip' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
  ]);
  const baseRevision = await getPackageArtifactStore().ensureProject(project.id);
  const originalRun = getPiAuthoringAdapter().start(
    project.id,
    '生成知识包',
    baseRevision.oid,
    { nodeId: 'waiting_generation' },
  ).run;

  const result = await generatePackage(project, {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C1'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart, {
    skipFailedPhases: ['operation_batch_1'],
    resumeRunId: originalRun.id,
  });

  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.operations.length, 0);
  assert.deepEqual(result.coverage.uncovered, [operationTargetQuestion(process.title)]);
  assert.equal(result.generationNotices[0]?.phase, 'operation_batch_1');
  assert.equal(getAuthoringState().snapshot(project.id).activeRuns.length, 0);
});

test('知识生成恢复时复用成功批次且不重复调用模型', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '生成批次断点续跑', domain: '银行营销', background: '' });
  const process = { id: 'handle_customer', title: '处理客户' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '客户', rationale: '客户主数据', sourceTables: ['customer'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1', key: 'customer', title: '客户', description: '客户业务对象',
    sourceTable: 'customer', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '客户号', sourceColumn: 'id' }],
  };
  const invalidOperation = {
    key: 'handle_customer', title: '处理客户', description: '处理客户',
    inputs: [], outputs: [], flow: ['读取不存在的对象'], relatedObjects: ['missing_object'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-checkpoint-resume', models: [{ id: 'generation-checkpoint-resume' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [invalidOperation] })]),
  ]);
  const context = {
    tables: [{
      name: 'customer', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['C1'], distinctRatio: 1 }],
    }],
    documents: [],
  };
  const llm = { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' };

  await assert.rejects(generatePackage(project, context, llm, flowchart), /等待专家确认是否跳过/u);
  const snapshot = getAuthoringState().snapshot(project.id);
  const interaction = snapshot.pendingInteractions[0];
  const run = snapshot.activeRuns[0];
  assert.ok(interaction);
  assert.ok(run);
  const adapter = getPiAuthoringAdapter();
  adapter.respond(project.id, interaction.id, 'checkpoint-skip', { value: 'skip' });
  adapter.fail(project.id, run.id, '模拟服务重启后的旧运行失败');
  const resumedRun = adapter.start(project.id, '生成知识包', run.baseRevision, {
    purpose: 'knowledge_generation_resume', priorRunId: run.id,
  }).run;

  const result = await generatePackage(project, context, llm, flowchart, {
    resumeRunId: resumedRun.id,
    skipFailedPhases: ['operation_batch_1'],
  });

  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.operations.length, 0);
  assert.equal(result.generationNotices[0]?.phase, 'operation_batch_1');
  const checkpointEvents = listNodes(project.id)
    .filter((item) => item.type === 'generate')
    .flatMap((item) => listTraceEvents(item.id));
  assert.ok(checkpointEvents.some((event) => event.title.includes('对象已从检查点恢复')));
  assert.ok(checkpointEvents.some((event) => event.title.includes('规则和指标已从检查点恢复')));
});

test('过期生成门禁在创建新 workflow 节点前失败', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '过期生成门禁', domain: '银行营销', background: '' });
  const flowchart = confirmedFlowchart([{ id: 'handle_customer', title: '处理客户' }]);

  await assert.rejects(generatePackage(project, {
    tables: [], documents: [],
  }, undefined, flowchart, {
    resumeRunId: 'ar_expired',
  }), /已结束或不可恢复/);

  assert.equal(listNodes(project.id).some((node) => node.type === 'generate'), false);
});

test('历史分析和对象提交中的表格展示标签在生成时规范化', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '历史来源修复', domain: '监管', background: '' });
  const process = { id: 'regulatory_scenario_lookup', title: '查询监管场景' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '国企穿透式监管',
      candidateObjects: [{
        name: '监管场景', rationale: '核心监管对象',
        sourceTables: ['十一个穿透式监管场景（11 行）'],
      }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1',
    key: 'regulatory_scenario', title: '监管场景', description: '监管场景',
    sourceTable: '十一个穿透式监管场景（11 行）',
    sourceRefs: [{
      kind: 'table', table: '十一个穿透式监管场景（11 行）', columns: ['序号'],
    }],
    primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '序号', sourceColumn: '序号', type: 'number', required: true }],
  };
  const operation = {
    key: 'query_regulatory_scenario', title: '查询监管场景', description: '查询监管场景',
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:regulatory_scenario'], relatedObjects: ['regulatory_scenario'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-source-catalog', models: [{ id: 'generation-source-catalog' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: '十一个穿透式监管场景', rowCount: 11,
      fields: [{ name: '序号', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 }],
    }],
    documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.equal(result.spec.objects[0].sourceTable, '十一个穿透式监管场景');
  assert.equal(result.spec.objects[0].sourceRefs?.[0]?.kind, 'table');
  assert.equal((result.spec.objects[0].sourceRefs?.[0] as any).table, '十一个穿透式监管场景');
});

test('对象字段映射无效时在当前批次自动重交', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '字段映射重交', domain: '监管', background: '' });
  const process = { id: 'regulatory_scenario_lookup', title: '查询监管场景' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '监管',
      candidateObjects: [{ name: '监管场景', rationale: '监管对象', sourceTables: ['监管场景'] }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = (sourceColumn: string) => ({
    candidateId: 'candidate_1',
    key: 'regulatory_scenario', title: '监管场景', description: '监管场景',
    sourceTable: '监管场景', primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '序号', sourceColumn, type: 'number', required: true }],
  });
  const operation = {
    key: 'query_regulatory_scenario', title: '查询监管场景', description: '查询监管场景',
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:regulatory_scenario'], relatedObjects: ['regulatory_scenario'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const core = createFauxCore({ provider: 'generation-mapping-repair', models: [{ id: 'generation-mapping-repair' }] });
  let objectCalls = 0;
  const llm = {
    model: core.getModel(),
    getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      if (system.includes('submit_objects')) {
        objectCalls += 1;
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_objects', {
          objects: [object(objectCalls === 1 ? '不存在字段' : '序号')],
        })])]);
      } else if (system.includes('submit_rules_metrics')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })])]);
      } else if (system.includes('submit_operations')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })])]);
      } else {
        throw new Error(`未处理的测试 prompt: ${system.slice(0, 80)}`);
      }
      return core.streamSimple(model as any, context, options as any);
    }) as any,
  };

  const result = await generatePackage(project, {
    tables: [{
      name: '监管场景', rowCount: 1,
      fields: [{ name: '序号', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 }],
    }], documents: [],
  }, llm, flowchart);

  assert.equal(result.spec.objects[0].fields[0].sourceColumn, '序号');
  assert.equal(objectCalls, 2);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  assert.ok(listTraceEvents(node!.id).some((event) =>
    event.phase === 'object_batch_1' && event.kind === 'repair_started'));
});

test('对象通过候选 ID 认领中英双语候选名称', async () => {
  const project = await createGenerationProject({ owner: 'tester', name: '候选 ID 绑定', domain: '监管', background: '' });
  const process = { id: 'risk_event_lookup', title: '查询风险事件' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '穿透式监管',
      candidateObjects: [{
        name: '风险事件（RiskEvent）', rationale: '监管风险事件', sourceTables: ['监管场景'],
      }],
      openQuestions: [], materialAssessments: [], candidateArtifacts: [],
    },
  });
  const object = {
    candidateId: 'candidate_1',
    key: 'RiskEvent', title: '风险事件', description: '监管风险事件',
    sourceRefs: [{ kind: 'table', table: '监管场景', columns: ['序号'] }], primaryKey: ['id'],
    fields: [{ key: 'id', cnName: '序号', sourceColumn: '序号', type: 'number', required: true }],
  };
  const operation = {
    key: 'query_risk_event', title: '查询风险事件', description: '查询风险事件',
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:RiskEvent'], relatedObjects: ['RiskEvent'],
    competencyQuestionId: operationTargetId(process.id),
  };
  const faux = createFauxCore({ provider: 'generation-candidate-id', models: [{ id: 'generation-candidate-id' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: '监管场景', rowCount: 1,
      fields: [{ name: '序号', inferredType: 'number', sampleValues: ['1'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, flowchart);

  assert.equal(result.spec.objects[0].candidateId, 'candidate_1');
  assert.equal(result.spec.objects[0].key, 'RiskEvent');
});

test('文档对象生成继承材料分析证据和后端全文覆盖记录', async () => {
  const project = await createGenerationProject({
    owner: 'tester', name: '文档对象证据传递', domain: '银行营销', background: '营销产品推荐',
  });
  const process = { id: 'recommend_product', title: '推荐营销产品' };
  const flowchart = confirmedFlowchart([process]);
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 0,
    output: {
      domainOverview: '银行营销',
      candidateObjects: [{ name: '营销产品目录', rationale: '产品匹配知识库', sourceTables: [] }],
      openQuestions: [],
      materialAssessments: [{
        source: '营销产品手册.docx', sourceType: 'document', suitableFor: ['object'],
        rationale: '全文包含各章节产品定义',
      }],
      candidateArtifacts: [{
        kind: 'object', name: '对公营销产品目录', rationale: '正文定义了产品和适用场景',
        sources: ['营销产品手册.docx'], evidence: '服务渠道和竞品章节证据位于 offset~12000',
        readiness: 'extractable',
      }],
      documentReadCoverage: [{
        documentId: 'doc_manual', filename: '营销产品手册.docx',
        revisionFingerprint: 'doc_manual:rev1', totalChars: 12_500,
        ranges: [[0, 12_500]], contiguousEnd: 12_500, eofSeen: true, complete: true,
      }],
    },
  });

  const object = {
    candidateId: 'candidate_1', key: 'MarketingProductCatalog', title: '营销产品目录',
    description: '营销产品和适用场景目录', fields: [],
  };
  const operation = {
    key: 'recommend_product', title: '推荐营销产品', description: '根据客户需求推荐产品',
    inputs: [], outputs: [{ field: 'products', type: 'array' }],
    flow: ['查询 object:MarketingProductCatalog', '返回匹配产品'],
    relatedObjects: ['MarketingProductCatalog'],
    competencyQuestionId: operationTargetId(process.id),
  };
  let objectPrompt = '';
  const core = createFauxCore({ provider: 'document-object-evidence', models: [{ id: 'document-object-evidence' }] });
  const llm = {
    model: core.getModel(), getApiKey: () => 'faux',
    streamFn: ((model: unknown, context: any, options: unknown) => {
      const system = String(context.systemPrompt ?? '');
      if (system.includes('submit_objects')) {
        objectPrompt = system;
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })])]);
      } else if (system.includes('submit_rules_metrics')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })])]);
      } else if (system.includes('submit_operations')) {
        core.setResponses([fauxAssistantMessage([fauxToolCall('submit_operations', { operations: [operation] })])]);
      } else {
        throw new Error(`未处理的测试 prompt: ${system.slice(0, 80)}`);
      }
      return core.streamSimple(model as any, context, options as any);
    }) as any,
  };

  const result = await generatePackage(project, {
    tables: [],
    documents: [{
      id: 'doc_manual', filename: '营销产品手册.docx', textContent: 'x'.repeat(12_500),
      revisionFingerprint: 'doc_manual:rev1',
    }],
  }, llm, flowchart);

  assert.match(objectPrompt, /服务渠道和竞品章节证据位于 offset~12000/u);
  const source = result.spec.objects[0].sourceRefs?.[0];
  assert.equal(source?.kind, 'document');
  assert.deepEqual(source?.kind === 'document' ? source.readCoverage : undefined, {
    revisionFingerprint: 'doc_manual:rev1', totalChars: 12_500,
    ranges: [[0, 12_500]], contiguousEnd: 12_500, eofSeen: true, complete: true,
  });
});
