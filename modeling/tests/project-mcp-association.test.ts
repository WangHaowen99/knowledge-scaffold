import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

process.env.DATA_DIR = mkdtempSync(join(tmpdir(), 'modeling-mcp-association-'));

const {
  createProject,
  getProject,
  getProjectByExternalId,
  replaceProjectMembers,
  listProjectMembers,
  canSyncProjectMembers,
} = await import('../src/projects/repo.js');

test('项目 external_id 可稳定查找并作为幂等关联键', () => {
  const externalId = 'onto:pj_idempotent';
  const project = createProject({
    owner: 'alice',
    name: '幂等项目',
    externalId,
    runtime: 'agent',
  });

  assert.equal(getProjectByExternalId(externalId)?.id, project.id);
  assert.equal(getProject(project.id)?.externalId, externalId);
});

test('成员同步只允许项目 owner 或系统管理员', () => {
  const project = { owner: 'alice' };
  assert.equal(canSyncProjectMembers(project, 'alice', 'modeler'), true);
  assert.equal(canSyncProjectMembers(project, 'admin', 'admin'), true);
  assert.equal(canSyncProjectMembers(project, 'bob', 'modeler'), false);
  assert.equal(canSyncProjectMembers(project, 'bob', 'viewer'), false);
  assert.equal(canSyncProjectMembers(null, 'admin', 'admin'), false);
});

test('角色镜像可完整替换并保留 owner 单独语义', () => {
  const project = createProject({ owner: 'alice', name: '角色镜像' });
  const members = replaceProjectMembers(project.id, project.owner, [
    { username: 'bob', role: 'modeler' },
    { username: 'carol', role: 'viewer' },
  ]);

  assert.deepEqual(
    members.map(({ username, role }) => ({ username, role })),
    [
      { username: 'alice', role: 'owner' },
      { username: 'bob', role: 'modeler' },
      { username: 'carol', role: 'viewer' },
    ],
  );
  assert.equal(listProjectMembers(project.id).length, 3);
  assert.throws(
    () => replaceProjectMembers(project.id, 'bob', []),
    /只有项目负责人可以管理成员/,
  );
});
