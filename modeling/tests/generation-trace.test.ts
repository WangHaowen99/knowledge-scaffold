import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createFauxCore, fauxAssistantMessage, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'generation-trace-'));

const { createProject } = await import('../src/projects/repo.js');
const { upsertNode, listNodes } = await import('../src/workflow/engine.js');
const { generatePackage } = await import('../src/workflow/m2.js');
const { replaceCompetency, setCompetencySelection } = await import('../src/workflow/m2-repo.js');
const { listTraceEvents } = await import('../src/workflow/trace.js');
const { getAuthoringState } = await import('../src/authoring/state.js');
const { getPiAuthoringAdapter } = await import('../src/authoring/pi-adapter.js');
const { getPackageArtifactStore } = await import('../src/artifacts/runtime.js');

test('知识包分阶段生成过程持久化', async () => {
  const project = createProject({ owner: 'tester', name: '生成轨迹', domain: '银行营销', background: '企业客户产品推荐' });
  const questions = ['问题一', '问题二', '问题三', '问题四', '问题五'];
  const competency = replaceCompetency(project.id, questions);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
  const operations = questions.map((question, index) => ({
    key: `answer_${index + 1}`,
    title: `回答${index + 1}`,
    description: `回答${question}`,
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: ['查询 object:customer', '返回结果'],
    relatedObjects: ['customer'],
    competencyQuestionId: competency[index].id,
    competencyQuestion: question,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, 'mindsdb');

  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.operations.length, 5);
  assert.equal(result.spec.dataSourceType, 'mindsdb');
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

test('AI 辅助构建始终优先按人工确认流程生成业务操作', async () => {
  const project = createProject({ owner: 'tester', name: '流程驱动生成', domain: '审批', background: '业务审批流程' });
  const legacy = replaceCompetency(project.id, ['旧版问题不应覆盖已确认流程']);
  setCompetencySelection(project.id, legacy.map((item) => item.id));
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
    competencyQuestionId: 'process_approval',
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, {
    diagrams: [{
      id: 'approval', title: '申请审批', trigger: '收到申请', actors: ['经办人', '审批人'],
      inputs: ['申请单'], exceptions: ['资料不全时退回'], outputs: ['审批结果'], handoffs: ['通知经办人'],
      nodes: [
        { id: 'start:approval', label: '收到申请', kind: 'start' },
        { id: 'step:approval:review', label: '审批判断', kind: 'decision' },
        { id: 'end:approval', label: '审批结果', kind: 'end' },
      ],
      edges: [
        { from: 'start:approval', to: 'step:approval:review' },
        { from: 'step:approval:review', to: 'end:approval', label: '通过' },
      ],
    }],
  });

  assert.equal(result.spec.operations.length, 1);
  assert.equal(result.spec.operations[0].competencyQuestionId, 'process_approval');
  assert.match(result.spec.operations[0].competencyQuestion ?? '', /申请审批/);
  assert.equal(result.coverage.uncovered.length, 0);
});

test('对象批次只提交部分分析候选时直接接受有依据的对象', async () => {
  const project = createProject({ owner: 'tester', name: '对象补齐', domain: '供应链运营', background: '订单履约查询' });
  const questions = ['如何查询仓储位置数据？'];
  const competency = replaceCompetency(project.id, questions);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
    competencyQuestion: questions[0],
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' });

  assert.deepEqual(result.spec.objects.map((item) => item.key), ['WarehouseLocation']);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  const events = listTraceEvents(node!.id);
  assert.equal(events.some((event) => event.kind === 'repair_started'), false);
});

test('操作批次引用不存在对象或漏掉问题时会自动重交', async () => {
  const project = createProject({ owner: 'tester', name: '操作补齐', domain: '供应链运营', background: '仓储位置查询' });
  const questions = ['如何查询仓储位置基础信息？', '如何查询仓储位置库存？'];
  const competency = replaceCompetency(project.id, questions);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
  const operationFor = (key, question, questionId, relatedObjects = ['WarehouseLocation']) => ({
    key, title: question, description: question,
    inputs: [], outputs: [{ field: 'result', type: 'array' }],
    flow: [`查询 object:${relatedObjects[0]}`, '返回结果'], relatedObjects,
    competencyQuestionId: questionId, competencyQuestion: question,
  });
  const repaired = [
    operationFor('query_yard_base', questions[0], competency[0].id),
    operationFor('query_yard_inventory', questions[1], competency[1].id),
  ];
  const faux = createFauxCore({ provider: 'generation-operation-repair', models: [{ id: 'generation-operation-repair' }] });
  faux.setResponses([
    fauxAssistantMessage([fauxToolCall('submit_objects', { objects: [object] })]),
    fauxAssistantMessage([fauxToolCall('submit_rules_metrics', { rules: [], metrics: [] })]),
    // 首次提交同时存在悬空对象引用和未精确认领的问题。
    fauxAssistantMessage([fauxToolCall('submit_operations', {
      operations: [
        operationFor('query_yard_base', questions[0], competency[0].id, ['MissingObject']),
      ],
    })]),
    fauxAssistantMessage([fauxToolCall('submit_operations', { operations: repaired })]),
  ]);

  const result = await generatePackage(project, {
    tables: [{
      name: 'warehouse_location', rowCount: 1,
      fields: [{ name: 'id', inferredType: 'string', sampleValues: ['1'], distinctRatio: 1 }],
    }], documents: [],
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' });

  assert.deepEqual(result.spec.operations.map((item) => item.key), ['query_yard_base', 'query_yard_inventory']);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  const events = listTraceEvents(node!.id);
  assert.ok(events.some((event) => event.kind === 'validation_warning' && event.summary?.includes('未覆盖 1')));
  assert.ok(events.some((event) => event.kind === 'repair_started' && event.phase === 'operation_batch_1'));
  assert.ok(events.some((event) => event.kind === 'repair_completed' && event.phase === 'operation_batch_1'));
});

test('操作通过领域问题 ID 绑定并由服务端回填原始问题文本', async () => {
  const project = createProject({ owner: 'tester', name: '问题 ID 绑定', domain: '银行营销', background: '客户产品推荐' });
  const question = '如何为符合条件的企业客户推荐存款产品？';
  const competency = replaceCompetency(project.id, [question]);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' });

  assert.equal(result.spec.operations[0].competencyQuestionId, competency[0].id);
  assert.equal(result.spec.operations[0].competencyQuestion, question);
});

test('知识包自动补交耗尽后等待专家决定是否跳过', async () => {
  const project = createProject({ owner: 'tester', name: '生成失败轨迹', domain: '银行营销', background: '' });
  const questions = ['问题一', '问题二', '问题三', '问题四', '问题五'];
  const competency = replaceCompetency(project.id, questions);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }), /等待专家确认是否跳过/);

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
  const project = createProject({ owner: 'tester', name: '专家跳过生成批次', domain: '银行营销', background: '' });
  const competency = replaceCompetency(project.id, ['如何处理客户？']);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' }, undefined, {
    skipFailedPhases: ['operation_batch_1'],
    resumeRunId: originalRun.id,
  });

  assert.equal(result.spec.objects.length, 1);
  assert.equal(result.spec.operations.length, 0);
  assert.deepEqual(result.coverage.uncovered, ['如何处理客户？']);
  assert.equal(result.generationNotices[0]?.phase, 'operation_batch_1');
  assert.equal(getAuthoringState().snapshot(project.id).activeRuns.length, 0);
});

test('过期生成门禁在创建新 workflow 节点前失败', async () => {
  const project = createProject({ owner: 'tester', name: '过期生成门禁', domain: '银行营销', background: '' });
  const competency = replaceCompetency(project.id, ['如何处理客户？']);
  setCompetencySelection(project.id, competency.map((item) => item.id));

  await assert.rejects(generatePackage(project, {
    tables: [], documents: [],
  }, undefined, undefined, {
    resumeRunId: 'ar_expired',
  }), /已结束或不可恢复/);

  assert.equal(listNodes(project.id).some((node) => node.type === 'generate'), false);
});

test('历史分析和对象提交中的表格展示标签在生成时规范化', async () => {
  const project = createProject({ owner: 'tester', name: '历史来源修复', domain: '监管', background: '' });
  const question = '如何查询监管场景？';
  const competency = replaceCompetency(project.id, [question]);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
    competencyQuestion: question,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' });

  assert.equal(result.spec.objects[0].sourceTable, '十一个穿透式监管场景');
  assert.equal(result.spec.objects[0].sourceRefs?.[0]?.kind, 'table');
  assert.equal((result.spec.objects[0].sourceRefs?.[0] as any).table, '十一个穿透式监管场景');
});

test('对象字段映射无效时在当前批次自动重交', async () => {
  const project = createProject({ owner: 'tester', name: '字段映射重交', domain: '监管', background: '' });
  const question = '如何查询监管场景？';
  const competency = replaceCompetency(project.id, [question]);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
    competencyQuestion: question,
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
  }, llm);

  assert.equal(result.spec.objects[0].fields[0].sourceColumn, '序号');
  assert.equal(objectCalls, 2);
  const node = [...listNodes(project.id)].reverse().find((item) => item.type === 'generate');
  assert.ok(listTraceEvents(node!.id).some((event) =>
    event.phase === 'object_batch_1' && event.kind === 'repair_started'));
});

test('对象通过候选 ID 认领中英双语候选名称', async () => {
  const project = createProject({ owner: 'tester', name: '候选 ID 绑定', domain: '监管', background: '' });
  const question = '如何查询风险事件？';
  const competency = replaceCompetency(project.id, [question]);
  setCompetencySelection(project.id, competency.map((item) => item.id));
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
    competencyQuestionId: competency[0].id,
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
  }, { model: faux.getModel(), streamFn: faux.streamSimple, getApiKey: () => 'faux' });

  assert.equal(result.spec.objects[0].candidateId, 'candidate_1');
  assert.equal(result.spec.objects[0].key, 'RiskEvent');
});
