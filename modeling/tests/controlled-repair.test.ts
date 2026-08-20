import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';

import {
  ArtifactRevisionRepo,
  IsomorphicGitArtifactRepository,
  PackageArtifactStore,
} from '../src/artifacts/index.js';
import {
  buildAutomaticRepairPlan,
  classifyRepairFinding,
  ControlledRepairAttemptStore,
  ControlledRepairRunner,
} from '../src/ai-construction/controlled-repair.js';
import { AiConstructionOrchestrator } from '../src/ai-construction/orchestrator.js';
import { AiConstructionTaskGraph } from '../src/ai-construction/task-graph.js';
import { PiAuthoringAdapter } from '../src/authoring/pi-adapter.js';
import { ControlledRepairController } from '../src/authoring/controlled-repair-controller.js';
import { AuthoringState } from '../src/authoring/state.js';
import type { PackageSpec, Project } from '../src/types.js';
import { runFormatChecks, type CheckFinding } from '../src/workflow/package-check.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { PackageCandidateRejectedError } from '../src/workflow/package-chat.js';

const schema = await readFile(new URL('../src/db/schema.sql', import.meta.url), 'utf8');

const projectRecord = (id: string): Project => ({
  id,
  owner: 'alice',
  name: '受控修复测试',
  domain: '审批',
  background: '',
  stage: 'generate',
  kind: 'modeling',
  createdAt: '2026-07-24T00:00:00.000Z',
  updatedAt: '2026-07-24T00:00:00.000Z',
});

async function fixture(t: test.TestContext) {
  const directory = await mkdtemp(join(tmpdir(), 'controlled-repair-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const db = new DatabaseSync(':memory:');
  t.after(() => db.close());
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(schema);
  const project = projectRecord(`pj_${Math.random().toString(16).slice(2, 14)}`);
  db.prepare(`INSERT INTO projects (
    id, owner, kind, name, domain, background, stage, created_at, updated_at
  ) VALUES (?,?,?,?,?,?,?,?,?)`).run(
    project.id,
    project.owner,
    project.kind,
    project.name,
    project.domain,
    project.background,
    project.stage,
    project.createdAt,
    project.updatedAt,
  );
  let tick = 0;
  const repository = new IsomorphicGitArtifactRepository({
    rootDir: join(directory, 'projects'),
    now: () => new Date(Date.UTC(2026, 6, 24, 0, 0, tick++)),
  });
  const store = new PackageArtifactStore({
    db,
    repository,
    revisions: new ArtifactRevisionRepo(db),
  });
  const initialRevision = await store.ensureProject(project.id);
  const initialSpec: PackageSpec = {
    id: 'controlled-repair',
    name: '受控修复测试',
    description: '用于验证分级校验与受控修复',
    version: '1.0.0',
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
  const initialPackage = await store.savePackageVersion(project.id, {
    expectedRevision: initialRevision.oid,
    spec: initialSpec,
    coverage: { covered: [], uncovered: [] },
    action: {
      cause: 'test_setup', summary: '建立受控修复测试知识包', actorKind: 'system', actorName: '测试',
    },
  });
  const graph = new AiConstructionTaskGraph(db);
  let snapshot = graph.initialize(project.id);
  snapshot = graph.apply(project.id, {
    type: 'complete_task', expectedRevision: snapshot.revision,
    taskId: snapshot.tasks[0].id, outputRevisionOid: initialPackage.revision.oid,
  });
  snapshot = graph.apply(project.id, {
    type: 'confirm_milestone', expectedRevision: snapshot.revision,
    milestoneId: 'goal_confirmed', packageRevisionOid: initialPackage.revision.oid,
    actor: { username: 'alice', role: 'project_owner' },
  });
  let priorTaskId = snapshot.tasks[0].id;
  for (const [milestoneId, kind] of [
    ['materials_ready', 'material_analysis'],
    ['process_confirmed', 'process_review'],
  ] as const) {
    snapshot = graph.apply(project.id, {
      type: 'plan_task', expectedRevision: snapshot.revision,
      task: {
        kind, milestoneId, title: kind, goal: kind, required: true,
        dependsOn: [priorTaskId],
      },
    });
    const task = snapshot.tasks.at(-1)!;
    priorTaskId = task.id;
    snapshot = graph.apply(project.id, {
      type: 'complete_task', expectedRevision: snapshot.revision,
      taskId: task.id, outputRevisionOid: initialPackage.revision.oid,
    });
    snapshot = graph.apply(project.id, {
      type: 'confirm_milestone', expectedRevision: snapshot.revision,
      milestoneId, packageRevisionOid: initialPackage.revision.oid,
      actor: { username: 'alice', role: 'project_owner' },
    });
  }
  for (const [kind, dependsOn] of [
    ['operation_extraction', [priorTaskId]],
    ['knowledge_extraction', []],
    ['package_validation', []],
  ] as const) {
    const dependency = kind === 'operation_extraction'
      ? dependsOn
      : [snapshot.tasks.at(-1)!.id];
    snapshot = graph.apply(project.id, {
      type: 'plan_task', expectedRevision: snapshot.revision,
      task: {
        kind, milestoneId: 'knowledge_confirmed', title: kind, goal: kind,
        required: true, dependsOn: dependency,
      },
    });
  }
  for (const kind of ['operation_extraction', 'knowledge_extraction'] as const) {
    const task = snapshot.tasks.find((item) => item.kind === kind)!;
    snapshot = graph.apply(project.id, {
      type: 'complete_task', expectedRevision: snapshot.revision,
      taskId: task.id, outputRevisionOid: initialPackage.revision.oid,
    });
  }
  const validationTask = snapshot.tasks.find((item) => item.kind === 'package_validation')!;
  snapshot = graph.apply(project.id, {
    type: 'start_task', expectedRevision: snapshot.revision, taskId: validationTask.id,
  });
  snapshot = graph.apply(project.id, {
    type: 'fail_task', expectedRevision: snapshot.revision, taskId: validationTask.id,
  });
  const state = new AuthoringState(db);
  const attempts = new ControlledRepairAttemptStore(db);
  const orchestrator = new AiConstructionOrchestrator({ db, graph, state });
  return {
    db, project, store, graph, state, attempts, orchestrator,
    adapter: new PiAuthoringAdapter(state),
  };
}

const finding = (overrides: Partial<CheckFinding>): CheckFinding => ({
  id: 'finding-1', source: 'semantic', severity: '错误', category: '描述缺失',
  target: 'Approve', message: '操作说明不完整', suggestion: '补充操作说明',
  ...overrides,
});

test('校验问题按安全结构、语义修改和专家业务决策三级分类', () => {
  assert.equal(classifyRepairFinding(finding({
    source: 'format', category: 'manifest 非法', target: 'manifest.yaml',
  })).authority, 'automatic');
  assert.equal(classifyRepairFinding(finding({
    category: '描述过简', message: '对象业务说明不足',
  })).authority, 'approval_required');
  assert.equal(classifyRepairFinding(finding({
    source: 'format', category: 'mapping 非法', target: 'mappings/object-data-mappings.json',
    message: '对象 Customer 的 primary_key 与字段 key 不一致',
  })).authority, 'approval_required');
  assert.equal(classifyRepairFinding(finding({
    source: 'format', category: 'mapping 非法', target: 'mappings/object-data-mappings.json',
    message: 'mapping 缺少 schema_version',
  })).authority, 'automatic');
  assert.equal(classifyRepairFinding(finding({
    source: 'format', category: 'mapping 非法', target: 'mappings/object-data-mappings.json',
    message: 'mappings/object-data-mappings.json 不是合法 JSON',
  })).authority, 'approval_required');
  assert.equal(classifyRepairFinding(finding({
    category: '口径冲突', message: '两个操作采用了不同计算口径',
  })).authority, 'expert_decision');
});

test('自动修复 frontmatter 时采用规格中的结构化头部但保留人工正文', () => {
  const spec: PackageSpec = {
    id: 'repair', name: '修复', description: '修复测试', version: '1.0.0',
    language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [{
      key: 'Application', title: '申请单', description: '用于审批的业务申请单',
      fields: [],
    }],
    operations: [], rules: [], metrics: [], scripts: [],
  };
  const files = renderPackage(spec);
  files['objects/Application.md'] = [
    '---',
    'resource: wrong',
    'key: bad key',
    'title: 人工标题',
    'custom_note: 需要保留',
    '---',
    '# 人工维护正文',
    '',
    '这一段不能被自动覆盖。',
  ].join('\n');
  const plan = buildAutomaticRepairPlan(spec, files, [{
    authority: 'automatic',
    reason: '结构问题',
    finding: finding({
      source: 'format', category: 'resource 声明不符', target: 'objects/Application.md',
    }),
  }, {
    authority: 'automatic',
    reason: '结构问题',
    finding: finding({
      id: 'finding-2', source: 'format', category: 'key 非法', target: 'objects/Application.md',
    }),
  }]);

  assert.equal(plan.actions[0].mode, 'repair_frontmatter');
  assert.match(plan.files['objects/Application.md'], /resource: object/);
  assert.match(plan.files['objects/Application.md'], /key: Application/);
  assert.match(plan.files['objects/Application.md'], /title: 人工标题/);
  assert.match(plan.files['objects/Application.md'], /custom_note: 需要保留/);
  assert.match(plan.files['objects/Application.md'], /# 人工维护正文/);
  assert.match(plan.files['objects/Application.md'], /这一段不能被自动覆盖/);
});

test('安全结构问题形成独立修订并重跑原知识包校验任务', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const before = await store.current(project.id);
  await store.saveFile(project.id, {
    expectedRevision: before!.revision.oid,
    path: 'package/manifest.yaml',
    content: 'kind: broken\n',
    action: {
      cause: 'test_setup', summary: '破坏 manifest', actorKind: 'system', actorName: '测试',
    },
  });
  const corrupted = await store.current(project.id);
  const validator = async (_project: Project, generated: any) => {
    const findings = runFormatChecks(generated.spec, generated.files);
    return {
      errors: findings.filter((item) => item.severity === '错误').length,
      warnings: 0,
      hints: 0,
      findings,
    };
  };
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator, checkReader: () => null,
  });

  const result = await runner.run(project, { tables: [], documents: [] });

  assert.equal(result.status, 'automatic_repaired');
  assert.notEqual(result.revision, corrupted!.revision.oid);
  assert.equal(result.validation.errors, 0);
  assert.match((await store.readFile(project.id, 'package/manifest.yaml')).content, /kind: ontology-package/);
  assert.equal(result.attempts.length, 1);
  assert.equal(result.attempts[0].authority, 'automatic');
  assert.equal(result.attempts[0].status, 'completed');
  const snapshot = graph.snapshot(project.id)!;
  assert.equal(snapshot.tasks.find((item) => item.kind === 'package_validation')?.status, 'completed');
  assert.equal(snapshot.tasks.find((item) => item.kind === 'package_validation')?.outputRevisionOid, result.revision);
  assert.equal(snapshot.tasks.find((item) => item.kind === 'package_repair')?.required, false);
  assert.equal(snapshot.tasks.find((item) => item.kind === 'package_repair')?.status, 'completed');
  const history = await store.history(project.id);
  assert.equal(history[0].cause, 'automatic_repair');
});

test('自动修复达到两次上限后停止并保留尝试动作', async (t) => {
  const { project, store, state, attempts, orchestrator, adapter } = await fixture(t);
  let current = await store.current(project.id);
  for (const [path, content] of [
    ['package/manifest.yaml', 'kind: broken\n'],
    ['package/objects/index.md', 'broken objects index\n'],
    ['package/operations/index.md', 'broken operations index\n'],
  ]) {
    const saved = await store.saveFile(project.id, {
      expectedRevision: current!.revision.oid,
      path,
      content,
      action: {
        cause: 'test_setup', summary: `破坏 ${path}`, actorKind: 'system', actorName: '测试',
      },
    });
    current = await store.current(project.id);
    assert.equal(saved.revision.oid, current!.revision.oid);
  }
  const validator = async (_project: Project, generated: any) => {
    const ordered = [
      ['manifest.yaml', 'manifest 非法'],
      ['objects/index.md', '对象索引非法'],
      ['operations/index.md', '索引与详情不一致'],
    ] as const;
    const canonical = renderPackage(generated.spec);
    const broken = ordered.find(([path]) => generated.files[path] !== canonical[path]);
    const findings = broken ? [finding({
      id: `broken-${broken[0]}`,
      source: 'format',
      category: broken[1],
      target: broken[0],
      message: `${broken[0]} 结构错误`,
    })] : [];
    return { errors: findings.length, warnings: 0, hints: 0, findings };
  };
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator, checkReader: () => null, maxAutomaticAttempts: 2,
  });

  const result = await runner.run(project, { tables: [], documents: [] });

  assert.equal(result.status, 'stopped');
  assert.equal(result.validation.errors, 1);
  assert.equal(result.attempts.length, 2);
  assert.ok(result.attempts.every((attempt) => attempt.status === 'completed'));
  assert.match((result.message.blocks[1] as any).text, /连续尝试 2 次/);
  assert.match((result.message.blocks[1] as any).text, /已尝试动作/);
});

test('知识语义问题只生成变更方案，修订变化后旧待办会失效并重新规划', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  const saved = await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '完成审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备语义问题', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    id: 'description-short',
    category: '描述过简',
    target: 'Approve',
    message: '审批操作没有说明适用范围',
    suggestion: '补充审批操作的适用范围与排除项',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });

  const result = await runner.run(project, { tables: [], documents: [] });

  assert.equal(result.status, 'waiting_approval');
  assert.equal(result.revision, saved.revision.oid);
  assert.equal((await store.current(project.id))?.revision.oid, saved.revision.oid);
  assert.equal(result.interaction?.type, 'change_plan');
  assert.equal(result.interaction?.payload.purpose, 'controlled_repair');
  assert.deepEqual((result.interaction?.payload.changes as any[]).map((change) => [
    change.targetKind, change.targetKey, change.action,
  ]), [['operation', 'Approve', 'modify']]);

  const repeated = await runner.run(project, { tables: [], documents: [] });
  assert.equal(repeated.status, 'waiting_approval');
  assert.equal(repeated.interaction?.id, result.interaction?.id);
  assert.equal(repeated.attempts.length, 1);

  const oldAttemptId = result.attempts[0].id;
  const oldRepairTaskId = graph.snapshot(project.id)!.tasks.find(
    (item) => item.kind === 'package_repair',
  )!.id;
  const revised = await store.saveFile(project.id, {
    expectedRevision: saved.revision.oid,
    path: 'package/index.md',
    content: `${(await store.readFile(project.id, 'package/index.md')).content}\n<!-- 新修订 -->\n`,
    action: {
      cause: 'manual_edit', summary: '等待期间产生新修订', actorKind: 'user', actorName: 'alice',
    },
  });
  const replanned = await runner.run(project, { tables: [], documents: [] });
  assert.equal(replanned.status, 'waiting_approval');
  assert.notEqual(replanned.interaction?.id, result.interaction?.id);
  assert.equal(replanned.interaction?.baseRevision, revised.revision.oid);
  assert.equal(state.snapshot(project.id).pendingInteractions.some(
    (interaction) => interaction.id === result.interaction?.id,
  ), false);
  assert.equal(attempts.get(oldAttemptId).status, 'stopped');
  const repairTasks = graph.snapshot(project.id)!.tasks.filter((item) => item.kind === 'package_repair');
  assert.equal(repairTasks.length, 2);
  assert.equal(repairTasks.find((item) => item.id === oldRepairTaskId)?.status, 'completed');
  assert.equal(repairTasks.at(-1)?.inputRevisionOid, revised.revision.oid);
});

test('语义方案应用临时失败后保留原待办和 attempt，后续仍可完成', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '完成审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备可重试语义方案', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    category: '描述过简', target: 'Approve', message: '审批适用范围不完整',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });
  const planned = await runner.run(project, { tables: [], documents: [] });
  const attemptId = planned.attempts[0].id;
  const beforeApply = await store.current(project.id);
  orchestrator.startKnowledgeRepair(project.id, {
    inputRevisionOid: beforeApply!.revision.oid,
    messageKey: 'test.repair.retry.start',
  });

  runner.failPlan(project.id, attemptId, '模型服务暂时不可用');

  assert.equal(attempts.get(attemptId).status, 'waiting_user');
  assert.match(attempts.get(attemptId).error ?? '', /暂时不可用/);
  assert.equal(planned.interaction?.status, 'pending');
  assert.equal(state.snapshot(project.id).pendingInteractions.some(
    (interaction) => interaction.id === planned.interaction?.id,
  ), true);
  assert.equal(graph.snapshot(project.id)!.tasks.find(
    (item) => item.kind === 'package_repair',
  )?.status, 'waiting_user');

  orchestrator.startKnowledgeRepair(project.id, {
    inputRevisionOid: beforeApply!.revision.oid,
    messageKey: 'test.repair.retry.resume',
  });
  await runner.completeApprovedPlan(project, { tables: [], documents: [] }, attemptId);
  assert.equal(attempts.get(attemptId).status, 'completed');
});

test('语义方案已提交新修订后发生故障会终止待办并链接输出修订', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '完成审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备已应用方案故障', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    category: '描述过简', target: 'Approve', message: '审批适用范围不完整',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });
  const planned = await runner.run(project, { tables: [], documents: [] });
  const attemptId = planned.attempts[0].id;
  const inputRevision = planned.revision;
  orchestrator.startKnowledgeRepair(project.id, {
    inputRevisionOid: inputRevision,
    messageKey: 'test.repair.applied-failure.start',
  });
  const applied = await store.saveFile(project.id, {
    expectedRevision: inputRevision,
    path: 'package/index.md',
    content: `${(await store.readFile(project.id, 'package/index.md')).content}\n<!-- semantic repair -->\n`,
    action: {
      cause: 'chat_apply', summary: '模拟已提交的语义修复', actorKind: 'user', actorName: 'alice',
    },
  });

  runner.failAppliedPlan(project.id, attemptId, '修订后校验服务中断', applied.revision.oid);

  const failed = attempts.get(attemptId);
  assert.equal(failed.status, 'failed');
  assert.equal(failed.outputRevision, applied.revision.oid);
  assert.equal(attempts.rootRevision(project.id, applied.revision.oid), inputRevision);
  assert.equal(graph.snapshot(project.id)!.tasks.find(
    (item) => item.kind === 'package_repair',
  )?.status, 'failed');
});

test('无法定位资源的专家回答不会关闭待办，补充资源 key 后可生成方案', async (t) => {
  const { project, store, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '根据资料完成业务审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备包级口径问题', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    id: 'package-caliber', category: '口径冲突', target: 'package',
    message: '审批时长存在冲突口径', suggestion: '明确应采用的审批时长口径',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });
  const waiting = await runner.run(project, { tables: [], documents: [] });

  await assert.rejects(runner.answerExpertDecision(
    project,
    waiting.interaction!,
    'missing-target',
    {
      answers: [{
        questionId: 'expert_decision', selected: [],
        other: '审批时长不包含补件等待时间。',
      }],
    },
  ), /无法定位到具体的知识资源|需要先确定要修改哪一个/);
  assert.equal(state.snapshot(project.id).pendingInteractions.some(
    (interaction) => interaction.id === waiting.interaction?.id,
  ), true);

  const planned = await runner.answerExpertDecision(
    project,
    waiting.interaction!,
    'with-target',
    {
      answers: [{
        questionId: 'expert_decision', selected: [],
        other: '修改 operation Approve：审批时长不包含补件等待时间。',
      }],
    },
  );
  assert.equal(planned.interaction.type, 'change_plan');
  assert.equal((planned.interaction.payload.changes as any[])[0].targetKey, 'Approve');
});

test('自动修订提交后校验崩溃仍记录输出修订并沿用原重试链', async (t) => {
  const { project, store, state, attempts, orchestrator, adapter } = await fixture(t);
  const before = await store.current(project.id);
  await store.saveFile(project.id, {
    expectedRevision: before!.revision.oid,
    path: 'package/manifest.yaml',
    content: 'kind: broken\n',
    action: {
      cause: 'test_setup', summary: '破坏 manifest', actorKind: 'system', actorName: '测试',
    },
  });
  const corrupted = await store.current(project.id);
  let validationCalls = 0;
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    checkReader: () => null,
    validator: async (_project, generated) => {
      validationCalls += 1;
      if (validationCalls > 1) throw new Error('校验服务中断');
      const findings = runFormatChecks(generated.spec, generated.files);
      return { errors: findings.length, warnings: 0, hints: 0, findings };
    },
  });

  await assert.rejects(
    runner.run(project, { tables: [], documents: [] }),
    /校验服务中断/,
  );
  const repaired = await store.current(project.id);
  const failedAttempts = attempts.listChain(project.id, corrupted!.revision.oid);
  assert.equal(failedAttempts.length, 1);
  assert.equal(failedAttempts[0].status, 'failed');
  assert.equal(failedAttempts[0].outputRevision, repaired!.revision.oid);
  assert.equal(attempts.rootRevision(project.id, repaired!.revision.oid), corrupted!.revision.oid);

  const resumed = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    checkReader: () => null,
    validator: async () => ({ errors: 0, warnings: 0, hints: 0, findings: [] }),
  });
  const result = await resumed.run(project, { tables: [], documents: [] });
  assert.equal(result.status, 'valid');
  assert.equal(result.attempts[0].id, failedAttempts[0].id);
});

test('业务口径问题暂停为领域专家必答问题，任务图显示等待用户', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  const spec: PackageSpec = {
    ...current!.spec,
    operations: [{
      key: 'Approve', title: '审批', description: '根据资料完成业务审批',
      inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
    }],
  };
  const saved = await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec,
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备审批操作', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    id: 'caliber-conflict',
    category: '口径冲突',
    target: 'Approve',
    message: '审批时长在两份资料中采用不同计算口径',
    suggestion: '明确审批时长是否包含补件等待时间',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });

  const result = await runner.run(project, { tables: [], documents: [] });

  assert.equal(result.status, 'waiting_expert');
  assert.equal(result.revision, saved.revision.oid);
  assert.equal(result.interaction?.type, 'question_batch');
  assert.equal(result.interaction?.payload.purpose, 'controlled_repair_expert');
  assert.equal(result.interaction?.payload.requiredRole, 'domain_expert');
  assert.equal((result.interaction?.payload.questions as any[])[0].required, true);
  const snapshot = graph.snapshot(project.id)!;
  assert.equal(snapshot.progress.currentTaskId, snapshot.tasks.find(
    (item) => item.kind === 'package_repair',
  )?.id);
  assert.equal(snapshot.progress.waitingFor, 'user_input');

  const response = {
    answers: [{
      questionId: 'expert_decision',
      selected: [],
      other: '审批时长不包含补件等待时间，以资料齐备时刻作为起点。',
    }],
  };
  const planned = await runner.answerExpertDecision(
    project, result.interaction!, 'expert-decision-1', response,
  );
  assert.equal(planned.interaction.type, 'change_plan');
  assert.equal(planned.interaction.payload.purpose, 'controlled_repair');
  assert.match(String((planned.interaction.payload.changes as any[])[0].description), /不包含补件等待时间/);
  assert.match(String(planned.interaction.payload.message), /专家明确决策/);
  const replayed = await runner.answerExpertDecision(
    project, result.interaction!, 'expert-decision-1', response,
  );
  assert.equal(replayed.replayed, true);
  assert.equal(replayed.interaction.id, planned.interaction.id);
});

test('批准受控修复方案只应用一次并支持回答幂等重放', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '完成审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备控制器批准方案', actorKind: 'system', actorName: '测试',
    },
  });
  const semanticFinding = finding({
    id: 'controller-description',
    category: '描述过简',
    target: 'Approve',
    message: '审批操作没有说明适用范围',
    suggestion: '补充审批操作的适用范围',
  });
  const validator = async (_project: Project, generated: any) => {
    const repaired = generated.spec.operations[0]?.description.includes('适用于资料齐备的申请');
    const findings = repaired ? [] : [semanticFinding];
    return { errors: findings.length, warnings: 0, hints: 0, findings };
  };
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator, checkReader: () => null,
  });
  const planned = await runner.run(project, { tables: [], documents: [] });
  assert.equal(planned.status, 'waiting_approval');
  let applyCalls = 0;
  const controller = new ControlledRepairController({
    state,
    graph,
    orchestrator,
    packages: store,
    adapter,
    runner,
    contextForProject: () => ({ tables: [], documents: [] }),
    applyPlan: async (_project, _history, _message, _plan, _tables, options) => {
      applyCalls += 1;
      assert.equal(options.maxCandidateAttempts, 3);
      const repairJob = state.snapshot(project.id).activeRuns.find((run) => (
        run.state.purpose === 'controlled_repair_apply'
      ));
      assert.equal(repairJob?.status, 'running');
      assert.equal(repairJob?.state.phase, '方案应用');
      assert.equal(repairJob?.state.currentStep, '按确认方案修改知识文件');
      assert.equal(repairJob?.state.recoveryAction, 'repair_knowledge_structure');
      assert.ok(repairJob?.state.lastHeartbeatAt);
      const before = await store.current(project.id);
      const saved = await store.savePackageVersion(project.id, {
        expectedRevision: before!.revision.oid,
        spec: {
          ...before!.spec,
          operations: before!.spec.operations.map((operation) => operation.key === 'Approve'
            ? { ...operation, description: '适用于资料齐备的申请，完成审批并记录结论' }
            : operation),
        },
        coverage: before!.coverage,
        action: {
          cause: 'agent_update', summary: '落实已批准修复方案', actorKind: 'agent', actorName: '测试助手',
        },
      });
      return { reply: '已应用修复方案', version: saved.packageVersion, revision: saved.revision.oid };
    },
  });
  const response = { action: 'approve' };
  const approved = await controller.respond(
    project,
    planned.interaction!,
    'approve-controlled-repair',
    response,
    'alice',
  );

  assert.equal(applyCalls, 1);
  assert.equal(approved.validation.errors, 0);
  assert.equal(attempts.get(planned.attempts[0].id).status, 'completed');
  assert.equal(state.interaction(project.id, planned.interaction!.id)?.status, 'answered');
  assert.equal(graph.snapshot(project.id)!.tasks.find(
    (task) => task.kind === 'package_repair',
  )?.status, 'completed');
  assert.equal(state.snapshot(project.id).activeRuns.some((run) => (
    run.state.purpose === 'controlled_repair_apply'
  )), false);

  const replayed = await controller.respond(
    project,
    state.interaction(project.id, planned.interaction!.id)!,
    'approve-controlled-repair',
    response,
    'alice',
  );
  assert.equal(replayed.replayed, true);
  assert.equal(applyCalls, 1);
});

test('受控修复候选三次映射失败后返回可重试错误且保留原待办', async (t) => {
  const { project, store, graph, state, attempts, orchestrator, adapter } = await fixture(t);
  const current = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: current!.revision.oid,
    spec: {
      ...current!.spec,
      operations: [{
        key: 'Approve', title: '审批', description: '完成审批',
        inputs: [], outputs: [], flow: ['受理申请'], relatedObjects: [],
      }],
    },
    coverage: current!.coverage,
    action: {
      cause: 'test_setup', summary: '准备候选失败', actorKind: 'system', actorName: '测试',
    },
  });
  const semanticFinding = finding({
    id: 'controller-mapping',
    category: '映射错误',
    target: 'Approve',
    message: '审批操作需要修复来源映射',
    suggestion: '修复来源映射',
  });
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings: [semanticFinding] }),
    checkReader: () => null,
  });
  const planned = await runner.run(project, { tables: [], documents: [] });
  assert.equal(planned.status, 'waiting_approval');
  const controller = new ControlledRepairController({
    state, graph, orchestrator, packages: store, adapter, runner,
    contextForProject: () => ({ tables: [], documents: [] }),
    applyPlan: async (_project, _history, _message, _plan, _tables, options) => {
      assert.equal(options.maxCandidateAttempts, 3);
      throw new PackageCandidateRejectedError(
        '候选修订连续 3 次生成失败',
        'evt_src_table 在来源表中不存在',
      );
    },
  });

  await assert.rejects(
    () => controller.respond(
      project,
      planned.interaction!,
      'approve-invalid-mapping',
      { action: 'approve' },
      'alice',
    ),
    (error: unknown) => error instanceof PackageCandidateRejectedError
      && /evt_src_table/u.test(error.feedback),
  );
  assert.equal(state.interaction(project.id, planned.interaction!.id)?.status, 'pending');
  assert.equal(attempts.get(planned.attempts[0].id).status, 'waiting_user');
});

// ---- 多资源冲突：让专家指定修改目标 ----

test('冲突类问题同时命中多个资源时给出候选而不是放弃', async () => {
  const { repairTargetCandidates, inferRepairTarget } = await import('../src/ai-construction/controlled-repair.js');
  const spec = {
    objects: [{ key: 'ipo_detail_info' }, { key: 'major_project_info' }],
    operations: [], rules: [], metrics: [], scripts: [],
  } as any;
  const finding = {
    id: 'f6_83216', source: 'semantic', severity: '错误', category: '对象定义冲突',
    target: 'ipo_detail_info、major_project_info',
    message: 'ipo_detail_info 和 major_project_info 两个对象的 candidateId 均为 candidate_3',
  } as any;

  const candidates = repairTargetCandidates(spec, finding);
  assert.deepEqual(candidates.map((item) => item.targetKey), ['ipo_detail_info', 'major_project_info']);
  // 单一目标无法推断，但候选是明确的——早先只能返回 null 然后抛错
  assert.equal(inferRepairTarget(spec, finding), null);
});

test('定位不到唯一目标时抛可识别错误并带上候选', async () => {
  const { buildSemanticRepairPlan, RepairTargetUnresolvedError } = await import('../src/ai-construction/controlled-repair.js');
  const spec = {
    objects: [{ key: 'a_info' }, { key: 'b_info' }],
    operations: [], rules: [], metrics: [], scripts: [],
  } as any;
  const finding = {
    id: 'f1', source: 'semantic', severity: '错误', category: '对象定义冲突',
    target: 'a_info、b_info', message: 'a_info 与 b_info 冲突',
  } as any;

  assert.throws(() => buildSemanticRepairPlan(spec, finding), (error: any) => {
    assert.ok(error instanceof RepairTargetUnresolvedError);
    assert.deepEqual(error.candidates.map((item: any) => item.targetKey), ['a_info', 'b_info']);
    assert.match(error.message, /需要先确定要修改哪一个/);
    return true;
  });
});

test('专家选定目标后可以生成修复方案', async () => {
  const { buildSemanticRepairPlan, parseExpertRepairTarget } = await import('../src/ai-construction/controlled-repair.js');
  const spec = {
    objects: [{ key: 'a_info' }, { key: 'b_info' }],
    operations: [], rules: [], metrics: [], scripts: [],
  } as any;
  const finding = {
    id: 'f1', source: 'semantic', severity: '错误', category: '对象定义冲突',
    target: 'a_info、b_info', message: 'a_info 与 b_info 冲突',
  } as any;

  const picked = parseExpertRepairTarget({
    answers: [
      { questionId: 'expert_decision', selected: ['两者应使用不同编号'], other: '' },
      { questionId: 'repair_target', selected: ['object:b_info'], other: '' },
    ],
  } as any);
  assert.deepEqual(picked, { targetKind: 'object', targetKey: 'b_info' });

  const plan = buildSemanticRepairPlan(spec, finding, '两者应使用不同编号', picked);
  assert.equal(plan.changes[0].targetKey, 'b_info');
  assert.equal(plan.changes[0].targetKind, 'object');
});

test('业务决策解析忽略目标选择那道题', async () => {
  const { parseExpertRepairDecision } = await import('../src/ai-construction/controlled-repair.js');
  const decision = parseExpertRepairDecision({
    answers: [
      { questionId: 'repair_target', selected: ['object:b_info'], other: '' },
      { questionId: 'expert_decision', selected: ['按业务口径分别编号'], other: '' },
    ],
  } as any);
  assert.equal(decision, '按业务口径分别编号');
});

test('资源目标只接受合法类型，业务决策中的冒号不能被当成目标', async () => {
  const { parseExpertRepairTarget } = await import('../src/ai-construction/controlled-repair.js');
  assert.equal(parseExpertRepairTarget({
    answers: [{
      questionId: 'repair_target',
      selected: ['step_8_field_fix:replace missing_field with actual_field'],
    }],
  } as any), undefined);
  assert.equal(parseExpertRepairTarget({
    answers: [{
      questionId: 'repair_target',
      selected: ['required:true'],
    }],
  } as any), undefined);
});

test('伪造修复目标在写入回答前被拒绝且原待办保持可重试', async (t) => {
  const { project, store, state, attempts, orchestrator, adapter } = await fixture(t);
  const before = await store.current(project.id);
  await store.savePackageVersion(project.id, {
    expectedRevision: before!.revision.oid,
    spec: {
      ...before!.spec,
      objects: [
        { key: 'lead', title: '营销线索', description: '营销线索', fields: [] },
        { key: 'bid', title: '招投标线索', description: '招投标线索', fields: [] },
      ],
      operations: [{
        key: 'recommend', title: '产品推荐', description: '根据线索推荐产品',
        inputs: [], outputs: [], flow: ['读取 object:bid'], relatedObjects: ['bid'],
      }],
    },
    coverage: before!.coverage,
    action: {
      cause: 'test_setup', summary: '准备多目标发现', actorKind: 'system', actorName: '测试',
    },
  });
  const findings = [finding({
    id: 'field-reference', category: '字段引用错误',
    target: 'recommend、bid',
    message: 'operation recommend 引用了 object bid 中不存在的字段',
    suggestion: '将操作中的错误字段改为招投标对象真实字段',
  })];
  const runner = new ControlledRepairRunner({
    orchestrator, packages: store, state, adapter, attempts,
    validator: async () => ({ errors: 1, warnings: 0, hints: 0, findings }),
    checkReader: () => null,
  });
  const waiting = await runner.run(project, { tables: [], documents: [] });
  assert.equal(waiting.interaction?.type, 'question_batch');

  await assert.rejects(runner.answerExpertDecision(
    project,
    waiting.interaction!,
    'forged-target',
    {
      answers: [
        {
          questionId: 'expert_decision',
          selected: ['step_8_field_fix:replace missing_field with actual_field'],
        },
        {
          questionId: 'repair_target',
          selected: ['step_8_field_fix:replace missing_field with actual_field'],
        },
      ],
    },
  ), /需要先确定要修改哪一个/);

  const snapshot = state.snapshot(project.id);
  assert.equal(state.interaction(project.id, waiting.interaction!.id)?.status, 'pending');
  assert.equal(snapshot.pendingInteractions.filter((item) => item.type === 'change_plan').length, 0);
  assert.equal(snapshot.activeRuns.length, 1);
});
