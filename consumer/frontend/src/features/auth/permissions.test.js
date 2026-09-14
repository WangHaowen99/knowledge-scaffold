// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';

import { can } from './permissions.js';

test('global observability remains available to administrators', () => {
  assert.equal(can({ role: 'admin' }, 'observability.global'), true);
  assert.equal(can({ role: 'user' }, 'observability.global'), false);
});

test('removed workspace connection permissions stay unavailable', () => {
  assert.equal(can({ role: 'admin' }, 'ws.connections.write'), false);
  assert.equal(can({ role: 'admin' }, 'ws.connections.test'), false);
});

test('ordinary users can create workspaces but cannot mutate member workspace resources', () => {
  const user = { role: 'user' };
  assert.equal(can(user, 'workspace.create'), true);
  assert.equal(can(user, 'workspace.manage'), false);
  assert.equal(can(user, 'ws.kb.import'), false);
  assert.equal(can(user, 'ws.kb.activate'), false);
  assert.equal(can(user, 'ws.data_source.write'), false);
  assert.equal(can(user, 'ws.kb.write'), false);
  assert.equal(can(user, 'ws.data_source.test'), false);
});

test('workspace owners receive full knowledge and data-source capabilities in their workspace', () => {
  const owner = { role: 'user', workspace_roles: { owned: 'admin', joined: 'member' } };
  assert.equal(can(owner, 'ws.kb.import', { workspaceId: 'owned' }), true);
  assert.equal(can(owner, 'ws.kb.activate', { workspaceId: 'owned' }), true);
  assert.equal(can(owner, 'ws.data_source.write', { workspaceId: 'owned' }), true);
  assert.equal(can(owner, 'ws.kb.write', { workspaceId: 'owned' }), true);
  assert.equal(can(owner, 'ws.data_source.test', { workspaceId: 'owned' }), true);
  assert.equal(can(owner, 'ws.kb.import', { workspaceId: 'joined' }), false);
  assert.equal(can(owner, 'ws.kb.activate', { workspaceId: 'joined' }), false);
  assert.equal(can(owner, 'ws.data_source.write', { workspaceId: 'joined' }), false);
  assert.equal(can(owner, 'ws.kb.write', { workspaceId: 'joined' }), false);
  assert.equal(can(owner, 'ws.data_source.test', { workspaceId: 'joined' }), false);
});

test('administrators retain all knowledge and data-source capabilities', () => {
  const admin = { role: 'admin' };
  assert.equal(can(admin, 'ws.kb.import'), true);
  assert.equal(can(admin, 'ws.kb.activate'), true);
  assert.equal(can(admin, 'ws.data_source.write'), true);
  assert.equal(can(admin, 'ws.kb.write'), true);
  assert.equal(can(admin, 'ws.data_source.test'), true);
});
