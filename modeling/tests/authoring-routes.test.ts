import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-authoring-routes-'));

const [{ registerAuthoringRoutes }, { getAuthoringState }, { createProject }, { getDb }] = await Promise.all([
  import('../src/authoring/routes.js'),
  import('../src/authoring/state.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
]);

function routeApp(t: test.TestContext, username = 'alice', options: Record<string, unknown> = {}) {
  const app = Fastify();
  app.addHook('preHandler', async (req) => {
    req.user = { username, role: 'modeler' };
  });
  registerAuthoringRoutes(app, options as any);
  t.after(() => app.close());
  return app;
}

test('会话快照、任务图命令和事件回放保持同一游标', async (t) => {
  const project = createProject({ owner: 'alice', name: '事件工作区' });
  const app = routeApp(t);

  const first = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  assert.equal(first.statusCode, 200);
  const firstBody = first.json();
  assert.equal(firstBody.aiConstruction.revision, 1);
  assert.equal(firstBody.cursor, 0);

  const taskId = firstBody.aiConstruction.tasks[0].id;
  const updated = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: { command: { type: 'start_task', expectedRevision: 1, taskId } },
  });
  assert.equal(updated.statusCode, 200, updated.body);
  assert.equal(updated.json().aiConstruction.revision, 2);
  assert.equal(updated.json().cursor, 1);

  const replay = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/events?after=0`,
  });
  assert.equal(replay.statusCode, 200);
  assert.deepEqual(replay.json().events.map((event: any) => [event.seq, event.type]), [
    [1, 'ai_construction.graph.updated'],
  ]);
  assert.equal(replay.json().events[0].payload.command, 'start_task');
});

test('通用待办接口不能绕过场景审核专用闭环', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '场景审核入口边界' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '场景推演', state: { stepId: 'scenario' },
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('场景推演运行创建失败');
  const opened = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: started.run.id,
    type: 'scenario_review',
    payload: { title: '确认异常场景' },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('场景待办创建失败');
  const app = routeApp(t);

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'generic-scenario-response', response: { action: 'accept' } },
  });

  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'SCENARIO_REVIEW_ENDPOINT_REQUIRED');
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'pending');
  assert.equal(state.snapshot(project.id).activeRuns.some((run) => run.id === started.run.id), true);
});

test('完成建模确认时固化验证候选并让任务图引用候选修订', async (t) => {
  const project = createProject({ owner: 'alice', name: '验证候选生命周期' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [], 'pkg-draft');

  let savedInput: any;
  let appliedCommand: any;
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-draft' },
        packageVersion: '0.1.0',
        spec: { status: 'draft', objects: [], operations: [] },
      };
    },
    async saveCurrentAsPackageVersion(_projectId: string, input: any) {
      savedInput = input;
      return {
        revision: { oid: 'pkg-rc-1' },
        packageVersion: input.version,
        spec: { status: input.status, objects: [], operations: [] },
      };
    },
  };
  const graph = {
    snapshot: () => ({ revision: 6 }),
    apply: (_projectId: string, command: any) => {
      appliedCommand = command;
      return { revision: 7, status: 'completed' };
    },
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
    completeProcessReview: () => { throw new Error('测试不应进入流程确认'); },
  };
  const app = routeApp(t, 'alice', { packages, graph, orchestrator });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: {
      command: {
        type: 'confirm_milestone',
        expectedRevision: 6,
        milestoneId: 'construction_completed',
        packageRevisionOid: 'pkg-draft',
        actor: { username: 'ignored', role: 'validator' },
      },
    },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(response.json().package, {
    version: '1.0.0-rc.1', revision: 'pkg-rc-1', status: 'review',
  });
  assert.equal(savedInput.expectedRevision, 'pkg-draft');
  assert.equal(savedInput.version, '1.0.0-rc.1');
  assert.equal(savedInput.status, 'review');
  assert.equal(appliedCommand.packageRevisionOid, 'pkg-rc-1');
  assert.equal(appliedCommand.actor.username, 'alice');
  assert.equal(response.json().cursor, 2);
});

test('暂缓当前修订阻断错误时持久化审计记录并推进编排状态', async (t) => {
  const project = createProject({ owner: 'alice', name: '暂缓错误继续验证' });
  const [{ saveCheck }, { getPackageErrorDeferral }] = await Promise.all([
    import('../src/workflow/package-check.js'),
    import('../src/workflow/package-error-deferral.js'),
  ]);
  saveCheck(project.id, '0.1.0', [{
    id: 'error-1', source: 'semantic', severity: '错误', category: '口径冲突',
    target: 'operation:CalculateScore', message: '指标口径尚未统一',
  }], 'pkg-current');

  let deferredInput: any;
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-current' },
        packageVersion: '0.1.0',
        spec: { status: 'draft', objects: [], operations: [] },
      };
    },
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
    completeProcessReview: () => { throw new Error('测试不应进入流程确认'); },
    deferKnowledgeErrors: (_projectId: string, input: any) => {
      deferredInput = input;
      return { aiConstruction: { revision: 8 }, message: { id: 'msg-deferred' } };
    },
  };
  const app = routeApp(t, 'alice', { packages, orchestrator });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/knowledge/defer-errors`,
    payload: { reason: '先跑完真实评测，再统一修复当前口径问题' },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().deferral.valid, true);
  assert.equal(response.json().deferral.errorCount, 1);
  assert.deepEqual(deferredInput, {
    packageRevisionOid: 'pkg-current',
    errorCount: 1,
    reason: '先跑完真实评测，再统一修复当前口径问题',
    actor: 'alice',
  });
  assert.equal(getPackageErrorDeferral(project.id, 'pkg-current')?.actor, 'alice');
});

test('待办回答支持请求号幂等并在会话中恢复运行态', async (t) => {
  const project = createProject({ owner: 'alice', name: '待办恢复' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认知识对象', state: { step: 1 },
  });
  assert.equal(run.kind, 'run');
  if (run.kind !== 'run') throw new Error('未创建运行');
  const pending = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: run.run.id,
    type: 'decision',
    payload: { title: '确认对象边界' },
  });
  assert.equal(pending.kind, 'interaction');
  if (pending.kind !== 'interaction') throw new Error('未创建待办');

  const app = routeApp(t);
  const answer = {
    method: 'POST' as const,
    url: `/api/projects/${project.id}/interactions/${pending.interaction.id}/respond`,
    payload: { requestId: 'answer-1', response: { accepted: true } },
  };
  const first = await app.inject(answer);
  assert.equal(first.statusCode, 200, first.body);
  assert.equal(first.json().replayed, false);
  assert.equal(first.json().run.status, 'running');

  const retry = await app.inject(answer);
  assert.equal(retry.statusCode, 200, retry.body);
  assert.equal(retry.json().replayed, true);

  const session = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  assert.equal(session.json().authoring.pendingInteractions.length, 0);
  assert.equal(session.json().authoring.activeRuns[0].status, 'running');
});

test('存量项目只在会话推进时按项目类型显式初始化任务图', async (t) => {
  const now = new Date().toISOString();
  getDb().prepare(`INSERT INTO projects
    (id, owner, kind, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?)`)
    .run('pj_legacy_route', 'alice', 'modeling', '存量项目', '', '', 'project_setup', now, now);
  getDb().prepare(`INSERT INTO projects
    (id, owner, kind, name, domain, background, stage, created_at, updated_at)
    VALUES (?,?,?,?,?,?,?,?,?)`)
    .run('pj_package_route', 'alice', 'package', '存量知识包', '', '', 'review', now, now);
  const app = routeApp(t);

  const session = await app.inject({ method: 'GET', url: '/api/projects/pj_legacy_route/session' });
  assert.equal(session.statusCode, 200, session.body);
  assert.equal(session.json().aiConstruction, null);

  const modelingAdvance = await app.inject({
    method: 'POST', url: '/api/projects/pj_legacy_route/session/advance',
  });
  assert.equal(modelingAdvance.statusCode, 200, modelingAdvance.body);
  assert.equal(modelingAdvance.json().aiConstruction.source, 'new');
  assert.equal(modelingAdvance.json().aiConstruction.tasks[0].kind, 'modeling_brief');

  const packageAdvance = await app.inject({
    method: 'POST', url: '/api/projects/pj_package_route/session/advance',
  });
  assert.equal(packageAdvance.statusCode, 200, packageAdvance.body);
  assert.equal(packageAdvance.json().aiConstruction.source, 'package_import');
  assert.equal(packageAdvance.json().aiConstruction.tasks[0].kind, 'package_baseline_review');
});

test('项目会话和任务图接口隔离非项目所有者', async (t) => {
  const project = createProject({ owner: 'alice', name: '权限隔离' });
  const app = routeApp(t, 'bob');

  const session = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/session` });
  const command = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/commands`,
    payload: { command: { type: 'start_task', expectedRevision: 1, taskId: 'hidden' } },
  });
  assert.equal(session.statusCode, 404);
  assert.equal(command.statusCode, 404);
});

test('可编辑流程图从草稿保存到原子提交形成单一事实源', async (t) => {
  const project = createProject({ owner: 'alice', name: '流程图确认' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认业务流程', baseRevision: 'pkg-base',
  });
  assert.equal(run.kind, 'run');
  if (run.kind !== 'run') throw new Error('未创建流程图任务');
  const pending = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: run.run.id,
    type: 'flowchart_review',
    baseRevision: 'pkg-base',
    payload: { purpose: 'process_review', taskId: 'task-process-review', diagrams: [] },
  });
  assert.equal(pending.kind, 'interaction');
  if (pending.kind !== 'interaction') throw new Error('未创建流程图待办');

  let saveInput: any;
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-base' }, packageVersion: '0.1.0', spec: {}, coverage: {}, files: [],
      };
    },
    async readPackageFiles() {
      return {
        revision: { oid: 'pkg-base' }, spec: {}, coverage: {},
        files: { 'diagrams/process_9.mmd': 'stale' },
      };
    },
    async saveFiles(_projectId: string, input: any) {
      saveInput = input;
      return {
        revision: { oid: 'pkg-flowchart' }, packageVersion: '0.1.0', spec: {}, coverage: {},
        files: Object.keys(input.files),
      };
    },
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
    completeProcessReview: () => ({
      aiConstruction: { revision: 8 },
      message: { id: 'message-flowchart' },
    }),
  };
  const app = routeApp(t, 'alice', { packages, orchestrator });
  const base = `/api/projects/${project.id}/interactions/${pending.interaction.id}`;

  const created = await app.inject({ method: 'GET', url: `${base}/flowchart-draft` });
  assert.equal(created.statusCode, 200, created.body);
  assert.equal(created.json().draftRevision, 1);
  assert.deepEqual(created.json().document, { diagrams: [] });

  const emptySubmit = await app.inject({
    method: 'POST', url: `${base}/respond`,
    payload: { requestId: 'flowchart-empty', response: { action: 'accept', draftRevision: 1 } },
  });
  assert.equal(emptySubmit.statusCode, 409, emptySubmit.body);
  assert.equal(emptySubmit.json().code, 'FLOWCHART_EMPTY_DOCUMENT');

  const document = {
    diagrams: [{
      id: 'approval', title: '授信审批',
      processMeta: { actors: ['客户经理'], inputs: ['申请材料'], exceptions: [], handoffs: [] },
      nodes: [
        { id: 'start', label: '收到申请', kind: 'start' },
        { id: 'review', label: '审核材料', kind: 'action' },
        { id: 'end', label: '形成结论', kind: 'end' },
      ],
      edges: [
        { id: 'start-review', from: 'start', to: 'review', kind: 'sequence' },
        { id: 'review-end', from: 'review', to: 'end', kind: 'sequence' },
      ],
    }],
  };
  const saved = await app.inject({
    method: 'PUT', url: `${base}/flowchart-draft`,
    payload: { expectedRevision: 1, document },
  });
  assert.equal(saved.statusCode, 200, saved.body);
  assert.equal(saved.json().draftRevision, 2);
  assert.equal(saved.json().warnings.length, 0);

  const answer = {
    method: 'POST' as const,
    url: `${base}/respond`,
    payload: { requestId: 'flowchart-accept', response: { action: 'accept', draftRevision: 2 } },
  };
  const submitted = await app.inject(answer);
  assert.equal(submitted.statusCode, 200, submitted.body);
  assert.equal(submitted.json().packageRevision, 'pkg-flowchart');
  assert.equal(saveInput.expectedRevision, 'pkg-base');
  assert.deepEqual(saveInput.deletePaths, ['package/diagrams/process_9.mmd']);
  assert.deepEqual(JSON.parse(saveInput.files['package/diagrams/processes.json']).diagrams[0].id, 'approval');
  assert.match(saveInput.files['package/diagrams/process_1.mmd'], /^flowchart TD/m);

  const retry = await app.inject(answer);
  assert.equal(retry.statusCode, 200, retry.body);
  assert.equal(retry.json().replayed, true);
  assert.equal(getDb().prepare(`SELECT status FROM flowchart_review_finalizations
    WHERE interaction_id = ?`).get(pending.interaction.id)?.status, 'completed');
});

test('建模目标待办回答后完成目标任务而不是只关闭待办', async (t) => {
  const project = createProject({ owner: 'alice', name: '建模目标续跑' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认建模目标', baseRevision: 'pkg-goal',
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建目标任务');
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'question_batch', baseRevision: 'pkg-goal',
    payload: { purpose: 'modeling_brief', taskId: 'task-goal', questions: [] },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建目标待办');

  let completedInput: any;
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-goal' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
    },
    modelingBriefResolver: async () => ({
      problem: '缩短审批时间', targetUsers: '客户经理',
      scenarios: '授信审批', successCriteria: '审批时长可量化下降',
    }),
    orchestrator: {
      completeModelingBrief: (_projectId: string, input: any) => {
        completedInput = input;
        return { aiConstruction: { revision: 4 }, message: { id: 'goal-completed' } };
      },
    },
  });
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'goal-answer', response: { answers: [{ questionId: 'goal_text', other: '缩短审批时间' }] } },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().run.status, 'completed');
  assert.equal(completedInput.taskId, 'task-goal');
  assert.equal(completedInput.packageRevisionOid, 'pkg-goal');
  assert.equal(completedInput.brief.problem, '缩短审批时间');
});

test('无完整资料候选时流程访谈按核心信息和细节两段续跑', async (t) => {
  const project = createProject({ owner: 'alice', name: '两段流程访谈' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '业务流程访谈', baseRevision: 'pkg-process',
    state: { purpose: 'process_interview', stage: 'core', taskId: 'task-process' },
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建流程访谈任务');
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'question_batch', baseRevision: 'pkg-process',
    payload: {
      purpose: 'process_interview', stage: 'core', taskId: 'task-process', candidates: [],
      questions: [
        { id: 'process_title' }, { id: 'trigger' }, { id: 'actors' }, { id: 'outputs' },
      ],
    },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建流程访谈待办');

  let completedInput: any;
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-process' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
    },
    orchestrator: {
      completeProcessInterview: (_projectId: string, input: any) => {
        completedInput = input;
        return { aiConstruction: { revision: 7 }, interaction: { id: 'flowchart-review' } };
      },
    },
  });
  const invalid = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'process-core-invalid', response: { answers: [] } },
  });
  assert.equal(invalid.statusCode, 400, invalid.body);
  assert.equal(invalid.json().code, 'INVALID_PROCESS_INTERVIEW_INPUT');

  const core = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'process-core',
      response: { answers: [
        { questionId: 'process_title', other: '授信审批' },
        { questionId: 'trigger', other: '客户提交申请' },
        { questionId: 'actors', other: '客户经理；审批人' },
        { questionId: 'outputs', other: '审批结论' },
      ] },
    },
  });
  assert.equal(core.statusCode, 200, core.body);
  assert.equal(core.json().nextInteraction.payload.stage, 'details');
  assert.equal(core.json().nextInteraction.payload.core.title, '授信审批');
  assert.deepEqual(core.json().nextInteraction.payload.questions.map((question: any) => question.id), [
    'inputs', 'steps', 'exceptions', 'handoffs',
  ]);

  const detailsInteraction = core.json().nextInteraction;
  const details = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${detailsInteraction.id}/respond`,
    payload: {
      requestId: 'process-details',
      response: { answers: [
        { questionId: 'inputs', other: '申请材料' },
        { questionId: 'steps', other: '受理申请\n判断：材料是否完整\n形成审批结论' },
        { questionId: 'exceptions', other: '材料缺失时退回' },
        { questionId: 'handoffs', other: '客户经理提交审批人' },
      ] },
    },
  });
  assert.equal(details.statusCode, 200, details.body);
  assert.equal(details.json().run.status, 'completed');
  assert.equal(completedInput.taskId, 'task-process');
  assert.equal(completedInput.outcome.processes[0].title, '授信审批');
  assert.deepEqual(completedInput.outcome.processes[0].steps.map((step: any) => step.kind), [
    'action', 'decision', 'action',
  ]);
});

test('脚本候选和契约待办连续推进到受控变更方案', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本生成续跑' });
  const state = getAuthoringState();
  const decisionRun = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认脚本需求', baseRevision: 'pkg-script',
  });
  assert.equal(decisionRun.kind, 'run');
  if (decisionRun.kind !== 'run') throw new Error('未创建脚本决策任务');
  const candidates = [{
    operationKey: 'CalculateScore', title: '计算评分', confidence: 'high', reason: '包含数值输出',
    suggestedContract: '输入：items\n输出：score\n测试：重复输入结果一致',
  }];
  const decision = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: decisionRun.run.id,
    type: 'question_batch', baseRevision: 'pkg-script',
    payload: { purpose: 'script_decision', taskId: 'task-decision', candidates, questions: [] },
  });
  assert.equal(decision.kind, 'interaction');
  if (decision.kind !== 'interaction') throw new Error('未创建脚本决策待办');

  let contractInteraction: any;
  let plannedRequirement = '';
  let plannedOptions: any;
  const orchestrator = {
    completeScriptDecision: () => {
      const run = state.apply({
        kind: 'start_run', projectId: project.id, intent: '确认脚本契约', baseRevision: 'pkg-script',
      });
      if (run.kind !== 'run') throw new Error('未创建脚本契约任务');
      const opened = state.apply({
        kind: 'open_interaction', projectId: project.id, runId: run.run.id,
        type: 'question_batch', baseRevision: 'pkg-script',
        payload: { purpose: 'script_contract', taskId: 'task-generation', candidates, questions: [] },
      });
      if (opened.kind !== 'interaction') throw new Error('未创建脚本契约待办');
      contractInteraction = opened.interaction;
      return { aiConstruction: { revision: 8 }, interaction: opened.interaction };
    },
    startScriptGeneration: () => ({ aiConstruction: { revision: 9 } }),
    waitForScriptApproval: () => ({ aiConstruction: { revision: 10 }, message: { id: 'script-waiting' } }),
    failScriptGeneration: () => ({ aiConstruction: { revision: 11 } }),
  };
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-script' }, packageVersion: '1.0.0-rc.1', spec: { objects: [], operations: [] } };
      },
    },
    orchestrator,
    packageChatPlan: async (_project: any, _history: any[], requirement: string, options: any) => {
      plannedRequirement = requirement;
      plannedOptions = options;
      return { interaction: { id: 'script-plan', runId: 'run-script-plan', baseRevision: 'pkg-script' } };
    },
  });

  const selected = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${decision.interaction.id}/respond`,
    payload: {
      requestId: 'script-decision-answer',
      response: { answers: [{ questionId: 'script_candidates', selected: ['CalculateScore'] }] },
    },
  });
  assert.equal(selected.statusCode, 200, selected.body);
  assert.equal(selected.json().run.status, 'completed');
  assert.equal(contractInteraction.payload.purpose, 'script_contract');

  const contracted = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${contractInteraction.id}/respond`,
    payload: {
      requestId: 'script-contract-answer',
      response: { answers: [{ questionId: 'script_contract_1', selected: [candidates[0].suggestedContract] }] },
    },
  });
  assert.equal(contracted.statusCode, 200, contracted.body);
  assert.match(plannedRequirement, /CalculateScore/);
  assert.match(plannedRequirement, /重复输入结果一致/);
  assert.equal(plannedOptions.purpose, 'script_generation');
  assert.equal(contracted.json().scriptPlan.interaction.id, 'script-plan');
});

test('脚本变更在保存前必须通过格式、规则和语义候选校验', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本候选保存前校验' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '生成脚本变更方案', baseRevision: 'pkg-script-base',
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建脚本生成任务');
  const opened = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: started.run.id,
    type: 'change_plan',
    baseRevision: 'pkg-script-base',
    payload: {
      purpose: 'script_generation',
      summary: '新增确定性计算脚本',
      message: '为计划校验生成脚本',
      history: [],
      changes: [{
        id: 'script-change', targetKind: 'script', targetKey: 'calculate_plan', action: 'add',
        description: '生成参数化计划校验脚本',
      }],
    },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建脚本方案待办');

  let validationDecision: any;
  let failedReason = '';
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return {
          revision: { oid: 'pkg-script-base' },
          packageVersion: '1.0.0-rc.1',
          spec: {
            id: 'script-validation', name: '脚本候选校验', description: '脚本候选校验',
            version: '1.0.0-rc.1', language: 'zh-CN', defaultProviderAlias: 'data',
            objects: [], operations: [], rules: [], metrics: [], scripts: [],
          },
        };
      },
    },
    orchestrator: {
      startScriptGeneration: () => ({ aiConstruction: { revision: 4 } }),
      failScriptGeneration: (_projectId: string, reason: string) => {
        failedReason = reason;
        return { aiConstruction: { revision: 5 } };
      },
    },
    packageChatApply: async (
      _project: any,
      _history: any[],
      _message: string,
      _plan: any,
      _tables: any[],
      options: any,
    ) => {
      assert.equal(typeof options.validateCandidate, 'function');
      validationDecision = await options.validateCandidate({
        spec: {
          id: 'invalid-script-candidate', name: '不完整候选', description: '缺少渲染文件',
          version: '1.0.0-rc.2', language: 'zh-CN', defaultProviderAlias: 'data',
          objects: [], operations: [], rules: [], metrics: [], scripts: [],
        },
        files: {},
      });
      throw new Error('候选未通过时不得保存');
    },
  });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'approve-invalid-script-candidate',
      response: { action: 'approve', selectedChangeIds: ['script-change'] },
    },
  });

  assert.equal(response.statusCode, 500, response.body);
  assert.equal(validationDecision.accepted, false);
  assert.equal(validationDecision.disposition, 'rejected');
  assert.match(validationDecision.feedback, /阻断错误/);
  assert.match(failedReason, /候选未通过/);
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'pending');
});

test('知识生成只读取当前 Git 修订中已确认的流程文档', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Git 流程事实源' });
  const document = {
    diagrams: [{
      id: 'approval', title: '授信审批',
      nodes: [{ id: 'start', label: '收到申请', kind: 'start' }, { id: 'end', label: '完成审批', kind: 'end' }],
      edges: [{ id: 'edge', from: 'start', to: 'end', kind: 'sequence' }],
    }],
  };
  let generatedFlowchart: any;
  const app = routeApp(t, 'alice', {
    graph: {
      snapshot: () => ({
        confirmations: [{ milestoneId: 'process_confirmed', packageRevisionOid: 'pkg-flow-current' }],
      }),
    },
    packages: {
      async current() {
        return { revision: { oid: 'pkg-flow-current' }, packageVersion: '1.0.0-rc.1', spec: { objects: [], operations: [] } };
      },
      async readPackageFiles() {
        return {
          revision: { oid: 'pkg-flow-current' }, spec: { objects: [], operations: [] }, coverage: {},
          files: { 'diagrams/processes.json': JSON.stringify(document) }, binaryFiles: [],
        };
      },
    },
    knowledgeRunner: {
      async run(_project: any, _context: any, flowchart: any) {
        generatedFlowchart = flowchart;
        return { version: '1.0.0-rc.2', revisionId: 'pkg-knowledge' };
      },
    },
  });

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/generate`, payload: {},
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(generatedFlowchart.diagrams[0].id, 'approval');
  assert.equal(response.json().revisionId, 'pkg-knowledge');
});

test('生成门禁决定在原运行失败后通过新运行幂等续跑', async (t) => {
  const project = createProject({ owner: 'alice', name: '生成门禁失败恢复' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '生成知识包', baseRevision: 'pkg-gate',
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建知识生成任务');
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'decision', baseRevision: 'pkg-gate',
    payload: { purpose: 'generation_gate_skip', phase: 'operation_batch_2' },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建生成门禁待办');
  const answered = state.apply({
    kind: 'respond_interaction', projectId: project.id, interactionId: opened.interaction.id,
    responseId: 'generation-gate-answer', response: { value: 'skip' },
  });
  assert.equal(answered.kind, 'interaction');
  if (answered.kind !== 'interaction') throw new Error('未回答生成门禁待办');
  state.apply({
    kind: 'update_run', projectId: project.id, runId: started.run.id,
    status: 'failed', error: '恢复时进程中断',
  });

  let continuation: any;
  const document = {
    diagrams: [{
      id: 'process', title: '审批',
      nodes: [{ id: 'start', label: '开始', kind: 'start' }, { id: 'end', label: '结束', kind: 'end' }],
      edges: [{ id: 'edge', from: 'start', to: 'end', kind: 'sequence' }],
    }],
  };
  const app = routeApp(t, 'alice', {
    graph: {
      snapshot: () => ({
        confirmations: [{ milestoneId: 'process_confirmed', packageRevisionOid: 'pkg-gate' }],
      }),
    },
    packages: {
      async current() {
        return { revision: { oid: 'pkg-gate' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
      async readPackageFiles() {
        return {
          revision: { oid: 'pkg-gate' }, spec: { objects: [], operations: [] }, coverage: {},
          files: { 'diagrams/processes.json': JSON.stringify(document) }, binaryFiles: [],
        };
      },
    },
    knowledgeRunner: {
      async run(_project: any, _context: any, _flowchart: any, options: any) {
        continuation = options;
        return { revisionId: 'pkg-resumed', version: '1.0.0-rc.1' };
      },
    },
  });
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'generation-gate-answer', response: { value: 'skip' } },
  });

  assert.equal(response.statusCode, 200, response.body);
  assert.deepEqual(continuation.skipFailedPhases, ['operation_batch_2']);
  assert.notEqual(continuation.resumeRunId, started.run.id);
  const resumedRun = state.snapshot(project.id).activeRuns.find((run) => run.id === continuation.resumeRunId);
  assert.equal(resumedRun?.baseRevision, 'pkg-gate');
});
