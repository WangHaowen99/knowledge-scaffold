import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AUTH_CLEARED_EVENT,
  authFetch,
  clearAuthToken,
  getAuthToken,
  getRefreshToken,
  setCurrentWorkspaceId,
  setAuthToken,
} from './auth.js';

const createStorage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
};

const installWindow = () => {
  const target = new EventTarget();
  target.localStorage = createStorage();
  globalThis.window = target;
  return target;
};

test('concurrent 401 responses share one refresh request and retry once', async () => {
  installWindow();
  setAuthToken('old-access', 'old-refresh');
  let refreshCalls = 0;
  let protectedCalls = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') {
      refreshCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return new Response(JSON.stringify({ token: 'new-access', refresh_token: 'new-refresh' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    protectedCalls += 1;
    const auth = new Headers(options.headers).get('Authorization');
    return new Response('', { status: auth === 'Bearer new-access' ? 200 : 401 });
  };

  const [first, second] = await Promise.all([
    authFetch('/protected'),
    authFetch('/protected'),
  ]);

  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 4);
  assert.equal(getAuthToken(), 'new-access');
  assert.equal(getRefreshToken(), 'new-refresh');
});

test('failed refresh clears both credentials and emits the logout signal', async () => {
  const target = installWindow();
  setAuthToken('expired-access', 'expired-refresh');
  let cleared = 0;
  target.addEventListener(AUTH_CLEARED_EVENT, () => { cleared += 1; });
  globalThis.fetch = async (url) => (
    new Response('', { status: url === '/auth/refresh' ? 401 : 401 })
  );

  const response = await authFetch('/protected');

  assert.equal(response.status, 401);
  assert.equal(getAuthToken(), null);
  assert.equal(getRefreshToken(), null);
  assert.equal(cleared, 1);
});

test('a local login token removes a refresh token left by a previous remote session', () => {
  installWindow();
  setAuthToken('remote-access', 'remote-refresh');
  setAuthToken('local-access');

  assert.equal(getAuthToken(), 'local-access');
  assert.equal(getRefreshToken(), null);
});

test('401 retry keeps the workspace captured by the original request', async () => {
  installWindow();
  setAuthToken('old-access', 'old-refresh');
  setCurrentWorkspaceId('workspace-a');
  const protectedWorkspaces = [];

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') {
      setCurrentWorkspaceId('workspace-b');
      return new Response(JSON.stringify({ token: 'new-access', refresh_token: 'new-refresh' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    const headers = new Headers(options.headers);
    protectedWorkspaces.push(headers.get('X-Workspace-Id'));
    return new Response('', {
      status: headers.get('Authorization') === 'Bearer new-access' ? 200 : 401,
    });
  };

  const response = await authFetch('/protected');

  assert.equal(response.status, 200);
  assert.deepEqual(protectedWorkspaces, ['workspace-a', 'workspace-a']);
});

test('logout during refresh cannot restore stale credentials', async () => {
  installWindow();
  setAuthToken('old-access', 'old-refresh');
  let releaseRefresh;
  const refreshReady = new Promise((resolve) => { releaseRefresh = resolve; });
  let refreshStarted;
  const started = new Promise((resolve) => { refreshStarted = resolve; });

  globalThis.fetch = async (url) => {
    if (url !== '/auth/refresh') return new Response('', { status: 401 });
    refreshStarted();
    await refreshReady;
    return new Response(JSON.stringify({ token: 'stale-access', refresh_token: 'stale-refresh' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const request = authFetch('/protected');
  await started;
  clearAuthToken();
  releaseRefresh();
  const response = await request;

  assert.equal(response.status, 401);
  assert.equal(getAuthToken(), null);
  assert.equal(getRefreshToken(), null);
});

test('a new login during refresh cannot be cleared by the stale 401 request', async () => {
  installWindow();
  setAuthToken('old-token', 'old-refresh');
  let releaseRefresh;
  const refreshReady = new Promise((resolve) => { releaseRefresh = resolve; });
  let refreshStarted;
  const started = new Promise((resolve) => { refreshStarted = resolve; });
  globalThis.fetch = async (url) => {
    if (url === '/protected') return new Response('', { status: 401 });
    if (url === '/auth/refresh') {
      refreshStarted();
      await refreshReady;
      return new Response(JSON.stringify({
        token: 'stale-refreshed-token',
        refresh_token: 'stale-refreshed-refresh',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const request = authFetch('/protected');
  await started;
  setAuthToken('new-login-token', 'new-login-refresh');
  releaseRefresh();

  const response = await request;
  assert.equal(response.status, 401);
  assert.equal(getAuthToken(), 'new-login-token');
  assert.equal(getRefreshToken(), 'new-login-refresh');
});

test('a request from a new login cannot be cleared after joining an older failed refresh', async () => {
  installWindow();
  setAuthToken('old-token', 'old-refresh');
  let releaseRefresh;
  const refreshReady = new Promise((resolve) => { releaseRefresh = resolve; });
  let refreshStarted;
  const started = new Promise((resolve) => { refreshStarted = resolve; });
  let protectedCalls = 0;

  globalThis.fetch = async (url) => {
    if (url === '/auth/refresh') {
      refreshStarted();
      await refreshReady;
      return new Response('', { status: 401 });
    }
    if (url === '/protected') {
      protectedCalls += 1;
      return new Response('', { status: 401 });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const oldRequest = authFetch('/protected');
  await started;
  setAuthToken('new-login-token', 'new-login-refresh');
  const newRequest = authFetch('/protected');
  releaseRefresh();

  const [oldResponse, newResponse] = await Promise.all([oldRequest, newRequest]);
  assert.equal(oldResponse.status, 401);
  assert.equal(newResponse.status, 401);
  assert.equal(protectedCalls, 2);
  assert.equal(getAuthToken(), 'new-login-token');
  assert.equal(getRefreshToken(), 'new-login-refresh');
});
