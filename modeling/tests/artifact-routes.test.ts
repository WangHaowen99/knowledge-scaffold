import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

import type { CoverageResult, PackageSpec } from '../src/types.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-artifact-routes-'));

const [
  { registerArtifactRoutes },
  { getPackageArtifactStore },
  { getAuthoringState },
  { createProject },
  m4repo,
  diagnosisRepo,
  { getDb },
] = await Promise.all([
  import('../src/artifacts/routes.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/authoring/state.js'),
  import('../src/projects/repo.js'),
  import('../src/workflow/m4-repo.js'),
  import('../src/evaluation/failure-diagnosis-repo.js'),
  import('../src/db/index.js'),
]);

const coverage: CoverageResult = { covered: [], uncovered: [] };

function spec(version: string, description: string): PackageSpec {
  return {
    id: 'revision-route-package',
    name: '版本接口知识包',
    description,
    version,
    language: 'zh-CN',
    defaultProviderAlias: 'data',
    objects: [],
    operations: [],
  };
}

function confirmOptimizationDiagnosis(projectId: string, roundId: string, revisionOid: string) {
  const [diagnosis] = diagnosisRepo.replaceFailureDiagnoses(
    projectId, roundId, '1.0.0', revisionOid, [{
      category: 'knowledge_gap',
      title: '知识说明缺失',
      summary: '独立资料确认需要补充说明',
      evidence: ['业务规则文档明确要求补充说明'],
      confidence: 'high',
      blocking: false,
      repairScope: 'package',
      packageOptimizable: true,
      resultIds: ['result-1'],
      relatedQuestions: ['如何处理该业务？'],
      suggestedAction: '补充可复用的知识说明',
    }],
  );
  return diagnosisRepo.resolveFailureDiagnosis({
    projectId,
    diagnosisId: diagnosis.id,
    action: 'confirm_rule',
    note: '确认该规则适用于所有同类业务。',
    actor: 'alice',
  });
}

function routeApp(t: test.TestContext, username = 'alice') {
  const app = Fastify();
  app.addHook('preHandler', async (req) => {
    req.user = { username, role: 'modeler' };
  });
  registerArtifactRoutes(app);
  t.after(() => app.close());
  return app;
}

test('版本列表、差异和 CAS 恢复形成闭环并发布项目事件', async (t) => {
  const project = createProject({ owner: 'alice', name: '版本工作区接口' });
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(project.id);
  const first = await store.savePackageVersion(project.id, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0', '第一版'),
    coverage,
    action: { cause: 'generate', summary: '生成第一版', actorKind: 'agent', actorName: '助手' },
  });
  const second = await store.savePackageVersion(project.id, {
    expectedRevision: first.revision.oid,
    spec: spec('1.1.0', '第二版'),
    coverage,
    action: { cause: 'manual_edit', summary: '更新说明', actorKind: 'user', actorName: 'alice' },
  });
  const app = routeApp(t);

  const listed = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/revisions` });
  assert.equal(listed.statusCode, 200, listed.body);
  assert.equal(listed.json().currentRevision, second.revision.oid);
  assert.deepEqual(listed.json().versions.map((item: any) => item.id), [
    second.revision.oid,
    first.revision.oid,
  ]);

  const diff = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/revisions/diff?from=${first.revision.oid}&to=${second.revision.oid}`,
  });
  assert.equal(diff.statusCode, 200, diff.body);
  assert.ok(diff.json().files.some((file: any) => file.path === 'manifest.yaml'));
  assert.equal(diff.json().files.every((file: any) => !file.path.startsWith('package/')), true);

  const conflict = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/revisions/restore`,
    payload: { targetRevision: first.revision.oid, expectedRevision: first.revision.oid },
  });
  assert.equal(conflict.statusCode, 409, conflict.body);
  assert.equal(conflict.json().code, 'revision_conflict');

  const restored = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/revisions/restore`,
    payload: { targetRevision: first.revision.oid, expectedRevision: second.revision.oid },
  });
  assert.equal(restored.statusCode, 200, restored.body);
  assert.notEqual(restored.json().currentRevision, first.revision.oid);
  assert.equal(restored.json().packageVersion, '1.0.0');
  const afterRestore = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/revisions` });
  assert.equal(afterRestore.statusCode, 200, afterRestore.body);
  assert.equal(afterRestore.json().versions[0].packageVersion, '1.0.0');
  assert.deepEqual(getAuthoringState().replay(project.id).events.map((event) => event.type), [
    'package.revision.created',
  ]);
});

test('版本接口不向其他项目所有者暴露修订', async (t) => {
  const project = createProject({ owner: 'alice', name: '版本权限隔离' });
  const app = routeApp(t, 'bob');
  const response = await app.inject({ method: 'GET', url: `/api/projects/${project.id}/revisions` });
  assert.equal(response.statusCode, 404);
});

test('候选修订接口支持预览、接受和幂等重放且禁止修改内部模型文件', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选修订接口' });
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(project.id);
  const generated = await store.savePackageVersion(project.id, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0', '候选修订基线'),
    coverage,
    action: { cause: 'generate', summary: '生成候选基线', actorKind: 'agent', actorName: '助手' },
  });
  const index = (await store.readFile(project.id, 'package/index.md')).content;
  const app = routeApp(t);

  const internalPath = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions`,
    payload: {
      expectedRevision: generated.revision.oid,
      summary: '尝试修改内部模型',
      groups: [{
        id: 'invalid-model', summary: '修改内部模型',
        changes: [{ kind: 'write', path: 'model/package-spec.json', content: '{}' }],
      }],
    },
  });
  assert.equal(internalPath.statusCode, 400, internalPath.body);

  const created = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions`,
    payload: {
      expectedRevision: generated.revision.oid,
      summary: '补充候选说明',
      groups: [{
        id: 'description', summary: '补充知识包说明',
        changes: [{ kind: 'write', path: 'index.md', content: `${index}\n候选接口说明\n` }],
      }],
    },
  });
  assert.equal(created.statusCode, 201, created.body);
  const candidateId = created.json().candidate.id;
  const diagnosis = confirmOptimizationDiagnosis(
    project.id, 'round-candidate-route', generated.revision.oid,
  );
  const [suggestion] = m4repo.insertSuggestions(
    project.id, 'round-candidate-route', '1.0.0', generated.revision.oid, [{
      title: '补充候选说明', detail: '根据独立资料补充说明', kind: 'clarify_description',
      diagnosisIds: [diagnosis.id],
    }],
  );
  m4repo.markCandidateRevision(project.id, [suggestion.id], candidateId);
  assert.equal(m4repo.getSuggestions([suggestion.id])[0].status, 'candidate');

  const listed = await app.inject({
    method: 'GET', url: `/api/projects/${project.id}/candidate-revisions`,
  });
  assert.equal(listed.statusCode, 200, listed.body);
  assert.equal(listed.json().pendingCount, 1);
  assert.equal(listed.json().hasPending, true);
  assert.equal(listed.json().candidates[0].id, candidateId);

  const preview = await app.inject({
    method: 'GET',
    url: `/api/projects/${project.id}/candidate-revisions/${candidateId}/preview`,
  });
  assert.equal(preview.statusCode, 200, preview.body);
  assert.match(preview.json().package.files['index.md'], /候选接口说明/u);
  assert.equal((await store.current(project.id))?.revision.oid, generated.revision.oid);

  const acceptRequest = {
    method: 'POST' as const,
    url: `/api/projects/${project.id}/candidate-revisions/${candidateId}/respond`,
    payload: { action: 'accept' },
  };
  const accepted = await app.inject(acceptRequest);
  assert.equal(accepted.statusCode, 200, accepted.body);
  assert.notEqual(accepted.json().currentRevision, generated.revision.oid);
  assert.equal(accepted.json().candidate.status, 'accepted');
  assert.equal(accepted.json().packageVersion, '1.0.0-rc.1');
  const appliedSuggestion = m4repo.getSuggestions([suggestion.id])[0];
  assert.equal(appliedSuggestion.status, 'applied');
  assert.equal(appliedSuggestion.appliedVersion, '1.0.0-rc.1');
  assert.equal(appliedSuggestion.appliedRevision, accepted.json().currentRevision);
  assert.equal((await store.current(project.id))?.packageVersion, '1.0.0-rc.1');

  getDb().prepare(`UPDATE opt_suggestions SET status = 'candidate', applied_revision_oid = NULL
    WHERE id = ?`).run(suggestion.id);

  const replayed = await app.inject(acceptRequest);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);
  assert.equal(replayed.json().currentRevision, accepted.json().currentRevision);
  assert.equal(replayed.json().packageVersion, accepted.json().packageVersion);
  assert.equal(m4repo.getSuggestions([suggestion.id])[0].status, 'applied');
});

test('同 revision 重跑诊断后旧优化候选不可接受且不影响其他来源候选', async (t) => {
  const project = createProject({ owner: 'alice', name: '诊断批次失效门禁' });
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(project.id);
  const generated = await store.savePackageVersion(project.id, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0', '诊断失效基线'),
    coverage,
    action: { cause: 'generate', summary: '生成诊断失效基线', actorKind: 'agent', actorName: '助手' },
  });
  const index = (await store.readFile(project.id, 'package/index.md')).content;
  const roundId = 'round-diagnosis-refresh';
  const diagnosis = confirmOptimizationDiagnosis(project.id, roundId, generated.revision.oid);
  const optimizationCandidate = await store.createCandidateRevision(project.id, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'optimization', summary: '旧诊断优化候选',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n旧诊断优化\n` }],
    }],
    action: {
      cause: 'optimization_candidate', summary: '旧诊断优化候选', actorKind: 'agent', actorName: '助手',
    },
  });
  const [suggestion] = m4repo.insertSuggestions(
    project.id, roundId, '1.0.0', generated.revision.oid, [{
      title: '旧诊断建议', detail: '旧诊断对应的修改', kind: 'clarify_description',
      diagnosisIds: [diagnosis.id],
    }],
  );
  m4repo.markCandidateRevision(project.id, [suggestion.id], optimizationCandidate.id);
  const otherCandidate = await store.createCandidateRevision(project.id, {
    expectedRevision: generated.revision.oid,
    groups: [{
      id: 'controlled-repair', summary: '其他来源候选',
      changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n其他来源修改\n` }],
    }],
    action: {
      cause: 'controlled_repair', summary: '其他来源候选', actorKind: 'agent', actorName: '助手',
    },
  });

  diagnosisRepo.replaceFailureDiagnoses(project.id, roundId, '1.0.0', generated.revision.oid, [{
    category: 'knowledge_gap', title: '重跑后的新诊断', summary: '诊断批次已经更新',
    evidence: ['新的独立证据'], confidence: 'high', blocking: false,
    repairScope: 'package', packageOptimizable: true, resultIds: ['result-1'],
    relatedQuestions: ['如何处理该业务？'], suggestedAction: '按新诊断重新生成建议',
  }]);
  const app = routeApp(t);
  const stale = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions/${optimizationCandidate.id}/respond`,
    payload: { action: 'accept' },
  });
  assert.equal(stale.statusCode, 409, stale.body);
  assert.equal(stale.json().code, 'optimization_diagnosis_stale');
  assert.equal((await store.getCandidateRevision(project.id, optimizationCandidate.id)).status, 'rejected');
  assert.equal(m4repo.getSuggestions([suggestion.id])[0].status, 'rejected');
  assert.equal((await store.current(project.id))?.revision.oid, generated.revision.oid);

  const acceptedOther = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions/${otherCandidate.id}/respond`,
    payload: { action: 'accept' },
  });
  assert.equal(acceptedOther.statusCode, 200, acceptedOther.body);
  assert.equal(acceptedOther.json().candidate.status, 'accepted');
});

test('候选退回、拒绝和基线过期会同步关联优化建议', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选状态同步' });
  const store = getPackageArtifactStore();
  const initial = await store.ensureProject(project.id);
  const generated = await store.savePackageVersion(project.id, {
    expectedRevision: initial.oid,
    spec: spec('1.0.0', '候选状态基线'),
    coverage,
    action: { cause: 'generate', summary: '生成候选状态基线', actorKind: 'agent', actorName: '助手' },
  });
  const index = (await store.readFile(project.id, 'package/index.md')).content;
  const createLinked = async (id: string) => {
    const candidate = await store.createCandidateRevision(project.id, {
      expectedRevision: generated.revision.oid,
      groups: [{
        id, summary: `候选 ${id}`,
        changes: [{ kind: 'write', path: 'package/index.md', content: `${index}\n${id}\n` }],
      }],
      action: { cause: 'optimization_candidate', summary: `候选 ${id}`, actorKind: 'agent', actorName: '助手' },
    });
    const [suggestion] = m4repo.insertSuggestions(
      project.id, `round-${id}`, '1.0.0', generated.revision.oid,
      [{ title: `建议 ${id}`, detail: '独立证据', kind: 'clarify_description' }],
    );
    m4repo.markCandidateRevision(project.id, [suggestion.id], candidate.id);
    return { candidate, suggestion };
  };
  const returned = await createLinked('returned');
  const rejected = await createLinked('rejected');
  const superseded = await createLinked('superseded');
  const app = routeApp(t);

  const reviseResponse = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions/${returned.candidate.id}/respond`,
    payload: { action: 'revise', note: '补充边界条件' },
  });
  assert.equal(reviseResponse.statusCode, 200, reviseResponse.body);
  const returnedSuggestion = m4repo.getSuggestions([returned.suggestion.id])[0];
  assert.equal(returnedSuggestion.status, 'pending');
  assert.equal(returnedSuggestion.candidateFeedback, '补充边界条件');

  const rejectResponse = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions/${rejected.candidate.id}/respond`,
    payload: { action: 'reject', note: '方向不正确' },
  });
  assert.equal(rejectResponse.statusCode, 200, rejectResponse.body);
  assert.equal(m4repo.getSuggestions([rejected.suggestion.id])[0].status, 'rejected');

  await store.saveFile(project.id, {
    expectedRevision: generated.revision.oid,
    path: 'package/index.md',
    content: `${index}\n当前修订已变化\n`,
    action: { cause: 'manual_edit', summary: '推进当前修订', actorKind: 'user', actorName: 'alice' },
  });
  const expiredResponse = await app.inject({
    method: 'POST',
    url: `/api/projects/${project.id}/candidate-revisions/${superseded.candidate.id}/respond`,
    payload: { action: 'accept' },
  });
  assert.equal(expiredResponse.statusCode, 409, expiredResponse.body);
  assert.equal(expiredResponse.json().code, 'revision_conflict');
  assert.equal((await store.getCandidateRevision(project.id, superseded.candidate.id)).status, 'superseded');
  assert.equal(m4repo.getSuggestions([superseded.suggestion.id])[0].status, 'rejected');
});

test('候选修订接口不向其他项目所有者暴露待处理内容', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选权限隔离' });
  const app = routeApp(t, 'bob');
  const response = await app.inject({
    method: 'GET', url: `/api/projects/${project.id}/candidate-revisions`,
  });
  assert.equal(response.statusCode, 404);
});
