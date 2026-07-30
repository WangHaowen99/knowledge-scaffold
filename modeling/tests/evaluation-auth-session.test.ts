import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EvaluationAuthSession } from '../src/evaluation/auth-session.js';

function unsignedJwt(expiresAtSeconds: number): string {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
  return `${encode({ alg: 'none' })}.${encode({ exp: expiresAtSeconds })}.signature`;
}

test('长任务在访问令牌临近过期时续签并保存轮换后的 refresh token', async () => {
  const now = 1_800_000_000_000;
  const expiring = unsignedJwt(Math.floor(now / 1000) + 30);
  const fresh = unsignedJwt(Math.floor(now / 1000) + 900);
  const calls: string[] = [];
  const session = new EvaluationAuthSession({
    accessToken: expiring,
    refreshToken: 'refresh-1',
    now: () => now,
    refresh: async (token) => {
      calls.push(token);
      return calls.length === 1
        ? { token: fresh, refreshToken: 'refresh-2', expiresIn: 900 }
        : { token: 'newer-access', refreshToken: 'refresh-3', expiresIn: 900 };
    },
  });

  assert.equal(await session.getAccessToken(), fresh);
  assert.equal(await session.refreshAfterUnauthorized(), 'newer-access');
  assert.deepEqual(calls, ['refresh-1', 'refresh-2']);
});

test('长任务收到 401 且没有续签凭据时给出可操作错误', async () => {
  const session = new EvaluationAuthSession({ accessToken: 'access-only' });
  await assert.rejects(
    session.refreshAfterUnauthorized(),
    /重新登录后再启动评测/,
  );
});
