import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-authoring-routes-'));

const [
  { registerAuthoringRoutes },
  { getAuthoringState },
  { createProject, setProjectMember },
  { getDb },
  { PackageCandidateRejectedError },
  { getPackageArtifactStore },
] = await Promise.all([
  import('../src/authoring/routes.js'),
  import('../src/authoring/state.js'),
  import('../src/projects/repo.js'),
  import('../src/db/index.js'),
  import('../src/workflow/package-chat.js'),
  import('../src/artifacts/index.js'),
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

test('项目事件允许 owner、modeler 和 viewer 读取，但对非成员隐藏', async (t) => {
  const project = createProject({ owner: 'event-owner', name: '成员事件权限' });
  setProjectMember(project.id, project.owner, 'event-modeler', 'modeler');
  setProjectMember(project.id, project.owner, 'event-viewer', 'viewer');

  for (const [username, expectedStatus] of [
    ['event-owner', 200],
    ['event-modeler', 200],
    ['event-viewer', 200],
    ['event-outsider', 404],
  ] as const) {
    const app = routeApp(t, username);
    const response = await app.inject({
      method: 'GET',
      url: `/api/projects/${project.id}/events?after=0`,
      headers: { accept: 'application/json' },
    });
    assert.equal(response.statusCode, expectedStatus, `${username}: ${response.body}`);
  }
});

test('项目成员被移除后，已有事件 SSE 连接会在权限复核时关闭', async (t) => {
  const project = createProject({ owner: 'stream-owner', name: '事件流动态权限' });
  setProjectMember(project.id, project.owner, 'stream-modeler', 'modeler');
  const app = routeApp(t, 'stream-modeler');
  const address = await app.listen({ host: '127.0.0.1', port: 0 });

  const response = await fetch(`${address}/api/projects/${project.id}/events?after=0`, {
    headers: { accept: 'text/event-stream' },
  });
  assert.equal(response.status, 200);
  assert.match(String(response.headers.get('content-type')), /text\/event-stream/);
  assert.ok(response.body);

  const reader = response.body!.getReader();
  const initial = await reader.read();
  assert.equal(initial.done, false);
  assert.match(new TextDecoder().decode(initial.value), /retry: 2000/);

  getDb().prepare('DELETE FROM project_members WHERE project_id = ? AND username = ?')
    .run(project.id, 'stream-modeler');

  const closed = await Promise.race([
    reader.read(),
    new Promise<never>((_resolve, reject) => {
      setTimeout(() => reject(new Error('成员权限撤销后 SSE 未及时关闭')), 4_500);
    }),
  ]);
  assert.equal(closed.done, true);

  const reconnect = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/events?after=0`,
    headers: { accept: 'application/json' },
  });
  assert.equal(reconnect.statusCode, 404);
});

test('项目成员可读取会话和订阅事件，非成员仍不可访问', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '成员事件订阅' });
  setProjectMember(project.id, 'alice', 'bob', 'viewer');
  const state = getAuthoringState();
  state.apply({
    kind: 'publish_event',
    projectId: project.id,
    type: 'package.revision.created',
    payload: { revisionId: 'rev-member-visible' },
  });

  const memberApp = routeApp(t, 'bob');
  const [session, advance, messages, graph, events] = await Promise.all([
    memberApp.inject({ method: 'GET', url: `/api/projects/${project.id}/session` }),
    memberApp.inject({ method: 'POST', url: `/api/projects/${project.id}/session/advance` }),
    memberApp.inject({ method: 'GET', url: `/api/projects/${project.id}/ai-construction/messages` }),
    memberApp.inject({ method: 'GET', url: `/api/projects/${project.id}/ai-construction` }),
    memberApp.inject({ method: 'GET', url: `/api/projects/${project.id}/events?after=0` }),
  ]);
  assert.equal(session.statusCode, 200, session.body);
  assert.equal(advance.statusCode, 200, advance.body);
  assert.equal(messages.statusCode, 200, messages.body);
  assert.equal(graph.statusCode, 200, graph.body);
  assert.equal(events.statusCode, 200, events.body);
  assert.equal(events.json().events[0].payload.revisionId, 'rev-member-visible');

  const outsiderApp = routeApp(t, 'mallory');
  const outsider = await outsiderApp.inject({
    method: 'GET', url: `/api/projects/${project.id}/events?after=0`,
  });
  assert.equal(outsider.statusCode, 404);
});

test('阶段回退接口先预览影响并恢复知识包后重开目标阶段', async (t) => {
  const project = createProject({ owner: 'alice', kind: 'package', name: '阶段回退接口' });
  const packages = getPackageArtifactStore();
  const initialized = await packages.initializeDraftPackage(project.id, {
    id: 'package-rollback-api', name: '阶段回退接口', description: '回退接口测试',
  });
  const graphModule = await import('../src/ai-construction/task-graph.js');
  const graph = new graphModule.AiConstructionTaskGraph(getDb());
  let snapshot = graph.snapshot(project.id)!;
  const materialsTask = snapshot.tasks[0];
  snapshot = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: materialsTask.id, outputRevisionOid: initialized.revision.oid,
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'materials_ready', packageRevisionOid: initialized.revision.oid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  snapshot = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: snapshot.revision,
    task: {
      kind: 'modeling_brief', milestoneId: 'goal_confirmed', title: '确认建模目标',
      goal: '确认目标', required: true, inputRevisionOid: initialized.revision.oid,
      dependsOn: [materialsTask.id],
    },
  });
  const goalTask = snapshot.tasks.at(-1)!;
  snapshot = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: goalTask.id, outputRevisionOid: initialized.revision.oid,
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: initialized.revision.oid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  snapshot = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: snapshot.revision,
    task: {
      kind: 'process_interview', milestoneId: 'process_confirmed', title: '业务流程访谈',
      goal: '确认核心流程', required: true, inputRevisionOid: initialized.revision.oid,
      dependsOn: [goalTask.id],
    },
  });
  const processTask = snapshot.tasks.at(-1)!;
  snapshot = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: processTask.id, outputRevisionOid: initialized.revision.oid,
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'process_confirmed', packageRevisionOid: initialized.revision.oid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  await packages.saveFiles(project.id, {
    expectedRevision: initialized.revision.oid,
    files: { 'package/notes/after-process.md': '# 后续阶段产物\n' },
    action: {
      cause: 'test_downstream_output', summary: '生成后续阶段产物',
      actorKind: 'agent', actorName: 'test-agent',
    },
  });

  const app = routeApp(t);
  const previewResponse = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/ai-construction/milestones/process_confirmed/rollback-preview`,
  });
  assert.equal(previewResponse.statusCode, 200, previewResponse.body);
  const preview = previewResponse.json().preview;
  assert.equal(preview.allowed, true);
  assert.match(preview.preservedOutputs.join('、'), /资料分析结论/);

  const beforePackage = await packages.current(project.id);
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/milestones/process_confirmed/rollback`,
    payload: {
      expectedGraphRevision: snapshot.revision,
      expectedPackageRevision: beforePackage!.revision.oid,
      requestId: 'rollback-process-api',
    },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().aiConstruction.progress.currentMilestoneId, 'process_confirmed');
  assert.equal(response.json().aiConstruction.tasks.find((task: any) => (
    task.id === processTask.id
  )).status, 'waiting_user');
  assert.equal(response.json().rollback.reason, '用户选择重新处理该阶段');
  assert.notEqual(response.json().rollback.restoredPackageRevisionOid, beforePackage!.revision.oid);
  const continuation = getDb().prepare(`SELECT message_key, seq, blocks_json
    FROM ai_construction_messages WHERE project_id = ?
      AND message_key = ?`).get(
    project.id,
    `milestone.rollback.resume.${response.json().rollback.id}`,
  ) as { message_key: string; seq: number; blocks_json: string } | undefined;
  assert.ok(continuation, '回退后必须追加当前阶段的可见继续卡');
  assert.equal(response.json().message.key, continuation.message_key);
  assert.equal(response.json().interaction?.type, 'question_batch');
  assert.equal(
    response.json().interaction?.baseRevision,
    response.json().rollback.restoredPackageRevisionOid,
    '回退后新待办必须绑定恢复修订，不能继续绑定历史确认修订',
  );
  const latestMessage = getDb().prepare(`SELECT message_key FROM ai_construction_messages
    WHERE project_id = ? ORDER BY seq DESC LIMIT 1`).get(project.id) as { message_key: string };
  assert.equal(latestMessage.message_key, continuation.message_key);

  // 兼容修复部署前已经生成的待办：它仍绑定历史确认修订，但该修订与当前恢复修订
  // 属于同一条已完成回退记录，选择候选时不能被当作真正的知识包并发修改拦截。
  getDb().prepare(`UPDATE authoring_interactions SET base_revision = ? WHERE id = ?`).run(
    initialized.revision.oid,
    response.json().interaction.id,
  );
  const legacySelection = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${response.json().interaction.id}/respond`,
    payload: {
      requestId: 'rollback-process-legacy-selection',
      response: { answers: [
        { questionId: 'process_title', other: '授信审批' },
        { questionId: 'trigger', other: '客户提交申请' },
        { questionId: 'actors', other: '客户经理；审批人' },
        { questionId: 'outputs', other: '审批结论' },
      ] },
    },
  });
  assert.equal(legacySelection.statusCode, 200, legacySelection.body);
  assert.equal(legacySelection.json().nextInteraction?.payload?.stage, 'details');

  // 模拟项目在旧版本完成回退：只有回退记录和任务图，没有本次回退的继续卡。
  // 重新进入工作台时必须幂等补卡，线上存量项目才能在部署后直接恢复。
  getDb().prepare(`DELETE FROM ai_construction_messages
    WHERE project_id = ? AND message_key = ?`).run(project.id, continuation.message_key);
  const reconciled = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/session/advance`,
  });
  assert.equal(reconciled.statusCode, 200, reconciled.body);
  assert.ok(reconciled.json().aiConstructionMessages.some((message: any) => (
    message.key === continuation.message_key
  )), '进入工作台时应补出当前阶段继续卡');
  assert.ok(reconciled.json().authoring.pendingInteractions.some((interaction: any) => (
    interaction.type === 'question_batch'
      && interaction.payload?.purpose === 'process_interview'
  )), '流程阶段应同时恢复可操作的访谈待办卡');

  const replay = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/milestones/process_confirmed/rollback`,
    payload: {
      expectedGraphRevision: snapshot.revision,
      expectedPackageRevision: beforePackage!.revision.oid,
      requestId: 'rollback-process-api',
    },
  });
  assert.equal(replay.statusCode, 200, replay.body);
  assert.equal(replay.json().rollback.id, response.json().rollback.id);
  const continuationCount = getDb().prepare(`SELECT COUNT(*) AS count
    FROM ai_construction_messages WHERE project_id = ? AND message_key = ?`).get(
    project.id,
    continuation.message_key,
  ) as { count: number };
  assert.equal(continuationCount.count, 1, '相同回退请求重放不能重复创建继续卡');

  const goalPreviewResponse = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/ai-construction/milestones/goal_confirmed/rollback-preview`,
  });
  assert.equal(goalPreviewResponse.statusCode, 200, goalPreviewResponse.body);
  const goalPreview = goalPreviewResponse.json().preview;
  assert.equal(goalPreview.allowed, true);
  assert.equal(goalPreview.restoreRevisionOid, initialized.revision.oid,
    '首个建模目标阶段必须恢复其确认修订，不能退到没有知识包的项目创建提交');

  const currentAfterFirstRollback = await packages.current(project.id);
  const goalRollback = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/milestones/goal_confirmed/rollback`,
    payload: {
      expectedGraphRevision: replay.json().aiConstruction.revision,
      expectedPackageRevision: currentAfterFirstRollback!.revision.oid,
      requestId: 'rollback-goal-after-process',
      reason: '重新确认建模目标',
    },
  });
  assert.equal(goalRollback.statusCode, 200, goalRollback.body);
  assert.equal(goalRollback.json().aiConstruction.progress.currentMilestoneId, 'goal_confirmed');
  assert.equal(goalRollback.json().aiConstruction.tasks.find((task: any) => (
    task.id === goalTask.id
  )).status, 'waiting_user');
});

test('新项目首阶段回退使用目标确认修订并可修复旧预览留下的 prepared 请求', async (t) => {
  const project = createProject({ owner: 'alice', name: '首阶段连续回退' });
  const packages = getPackageArtifactStore();
  const initialized = await packages.initializeDraftPackage(project.id, {
    id: 'package-first-milestone', name: '首阶段回退', description: '首阶段回退测试',
  });
  const graphModule = await import('../src/ai-construction/task-graph.js');
  const graph = new graphModule.AiConstructionTaskGraph(getDb());
  let snapshot = graph.snapshot(project.id)!;
  const goalTask = snapshot.tasks[0];
  snapshot = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: goalTask.id, outputRevisionOid: initialized.revision.oid,
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: initialized.revision.oid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  snapshot = graph.apply(project.id, {
    type: 'plan_task', expectedRevision: snapshot.revision,
    task: {
      kind: 'material_analysis', milestoneId: 'materials_ready', title: '围绕目标分析资料',
      goal: '分析资料', required: true, dependsOn: [goalTask.id],
    },
  });

  const app = routeApp(t);
  const previewResponse = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/ai-construction/milestones/goal_confirmed/rollback-preview`,
  });
  assert.equal(previewResponse.statusCode, 200, previewResponse.body);
  const preview = previewResponse.json().preview;
  assert.equal(preview.restoreRevisionOid, initialized.revision.oid);

  const projectCreated = getDb().prepare(`SELECT oid FROM artifact_revisions
    WHERE project_id = ? ORDER BY sequence LIMIT 1`).get(project.id) as { oid: string };
  const stalePreview = { ...preview, restoreRevisionOid: projectCreated.oid };
  getDb().prepare(`INSERT INTO ai_construction_milestone_rollbacks (
    id, project_id, request_id, target_milestone_id, graph_revision_before,
    base_package_revision_oid, current_package_revision_oid, actor_username,
    reason, impact_json, status, created_at
  ) VALUES (?,?,?,?,?,?,?,?,?,?,'prepared',?)`).run(
    'rollback_stale_preview', project.id, 'rollback-goal-stale-preview', 'goal_confirmed',
    snapshot.revision, projectCreated.oid, initialized.revision.oid, 'alice',
    '重新确认建模目标', JSON.stringify(stalePreview), new Date().toISOString(),
  );

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/milestones/goal_confirmed/rollback`,
    payload: {
      expectedGraphRevision: snapshot.revision,
      expectedPackageRevision: initialized.revision.oid,
      requestId: 'rollback-goal-stale-preview',
      reason: '重新确认建模目标',
    },
  });
  assert.equal(response.statusCode, 200, response.body);
  assert.equal(response.json().aiConstruction.progress.currentMilestoneId, 'goal_confirmed');
  assert.equal(response.json().aiConstruction.tasks.find((task: any) => (
    task.id === goalTask.id
  )).status, 'waiting_user');
  const audit = getDb().prepare(`SELECT base_package_revision_oid, status
    FROM ai_construction_milestone_rollbacks WHERE id = 'rollback_stale_preview'`).get() as any;
  assert.equal(audit.base_package_revision_oid, initialized.revision.oid);
  assert.equal(audit.status, 'completed');
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

test('暂缓当前修订必须修复的错误时持久化审计记录并推进编排状态', async (t) => {
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
        files: {
          'diagrams/processes.json': JSON.stringify({
            diagrams: [{ id: 'old-approval', title: '旧授信审批', nodes: [], edges: [] }],
          }),
          'diagrams/process_9.mmd': 'legacy',
          'diagrams/旧授信审批.mmd': 'stale',
          'diagrams/人工补充.mmd': 'manual',
        },
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
  assert.deepEqual(saveInput.deletePaths, [
    'package/diagrams/process_9.mmd',
    'package/diagrams/旧授信审批.mmd',
  ]);
  assert.deepEqual(Object.keys(saveInput.files).sort(), [
    'package/diagrams/index.md',
    'package/diagrams/processes.json',
    'package/diagrams/授信审批.mmd',
  ]);
  assert.deepEqual(JSON.parse(saveInput.files['package/diagrams/processes.json']).diagrams[0].id, 'approval');
  assert.match(saveInput.files['package/diagrams/授信审批.mmd'], /^flowchart TD/m);

  const retry = await app.inject(answer);
  assert.equal(retry.statusCode, 200, retry.body);
  assert.equal(retry.json().replayed, true);
  assert.equal(getDb().prepare(`SELECT status FROM flowchart_review_finalizations
    WHERE interaction_id = ?`).get(pending.interaction.id)?.status, 'completed');
});

test('Mermaid 导入先预览不写入，确认后追加新流程并保留来源信息', async (t) => {
  const project = createProject({ owner: 'operator_a', name: 'Mermaid 导入测试' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认业务流程', baseRevision: 'pkg-mermaid',
  });
  assert.equal(run.kind, 'run');
  if (run.kind !== 'run') throw new Error('未创建流程图任务');
  const pending = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: run.run.id,
    type: 'flowchart_review', baseRevision: 'pkg-mermaid',
    payload: { purpose: 'process_review', taskId: 'task-mermaid', diagrams: [] },
  });
  assert.equal(pending.kind, 'interaction');
  if (pending.kind !== 'interaction') throw new Error('未创建流程图待办');

  let currentRevision = 'pkg-mermaid';
  let currentFiles: Record<string, string> = {
    'diagrams/processes.json': JSON.stringify({ diagrams: [] }),
  };
  let savedFiles: Record<string, string> | undefined;
  let processConfirmed = false;
  let generatedFlowchart: any;
  let markGenerated: () => void = () => {};
  const generationStarted = new Promise<void>((resolve) => { markGenerated = resolve; });
  const app = routeApp(t, 'operator_a', {
    graph: {
      snapshot: () => ({
        confirmations: processConfirmed
          ? [{ milestoneId: 'process_confirmed', packageRevisionOid: currentRevision }]
          : [],
      }),
      initialize: () => ({ confirmations: [] }),
    },
    packages: {
      async current() {
        return { revision: { oid: currentRevision }, packageVersion: '0.1.0', spec: {}, files: [] };
      },
      async readPackageFiles() {
        return {
          revision: { oid: currentRevision }, spec: {}, coverage: {}, files: currentFiles,
        };
      },
      async saveFiles(_projectId: string, input: any) {
        savedFiles = input.files;
        currentFiles = Object.fromEntries(Object.entries(input.files).map(([path, content]) => [
          path.replace(/^package\//u, ''), content as string,
        ]));
        currentRevision = 'pkg-mermaid-saved';
        return {
          revision: { oid: currentRevision }, packageVersion: '0.1.0', spec: {},
          files: Object.keys(input.files),
        };
      },
    },
    orchestrator: {
      latestMaterialAnalysis: () => null,
      latestConfirmedModelingBrief: () => null,
      completeProcessReview: () => {
        processConfirmed = true;
        return { aiConstruction: { revision: 2 }, message: { id: 'message-mermaid' } };
      },
      failKnowledgeStructure: () => undefined,
    },
    knowledgeRunner: {
      async run(_project: any, _context: any, flowchart: any) {
        generatedFlowchart = flowchart;
        markGenerated();
        return { revisionId: 'pkg-knowledge', version: '0.1.1' };
      },
    },
  });
  const base = `/api/projects/${project.id}/interactions/${pending.interaction.id}`;
  const initial = await app.inject({ method: 'GET', url: `${base}/flowchart-draft` });
  assert.equal(initial.statusCode, 200, initial.body);
  assert.equal(initial.json().draftRevision, 1);

  const preview = await app.inject({
    method: 'POST', url: `${base}/flowchart-draft/mermaid-import/preview`,
    payload: {
      expectedRevision: 1,
      title: '订单处理流程',
      filename: 'order-flow.mmd',
      source: 'flowchart TD\n  start([收到订单]) --> finish([完成处理])',
    },
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.equal(preview.json().summary.nodeCount, 2);
  assert.equal(preview.json().diagram.title, '订单处理流程');
  assert.equal((await app.inject({ method: 'GET', url: `${base}/flowchart-draft` })).json().draftRevision, 1);

  const imported = await app.inject({
    method: 'POST', url: `${base}/flowchart-draft/diagrams`,
    payload: {
      expectedRevision: 1,
      mode: 'mermaid',
      title: '订单处理流程',
      filename: 'order-flow.mmd',
      source: 'flowchart TD\n  start([收到订单]) --> finish([完成处理])',
    },
  });
  assert.equal(imported.statusCode, 200, imported.body);
  assert.equal(imported.json().draftRevision, 2);
  assert.equal(imported.json().document.diagrams.length, 1);
  assert.deepEqual(imported.json().document.diagrams[0].importSource, {
    kind: 'mermaid', diagramType: 'flowchart', filename: 'order-flow.mmd',
  });

  const rejected = await app.inject({
    method: 'POST', url: `${base}/flowchart-draft/mermaid-import/preview`,
    payload: { expectedRevision: 2, title: '坏流程', source: 'sequenceDiagram\n  A->>B: hi' },
  });
  assert.equal(rejected.statusCode, 400, rejected.body);
  assert.equal(rejected.json().code, 'MERMAID_DIAGRAM_TYPE_UNSUPPORTED');
  assert.equal(rejected.json().diagnostic.category, '图表类型不支持');
  assert.equal(rejected.json().diagnostic.line, 1);

  const confirmed = await app.inject({
    method: 'POST', url: `${base}/respond`,
    payload: {
      requestId: 'confirm-mermaid-flow',
      response: { action: 'accept', draftRevision: 2, acknowledgeWarnings: true },
    },
  });
  assert.equal(confirmed.statusCode, 200, confirmed.body);
  assert.match(savedFiles?.['package/diagrams/processes.json'] || '', /订单处理流程/u);
  assert.match(savedFiles?.['package/diagrams/订单处理流程.mmd'] || '', /^flowchart TD/mu);
  assert.match(savedFiles?.['package/diagrams/processes.json'] || '', /"kind": "mermaid"/u);

  const generation = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/generate`, payload: {},
  });
  assert.equal(generation.statusCode, 202, generation.body);
  await generationStarted;
  assert.equal(generatedFlowchart.diagrams[0].title, '订单处理流程');
  assert.deepEqual(generatedFlowchart.diagrams[0].importSource, {
    kind: 'mermaid', diagramType: 'flowchart', filename: 'order-flow.mmd',
  });
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
  // 脚本生成改到后台跑，测试要等作业真正收尾；waitForScriptApproval 是链路最后一步
  let markScriptPlanReady: () => void;
  const scriptPlanReady = new Promise<void>((resolve) => { markScriptPlanReady = resolve; });
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
    waitForScriptApproval: () => {
      markScriptPlanReady();
      return { aiConstruction: { revision: 10 }, message: { id: 'script-waiting' } };
    },
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

  const invalidSelection = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${decision.interaction.id}/respond`,
    payload: {
      requestId: 'script-decision-invalid',
      response: {
        answers: [{
          questionId: 'script_candidates', selected: ['CalculateScore', '__none__'],
        }],
      },
    },
  });
  assert.equal(invalidSelection.statusCode, 400, invalidSelection.body);
  assert.equal(invalidSelection.json().code, 'INVALID_INPUT');
  assert.match(invalidSelection.json().error, /不能同时/);
  assert.equal(
    state.snapshot(project.id).pendingInteractions.some((item) => item.id === decision.interaction.id),
    true,
  );

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
  // 脚本生成挪到后台跑，请求只负责受理：立刻 202 返回，不再等十几分钟
  assert.equal(contracted.statusCode, 202, contracted.body);
  assert.equal(contracted.json().accepted, true);
  // 受理时就把待办标为已回答——否则刷新页面它会原样回来，看着像没提交过
  assert.equal(contracted.json().interaction.status, 'answered');

  // 后台作业跑完才有方案：等它收尾（走到 waitForScriptApproval 说明整条链都成功了）
  await scriptPlanReady;
  assert.match(plannedRequirement, /CalculateScore/);
  assert.match(plannedRequirement, /重复输入结果一致/);
  assert.equal(plannedOptions.purpose, 'script_generation');
  assert.equal(plannedOptions.metadata.contractInteractionId, contractInteraction.id);
});

test('脚本变更方案支持沿用原契约直接调整', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本方案原位调整' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '生成脚本方案', baseRevision: 'pkg-script-revise',
  });
  if (started.kind !== 'run') throw new Error('未创建脚本方案运行');
  const payload = {
    purpose: 'script_generation',
    summary: '生成核算脚本',
    message: '原始脚本契约：输入 x，输出 y，测试 x=1 时 y=2',
    history: [],
    scriptTaskId: 'task-script-revise',
    scriptCount: 1,
    contractInteractionId: 'contract-script-revise',
    risks: ['缺失值不得按 0 处理'],
    changes: [{
      id: 'script-change', targetKind: 'script', targetKey: 'calculate_y', action: 'add',
      description: '生成确定性核算脚本',
    }],
  };
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'change_plan', baseRevision: 'pkg-script-revise', payload,
  });
  if (opened.kind !== 'interaction') throw new Error('未创建脚本方案待办');

  let plannedMessage = '';
  let plannedOptions: any;
  let startedCount = 0;
  let markReady: () => void;
  const ready = new Promise<void>((resolve) => { markReady = resolve; });
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-script-revise' }, packageVersion: '1.0.0-rc.1', spec: { objects: [], operations: [] } };
      },
    },
    orchestrator: {
      startScriptGeneration: () => {
        startedCount += 1;
        return { aiConstruction: { revision: 2 } };
      },
      waitForScriptApproval: () => {
        markReady();
        return { aiConstruction: { revision: 3 } };
      },
    },
    packageChatPlan: async (_project: any, _history: any[], message: string, options: any) => {
      plannedMessage = message;
      plannedOptions = options;
      return {
        plan: {
          summary: '调整后的核算脚本',
          changes: [{ targetKind: 'script', targetKey: 'calculate_y', action: 'add', description: '按意见修订' }],
        },
        interaction: { id: 'script-plan-revised', runId: 'run-script-plan-revised', baseRevision: 'pkg-script-revise' },
      };
    },
  });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'script-plan-revise',
      response: { action: 'revise', note: '缺失输入时返回 null，不得按 0 计算' },
    },
  });
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().accepted, true);
  assert.equal(response.json().run.status, 'running');
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'answered');

  await ready;
  assert.equal(startedCount, 1);
  assert.match(plannedMessage, /原始脚本契约：输入 x，输出 y/);
  assert.match(plannedMessage, /缺失输入时返回 null，不得按 0 计算/);
  assert.match(plannedMessage, /完整保留未被意见否定的脚本契约/);
  assert.equal(plannedOptions.purpose, 'script_generation');
  assert.equal(plannedOptions.intent, '调整确定性脚本方案');
  assert.equal(plannedOptions.interactionTitle, '确认调整后的确定性脚本方案');
  assert.equal(plannedOptions.metadata.scriptTaskId, 'task-script-revise');
  assert.equal(plannedOptions.metadata.contractInteractionId, 'contract-script-revise');
  assert.equal(plannedOptions.metadata.previousInteractionId, opened.interaction.id);
});

test('脚本方案调整失败后恢复原方案并保持可继续调整', async (t) => {
  const project = createProject({ owner: 'alice', name: '脚本方案调整失败恢复' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '生成脚本方案', baseRevision: 'pkg-script-revive',
  });
  if (started.kind !== 'run') throw new Error('未创建脚本方案运行');
  const payload = {
    purpose: 'script_generation', summary: '生成脚本', message: '原始契约', history: [],
    changes: [{ id: 'c1', targetKind: 'script', targetKey: 'calculate', action: 'add', description: '生成脚本' }],
  };
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'change_plan', baseRevision: 'pkg-script-revive', payload,
  });
  if (opened.kind !== 'interaction') throw new Error('未创建脚本方案待办');

  let waitingCount = 0;
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-script-revive' }, packageVersion: '1.0.0-rc.1', spec: { objects: [], operations: [] } };
      },
    },
    orchestrator: {
      startScriptGeneration: () => ({ aiConstruction: { revision: 2 } }),
      waitForScriptApproval: () => {
        waitingCount += 1;
        return { aiConstruction: { revision: 3 } };
      },
    },
    packageChatPlan: async () => { throw new Error('模型暂时不可用'); },
  });

  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'script-plan-revise-fails',
      response: { action: 'revise', note: '修正空值契约' },
    },
  });
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'answered');

  const deadline = Date.now() + 5_000;
  let revived: any;
  for (;;) {
    revived = state.snapshot(project.id).pendingInteractions.find((item) => (
      item.type === 'change_plan' && item.id !== opened.interaction.id
    ));
    if (revived && waitingCount > 0) break;
    if (Date.now() > deadline) throw new Error('调整失败后没有恢复可修改的脚本方案');
    await new Promise((resolve) => { setTimeout(resolve, 20); });
  }
  assert.equal(revived.payload.purpose, 'script_generation');
  assert.equal(revived.payload.changes[0].targetKey, 'calculate');
  assert.equal(waitingCount, 1);
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
  assert.match(validationDecision.feedback, /必须修复的问题/);
  assert.match(failedReason, /候选未通过/);
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'pending');
});

test('最终审阅阶段放弃建模调整后追加一次性完成确认消息', async (t) => {
  const project = createProject({ owner: 'alice', name: '放弃调整后完成确认' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '修改知识包', baseRevision: 'pkg-completion-base',
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建知识包修改任务');
  const opened = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: started.run.id,
    type: 'change_plan',
    baseRevision: 'pkg-completion-base',
    payload: {
      purpose: 'package_chat', summary: '调整知识包', message: '调整知识包', history: [],
      changes: [{
        id: 'change-1', targetKind: 'object', targetKey: 'customer', action: 'modify',
        description: '调整客户对象',
      }],
    },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建方案待办');

  const graph = {
    snapshot: () => ({
      revision: 33,
      status: 'active',
      milestones: [{ id: 'construction_completed', status: 'active' }],
      tasks: [{
        id: 'completion-review', kind: 'completion_review',
        milestoneId: 'construction_completed', status: 'completed',
      }],
    }),
  };
  const app = routeApp(t, 'alice', { graph });
  const payload = {
    requestId: 'reject-package-plan-at-completion',
    response: { action: 'reject' },
  };

  const rejected = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload,
  });

  assert.equal(rejected.statusCode, 200, rejected.body);
  assert.equal(rejected.json().run.status, 'completed');
  const completionMessage = rejected.json().aiConstructionMessages.find((message: any) => (
    message.blocks.some((block: any) => block.type === 'next_actions'
      && block.actions.some((action: any) => action.id === 'confirm_construction'))
  ));
  assert.ok(completionMessage);
  assert.match(completionMessage.blocks[0].title, /待最终确认/u);
  assert.match(JSON.stringify(completionMessage.blocks), /确认并进入场景推演/u);
  assert.equal(state.snapshot(project.id).pendingInteractions.length, 0);

  const replayed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload,
  });
  assert.equal(replayed.statusCode, 200, replayed.body);
  const stored = getDb().prepare(`SELECT COUNT(*) AS count FROM ai_construction_messages
    WHERE project_id = ? AND message_key LIKE 'chat.completion.%'`).get(project.id) as { count: number };
  assert.equal(stored.count, 1);
});

test('知识包候选校验失败后反馈专家并可重新生成待确认方案', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选校验反馈闭环' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '修改知识包', baseRevision: 'pkg-feedback-base',
  });
  assert.equal(started.kind, 'run');
  if (started.kind !== 'run') throw new Error('未创建知识包修改任务');
  const opened = state.apply({
    kind: 'open_interaction',
    projectId: project.id,
    runId: started.run.id,
    type: 'change_plan',
    baseRevision: 'pkg-feedback-base',
    payload: {
      purpose: 'package_chat',
      summary: '新增跨境业务事件',
      message: '拆分企业成长事件',
      history: [],
      changes: [{
        id: 'cross-border-change', targetKind: 'object', targetKey: 'cross_border_event', action: 'add',
        description: '新增跨境业务事件，主键使用客户字段',
      }],
      risks: [],
    },
  });
  assert.equal(opened.kind, 'interaction');
  if (opened.kind !== 'interaction') throw new Error('未创建方案待办');

  let revisionRequirement = '';
  let revisedInteractionId = '';
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return {
          revision: { oid: 'pkg-feedback-base' },
          packageVersion: '1.0.0-rc.1',
          spec: { objects: [], operations: [], rules: [], metrics: [], scripts: [] },
        };
      },
    },
    packageChatApply: async () => {
      throw new PackageCandidateRejectedError(
        '候选修订生成失败',
        '对象「跨境业务事件」主键「cust_id」不属于对象字段',
      );
    },
    packageChatPlan: async (_project: any, _history: any[], requirement: string) => {
      revisionRequirement = requirement;
      const nextRun = state.apply({
        kind: 'start_run', projectId: project.id, intent: '调整知识包修改方案', baseRevision: 'pkg-feedback-base',
      });
      if (nextRun.kind !== 'run') throw new Error('未创建方案调整任务');
      const nextInteraction = state.apply({
        kind: 'open_interaction',
        projectId: project.id,
        runId: nextRun.run.id,
        type: 'change_plan',
        baseRevision: 'pkg-feedback-base',
        payload: {
          purpose: 'package_chat', summary: '修正跨境业务事件主键', message: requirement, history: [],
          changes: [{
            id: 'revised-cross-border-change', targetKind: 'object', targetKey: 'cross_border_event', action: 'add',
            description: '主键改为引用 fields[].key 中的 cross_bor_cust_id',
          }],
        },
      });
      if (nextInteraction.kind !== 'interaction') throw new Error('未创建调整后方案待办');
      revisedInteractionId = nextInteraction.interaction.id;
      return {
        plan: {
          summary: '修正跨境业务事件主键',
          changes: [{
            targetKind: 'object' as const, targetKey: 'cross_border_event', action: 'add' as const,
            description: '主键改为引用 fields[].key 中的 cross_bor_cust_id',
          }],
        },
        interaction: {
          id: nextInteraction.interaction.id,
          runId: nextRun.run.id,
          baseRevision: 'pkg-feedback-base',
        },
      };
    },
  });

  const failed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'approve-invalid-package-candidate',
      response: { action: 'approve', selectedChangeIds: ['cross-border-change'] },
    },
  });
  assert.equal(failed.statusCode, 422, failed.body);
  assert.equal(failed.json().code, 'PACKAGE_CANDIDATE_REJECTED');
  assert.match(failed.json().feedback, /cust_id.*不属于对象字段/u);
  assert.match(failed.json().aiConstructionMessages[0].blocks[0].text, /当前知识包未更改/u);
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'pending');
  assert.equal(state.snapshot(project.id).activeRuns.find((run) => run.id === started.run.id)?.status, 'waiting');

  const revised = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'revise-invalid-package-plan',
      response: { action: 'revise', note: '主键必须使用对象字段 key，不要使用原始列名' },
    },
  });
  // 方案调整改到后台跑，请求只负责受理
  assert.equal(revised.statusCode, 202, revised.body);
  assert.equal(revised.json().accepted, true);
  // 受理时就标记已回答：否则刷新页面待办原样回来，看着像没提交过
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'answered');

  // 等后台把新方案生成出来
  const deadline = Date.now() + 5_000;
  while (state.interaction(project.id, revisedInteractionId)?.status !== 'pending') {
    if (Date.now() > deadline) throw new Error('调整后的方案待办未在预期时间内出现');
    await new Promise((resolve) => { setTimeout(resolve, 20); });
  }
  assert.match(revisionRequirement, /上次候选校验反馈.*cust_id.*不属于对象字段/su);
  assert.match(revisionRequirement, /专家调整意见.*对象字段 key/su);
});

test('知识生成只读取当前 Git 修订中已确认的流程文档', async (t) => {
  const project = createProject({ owner: 'alice', name: 'Git 流程事实源' });
  // 生成改到后台跑，测试要等作业真正开始才能断言它拿到了哪份流程图
  let markGenerated: () => void = () => {};
  const generationStarted = new Promise<void>((resolve) => { markGenerated = resolve; });
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
        markGenerated();
        return { version: '1.0.0-rc.2', revisionId: 'pkg-knowledge' };
      },
    },
  });

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/generate`, payload: {},
  });
  // 生成要跑十几分钟，请求只负责受理
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().accepted, true);
  await generationStarted;
  assert.equal(generatedFlowchart.diagrams[0].id, 'approval');
});

test('生成门禁决定在原运行失败后通过新运行幂等续跑', async (t) => {
  const project = createProject({ owner: 'alice', name: '生成门禁失败恢复' });
  // 续跑挪到后台，等作业真正开始才能断言它拿到的续跑参数
  let markResumed: () => void = () => {};
  const resumeStarted = new Promise<void>((resolve) => { markResumed = resolve; });
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
        markResumed();
        return { revisionId: 'pkg-resumed', version: '1.0.0-rc.1' };
      },
    },
  });
  const response = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'generation-gate-answer', response: { value: 'skip' } },
  });

  // 门禁续跑的是知识生成，同样挪到后台：请求只负责受理
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().accepted, true);
  await resumeStarted;
  assert.deepEqual(continuation.skipFailedPhases, ['operation_batch_2']);
  assert.notEqual(continuation.resumeRunId, started.run.id);
  const resumedRun = state.snapshot(project.id).activeRuns.find((run) => run.id === continuation.resumeRunId);
  assert.equal(resumedRun?.baseRevision, 'pkg-gate');
});

// ---- 「确认完成 AI 辅助构建」的拦截：每一条都要给出可执行的下一步 ----

function completionApp(t: test.TestContext, projectId: string, overrides: any = {}) {
  const packages = {
    async current() {
      return {
        revision: { oid: 'pkg-draft' },
        packageVersion: '0.1.0',
        spec: { status: 'draft', objects: [], operations: [], ...(overrides.spec ?? {}) },
      };
    },
    async saveCurrentAsPackageVersion() {
      return { revision: { oid: 'pkg-rc-1' }, packageVersion: '1.0.0-rc.1', spec: { status: 'review' } };
    },
  };
  const graph = {
    snapshot: () => ({ revision: 6 }),
    apply: () => ({ revision: 7, status: 'completed' }),
  };
  const orchestrator = {
    latestMaterialAnalysis: () => null,
    latestConfirmedModelingBrief: () => null,
  };
  const options: any = { packages, graph, orchestrator };
  if (overrides.pendingInteractions) {
    const real = getAuthoringState();
    options.state = {
      ...real,
      snapshot: (id: string) => ({
        ...real.snapshot(id),
        pendingInteractions: overrides.pendingInteractions,
      }),
      apply: real.apply.bind(real),
      replay: real.replay.bind(real),
    };
  }
  return routeApp(t, 'alice', options);
}

const confirmCompletion = (app: any, projectId: string) => app.inject({
  method: 'POST',
  url: `/api/projects/${projectId}/ai-construction/commands`,
  payload: {
    command: {
      type: 'confirm_milestone', expectedRevision: 6,
      milestoneId: 'construction_completed', packageRevisionOid: 'pkg-draft',
      actor: { username: 'alice', role: 'validator' },
    },
  },
});

test('未校验时拦截并给出立即校验的动作', async (t) => {
  const project = createProject({ owner: 'alice', name: '拦截-未校验' });
  const response = await confirmCompletion(completionApp(t, project.id), project.id);
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_CHECK_REQUIRED');
  assert.deepEqual(response.json().nextActions.map((item: any) => item.id), ['run_package_check']);
});

test('存在必须修复的错误时拦截并给出修缮与暂缓两条出路', async (t) => {
  const project = createProject({ owner: 'alice', name: '拦截-有错误' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [
    { id: 'f1', source: 'rule', severity: '错误', category: '引用缺失', target: 'objects/o1', message: '缺少引用' } as any,
  ], 'pkg-draft');

  const response = await confirmCompletion(completionApp(t, project.id), project.id);
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PACKAGE_CHECK_FAILED');
  assert.equal(response.json().errorCount, 1);
  // 这两个动作系统里本来就有，之前没有带出来
  assert.deepEqual(response.json().nextActions.map((item: any) => item.id), [
    'repair_knowledge_structure', 'defer_validation_errors', 'view_validation',
  ]);
});

test('只有警告和提示时可以直接完成 AI 辅助建模', async (t) => {
  const project = createProject({ owner: 'alice', name: '完成-非阻断发现' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [
    { id: 'w1', source: 'rule', severity: '警告', category: '命名建议', target: 'objects/o1', message: '建议统一命名' } as any,
    { id: 'h1', source: 'rule', severity: '提示', category: '可读性', target: 'objects/o1', message: '可以补充说明' } as any,
  ], 'pkg-draft');
  const response = await confirmCompletion(completionApp(t, project.id), project.id);
  assert.equal(response.statusCode, 200, response.body);
});

test('缺证据时把明细和文件入口一起返回', async (t) => {
  const project = createProject({ owner: 'alice', name: '拦截-缺证据' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [], 'pkg-draft');

  const app = completionApp(t, project.id, {
    spec: {
      objects: [],
      operations: [{ key: 'plan', name: '编制方案', evidence: [] }],
    },
  });
  const response = await confirmCompletion(app, project.id);
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'KNOWLEDGE_EVIDENCE_REQUIRED');
  assert.deepEqual(response.json().problems, ['operation:plan 缺少流程或访谈证据']);
  assert.deepEqual(response.json().nextActions.map((item: any) => item.path), ['operations/index.md']);
});

test('场景待办拦截时指明去哪里处理', async (t) => {
  const project = createProject({ owner: 'alice', name: '拦截-场景待办' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [], 'pkg-draft');

  const app = completionApp(t, project.id, {
    pendingInteractions: [{
      id: 'sc-1', projectId: project.id, runId: 'ar-1', type: 'scenario_review',
      status: 'pending', payload: { title: '场景复核' }, createdAt: '2026-07-31T00:00:00.000Z',
    }],
  });
  const response = await confirmCompletion(app, project.id);
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'PENDING_INTERACTIONS');
  assert.equal(response.json().pendingInteractions[0].location, 'scenario');
  assert.deepEqual(response.json().nextActions.map((item: any) => item.id), ['open_scenario_review']);
});

/** 助手栏不会渲染修订已变的受控修复待办，后端若仍拦截就成了死路 */
test('修订已变的受控修复待办不再阻塞完成建模', async (t) => {
  const project = createProject({ owner: 'alice', name: '拦截-失效待办' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [], 'pkg-draft');

  const app = completionApp(t, project.id, {
    pendingInteractions: [{
      id: 'stale-1', projectId: project.id, runId: 'ar-1', type: 'change_plan',
      status: 'pending', baseRevision: 'pkg-old',
      payload: { purpose: 'controlled_repair' }, createdAt: '2026-07-31T00:00:00.000Z',
    }],
  });
  const response = await confirmCompletion(app, project.id);
  assert.equal(response.statusCode, 200, response.body);
});

test('脚本生成受理后待办立即变已回答，刷新不会再冒出来', async (t) => {
  const project = createProject({ owner: 'alice', name: '受理即落库' });
  const state = getAuthoringState();
  const run = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认脚本契约', baseRevision: 'pkg-async',
  });
  if (run.kind !== 'run') throw new Error('未创建运行');
  const candidates = [{
    operationKey: 'CalcA', title: '计算 A', confidence: 'high', reason: 'r',
    suggestedContract: '输入：x\n输出：y\n测试：一致',
  }];
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: run.run.id,
    type: 'question_batch', baseRevision: 'pkg-async',
    payload: { purpose: 'script_contract', taskId: 'task-async', candidates, questions: [] },
  });
  if (opened.kind !== 'interaction') throw new Error('未创建待办');

  // 让后台生成一直不返回，模拟「十几分钟还没跑完」的那段时间
  let releasePlan: () => void = () => {};
  const planStalled = new Promise<void>((resolve) => { releasePlan = resolve; });
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-async' }, packageVersion: '1.0.0-rc.1', spec: { objects: [], operations: [] } };
      },
    },
    orchestrator: {
      startScriptGeneration: () => ({ aiConstruction: { revision: 2 } }),
      waitForScriptApproval: () => ({ aiConstruction: { revision: 3 } }),
      failScriptGeneration: () => ({ aiConstruction: { revision: 4 } }),
    },
    packageChatPlan: async () => {
      await planStalled;
      return { interaction: { id: 'plan-async', runId: 'r', baseRevision: 'pkg-async' } };
    },
    jobHeartbeatIntervalMs: 5,
  });

  const accepted = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: {
      requestId: 'async-answer',
      response: { answers: [{ questionId: 'script_contract_1', selected: [candidates[0].suggestedContract] }] },
    },
  });

  // 生成还卡着，但请求已经返回了——这正是异步化要的
  assert.equal(accepted.statusCode, 202, accepted.body);
  // 关键断言：此刻待办已经是 answered。原先要等生成跑完才 respond，
  // 这段时间刷新页面待办会原样回来，用户以为没提交成功，再点就撞租约。
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'answered');
  const stillPending = state.snapshot(project.id).pendingInteractions
    .some((item) => item.id === opened.interaction.id);
  assert.equal(stillPending, false, '受理后不该还挂在待办列表里');
  const running = state.snapshot(project.id).activeRuns.find((item) => item.id === run.run.id);
  assert.equal(running?.state.currentStep, '正在根据已确认契约形成受控变更计划');
  const heartbeatAt = String(running?.state.lastHeartbeatAt ?? '');
  await new Promise((resolve) => setTimeout(resolve, 30));
  const heartbeatAfter = String(state.snapshot(project.id).activeRuns
    .find((item) => item.id === run.run.id)?.state.lastHeartbeatAt ?? '');
  assert.ok(Date.parse(heartbeatAfter) > Date.parse(heartbeatAt), '脚本生成期间应持续刷新运行心跳');

  releasePlan();
});

test('知识生成的前置校验仍然同步报错，不会先受理再在后台失败', async (t) => {
  const project = createProject({ owner: 'alice', name: '生成前置校验' });
  let ran = false;
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-x' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
      async readPackageFiles() { return { files: {} }; },
    },
    knowledgeRunner: { async run() { ran = true; return {}; } },
  });

  // 流程未确认：必须当场 409，而不是回 202 然后在后台悄悄失败
  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/generate`, payload: {},
  });
  assert.notEqual(response.statusCode, 202, '前置校验不该被受理');
  assert.ok(response.statusCode >= 400, response.body);
  assert.equal(ran, false, '校验没过就不该启动后台生成');
});

test('逐项修复只负责受理，不等长任务跑完', async (t) => {
  const project = createProject({ owner: 'alice', name: '修复异步受理' });
  const app = routeApp(t, 'alice');

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/repair`, payload: {},
  });

  // 受控修复要跑 5~30 分钟，请求必须立刻回来；失败由后台写成带出口的助手消息
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().accepted, true);
});

test('逐项修复会关闭未提交的暂缓表单并复用当前修复方案', async (t) => {
  const project = createProject({ owner: 'alice', name: '修复与暂缓互斥' });
  const state = getAuthoringState();
  const open = (purpose: string, findingId: string, type: 'question_batch' | 'change_plan') => {
    const started = state.apply({
      kind: 'start_run', projectId: project.id, intent: purpose, baseRevision: 'pkg-current',
      state: { purpose, findingId },
    });
    if (started.kind !== 'run') throw new Error('运行创建失败');
    const opened = state.apply({
      kind: 'open_interaction', projectId: project.id, runId: started.run.id,
      type, baseRevision: 'pkg-current',
      payload: { purpose, findingId, title: purpose, questions: [] },
    });
    if (opened.kind !== 'interaction') throw new Error('待办创建失败');
    return opened.interaction;
  };
  const ignored1 = open('check_finding_ignore', 'warning-1', 'question_batch');
  const ignored2 = open('check_finding_ignore', 'warning-2', 'question_batch');
  const repair = open('controlled_repair', 'error-1', 'change_plan');
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-current' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
    },
  });

  const response = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/ai-construction/knowledge/repair`, payload: {},
  });
  assert.equal(response.statusCode, 202, response.body);
  assert.equal(response.json().reused, true);
  assert.equal(response.json().interaction.id, repair.id);
  assert.equal(response.json().cancelledIgnoreCount, 2);
  assert.equal(state.interaction(project.id, ignored1.id)?.status, 'cancelled');
  assert.equal(state.interaction(project.id, ignored2.id)?.status, 'cancelled');
  assert.equal(state.interaction(project.id, repair.id)?.status, 'pending');
});

test('方案调整生成失败后重建可确认方案，专家不会卡在没法再改的状态', async (t) => {
  const project = createProject({ owner: 'alice', name: '调整失败仍可重试' });
  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '修改知识包', baseRevision: 'pkg-revise-fail',
  });
  if (started.kind !== 'run') throw new Error('未创建运行');
  const payload = {
    purpose: 'package_chat',
    summary: '新增跨境业务事件',
    message: '拆分企业成长事件',
    history: [],
    risks: [],
    changes: [{
      id: 'c1', targetKind: 'object', targetKey: 'cross_border_event', action: 'add',
      description: '新增跨境业务事件',
    }],
  };
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'change_plan', baseRevision: 'pkg-revise-fail', payload,
  });
  if (opened.kind !== 'interaction') throw new Error('未创建方案待办');

  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-revise-fail' }, packageVersion: '0.1.0', spec: { objects: [], operations: [] } };
      },
    },
    packageChatPlan: async () => { throw new Error('模型调用失败'); },
  });

  const revised = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/interactions/${opened.interaction.id}/respond`,
    payload: { requestId: 'revise-fails', response: { action: 'revise', note: '换个写法' } },
  });
  assert.equal(revised.statusCode, 202, revised.body);
  // respond 已经前移，原待办变成 answered——所以失败后必须还回一个能再改的方案
  assert.equal(state.interaction(project.id, opened.interaction.id)?.status, 'answered');

  const deadline = Date.now() + 5_000;
  let revived: any;
  for (;;) {
    revived = state.snapshot(project.id).pendingInteractions
      .find((item) => item.type === 'change_plan' && item.id !== opened.interaction.id);
    if (revived) break;
    if (Date.now() > deadline) throw new Error('生成失败后没有重建可确认方案，专家将无法继续调整');
    await new Promise((resolve) => { setTimeout(resolve, 20); });
  }
  // 重建的方案要保留原内容，否则等于把专家之前的工作丢了
  assert.equal(revived.payload.summary, '新增跨境业务事件');
  assert.equal(revived.payload.changes[0].targetKey, 'cross_border_event');
});

test('增量补充：建模没走完时拒绝，走完后开一轮且不动已确认阶段', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量补充入口' });
  const db = getDb();
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return { revision: { oid: 'pkg-inc' }, packageVersion: '1.0.0', spec: { objects: [], operations: [] } };
      },
    },
  });
  const url = `/api/projects/${project.id}/ai-construction/increment/start`;

  // 建模没走完就补资料该走原流程：「只改增量」需要一个确认过的完整知识包做基准
  const tooEarly = await app.inject({ method: 'POST', url, payload: {} });
  assert.equal(tooEarly.statusCode, 409, tooEarly.body);
  assert.equal(tooEarly.json().code, 'INCREMENT_REQUIRES_COMPLETED_PACKAGE');
  // 无出口的红字是这个仓库反复出现的问题，新错误码也要有下一步
  assert.ok(tooEarly.json().error);

  // 把项目置为建模完成
  db.prepare(`UPDATE ai_construction_milestones SET status = 'confirmed'
    WHERE project_id = ?`).run(project.id);
  db.prepare(`UPDATE ai_construction_tasks SET status = 'completed' WHERE project_id = ?`).run(project.id);

  const started = await app.inject({ method: 'POST', url, payload: {} });
  assert.equal(started.statusCode, 202, started.body);
  assert.equal(started.json().accepted, true);

  const milestones = db.prepare(`SELECT milestone_id, status FROM ai_construction_milestones
    WHERE project_id = ?`).all(project.id) as any[];
  const increment = milestones.find((item) => item.milestone_id === 'increment_confirmed');
  assert.equal(increment?.status, 'active', '增量阶段应被激活');
  // 关键：已确认的阶段一个都不能被动过，否则又变成「重走一遍」
  for (const item of milestones.filter((row) => row.milestone_id !== 'increment_confirmed')) {
    assert.equal(item.status, 'confirmed', `${item.milestone_id} 不该被改动`);
  }
  const intake = db.prepare(`SELECT COUNT(*) AS n FROM ai_construction_tasks
    WHERE project_id = ? AND task_kind = 'incremental_intake'`).get(project.id) as { n: number };
  assert.equal(intake.n, 1);

  // 上一轮没做完就再点一次，不该攒出第二条吸收任务
  await app.inject({ method: 'POST', url, payload: {} });
  const again = db.prepare(`SELECT COUNT(*) AS n FROM ai_construction_tasks
    WHERE project_id = ? AND task_kind = 'incremental_intake'`).get(project.id) as { n: number };
  assert.equal(again.n, 1);
});

test('生成增量方案时强制全文读取本轮新增文本资料', async (t) => {
  const project = createProject({ owner: 'alice', name: '增量全文读取入口' });
  const db = getDb();
  let plannedOptions: any;
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return {
          revision: { oid: 'pkg-increment-full-read' },
          packageVersion: '1.0.0',
          spec: { objects: [], operations: [], rules: [], metrics: [], scripts: [] },
        };
      },
    },
    packageChatPlan: async (_project: any, _history: any[], _requirement: string, options: any) => {
      plannedOptions = options;
      return {
        plan: { summary: '增量方案', changes: [], risks: [] },
        interaction: {
          id: 'increment-full-read-plan',
          runId: 'increment-full-read-run',
          baseRevision: 'pkg-increment-full-read',
        },
      };
    },
  });
  const startUrl = `/api/projects/${project.id}/ai-construction/increment/start`;

  // 首次调用初始化任务图；随后模拟既有建模已全部确认。
  const beforeCompletion = await app.inject({ method: 'POST', url: startUrl, payload: {} });
  assert.equal(beforeCompletion.statusCode, 409, beforeCompletion.body);
  db.prepare(`UPDATE ai_construction_milestones SET status = 'confirmed'
    WHERE project_id = ?`).run(project.id);
  db.prepare(`UPDATE ai_construction_tasks SET status = 'completed'
    WHERE project_id = ?`).run(project.id);
  const started = await app.inject({ method: 'POST', url: startUrl, payload: {} });
  assert.equal(started.statusCode, 202, started.body);

  const { insertDocument } = await import('../src/documents/repo.js');
  const document = insertDocument({
    projectId: project.id,
    filename: '新增规则.md',
    kind: 'markdown',
    parseStatus: 'parsed',
    textContent: '本轮新增规则正文。',
  });
  // 避免测试运行过快时资料和增量任务落在同一毫秒，确保属于本轮新增资料。
  db.prepare('UPDATE documents SET created_at = ? WHERE id = ?')
    .run('2099-01-01T00:00:00.000Z', document.id);

  const proposed = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/ai-construction/increment/propose`,
    payload: {},
  });
  assert.equal(proposed.statusCode, 202, proposed.body);

  const deadline = Date.now() + 2_000;
  while (!plannedOptions && Date.now() <= deadline) {
    await new Promise((resolve) => { setTimeout(resolve, 10); });
  }
  assert.ok(plannedOptions, '增量方案后台作业应调用 packageChatPlan');
  assert.equal(plannedOptions.documentAccess, 'full');
  assert.deepEqual(plannedOptions.documents, [{
    filename: '新增规则.md', textContent: '本轮新增规则正文。',
  }]);
});

// ---- 体检发现交给建模助手处理 ----

function findingApp(t: test.TestContext) {
  return routeApp(t, 'alice', {
    packages: {
      async current() {
        return {
          revision: { oid: 'pkg-draft' },
          packageVersion: '0.1.0',
          spec: { status: 'draft', objects: [], operations: [] },
        };
      },
    } as any,
  });
}

const handOff = (app: any, projectId: string, mode: 'review' | 'ignore', findingId: string) => app.inject({
  method: 'POST',
  url: `/api/projects/${projectId}/package/check/findings/${mode}`,
  payload: { findingId },
});

test('「去修改」在助手里落一张卡片，并指向这条发现对应的文件', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-修改' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'f1', source: 'rule', severity: '错误', category: '引用缺失',
    target: 'objects/index.md', message: '缺少引用', suggestion: '补上来源引用',
  } as any], 'pkg-draft');

  const app = findingApp(t);
  const response = await handOff(app, project.id, 'review', 'f1');
  assert.equal(response.statusCode, 200, response.body);
  const blocks = response.json().message.blocks;
  const actions = blocks.find((block: any) => block.type === 'next_actions').actions;
  assert.deepEqual(actions.map((item: any) => item.id), [
    'repair_knowledge_structure', 'open_package_file',
  ]);
  // target 是包内路径时才给「打开文件」，路径要跟着这条发现走
  assert.equal(actions[1].path, 'objects/index.md');
  assert.match(blocks.find((block: any) => block.type === 'text').text, /补上来源引用/);

  const reopened = await handOff(app, project.id, 'review', 'f1');
  assert.equal(reopened.statusCode, 200, reopened.body);
  assert.notEqual(reopened.json().message.id, response.json().message.id);
  assert.ok(reopened.json().message.seq > response.json().message.seq);
});

test('target 是语义目标时也把目标交给前端解析到具体文件', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-非路径' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'f1', source: 'rule', severity: '错误', category: '定义冲突',
    target: 'ipo_detail_info', message: '对象定义冲突',
  } as any], 'pkg-draft');

  const response = await handOff(findingApp(t), project.id, 'review', 'f1');
  assert.equal(response.statusCode, 200, response.body);
  const actions = response.json().message.blocks
    .find((block: any) => block.type === 'next_actions').actions;
  assert.deepEqual(actions.map((item: any) => item.id), [
    'repair_knowledge_structure', 'open_package_file',
  ]);
  assert.equal(actions[1].path, 'ipo_detail_info');
});

test('AI 辅助建模中的警告不再创建忽略待办', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-忽略警告' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'w1', source: 'rule', severity: '警告', category: '命名建议',
    target: 'objects/index.md', message: '命名不一致',
  } as any], 'pkg-draft');

  const app = findingApp(t);
  const response = await handOff(app, project.id, 'ignore', 'w1');
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'CHECK_FINDING_NON_BLOCKING');
  assert.equal(getAuthoringState().snapshot(project.id).pendingInteractions.length, 0);
});

test('警告不再支持旧的放行待办流程', async (t) => {
  const project = createProject({ owner: 'alice', name: '告警放行修订冲突恢复' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  const finding = {
    id: 'w-stale', source: 'rule', severity: '警告', category: '命名建议',
    target: 'objects/index.md', message: '命名不一致',
  } as any;
  let currentRevision = 'pkg-before';
  saveCheck(project.id, '0.1.0', [finding], currentRevision);
  const app = routeApp(t, 'alice', {
    packages: {
      async current() {
        return {
          revision: { oid: currentRevision },
          packageVersion: '0.1.0',
          spec: { status: 'draft', objects: [], operations: [] },
        };
      },
    } as any,
  });

  const response = await handOff(app, project.id, 'ignore', finding.id);
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'CHECK_FINDING_NON_BLOCKING');
  assert.equal(getAuthoringState().snapshot(project.id).pendingInteractions.length, 0);
});

test('读取项目会话会自动取消旧修订的告警放行待办', async (t) => {
  const project = createProject({ owner: 'alice', name: '告警放行会话自动对账' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  const finding = {
    id: 'w-session-stale', source: 'rule', severity: '警告', category: '命名建议',
    target: 'objects/index.md', message: '命名不一致',
  } as any;
  let currentRevision = 'pkg-session-before';
  saveCheck(project.id, '0.1.0', [finding], currentRevision);
  const packages = {
    async ensureProject() {},
    async current() {
      return {
        revision: { oid: currentRevision },
        packageVersion: '0.1.0',
        spec: { status: 'draft', objects: [], operations: [] },
      };
    },
    async countPendingCandidateRevisions() { return 0; },
  };
  const app = routeApp(t, 'alice', { packages });

  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '旧告警放行',
    baseRevision: 'pkg-session-before', state: { purpose: 'check_finding_ignore' },
  });
  if (started.kind !== 'run') throw new Error('运行创建失败');
  const opened = state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'question_batch', baseRevision: 'pkg-session-before',
    payload: { purpose: 'check_finding_ignore', scope: 'warning', findingId: finding.id, title: '旧告警' },
  });
  if (opened.kind !== 'interaction') throw new Error('待办创建失败');
  const staleInteraction = opened.interaction;

  currentRevision = 'pkg-session-after';
  saveCheck(project.id, '0.1.1', [], currentRevision);
  const refreshed = await app.inject({
    method: 'GET', url: `/api/projects/${project.id}/session`,
  });
  assert.equal(refreshed.statusCode, 200, refreshed.body);
  assert.equal(
    refreshed.json().authoring.pendingInteractions
      .some((item: any) => item.id === staleInteraction.id),
    false,
  );
  assert.equal(
    refreshed.json().authoring.activeRuns
      .some((item: any) => item.id === staleInteraction.runId),
    false,
  );
  assert.equal(getAuthoringState().interaction(project.id, staleInteraction.id)?.status, 'cancelled');
  const run = getDb().prepare('SELECT status, result_json FROM authoring_runs WHERE id = ?')
    .get(staleInteraction.runId) as { status: string; result_json: string };
  assert.equal(run.status, 'cancelled');
  assert.equal(JSON.parse(run.result_json).disposition, 'archived_non_blocking_finding');
});

test('忽略错误时说清会整批暂缓，不让人以为只忽略了点开的那一条', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-忽略错误' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [
    { id: 'e1', source: 'rule', severity: '错误', category: '引用缺失', target: 'objects/index.md', message: '缺引用' },
    { id: 'e2', source: 'rule', severity: '错误', category: '映射缺失', target: 'mapping/index.md', message: '缺映射' },
  ] as any, 'pkg-draft');

  const opened = await handOff(findingApp(t), project.id, 'ignore', 'e1');
  assert.equal(opened.statusCode, 200, opened.body);
  const payload = opened.json().interaction.payload;
  assert.equal(payload.scope, 'errors');
  assert.equal(payload.errorCount, 2);
  assert.match(String(payload.description), /全部 2 个必须修复的错误/);
});

test('警告不再创建理由待办，因此不存在警告理由校验阻塞', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-理由不足' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'w1', source: 'rule', severity: '警告', category: '命名建议',
    target: 'objects/index.md', message: '命名不一致',
  } as any], 'pkg-draft');

  const app = findingApp(t);
  const response = await handOff(app, project.id, 'ignore', 'w1');
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'CHECK_FINDING_NON_BLOCKING');
});

test('提示类发现不给 AI 辅助建模忽略通道', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-提示' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'h1', source: 'rule', severity: '提示', category: '可读性',
    target: 'objects/index.md', message: '可以补充说明',
  } as any], 'pkg-draft');

  const response = await handOff(findingApp(t), project.id, 'ignore', 'h1');
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'CHECK_FINDING_NON_BLOCKING');
});

test('开场先说「已就绪」再进入建模目标确认，而不是倒着来', async (t) => {
  const project = createProject({ owner: 'alice', name: '开场顺序' });
  const app = routeApp(t);

  const advanced = await app.inject({
    method: 'POST', url: `/api/projects/${project.id}/session/advance`,
  });
  assert.equal(advanced.statusCode, 200, advanced.body);

  // 助手栏按 seq 排；欢迎语写在推进之后会让它反而排到「进入建模目标确认」下面
  const titles = advanced.json().aiConstructionMessages
    .map((message: any) => message.blocks.find((block: any) => block.type === 'status')?.title)
    .filter(Boolean);
  const welcome = titles.indexOf('AI 辅助构建已就绪');
  const goal = titles.indexOf('进入建模目标确认');
  assert.notEqual(welcome, -1, `没有找到欢迎语：${JSON.stringify(titles)}`);
  assert.notEqual(goal, -1, `没有找到建模目标卡片：${JSON.stringify(titles)}`);
  assert.ok(welcome < goal, `欢迎语应排在建模目标确认之前，实际顺序：${JSON.stringify(titles)}`);
});

test('已有待办不改变警告的非阻断语义', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-待办冲突' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [{
    id: 'w1', source: 'rule', severity: '警告', category: '命名建议',
    target: 'objects/index.md', message: '命名不一致',
  } as any], 'pkg-draft');

  const state = getAuthoringState();
  const started = state.apply({
    kind: 'start_run', projectId: project.id, intent: '确认建模目标', state: { purpose: 'modeling_brief' },
  });
  if (started.kind !== 'run') throw new Error('运行创建失败');
  state.apply({
    kind: 'open_interaction', projectId: project.id, runId: started.run.id,
    type: 'question_batch',
    payload: { purpose: 'modeling_brief', title: '请先确认建模目标', questions: [] },
  });

  const response = await handOff(findingApp(t), project.id, 'ignore', 'w1');
  assert.equal(response.statusCode, 409, response.body);
  assert.equal(response.json().code, 'CHECK_FINDING_NON_BLOCKING');
});

test('多条警告都不会创建暂缓待办', async (t) => {
  const project = createProject({ owner: 'alice', name: '发现交接-暂缓互斥' });
  const { saveCheck } = await import('../src/workflow/package-check.js');
  saveCheck(project.id, '0.1.0', [
    { id: 'w1', source: 'rule', severity: '警告', category: '命名建议', target: 'objects/a.md', message: '命名一' },
    { id: 'w2', source: 'rule', severity: '警告', category: '命名建议', target: 'objects/b.md', message: '命名二' },
  ] as any, 'pkg-draft');
  const app = findingApp(t);

  const first = await handOff(app, project.id, 'ignore', 'w1');
  const second = await handOff(app, project.id, 'ignore', 'w2');
  assert.equal(first.statusCode, 409, first.body);
  assert.equal(second.statusCode, 409, second.body);
  assert.equal(first.json().code, 'CHECK_FINDING_NON_BLOCKING');
  assert.equal(second.json().code, 'CHECK_FINDING_NON_BLOCKING');
  assert.equal(getAuthoringState().snapshot(project.id).pendingInteractions.length, 0);
});
