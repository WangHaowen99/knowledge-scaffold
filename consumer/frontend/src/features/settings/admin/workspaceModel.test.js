import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canEditWorkspace,
  chooseWorkspaceFallback,
  classifyWorkspaceDeleteResult,
  filterAndPaginateWorkspaces,
  normalizeWorkspaceList,
} from './workspaceModel.js';

test('workspace payload is normalized then searched and paged on the client', () => {
  const normalized = normalizeWorkspaceList({
    default_workspace_id: 'beta',
    workspaces: [
      { id: 'alpha', name: 'Alpha', owner: 'alice', members: ['bob'] },
      { id: 'beta', name: 'Beta', description: 'Finance', is_default: true },
      { id: 'gamma', name: 'Gamma', description: 'Factory' },
    ],
  });
  assert.equal(normalized.defaultWorkspaceId, 'beta');
  assert.deepEqual(filterAndPaginateWorkspaces(normalized.workspaces, {
    query: 'factory', page: 1, pageSize: 1,
  }).items.map((item) => item.id), ['gamma']);
  const second = filterAndPaginateWorkspaces(normalized.workspaces, { page: 2, pageSize: 2 });
  assert.equal(second.pageCount, 2);
  assert.deepEqual(second.items.map((item) => item.id), ['gamma']);
});

test('workspace edit follows backend global-admin and owner authorization', () => {
  assert.equal(canEditWorkspace({ username: 'alice', role: 'user' }, { owner: 'alice' }), true);
  assert.equal(canEditWorkspace({ username: 'bob', role: 'user' }, { owner: 'alice', role: 'member' }), false);
  assert.equal(canEditWorkspace({ username: 'root', role: 'admin' }, { owner: 'alice' }), true);
});

test('deleting the current workspace falls back to default then first remaining workspace', () => {
  assert.equal(chooseWorkspaceFallback({
    deletedWorkspaceId: 'alpha', currentWorkspaceId: 'alpha', defaultWorkspaceId: 'beta',
    workspaces: [{ id: 'beta' }, { id: 'gamma' }],
  }), 'beta');
  assert.equal(chooseWorkspaceFallback({
    deletedWorkspaceId: 'alpha', currentWorkspaceId: 'alpha', defaultWorkspaceId: 'missing',
    workspaces: [{ id: 'gamma' }],
  }), 'gamma');
});

test('workspace deletion exposes default protection and 409 conflicts distinctly', () => {
  assert.equal(classifyWorkspaceDeleteResult(400, {
    detail: '默认工作空间不可删除，请先切换默认工作空间',
  }).kind, 'default_protected');
  assert.equal(classifyWorkspaceDeleteResult(409, { detail: '资源仍被引用' }).kind, 'conflict');
  assert.equal(classifyWorkspaceDeleteResult(204).kind, 'deleted');
});
