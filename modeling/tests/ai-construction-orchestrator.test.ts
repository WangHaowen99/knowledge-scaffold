import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'ai-construction-orchestrator-'));

const { AiConstructionOrchestrator } = await import('../src/ai-construction/orchestrator.js');
const { AiConstructionTaskGraph } = await import('../src/ai-construction/task-graph.js');
const { AiConstructionMessageStore } = await import('../src/ai-construction/messages.js');
const { AuthoringState } = await import('../src/authoring/state.js');
const { getDb } = await import('../src/db/index.js');
const { createProject } = await import('../src/projects/repo.js');
const { upsertNode } = await import('../src/workflow/engine.js');

const createMaterialFirstProject = (name: string) => createProject({
  owner: 'alice', name, kind: 'package',
});

function advanceGraphToDeterministic(projectId: string) {
  const graph = new AiConstructionTaskGraph(getDb());
  let snapshot = graph.snapshot(projectId)!;
  snapshot = graph.apply(projectId, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: snapshot.tasks[0].id, outputRevisionOid: 'revision-materials',
  });
  snapshot = graph.apply(projectId, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'revision-materials',
    actor: { username: 'alice', role: 'project_owner' },
  });
  for (const [milestoneId, kind] of [
    ['goal_confirmed', 'modeling_brief'],
    ['process_confirmed', 'process_review'],
    ['knowledge_confirmed', 'package_validation'],
  ] as const) {
    snapshot = graph.apply(projectId, {
      type: 'plan_task', expectedRevision: snapshot.revision,
      task: {
        kind, milestoneId, title: `完成 ${milestoneId}`,
        goal: `为测试完成 ${milestoneId}`, required: true,
        dependsOn: [],
      },
    });
    const task = snapshot.tasks.find((item) => item.milestoneId === milestoneId)!;
    snapshot = graph.apply(projectId, {
      type: 'complete_task', expectedRevision: snapshot.revision,
      taskId: task.id, outputRevisionOid: `revision-${milestoneId}`,
    });
    snapshot = graph.apply(projectId, {
      type: 'confirm_milestone', expectedRevision: snapshot.revision,
      milestoneId, packageRevisionOid: `revision-${milestoneId}`,
      actor: { username: 'alice', role: 'project_owner' },
    });
  }
  return { graph, snapshot };
}

test('新项目无需先上传资料即可收集建模目标', () => {
  const project = createProject({ owner: 'alice', name: '目标先行建模' });
  const graph = new AiConstructionTaskGraph(getDb()).snapshot(project.id)!;
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });

  const started = orchestrator.advanceAfterMilestoneConfirmation(project.id, graph);

  assert.equal(started.aiConstruction.progress.currentMilestoneId, 'goal_confirmed');
  assert.equal(started.aiConstruction.tasks[0].kind, 'modeling_brief');
  assert.equal(started.aiConstruction.tasks[0].status, 'waiting_user');
  assert.equal(started.interaction?.payload.purpose, 'modeling_brief');
  assert.match(String(started.interaction?.payload.description), /先确认建模目标/);
  assert.doesNotMatch(String(started.interaction?.payload.description), /候选来自已上传资料/);
  assert.deepEqual((started.message?.blocks.at(-1) as any).actions.map((action: any) => action.id), [
    'explain_goal',
  ]);
});

test('新项目确认建模目标后才规划资料分析', () => {
  const project = createProject({ owner: 'alice', name: '目标确认后分析资料' });
  const graph = new AiConstructionTaskGraph(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  const goalTask = graph.snapshot(project.id)!.tasks[0];
  const completed = orchestrator.completeModelingBrief(project.id, {
    taskId: goalTask.id,
    packageRevisionOid: 'revision-goal',
    messageKey: 'goal.first.completed',
    brief: {
      problem: '形成减一线分流决策知识',
      targetUsers: '生产计划人员',
      scenarios: '生产方案编制',
      successCriteria: '产出可执行且可追溯的方案',
    },
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: completed.aiConstruction.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'revision-goal',
    actor: { username: 'alice', role: 'project_owner' },
  });

  const advanced = orchestrator.advanceAfterMilestoneConfirmation(project.id, confirmed);

  assert.equal(advanced.aiConstruction.progress.currentMilestoneId, 'materials_ready');
  assert.deepEqual(advanced.aiConstruction.tasks.map((task) => [task.kind, task.milestoneId, task.status]), [
    ['modeling_brief', 'goal_confirmed', 'completed'],
    ['material_analysis', 'materials_ready', 'pending'],
  ]);
  assert.deepEqual((advanced.message?.blocks.at(-1) as any).actions.map((action: any) => action.id), [
    'upload_materials', 'analyze_materials',
  ]);
});

test('新项目只接受携带当前已确认目标的资料分析结果', () => {
  const project = createProject({ owner: 'alice', name: '资料分析目标门禁' });
  const graph = new AiConstructionTaskGraph(getDb());
  const state = new AuthoringState(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb(), state });
  const initial = graph.snapshot(project.id)!;
  const goal = orchestrator.advanceAfterMilestoneConfirmation(project.id, initial);
  const brief = {
    problem: '生成减一线分流方案', targetUsers: '生产计划人员',
    scenarios: '编制日度生产方案', successCriteria: '方案可执行且可追溯',
  };
  const answered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: goal.interaction!.id,
    responseId: 'goal-gate-answer', response: { answers: [] },
  });
  if (answered.kind !== 'interaction') throw new Error('回答建模目标失败');
  state.apply({
    kind: 'update_run', projectId: project.id, runId: answered.run.id, status: 'completed',
    result: { purpose: 'modeling_brief', ...brief },
  });
  const completed = orchestrator.completeModelingBrief(project.id, {
    taskId: initial.tasks[0].id, packageRevisionOid: 'revision-goal',
    messageKey: 'goal.gate.completed', brief,
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: completed.aiConstruction.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'revision-goal',
    actor: { username: 'alice', role: 'project_owner' },
  });
  orchestrator.advanceAfterMilestoneConfirmation(project.id, confirmed);
  const analysisStarted = orchestrator.startMaterialAnalysis(project.id, 'goal-gate-analysis-progress');
  assert.match(JSON.stringify(analysisStarted.message.blocks), /生成减一线分流方案/);

  assert.throws(() => orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'revision-materials', messageKey: 'goal-gate-analysis-completed',
    analysis: { domainOverview: '未绑定目标的分析', candidateObjects: [], openQuestions: [] },
  }), /资料分析结果未绑定当前已确认建模目标/);

  const accepted = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'revision-materials', messageKey: 'goal-gate-analysis-completed',
    analysis: {
      domainOverview: '围绕已确认目标的分析', modelingBrief: brief,
      candidateObjects: [], openQuestions: [],
    },
  });
  assert.equal(accepted.aiConstruction.tasks.find((task) => task.kind === 'material_analysis')?.status, 'completed');
});

test('目标先行项目在资料确认后使用当前资料修订创建流程访谈', () => {
  const project = createProject({ owner: 'alice', name: '流程访谈使用当前资料修订' });
  const graph = new AiConstructionTaskGraph(getDb());
  const state = new AuthoringState(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb(), state });
  const brief = {
    problem: '校验月度排产计划', targetUsers: '生产计划人员',
    scenarios: '计划下达前校验', successCriteria: '结果可追溯',
  };
  const goal = orchestrator.advanceAfterMilestoneConfirmation(project.id, graph.snapshot(project.id)!);
  const answered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: goal.interaction!.id,
    responseId: 'goal-current-revision', response: { answers: [] },
  });
  if (answered.kind !== 'interaction') throw new Error('回答建模目标失败');
  state.apply({
    kind: 'update_run', projectId: project.id, runId: answered.run.id, status: 'completed',
    result: { purpose: 'modeling_brief', ...brief },
  });
  let snapshot = orchestrator.completeModelingBrief(project.id, {
    taskId: graph.snapshot(project.id)!.tasks[0].id,
    packageRevisionOid: 'revision-goal', messageKey: 'goal.current-revision', brief,
  }).aiConstruction;
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'revision-goal',
    actor: { username: 'alice', role: 'project_owner' },
  });
  orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot);
  orchestrator.startMaterialAnalysis(project.id, 'materials.current-revision.progress');
  snapshot = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'revision-materials', messageKey: 'materials.current-revision.completed',
    analysis: {
      domainOverview: '排产与装置资料', modelingBrief: brief,
      candidateObjects: [], openQuestions: [],
    },
  }).aiConstruction;
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'revision-materials',
    actor: { username: 'alice', role: 'project_owner' },
  });

  const process = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot);

  assert.equal(process.interaction?.payload.purpose, 'process_interview');
  assert.equal(process.interaction?.baseRevision, 'revision-materials');
});

test('资料分析完成后推进当前任务并生成可确认的结构化消息', () => {
  const project = createMaterialFirstProject('资料分析编排');
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });

  const result = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'materials.analyzed.node-1',
    analysis: {
      domainOverview: '该资料描述了企业授信审批流程。',
      candidateObjects: [{ name: '企业客户', rationale: '核心参与对象', sourceTables: [] }],
      openQuestions: [{ topic: '审批边界', why: '需要专家确认例外分支' }],
    },
  });

  assert.equal(result.aiConstruction.tasks[0].status, 'completed');
  assert.equal(result.aiConstruction.tasks[0].outputRevisionOid, 'draft-revision-1');
  assert.equal(result.message.key, 'materials.analyzed.node-1');
  assert.match((result.message.blocks[1] as any).text, /企业授信审批流程/);
  assert.deepEqual((result.message.blocks[2] as any).actions.map((action: any) => action.id), [
    'confirm_materials', 'upload_materials',
  ]);

  const repeated = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'materials.analyzed.node-1',
    analysis: {
      domainOverview: '不会重复写入消息。', candidateObjects: [], openQuestions: [],
    },
  });
  assert.equal(repeated.message.id, result.message.id);
  assert.equal(new AiConstructionMessageStore(getDb()).list(project.id).length, 1);
});

test('资料分析运行中可以持续发布阶段进展并在失败时回到可重试状态', () => {
  const project = createMaterialFirstProject('资料分析进展');
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });

  const started = orchestrator.startMaterialAnalysis(project.id);
  assert.equal(started.aiConstruction.tasks[0].status, 'running');
  assert.equal((started.message.blocks[0] as any).title, '开始分析建模资料');

  const progressing = orchestrator.reportMaterialAnalysisProgress(project.id, {
    title: '正在提取候选业务目标和流程',
    summary: '已完成领域概览，正在整理第 1/2 批材料',
    percent: 48,
    elapsedSeconds: 20,
  });
  assert.equal((progressing.message.blocks[0] as any).progress, 48);
  assert.match((progressing.message.blocks[1] as any).text, /候选业务目标和流程/);
  assert.ok((progressing.message.blocks[0] as any).eta.maxMinutes >= 1);

  const failed = orchestrator.failMaterialAnalysis(project.id, '模型服务暂时不可用');
  assert.equal(failed.aiConstruction.tasks[0].status, 'failed');
  assert.match((failed.message.blocks[1] as any).text, /模型服务暂时不可用/);
});

test('资料清单进度表示资料读取完成度且分析运行中不再提供重复启动动作', () => {
  const project = createMaterialFirstProject('资料清单状态准确');
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  const parsedDocument = {
    id: 'doc-parsed', projectId: project.id, filename: '流程.xlsx', kind: 'excel' as const,
    parseStatus: 'parsed' as const, tables: [{ name: '流程', fields: [], rowCount: 8 }],
    createdAt: '2026-07-25T00:00:00.000Z',
  };

  const received = orchestrator.materialsUploaded(project.id, parsedDocument, [parsedDocument]);
  assert.equal((received!.message.blocks[0] as any).progress, 100);
  assert.deepEqual((received!.message.blocks[2] as any).actions.map((action: any) => action.id), [
    'upload_materials', 'review_package',
  ]);

  orchestrator.startMaterialAnalysis(project.id, 'analysis.progress.inventory-lock');
  const running = orchestrator.materialsUploaded(project.id, parsedDocument, [parsedDocument]);
  assert.equal((running!.message.blocks[0] as any).progress, 100);
  assert.deepEqual((running!.message.blocks[2] as any).actions.map((action: any) => action.id), [
    'upload_materials',
  ]);
  assert.match((running!.message.blocks[1] as any).text, /资料分析正在进行/);
});

test('资料里程碑确认前补充材料后可以重新运行已完成的分析任务', () => {
  const project = createMaterialFirstProject('补充资料后重分析');
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  const completed = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'revision-before-more-materials',
    messageKey: 'materials.first.completed',
    analysis: { domainOverview: '第一轮分析', candidateObjects: [], openQuestions: [] },
  });
  assert.equal(completed.aiConstruction.tasks[0].status, 'completed');

  const restarted = orchestrator.startMaterialAnalysis(project.id, 'analysis.progress.retry');
  assert.equal(restarted.aiConstruction.tasks[0].status, 'running');
  assert.equal(restarted.aiConstruction.tasks[0].outputRevisionOid, undefined);
});

test('确认资料里程碑后自动规划建模目标任务并提示下一步', () => {
  const project = createMaterialFirstProject('目标任务编排');
  const graph = new AiConstructionTaskGraph(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 1,
    output: { domainOverview: '营销资料概览', candidateObjects: [], openQuestions: [] },
  });
  const analyzed = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'materials.analyzed.node-2',
    analysis: { domainOverview: '营销资料概览', candidateObjects: [], openQuestions: [] },
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone',
    expectedRevision: analyzed.aiConstruction.revision,
    milestoneId: 'materials_ready',
    packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  });

  const advanced = orchestrator.advanceAfterMilestoneConfirmation(project.id, confirmed);
  const goalTask = advanced.aiConstruction.tasks.find((task) => task.kind === 'modeling_brief');
  assert.equal(goalTask?.milestoneId, 'goal_confirmed');
  assert.equal(goalTask?.status, 'waiting_user');
  assert.equal(advanced.interaction?.type, 'question_batch');
  assert.equal(advanced.interaction?.payload.purpose, 'modeling_brief');
  assert.deepEqual((advanced.message?.blocks[2] as any).actions.map((action: any) => action.id), [
    'explain_goal', 'review_materials',
  ]);

  const repeated = orchestrator.advanceAfterMilestoneConfirmation(project.id, advanced.aiConstruction);
  assert.equal(repeated.aiConstruction.revision, advanced.aiConstruction.revision);
  assert.equal(repeated.aiConstruction.tasks.filter((task) => task.kind === 'modeling_brief').length, 1);
  assert.equal(repeated.message?.id, advanced.message?.id);
});

test('已整理的建模目标可以退回重新填写并恢复结构化问题', () => {
  const project = createMaterialFirstProject('修改建模目标');
  const graph = new AiConstructionTaskGraph(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  upsertNode({
    projectId: project.id, type: 'analyze', status: 'done', ordinal: 1,
    output: { domainOverview: '营销资料概览', candidateObjects: [], openQuestions: [] },
  });
  const analyzed = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'materials.analyzed.node-revise',
    analysis: { domainOverview: '营销资料概览', candidateObjects: [], openQuestions: [] },
  });
  const confirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: analyzed.aiConstruction.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const advanced = orchestrator.advanceAfterMilestoneConfirmation(project.id, confirmed);
  const task = advanced.aiConstruction.tasks.find((item) => item.kind === 'modeling_brief')!;
  const state = new AuthoringState(getDb());
  const answered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: advanced.interaction!.id,
    responseId: 'goal-answer-revise', response: { answers: [] },
  });
  if (answered.kind !== 'interaction') throw new Error('回答建模目标失败');
  state.apply({
    kind: 'update_run', projectId: project.id, runId: answered.run.id, status: 'completed',
    result: { purpose: 'modeling_brief' },
  });
  const completed = orchestrator.completeModelingBrief(project.id, {
    taskId: task.id,
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'goal.answered.first',
    brief: {
      problem: '形成营销推荐知识', targetUsers: '客户经理',
      scenarios: '客户拜访前', successCriteria: '推荐可追溯',
    },
  });

  const revised = orchestrator.reviseModelingBrief(project.id, {
    packageRevisionOid: 'draft-revision-1',
    brief: {
      problem: '形成营销推荐知识', targetUsers: '客户经理',
      scenarios: '客户拜访前', successCriteria: '推荐可追溯',
    },
  });

  assert.ok(completed.aiConstruction.revision < revised.aiConstruction.revision);
  assert.equal(revised.aiConstruction.tasks.find((item) => item.id === task.id)?.status, 'waiting_user');
  assert.equal(revised.interaction?.payload.purpose, 'modeling_brief');
  assert.deepEqual((revised.interaction?.payload.questions as any[]).map((question) => [
    question.id, question.defaultValue,
  ]), [
    ['problem', ['形成营销推荐知识']],
    ['target_users', ['客户经理']],
    ['scenarios', ['客户拜访前']],
    ['success_criteria', ['推荐可追溯']],
  ]);
  assert.ok((revised.interaction?.payload.questions as any[]).every(
    (question) => question.allowOther === true,
  ));
});

test('确认建模目标后自动规划流程访谈、流程图和人工确认任务', () => {
  const project = createMaterialFirstProject('流程访谈编排');
  const graph = new AiConstructionTaskGraph(getDb());
  const state = new AuthoringState(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb(), state });
  const analyzed = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-1',
    messageKey: 'materials.analyzed.process-flow',
    analysis: { domainOverview: '授信资料概览', candidateObjects: [], openQuestions: [] },
  });
  const materialsConfirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: analyzed.aiConstruction.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const goalStarted = orchestrator.advanceAfterMilestoneConfirmation(project.id, materialsConfirmed);
  const goalTask = goalStarted.aiConstruction.tasks.find((item) => item.kind === 'modeling_brief')!;
  const goalAnswered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: goalStarted.interaction!.id,
    responseId: 'goal-before-process', response: { answers: [] },
  });
  if (goalAnswered.kind !== 'interaction') throw new Error('回答建模目标失败');
  state.apply({
    kind: 'update_run', projectId: project.id, runId: goalAnswered.run.id, status: 'completed',
    result: { purpose: 'modeling_brief' },
  });
  const goalCompleted = orchestrator.completeModelingBrief(project.id, {
    taskId: goalTask.id, packageRevisionOid: 'draft-revision-1', messageKey: 'goal.process.ready',
    brief: {
      problem: '沉淀授信流程', targetUsers: '客户经理',
      scenarios: '授信申请', successCriteria: '流程可复用',
    },
  });
  const goalConfirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: goalCompleted.aiConstruction.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'draft-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  });

  const advanced = orchestrator.advanceAfterMilestoneConfirmation(project.id, goalConfirmed);
  const processTasks = advanced.aiConstruction.tasks.filter(
    (item) => item.milestoneId === 'process_confirmed',
  );
  assert.deepEqual(processTasks.map((item) => [item.kind, item.status]), [
    ['process_interview', 'waiting_user'],
    ['process_diagram', 'pending'],
    ['process_review', 'pending'],
  ]);
  assert.equal(advanced.interaction?.payload.purpose, 'process_interview');
  assert.equal(advanced.interaction?.payload.stage, 'core');
  assert.deepEqual((advanced.interaction?.payload.questions as any[]).map((item) => item.id), [
    'process_title', 'trigger', 'actors', 'outputs',
  ]);
  assert.equal((advanced.message?.blocks[0] as any).title, '进入业务流程访谈');
});

test('流程访谈完成后生成 Mermaid 候选并等待人工确认', () => {
  const project = createMaterialFirstProject('流程图候选编排');
  const graph = new AiConstructionTaskGraph(getDb());
  const state = new AuthoringState(getDb());
  const orchestrator = new AiConstructionOrchestrator({ db: getDb(), state });
  const analyzed = orchestrator.completeMaterialAnalysis(project.id, {
    packageRevisionOid: 'draft-revision-2', messageKey: 'materials.diagram',
    analysis: { domainOverview: '流程资料', candidateObjects: [], openQuestions: [] },
  });
  let snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: analyzed.aiConstruction.revision,
    milestoneId: 'materials_ready', packageRevisionOid: 'draft-revision-2',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const goal = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot);
  const goalTask = goal.aiConstruction.tasks.find((item) => item.kind === 'modeling_brief')!;
  const goalAnswer = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: goal.interaction!.id,
    responseId: 'goal-diagram', response: { answers: [] },
  });
  if (goalAnswer.kind !== 'interaction') throw new Error('回答建模目标失败');
  state.apply({ kind: 'update_run', projectId: project.id, runId: goalAnswer.run.id, status: 'completed' });
  snapshot = orchestrator.completeModelingBrief(project.id, {
    taskId: goalTask.id, packageRevisionOid: 'draft-revision-2', messageKey: 'goal.diagram',
    brief: { problem: '流程建模', targetUsers: '专家', scenarios: '审批', successCriteria: '可视化' },
  }).aiConstruction;
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: 'draft-revision-2',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const process = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot);
  const interviewTask = process.aiConstruction.tasks.find((item) => item.kind === 'process_interview')!;
  const answered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: process.interaction!.id,
    responseId: 'process-core', response: { answers: [] },
  });
  if (answered.kind !== 'interaction') throw new Error('回答流程访谈失败');
  state.apply({ kind: 'update_run', projectId: project.id, runId: answered.run.id, status: 'completed' });

  const completed = orchestrator.completeProcessInterview(project.id, {
    taskId: interviewTask.id,
    packageRevisionOid: 'draft-revision-2',
    messageKey: 'process.diagram.ready',
    outcome: {
      summary: '已完成授信审批流程访谈',
      processes: [{
        id: 'process_1', title: '授信审批', trigger: '提交申请', actors: ['客户经理'],
        inputs: ['申请资料'],
        steps: [
          { id: 'step_1', label: '受理申请', kind: 'action' },
          { id: 'step_2', label: '资料是否完整', kind: 'decision' },
        ],
        transitions: [{ from: 'step_1', to: 'step_2' }],
        exceptions: ['资料缺失时退回'], outputs: ['审批结论'], handoffs: ['交给审批人'],
      }],
    },
  });

  assert.deepEqual(completed.aiConstruction.tasks
    .filter((item) => item.milestoneId === 'process_confirmed')
    .map((item) => [item.kind, item.status]), [
    ['process_interview', 'completed'],
    ['process_diagram', 'completed'],
    ['process_review', 'waiting_user'],
  ]);
  assert.equal(completed.interaction?.type, 'flowchart_review');
  assert.equal(completed.interaction?.payload.purpose, 'process_review');
  assert.match(String((completed.interaction?.payload.candidateFiles as any)?.['diagrams/process_1.mmd']), /flowchart TD/);
  assert.equal((completed.message?.blocks[0] as any).title, '流程图草稿已生成');

  const reviewed = orchestrator.completeProcessReview(project.id, {
    taskId: completed.aiConstruction.tasks.find((item) => item.kind === 'process_review')!.id,
    packageRevisionOid: 'draft-revision-3',
    messageKey: 'process.review.accepted',
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: reviewed.aiConstruction.revision,
    milestoneId: 'process_confirmed', packageRevisionOid: 'draft-revision-3',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const knowledge = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot);

  assert.deepEqual(knowledge.aiConstruction.tasks
    .filter((item) => item.milestoneId === 'knowledge_confirmed')
    .map((item) => [item.kind, item.status]), [
    ['operation_extraction', 'pending'],
    ['knowledge_extraction', 'pending'],
    ['package_validation', 'pending'],
  ]);
  assert.deepEqual((knowledge.message?.blocks[2] as any).actions.map((item: any) => item.id), [
    'generate_knowledge_structure', 'review_process_files',
  ]);

  const started = orchestrator.startKnowledgeStructure(project.id);
  assert.deepEqual(started.aiConstruction.tasks
    .filter((item) => item.milestoneId === 'knowledge_confirmed')
    .map((item) => [item.kind, item.status]), [
    ['operation_extraction', 'running'],
    ['knowledge_extraction', 'pending'],
    ['package_validation', 'pending'],
  ]);
  const failed = orchestrator.failKnowledgeStructure(project.id, '模型响应中断');
  assert.equal(failed.aiConstruction.tasks.find(
    (item) => item.kind === 'operation_extraction',
  )?.status, 'failed');
  assert.deepEqual((failed.message.blocks[2] as any).actions.map((item: any) => item.id), [
    'generate_knowledge_structure', 'review_process_files',
  ]);
  const retried = orchestrator.startKnowledgeStructure(project.id);
  assert.equal(retried.aiConstruction.tasks.find(
    (item) => item.kind === 'operation_extraction',
  )?.status, 'running');

  const waiting = orchestrator.waitForKnowledgeStructureDecision(project.id, {
    interactionId: 'interaction-generation-gate',
    failure: {
      phase: 'operation-batch-1', batchKind: 'operation', batchIndex: 0,
      title: '业务操作批次仍有缺失', issues: ['缺少一个必要输出定义'],
    },
  });
  assert.equal(waiting.aiConstruction.tasks.find(
    (item) => item.kind === 'operation_extraction',
  )?.status, 'waiting_user');
  assert.match((waiting.message.blocks[1] as any).text, /专家选择重新生成或跳过该批/);
  const resumed = orchestrator.startKnowledgeStructure(project.id);
  assert.equal(resumed.aiConstruction.tasks.find(
    (item) => item.kind === 'operation_extraction',
  )?.status, 'running');

  const validating = orchestrator.startKnowledgeStructureValidation(project.id, {
    packageRevisionOid: 'generated-revision-1',
    version: '1.0.0',
    messageKey: 'knowledge.validating.v1.0.0',
  });
  assert.deepEqual(validating.aiConstruction.tasks
    .filter((item) => item.milestoneId === 'knowledge_confirmed')
    .map((item) => [item.kind, item.status, item.outputRevisionOid]), [
    ['operation_extraction', 'completed', 'generated-revision-1'],
    ['knowledge_extraction', 'completed', 'generated-revision-1'],
    ['package_validation', 'running', undefined],
  ]);
  assert.equal((validating.message.blocks[0] as any).title, '正在校验知识结构');

  const rejected = orchestrator.completeKnowledgeStructure(project.id, {
    packageRevisionOid: 'generated-revision-1',
    messageKey: 'knowledge.generated.invalid.v1.0.0',
    version: '1.0.0',
    counts: { operations: 1, objects: 3, relations: 2, rules: 4, metrics: 1 },
    validation: { errors: 1, warnings: 1, hints: 2 },
  });
  assert.equal(rejected.aiConstruction.tasks.find(
    (item) => item.kind === 'package_validation',
  )?.status, 'failed');
  assert.deepEqual((rejected.message.blocks[3] as any).actions.map((item: any) => item.id), [
    'generate_knowledge_structure', 'view_validation',
  ]);
  const validationRetried = orchestrator.startKnowledgeStructure(project.id);
  assert.equal(validationRetried.aiConstruction.tasks.find(
    (item) => item.kind === 'package_validation',
  )?.status, 'running');

  const generated = orchestrator.completeKnowledgeStructure(project.id, {
    packageRevisionOid: 'generated-revision-1',
    messageKey: 'knowledge.generated.v1.0.0',
    version: '1.0.0',
    counts: { operations: 1, objects: 3, relations: 2, rules: 4, metrics: 1 },
    validation: { errors: 0, warnings: 1, hints: 2 },
  });
  assert.deepEqual(generated.aiConstruction.tasks
    .filter((item) => item.milestoneId === 'knowledge_confirmed')
    .map((item) => [item.kind, item.status, item.outputRevisionOid]), [
    ['operation_extraction', 'completed', 'generated-revision-1'],
    ['knowledge_extraction', 'completed', 'generated-revision-1'],
    ['package_validation', 'completed', 'generated-revision-1'],
  ]);
  assert.match((generated.message.blocks[1] as any).text, /operation 1 个/);
  assert.match((generated.message.blocks[1] as any).text, /错误 0/);
  assert.equal(generated.message.blocks[2].type, 'validation');
  assert.deepEqual((generated.message.blocks[2] as any).counts, { errors: 0, warnings: 1, hints: 2 });
  assert.deepEqual((generated.message.blocks[3] as any).actions.map((item: any) => item.id), [
    'confirm_knowledge', 'review_knowledge_files',
  ]);

  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: generated.aiConstruction.revision,
    milestoneId: 'knowledge_confirmed', packageRevisionOid: 'generated-revision-1',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const deterministic = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot, {
    scriptCandidates: [{
      operationKey: 'CalculateYield', title: '计算产品收率',
      confidence: 'high', reason: '包含数值输出',
    }],
  });
  const decisionTask = deterministic.aiConstruction.tasks.find(
    (item) => item.kind === 'script_decision',
  )!;
  assert.equal(decisionTask.status, 'waiting_user');
  assert.equal(deterministic.interaction?.payload.purpose, 'script_decision');
  assert.deepEqual((deterministic.interaction?.payload.questions as any[])[0].options, [
    {
      value: 'CalculateYield', label: '计算产品收率',
      description: '高置信度 · 包含数值输出',
    },
    {
      value: '__none__', label: '本轮暂不生成脚本',
      description: '保留为 operation、rule 或 metric，不生成确定性代码',
    },
  ]);

  const contracted = orchestrator.completeScriptDecision(project.id, {
    taskId: decisionTask.id,
    packageRevisionOid: 'generated-revision-1',
    selectedCandidates: [{
      operationKey: 'CalculateYield', title: '计算产品收率',
      confidence: 'high', reason: '包含数值输出',
      suggestedContract: '输入：input(number，必填)\n输出：result(number)\n测试：同一合法输入重复执行结果一致。',
    }],
    messageKey: 'script.decision.selected',
  });
  assert.equal(contracted.aiConstruction.tasks.find((item) => item.id === decisionTask.id)?.status, 'completed');
  assert.equal(contracted.aiConstruction.tasks.find((item) => item.kind === 'script_generation')?.status, 'waiting_user');
  assert.equal(contracted.interaction?.payload.purpose, 'script_contract');
  assert.deepEqual((contracted.interaction?.payload.questions as any[]).map((item) => [
    item.id, item.kind, item.required,
  ]), [['script_contract_1', 'single', true]]);
  assert.equal((contracted.interaction?.payload.questions as any[])[0].allowOther, true);
  assert.match(String((contracted.interaction?.payload.questions as any[])[0].options?.[0]?.label), /采用系统建议契约/);
  assert.match(String((contracted.interaction?.payload.questions as any[])[0].q), /计算产品收率/);

  const generating = orchestrator.startScriptGeneration(project.id);
  assert.equal(generating.aiConstruction.tasks.find(
    (item) => item.kind === 'script_generation',
  )?.status, 'running');
  assert.equal((generating.message.blocks[0] as any).title, '正在生成确定性脚本方案');

  const awaitingApproval = orchestrator.waitForScriptApproval(project.id);
  assert.equal(awaitingApproval.aiConstruction.tasks.find(
    (item) => item.kind === 'script_generation',
  )?.status, 'waiting_user');
  assert.equal((awaitingApproval.message.blocks[0] as any).title, '脚本变更方案等待确认');

  const applying = orchestrator.startScriptGeneration(project.id);
  assert.equal(applying.aiConstruction.tasks.find(
    (item) => item.kind === 'script_generation',
  )?.status, 'running');
  const scriptsCompleted = orchestrator.completeScriptGeneration(project.id, {
    packageRevisionOid: 'generated-revision-2',
    messageKey: 'script.generated.v1.1.0',
    scriptCount: 1,
    validation: { errors: 0, warnings: 1, hints: 0 },
  });
  assert.equal(scriptsCompleted.aiConstruction.tasks.find(
    (item) => item.kind === 'script_generation',
  )?.status, 'completed');
  assert.deepEqual((scriptsCompleted.message.blocks[2] as any).actions.map(
    (item: any) => item.id,
  ), ['confirm_deterministic', 'review_knowledge_files']);
});

test('选择确定性脚本候选后先确认输入输出和测试契约', () => {
  const project = createMaterialFirstProject('确定性脚本契约');
  const { snapshot } = advanceGraphToDeterministic(project.id);
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  const candidate = {
    operationKey: 'CalculateYield', title: '计算产品收率',
    confidence: 'high' as const, reason: '包含数值输出',
    suggestedContract: '输入：input(number，必填)\n输出：result(number)\n测试：同一合法输入重复执行结果一致。',
  };
  const decision = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot, {
    scriptCandidates: [candidate],
  });
  const decisionTask = decision.aiConstruction.tasks.find(
    (item) => item.kind === 'script_decision',
  )!;

  const contract = orchestrator.completeScriptDecision(project.id, {
    taskId: decisionTask.id,
    packageRevisionOid: 'revision-knowledge_confirmed',
    selectedCandidates: [candidate],
    messageKey: 'script.decision.selected',
  });

  const generationTask = contract.aiConstruction.tasks.find(
    (item) => item.kind === 'script_generation',
  )!;
  assert.equal(decisionTask.required, true);
  assert.equal(contract.aiConstruction.tasks.find(
    (item) => item.id === decisionTask.id,
  )?.status, 'completed');
  assert.equal(generationTask.status, 'waiting_user');
  assert.deepEqual(generationTask.dependsOn, [decisionTask.id]);
  assert.equal(contract.interaction?.payload.purpose, 'script_contract');
  assert.deepEqual(contract.interaction?.payload.candidates, [candidate]);
  assert.deepEqual(contract.interaction?.payload.questions, [{
    id: 'script_contract_1', kind: 'single',
    q: '请确认“计算产品收率”脚本的输入、输出和至少一个测试案例',
    required: true,
    allowOther: true,
    options: [{
      value: candidate.suggestedContract,
      label: '采用系统建议契约',
      description: candidate.suggestedContract,
    }],
  }]);

  const generating = orchestrator.startScriptGeneration(project.id);
  assert.equal(generating.aiConstruction.tasks.find(
    (item) => item.id === generationTask.id,
  )?.status, 'running');
  assert.equal((generating.message.blocks[0] as any).title, '正在生成确定性脚本方案');

  const approval = orchestrator.waitForScriptApproval(project.id);
  assert.equal(approval.aiConstruction.tasks.find(
    (item) => item.id === generationTask.id,
  )?.status, 'waiting_user');
  assert.equal((approval.message.blocks[0] as any).title, '脚本变更方案等待确认');

  const failed = orchestrator.failScriptGeneration(project.id, '知识包校验仍有阻断错误');
  assert.equal(failed.aiConstruction.tasks.find(
    (item) => item.id === generationTask.id,
  )?.status, 'failed');
  assert.deepEqual((failed.message.blocks[2] as any).actions.map((item: any) => item.id), [
    'defer_script_generation', 'review_knowledge_files',
  ]);
  orchestrator.startScriptGeneration(project.id);

  const completed = orchestrator.completeScriptGeneration(project.id, {
    packageRevisionOid: 'revision-script-v1', messageKey: 'script.generated.v1',
    scriptCount: 1,
    validation: { errors: 0, warnings: 1, hints: 0 },
  });
  assert.equal(completed.aiConstruction.tasks.find(
    (item) => item.id === generationTask.id,
  )?.status, 'completed');
  assert.deepEqual((completed.message.blocks[2] as any).actions.map((item: any) => item.id), [
    'confirm_deterministic', 'review_knowledge_files',
  ]);

  const graph = new AiConstructionTaskGraph(getDb());
  const deterministicConfirmed = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: completed.aiConstruction.revision,
    milestoneId: 'deterministic_confirmed', packageRevisionOid: 'revision-script-v1',
    actor: { username: 'alice', role: 'project_owner' },
  });
  const completion = orchestrator.advanceAfterMilestoneConfirmation(
    project.id,
    deterministicConfirmed,
  );
  const completionTask = completion.aiConstruction.tasks.find(
    (item) => item.kind === 'completion_review',
  )!;
  assert.equal(completionTask.status, 'completed');
  assert.deepEqual(completionTask.dependsOn, [generationTask.id]);
  assert.deepEqual((completion.message?.blocks[2] as any).actions.map((item: any) => item.id), [
    'confirm_construction', 'review_knowledge_files',
  ]);
});

test('没有脚本需求时明确确认后可以结束确定性能力阶段', () => {
  const project = createMaterialFirstProject('无确定性脚本需求');
  const { snapshot } = advanceGraphToDeterministic(project.id);
  const orchestrator = new AiConstructionOrchestrator({ db: getDb() });
  const decision = orchestrator.advanceAfterMilestoneConfirmation(project.id, snapshot, {
    scriptCandidates: [],
  });
  const task = decision.aiConstruction.tasks.find((item) => item.kind === 'script_decision')!;

  const completed = orchestrator.completeScriptDecision(project.id, {
    taskId: task.id,
    packageRevisionOid: 'revision-knowledge_confirmed',
    selectedCandidates: [],
    messageKey: 'script.decision.none',
  });

  assert.equal(completed.aiConstruction.tasks.find((item) => item.id === task.id)?.status, 'completed');
  assert.equal(completed.aiConstruction.tasks.some((item) => item.kind === 'script_generation'), false);
  assert.deepEqual((completed.message?.blocks[2] as any).actions.map((item: any) => item.id), [
    'confirm_deterministic', 'review_knowledge_files',
  ]);
});
