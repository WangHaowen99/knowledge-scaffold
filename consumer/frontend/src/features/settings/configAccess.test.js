import assert from 'node:assert/strict';
import test from 'node:test';

import {
  WORKSPACE_ACCESS_STATE,
  configSectionDisabled,
  configSectionRequiresWorkspace,
  resolveWorkspaceAccess,
} from './configAccess.js';

test('only L2 sections and personal memory require a workspace', () => {
  assert.equal(configSectionRequiresWorkspace('workspace', 'info'), true);
  assert.equal(configSectionRequiresWorkspace('workspace', 'mcp'), true);
  assert.equal(configSectionRequiresWorkspace('personal', 'memory'), true);
  assert.equal(configSectionRequiresWorkspace('personal', 'prefs'), false);
  assert.equal(configSectionRequiresWorkspace('personal', 'account'), false);
  assert.equal(configSectionRequiresWorkspace('global', 'wsmgmt'), false);
});

test('workspace access remains closed until the selected workspace is confirmed', () => {
  assert.equal(resolveWorkspaceAccess(), WORKSPACE_ACCESS_STATE.LOADING);
  assert.equal(resolveWorkspaceAccess({
    workspacesLoaded: true,
    workspacesLoading: true,
    workspaces: [],
  }), WORKSPACE_ACCESS_STATE.LOADING);
  assert.equal(resolveWorkspaceAccess({
    workspacesLoaded: true,
    workspacesError: 'network unavailable',
  }), WORKSPACE_ACCESS_STATE.ERROR);
  assert.equal(resolveWorkspaceAccess({
    workspacesLoaded: true,
    workspaces: [],
  }), WORKSPACE_ACCESS_STATE.EMPTY);
  assert.equal(resolveWorkspaceAccess({
    workspacesLoaded: true,
    workspaceId: 'missing',
    workspaces: [{ id: 'alpha' }],
  }), WORKSPACE_ACCESS_STATE.UNAVAILABLE);
  assert.equal(resolveWorkspaceAccess({
    workspaceDataReady: true,
    workspacesLoaded: true,
    workspaceId: 'alpha',
    workspaces: [{ id: 'alpha' }],
  }), WORKSPACE_ACCESS_STATE.READY);
});

test('zero-workspace state blocks scoped sections but leaves global and local personal settings open', () => {
  const access = WORKSPACE_ACCESS_STATE.EMPTY;
  assert.equal(configSectionDisabled('workspace', 'info', access), true);
  assert.equal(configSectionDisabled('personal', 'memory', access), true);
  assert.equal(configSectionDisabled('global', 'wsmgmt', access), false);
  assert.equal(configSectionDisabled('personal', 'prefs', access), false);
  assert.equal(configSectionDisabled('personal', 'account', access), false);
});
