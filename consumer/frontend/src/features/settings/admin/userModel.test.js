import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canManageUserAccounts,
  classifyUserMutation,
  normalizeUserList,
  userActionPolicy,
} from './userModel.js';

test('user management requires both admin identity and capability', () => {
  assert.equal(canManageUserAccounts({ username: 'root', role: 'admin' }, true), true);
  assert.equal(canManageUserAccounts({ username: 'root', role: 'admin' }, false), false);
  assert.equal(canManageUserAccounts({ username: 'alice', role: 'user' }, true), false);
});

test('user list retains only public account fields', () => {
  const users = normalizeUserList({ users: [{
    username: 'alice', role: 'user', password_change_required: true, password: 'must-not-leak',
  }] });
  assert.deepEqual(users, [{ username: 'alice', role: 'user', password_change_required: true }]);
});

test('current account and bootstrap admin deletion are protected', () => {
  const current = { username: 'root', role: 'admin' };
  assert.equal(userActionPolicy(current, { username: 'root' }).canDelete, false);
  assert.equal(userActionPolicy(current, { username: 'admin' }).canDelete, false);
  assert.equal(userActionPolicy(current, { username: 'alice' }).canDelete, true);
  assert.equal(userActionPolicy(current, { username: 'root' }).canResetPassword, false);
});

test('202 reconciliation and 409 owner transfer remain separate terminal states', () => {
  const pending = classifyUserMutation(202, {
    success: true, reconciliation_required: true, reconciliation_id: 'cleanup-1',
  }, 'delete');
  assert.equal(pending.kind, 'reconciliation_pending');
  assert.equal(pending.reconciliationId, 'cleanup-1');

  const owner = classifyUserMutation(409, {
    code: 'WORKSPACE_OWNER_TRANSFER_REQUIRED', workspace_ids: ['alpha', 'beta'],
  }, 'delete');
  assert.equal(owner.kind, 'owner_transfer_required');
  assert.deepEqual(owner.workspaceIds, ['alpha', 'beta']);
});

test('password policy violations are preserved for field-level feedback', () => {
  const result = classifyUserMutation(400, {
    code: 'PASSWORD_POLICY_FAILED', error: '密码复杂度不满足要求',
    violations: ['min_length', 'character_classes'],
  }, 'reset');
  assert.equal(result.kind, 'password_policy');
  assert.deepEqual(result.violations, ['min_length', 'character_classes']);
});
