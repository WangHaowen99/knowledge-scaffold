import assert from 'node:assert/strict';
import test from 'node:test';
import { can, opsListenLabel, opsRoleCard, rolesOf } from '../src/state/roles.js';

const U = (role, extra = {}) => ({ username: 'u', role, auth_source: 'auth-server', ...extra });

test('rolesOf：总带 user；认 roles 数组；本地模式三种都有', () => {
  assert.deepEqual(rolesOf(null), []);
  assert.deepEqual(rolesOf(U('user')), ['user']);
  assert.deepEqual(rolesOf(U('ops')).sort(), ['ops', 'user']);
  assert.deepEqual(rolesOf(U('admin', { roles: ['user', 'admin', 'ops'] })).sort(), ['admin', 'ops', 'user']);
  assert.deepEqual(rolesOf({ username: 'local', role: 'admin', auth_source: 'local' }).sort(), ['admin', 'ops', 'user']);
});

test('运维面：ops 读写，admin 只读，user 不能看', () => {
  assert.equal(can(U('ops'), 'ops.read'), true);
  assert.equal(can(U('ops'), 'ops.write'), true);
  assert.equal(can(U('admin'), 'ops.read'), true);
  assert.equal(can(U('admin'), 'ops.write'), false);
  assert.equal(can(U('user'), 'ops.read'), false);
});

test('用户管理写只有 admin；回收站 ops 和 admin 都能操作', () => {
  assert.equal(can(U('admin'), 'users.write'), true);
  assert.equal(can(U('ops'), 'users.write'), false);
  assert.equal(can(U('ops'), 'users.read'), true);
  assert.equal(can(U('ops'), 'trash.write'), true);
  assert.equal(can(U('admin'), 'trash.write'), true);
  assert.equal(can(U('user'), 'trash.write'), false);
});

test('管理员专属：系统设置、插件上传', () => {
  assert.equal(can(U('admin'), 'admin'), true);
  assert.equal(can(U('ops'), 'admin'), false);
  assert.equal(can(U('user'), 'admin'), false);
  assert.equal(can(U('user'), 'unknown'), false);
});

test('左栏身份卡按角色给说法', () => {
  assert.equal(opsRoleCard(U('ops')).label, '运维');
  assert.equal(opsRoleCard(U('ops')).text, null);
  assert.equal(opsRoleCard(U('admin')).role, 'admin');
  assert.equal(opsRoleCard(U('admin')).text, '运维面只读');
  assert.equal(opsRoleCard(U('user')), null);
  assert.equal(opsRoleCard({ username: 'local', role: 'admin', auth_source: 'local' }).label, '本地模式');
});

test('运维监听端口：同源时为空', () => {
  assert.equal(opsListenLabel(''), null);
  assert.equal(opsListenLabel('http://127.0.0.1:8101'), ':8101');
  assert.equal(opsListenLabel('https://ops.example.com'), 'ops.example.com');
  assert.equal(opsListenLabel('not a url'), null);
});

test('isOwnedMeta：admin、ops 拿到的 ProjectMetaView 没有 package_name，不是自己的项目（V 阶段：外壳照样拉 PL-12、IN-01、FL-01、CS-06，控制台 4 个 403）', async () => {
  const { isOwnedMeta } = await import('../src/state/roles.js');
  assert.equal(isOwnedMeta(null), false);
  assert.equal(isOwnedMeta({ id: 'p1', name: 'x', owner: 'a', stage_label: '建模中', build_mode: 'L2' }), false);
  assert.equal(isOwnedMeta({ id: 'p1', name: 'x', package_name: 'p1', goal: 'g' }), true);
});
