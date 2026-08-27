import { test } from 'node:test';
import assert from 'node:assert/strict';
import { BackgroundAuthSessionRegistry } from '../src/auth/background-session.js';
import {
  EvaluationAuthenticationError,
  EvaluationAuthSession,
} from '../src/evaluation/auth-session.js';

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
    (error) => error instanceof EvaluationAuthenticationError
      && /重新登录后再启动评测/u.test(error.message),
  );
});

test('重新登录不会被较早发起的续期响应覆盖', async () => {
  const now = 1_800_000_000_000;
  const expiring = unsignedJwt(Math.floor(now / 1000) + 10);
  const reLoginAccess = unsignedJwt(Math.floor(now / 1000) + 900);
  let finishRefresh!: (value: { token: string; refreshToken: string }) => void;
  const session = new EvaluationAuthSession({
    accessToken: expiring,
    refreshToken: 'refresh-before-login',
    now: () => now,
    refresh: () => new Promise((resolve) => { finishRefresh = resolve; }),
  });

  const refreshing = session.getAccessToken();
  await new Promise<void>((resolve) => setImmediate(resolve));
  session.updateCredentials(reLoginAccess, 'refresh-after-login');
  finishRefresh({ token: 'stale-rotated-access', refreshToken: 'stale-rotated-refresh' });

  assert.equal(await refreshing, reLoginAccess);
  assert.equal(await session.getAccessToken(), reLoginAccess);
});

test('后台会话注册表原地更新对象，使运行中的任务看到重新登录凭据', async () => {
  let now = 1_800_000_000_000;
  const registry = new BackgroundAuthSessionRegistry(() => now, 10_000);
  const session = registry.remember('modeler', 'access-before-login', 'refresh-before-login');

  const updated = registry.remember('modeler', 'access-after-login', 'refresh-after-login');
  assert.equal(updated, session);
  assert.equal(await session.getAccessToken(), 'access-after-login');

  now += 10_001;
  assert.equal(registry.get('modeler'), undefined);
});
