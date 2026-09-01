import assert from 'node:assert/strict';
import test from 'node:test';
import * as accessState from './accessState.js';
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

test('a valid current-account stored selection wins over the preferred workspace', () => {
  assert.equal(typeof accessState.resolveWorkspaceSelection, 'function');
  assert.equal(accessState.resolveWorkspaceSelection({
    workspaces: [{ id: 'preferred' }, { id: 'remembered' }],
    storedWorkspaceId: 'remembered',
    preferredWorkspaceId: 'preferred',
    defaultWorkspaceId: 'preferred',
  }), 'remembered');
});

test('an unavailable stored selection falls back to the preferred workspace', () => {
  assert.equal(typeof accessState.resolveWorkspaceSelection, 'function');
  assert.equal(accessState.resolveWorkspaceSelection({
    workspaces: [{ id: 'global-default' }, { id: 'personal-preferred' }],
    storedWorkspaceId: 'removed',
    preferredWorkspaceId: 'personal-preferred',
    defaultWorkspaceId: 'global-default',
  }), 'personal-preferred');
});

test('unavailable stored and preferred selections fall back to a visible default', () => {
  assert.equal(typeof accessState.resolveWorkspaceSelection, 'function');
  assert.equal(accessState.resolveWorkspaceSelection({
    workspaces: [{ id: 'first-visible' }, { id: 'global-default' }],
    storedWorkspaceId: 'removed',
    preferredWorkspaceId: 'hidden',
    defaultWorkspaceId: 'global-default',
  }), 'global-default');
});

test('an unavailable default falls back to the first visible workspace', () => {
  assert.equal(typeof accessState.resolveWorkspaceSelection, 'function');
  assert.equal(accessState.resolveWorkspaceSelection({
    workspaces: [{ id: 'first-visible' }, { id: 'second-visible' }],
    storedWorkspaceId: 'removed',
    preferredWorkspaceId: 'hidden',
    defaultWorkspaceId: 'unavailable-default',
  }), 'first-visible');
});

test('an empty visible workspace list resolves to no selection', () => {
  assert.equal(typeof accessState.resolveWorkspaceSelection, 'function');
  assert.equal(accessState.resolveWorkspaceSelection({
    workspaces: [],
    storedWorkspaceId: 'remembered',
    preferredWorkspaceId: 'preferred',
    defaultWorkspaceId: 'global-default',
  }), '');
});
