import assert from 'node:assert/strict';
import test from 'node:test';

import { saveWorkspaceMembers, validateWorkspaceMember } from './api.js';


const response = (payload, { ok = true, status = 200 } = {}) => ({
  ok,
  status,
  json: async () => payload,
});


test('member validation uses the scoped endpoint and trims the username', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push({ url, options });
    return response({ valid: true, username: 'alice' });
  };

  const result = await validateWorkspaceMember('space / a', ' alice ', { request });

  assert.equal(result.username, 'alice');
  assert.equal(calls[0].url, '/agentic/workspaces/space%20%2F%20a/members/validate');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), { username: 'alice' });
});


test('member validation preserves backend identity error details', async () => {
  const request = async () => response({
    detail: '成员用户 ghost 不存在',
    code: 'WORKSPACE_MEMBER_NOT_FOUND',
    username: 'ghost',
    usernames: ['ghost'],
  }, { ok: false, status: 400 });

  await assert.rejects(
    () => validateWorkspaceMember('alpha', 'ghost', { request }),
    (error) => error.message === '成员用户 ghost 不存在'
      && error.status === 400
      && error.code === 'WORKSPACE_MEMBER_NOT_FOUND'
      && error.username === 'ghost'
      && error.usernames[0] === 'ghost',
  );
});


test('member save sends the complete replacement list and preserves outage code', async () => {
  const calls = [];
  const request = async (url, options) => {
    calls.push({ url, options });
    return response({
      detail: '认证服务暂不可用，无法校验工作空间用户',
      code: 'AUTH_DIRECTORY_UNAVAILABLE',
    }, { ok: false, status: 503 });
  };

  await assert.rejects(
    () => saveWorkspaceMembers('alpha', ['alice', 'bob'], { request }),
    (error) => error.status === 503 && error.code === 'AUTH_DIRECTORY_UNAVAILABLE',
  );
  assert.equal(calls[0].url, '/agentic/workspaces/alpha/members');
  assert.equal(calls[0].options.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[0].options.body), { members: ['alice', 'bob'] });
});


test('member save rejects non-array input without issuing a clear request', async () => {
  for (const members of [undefined, null, {}, 'alice', 1]) {
    let called = false;
    const request = async () => {
      called = true;
      return response({});
    };

    await assert.rejects(
      () => saveWorkspaceMembers('alpha', members, { request }),
      (error) => error instanceof TypeError && error.message === 'members 必须是数组',
    );
    assert.equal(called, false);
  }
});
