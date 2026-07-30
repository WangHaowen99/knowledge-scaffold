import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import Fastify from 'fastify';

import type { CoverageResult, PackageSpec } from '../src/types.js';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-artifact-routes-'));

const [{ registerArtifactRoutes }, { getPackageArtifactStore }, { getAuthoringState }, { createProject }] = await Promise.all([
  import('../src/artifacts/routes.js'),
  import('../src/artifacts/runtime.js'),
  import('../src/authoring/state.js'),
  import('../src/projects/repo.js'),
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

  const replayed = await app.inject(acceptRequest);
  assert.equal(replayed.statusCode, 200, replayed.body);
  assert.equal(replayed.json().replayed, true);
  assert.equal(replayed.json().currentRevision, accepted.json().currentRevision);
});

test('候选修订接口不向其他项目所有者暴露待处理内容', async (t) => {
  const project = createProject({ owner: 'alice', name: '候选权限隔离' });
  const app = routeApp(t, 'bob');
  const response = await app.inject({
    method: 'GET', url: `/api/projects/${project.id}/candidate-revisions`,
  });
  assert.equal(response.statusCode, 404);
});
