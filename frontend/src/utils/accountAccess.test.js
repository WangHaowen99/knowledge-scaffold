import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getAccountUserAction,
  getVisibleAccountUsers,
} from './accountAccess.js';

const users = [
  { username: 'admin', role: 'admin' },
  { username: 'user1', role: 'user' },
  { username: 'user2', role: 'user' },
];

test('account mode shows only the current user', () => {
  const visible = getVisibleAccountUsers({
    mode: 'account',
    currentUser: users[1],
    users,
  });

  assert.deepEqual(visible, [users[1]]);
});

test('admin mode keeps all users but marks the current user as non-editable', () => {
  const visible = getVisibleAccountUsers({
    mode: 'admin',
    currentUser: users[0],
    users,
  });

  assert.deepEqual(visible, users);
  assert.equal(getAccountUserAction({ mode: 'admin', currentUser: users[0], targetUser: users[0] }), 'current_account');
  assert.equal(getAccountUserAction({ mode: 'admin', currentUser: users[0], targetUser: users[1] }), 'reset_password');
});

test('account mode treats the current user as self password change', () => {
  assert.equal(getAccountUserAction({ mode: 'account', currentUser: users[0], targetUser: users[0] }), 'change_password');
});
