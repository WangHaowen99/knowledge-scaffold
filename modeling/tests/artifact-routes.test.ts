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
