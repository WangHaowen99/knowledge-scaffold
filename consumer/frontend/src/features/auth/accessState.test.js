import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canLoadProtectedData,
  canLoadWorkspaceData,
  requiresWorkspaceData,
} from './accessState.js';

test('protected data loads for every authenticated user', () => {
  assert.equal(canLoadProtectedData('checking', null), false);
  assert.equal(canLoadProtectedData('anonymous', null), false);
  assert.equal(canLoadProtectedData('authenticated', null), false);
  assert.equal(canLoadProtectedData('authenticated', { password_change_required: true }), true);
  assert.equal(canLoadProtectedData('authenticated', { password_change_required: false }), true);
});

test('workspace data waits for a loaded and valid workspace selection', () => {
  const workspaces = [{ id: 'alpha' }];
  const loaded = { loaded: true };
  assert.equal(canLoadWorkspaceData(false, loaded, 'alpha', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, { loaded: false }, 'alpha', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, { loaded: true, loading: true }, 'alpha', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, { loaded: true, error: 'failed' }, 'alpha', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, loaded, '', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, loaded, 'stale', workspaces), false);
  assert.equal(canLoadWorkspaceData(true, loaded, 'alpha', workspaces), true);
});

test('workspace-scoped surfaces stay blocked while global surfaces remain available', () => {
  assert.equal(requiresWorkspaceData('chat'), true);
  assert.equal(requiresWorkspaceData('kb'), true);
  assert.equal(requiresWorkspaceData('solver'), true);
  assert.equal(requiresWorkspaceData('observability', 'session'), true);
  assert.equal(requiresWorkspaceData('observability', 'global'), false);
  assert.equal(requiresWorkspaceData('config'), false);
});
