import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AUTH_CLEARED_EVENT,
  authFetch,
  getAuthToken,
  getRefreshToken,
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
