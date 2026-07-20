import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  canCreateWorkspace,
  canDeleteWorkspace,
  canEditWorkspace,
  canManageWorkspaceCollection,
  chooseWorkspaceFallback,
  classifyWorkspaceDeleteResult,
  filterAndPaginateWorkspaces,
  normalizeWorkspaceList,
} from './workspaceModel.js';

const currentDir = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(join(currentDir, 'WorkspaceAdminPanel.jsx'), 'utf8');
const appShellSource = readFileSync(join(currentDir, '../../../app/AppShell.jsx'), 'utf8');

test('authenticated users can create workspaces while collection management stays global-admin only', () => {
  assert.equal(canCreateWorkspace({ username: 'alice', role: 'user' }), true);
  assert.equal(canCreateWorkspace({ username: 'root', role: 'admin' }), true);
  assert.equal(canCreateWorkspace(null), false);
  assert.equal(canManageWorkspaceCollection({ username: 'alice', role: 'user' }), false);
  assert.equal(canManageWorkspaceCollection({ username: 'root', role: 'admin' }), true);
});

test('workspace creation is exposed to ordinary users without exposing global default controls', () => {
  assert.equal(panelSource.includes('const canCreate = canCreateWorkspace(currentUser);'), true);
  assert.equal(panelSource.includes('{canCreate && <button className="btn btn-p"'), true);
  assert.equal(panelSource.includes('canSetDefault={canManageCollection}'), true);
  assert.equal(panelSource.includes('{isCreate && canSetDefault && ('), true);
  assert.equal(panelSource.includes('const canDelete = canDeleteWorkspace(currentUser, workspace);'), true);
  assert.equal(panelSource.includes('{canDelete && <button className="btn btn-sm btn-danger"'), true);
  assert.equal(appShellSource.includes("const canCreateWorkspace = can('workspace.create');"), true);
  assert.equal(appShellSource.includes('action={canCreateWorkspace ? ('), true);
});

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

test('workspace deletion follows backend global-admin and owner authorization', () => {
  assert.equal(canDeleteWorkspace({ username: 'alice', role: 'user' }, { owner: 'alice' }), true);
  assert.equal(canDeleteWorkspace({ username: 'bob', role: 'user' }, { owner: 'alice', role: 'member' }), false);
  assert.equal(canDeleteWorkspace({ username: 'root', role: 'admin' }, { owner: 'alice' }), true);
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
