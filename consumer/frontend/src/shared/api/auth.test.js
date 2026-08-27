import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AUTH_CLEARED_EVENT,
  AUTH_STATE_STORAGE_KEY,
  authFetch,
  clearAuthToken,
  getAuthToken,
  getRefreshToken,
  loginWithPassword,
  logoutAuthSession,
  migrateLegacyAuthState,
  setCurrentWorkspaceId,
  setAuthToken,
  synchronizeAuthStateFromStorage,
} from './auth.js';

// Node 24 exposes an experimental global Web Locks implementation. These
// tests intentionally exercise the storage-restricted fallback used when the
// browser-owned window has no LockManager, so isolate that environment here.
const nodeNavigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
if (nodeNavigatorDescriptor?.configurable) {
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    enumerable: nodeNavigatorDescriptor.enumerable,
    value: undefined,
  });
  test.after(() => {
    Object.defineProperty(globalThis, 'navigator', nodeNavigatorDescriptor);
  });
}

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

const jwtWithExpiry = (expiresAtSeconds) => {
  const payload = btoa(JSON.stringify({ exp: expiresAtSeconds }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
  return `header.${payload}.signature`;
};

test('an expiring access token refreshes before the protected request', async () => {
  installWindow();
  await setAuthToken(jwtWithExpiry(Math.floor(Date.now() / 1000) - 1), 'old-refresh');
  let refreshCalls = 0;
  let protectedCalls = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') {
      refreshCalls += 1;
      return new Response(JSON.stringify({ token: 'new-access', refresh_token: 'new-refresh' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    protectedCalls += 1;
    return new Response('', {
      status: new Headers(options.headers).get('Authorization') === 'Bearer new-access' ? 200 : 401,
    });
  };

  const response = await authFetch('/protected');

  assert.equal(response.status, 200);
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 1);
});

test('concurrent requests with an expiring token share the proactive refresh', async () => {
  installWindow();
  await setAuthToken(jwtWithExpiry(Math.floor(Date.now() / 1000) + 5), 'old-refresh');
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
    return new Response('', {
      status: new Headers(options.headers).get('Authorization') === 'Bearer new-access' ? 200 : 401,
    });
  };

  const responses = await Promise.all([authFetch('/first'), authFetch('/second')]);

  assert.deepEqual(responses.map((response) => response.status), [200, 200]);
  assert.equal(refreshCalls, 1);
  assert.equal(protectedCalls, 2);
});

test('an account switch during proactive refresh aborts the original protected request', async () => {
  installWindow();
  await setAuthToken(jwtWithExpiry(Math.floor(Date.now() / 1000) - 1), 'old-refresh');
  let releaseRefresh;
  const refreshReady = new Promise((resolve) => { releaseRefresh = resolve; });
  let refreshStarted;
  const started = new Promise((resolve) => { refreshStarted = resolve; });
  let protectedCalls = 0;

  globalThis.fetch = async (url) => {
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
    protectedCalls += 1;
    return new Response('', { status: 200 });
  };

  const request = authFetch('/protected', { method: 'POST' });
  await started;
  await setAuthToken('new-login-token', 'new-login-refresh');
  releaseRefresh();

  const response = await request;
  assert.equal(response.status, 401);
  assert.equal(protectedCalls, 0);
  assert.equal(getAuthToken(), 'new-login-token');
  assert.equal(getRefreshToken(), 'new-login-refresh');
});

test('an account switch discards an old successful response before its body is exposed', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  let releaseRequest;
  const requestReady = new Promise((resolve) => { releaseRequest = resolve; });
  let requestStarted;
  const started = new Promise((resolve) => { requestStarted = resolve; });

  globalThis.fetch = async (url) => {
    assert.equal(url, '/protected');
    requestStarted();
    await requestReady;
    return new Response(JSON.stringify({ account: 'old-account' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const request = authFetch('/protected');
  await started;
  await setAuthToken('new-login-token', 'new-login-refresh');
  releaseRequest();

  const response = await request;
  assert.equal(response.status, 401);
  assert.equal(await response.text(), '');
  assert.equal(getAuthToken(), 'new-login-token');
  assert.equal(getRefreshToken(), 'new-login-refresh');
});

test('an account switch after response headers prevents reading the old response body', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  let releaseBody;
  const bodyReady = new Promise((resolve) => { releaseBody = resolve; });

  globalThis.fetch = async () => new Response(new ReadableStream({
    async pull(controller) {
      await bodyReady;
      controller.enqueue(new TextEncoder().encode(JSON.stringify({ secret: 'old-only' })));
      controller.close();
    },
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });

  const response = await authFetch('/protected');
  await setAuthToken('new-access', 'new-refresh');
  releaseBody();

  await assert.rejects(response.json(), { name: 'AuthStateChangedError' });
  assert.equal(getAuthToken(), 'new-access');
});

test('a guarded streaming response stops on an account switch without buffering the stream', async () => {
  installWindow();
  await setAuthToken('stream-access', 'stream-refresh');
  let sourceController;
  globalThis.fetch = async () => new Response(new ReadableStream({
    start(controller) {
      sourceController = controller;
    },
  }), { status: 200 });

  const response = await authFetch('/agentic/runs/run-1/events');
  const reader = response.body.getReader();
  sourceController.enqueue(new TextEncoder().encode('event: first\n\n'));
  const first = await reader.read();
  assert.equal(new TextDecoder().decode(first.value), 'event: first\n\n');

  const pendingRead = reader.read();
  await Promise.resolve();
  await setAuthToken('other-access', 'other-refresh');
  const guardTimeout = new Promise((resolve, reject) => {
    void resolve;
    setTimeout(() => reject(new Error('guard stayed pending')), 75);
  });
  await assert.rejects(Promise.race([pendingRead, guardTimeout]), {
    name: 'AuthStateChangedError',
  });
});

test('legacy access and refresh keys migrate atomically into the auth envelope', async () => {
  const target = installWindow();
  target.localStorage.setItem('onto_agent_token', 'legacy-access');
  target.localStorage.setItem('onto_agent_refresh_token', 'legacy-refresh');

  const result = await migrateLegacyAuthState();
  const envelope = JSON.parse(target.localStorage.getItem(AUTH_STATE_STORAGE_KEY));

  assert.equal(result.migrated, true);
  assert.equal(envelope.accessToken, 'legacy-access');
  assert.equal(envelope.refreshToken, 'legacy-refresh');
  assert.notEqual(envelope.sessionId, 'legacy-session');
  assert.equal(target.localStorage.getItem('onto_agent_token'), null);
  assert.equal(target.localStorage.getItem('onto_agent_refresh_token'), null);
  assert.equal(getAuthToken(), 'legacy-access');
});

test('a legacy login from an old tab replaces the previous envelope during migration', async () => {
  const target = installWindow();
  await setAuthToken('current-access', 'current-refresh');
  target.localStorage.setItem('onto_agent_token', 'old-tab-access');
  target.localStorage.setItem('onto_agent_refresh_token', 'old-tab-refresh');

  await migrateLegacyAuthState();

  assert.equal(getAuthToken(), 'old-tab-access');
  assert.equal(getRefreshToken(), 'old-tab-refresh');
  assert.equal(target.localStorage.getItem('onto_agent_token'), null);
  assert.equal(target.localStorage.getItem('onto_agent_refresh_token'), null);
});

test('logout revoke and a new login are serialized before the new credentials commit', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  const sequence = [];
  let releaseLogout;
  const logoutReady = new Promise((resolve) => { releaseLogout = resolve; });
  let logoutStarted;
  const started = new Promise((resolve) => { logoutStarted = resolve; });

  globalThis.fetch = async (url) => {
    if (url === '/auth/logout') {
      sequence.push('logout-start');
      logoutStarted();
      await logoutReady;
      sequence.push('logout-end');
      return new Response('', { status: 200 });
    }
    if (url === '/auth/login') {
      sequence.push('login');
      return new Response(JSON.stringify({
        token: 'new-access',
        refresh_token: 'new-refresh',
        user: { username: 'same-user' },
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const logout = logoutAuthSession();
  await started;
  const login = loginWithPassword('same-user', 'password');
  await Promise.resolve();
  assert.deepEqual(sequence, ['logout-start']);

  releaseLogout();
  const [logoutResult, loginResult] = await Promise.all([logout, login]);

  assert.equal(logoutResult.cleared, true);
  assert.equal(loginResult.ok, true);
  assert.deepEqual(sequence, ['logout-start', 'logout-end', 'login']);
  assert.equal(getAuthToken(), 'new-access');
  assert.equal(getRefreshToken(), 'new-refresh');
});

test('a stalled login times out and releases the credential lock for logout', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  const nativeSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, delay, ...args) => nativeSetTimeout(
    callback,
    delay === 10_000 ? 10 : delay,
    ...args,
  );
  let loginStarted;
  const started = new Promise((resolve) => { loginStarted = resolve; });
  globalThis.fetch = async (url) => {
    if (url === '/auth/login') {
      loginStarted();
      return new Promise(() => {});
    }
    if (url === '/auth/logout') return new Response('', { status: 200 });
    throw new Error(`Unexpected URL: ${url}`);
  };

  try {
    const login = loginWithPassword('same-user', 'password');
    await started;
    const logout = logoutAuthSession();
    await assert.rejects(login, { name: 'AuthRequestTimeoutError' });
    const logoutResult = await logout;
    assert.equal(logoutResult.cleared, true);
    assert.equal(getAuthToken(), null);
  } finally {
    globalThis.setTimeout = nativeSetTimeout;
  }
});

test('logout preserves a new envelope written by a client that bypasses the lock', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  let releaseLogout;
  const logoutReady = new Promise((resolve) => { releaseLogout = resolve; });
  let logoutStarted;
  const started = new Promise((resolve) => { logoutStarted = resolve; });
  globalThis.fetch = async (url) => {
    assert.equal(url, '/auth/logout');
    logoutStarted();
    await logoutReady;
    return new Response('', { status: 200 });
  };

  const logout = logoutAuthSession();
  await started;
  window.localStorage.setItem(AUTH_STATE_STORAGE_KEY, JSON.stringify({
    accessToken: 'bypass-access',
    refreshToken: 'bypass-refresh',
    sessionId: 'bypass-session',
    tokenVersion: 0,
  }));
  releaseLogout();
  const result = await logout;

  assert.equal(result.cleared, false);
  assert.equal(result.state.accessToken, 'bypass-access');
  assert.equal(getAuthToken(), 'bypass-access');
});

test('logout wins over an unmarked legacy login written while revoke is in flight', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  let releaseLogout;
  const logoutReady = new Promise((resolve) => { releaseLogout = resolve; });
  let logoutStarted;
  const started = new Promise((resolve) => { logoutStarted = resolve; });
  globalThis.fetch = async () => {
    logoutStarted();
    await logoutReady;
    return new Response('', { status: 200 });
  };

  const logout = logoutAuthSession();
  await started;
  window.localStorage.setItem('onto_agent_token', 'legacy-new-access');
  window.localStorage.setItem('onto_agent_refresh_token', 'legacy-new-refresh');
  releaseLogout();
  const result = await logout;

  assert.equal(result.cleared, true);
  assert.equal(result.state.accessToken, null);
  assert.equal(getAuthToken(), null);
  assert.equal(window.localStorage.getItem('onto_agent_token'), null);
});

test('logout wins over a same-session refresh committed while revoke is in flight', async () => {
  installWindow();
  const initialState = await setAuthToken('old-access', 'old-refresh');
  let releaseLogout;
  const logoutReady = new Promise((resolve) => { releaseLogout = resolve; });
  let logoutStarted;
  const started = new Promise((resolve) => { logoutStarted = resolve; });
  globalThis.fetch = async () => {
    logoutStarted();
    await logoutReady;
    return new Response('', { status: 200 });
  };

  const logout = logoutAuthSession();
  await started;
  window.localStorage.setItem(AUTH_STATE_STORAGE_KEY, JSON.stringify({
    accessToken: 'refreshed-access',
    refreshToken: 'refreshed-refresh',
    sessionId: initialState.sessionId,
    tokenVersion: initialState.tokenVersion + 1,
  }));
  releaseLogout();
  const result = await logout;

  assert.equal(result.cleared, true);
  assert.equal(getAuthToken(), null);
});

test('a direct cross-tab credential switch never refreshes or replays an old POST', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
  let releaseRequest;
  const requestReady = new Promise((resolve) => { releaseRequest = resolve; });
  let requestStarted;
  const started = new Promise((resolve) => { requestStarted = resolve; });
  const authorizations = [];
  let refreshCalls = 0;

  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') {
      refreshCalls += 1;
      return new Response('', { status: 500 });
    }
    authorizations.push(new Headers(options.headers).get('Authorization'));
    requestStarted();
    await requestReady;
    return new Response('', { status: 401 });
  };

  const request = authFetch('/protected', { method: 'POST', body: 'old-account-action' });
  await started;
  // Simulate another tab writing the shared token keys directly. The managed
  // session/version markers intentionally remain unchanged.
  window.localStorage.setItem(AUTH_STATE_STORAGE_KEY, JSON.stringify({
    accessToken: 'new-tab-access',
    refreshToken: 'new-tab-refresh',
    sessionId: 'new-tab-session',
    tokenVersion: 0,
  }));
  const synchronized = synchronizeAuthStateFromStorage();
  assert.equal(synchronized.identityChanged, true);
  releaseRequest();

  const response = await request;
  assert.equal(response.status, 401);
  assert.equal(refreshCalls, 0);
  assert.deepEqual(authorizations, ['Bearer old-access']);
  assert.equal(getAuthToken(), 'new-tab-access');
  assert.equal(getRefreshToken(), 'new-tab-refresh');
});

test('a transient proactive refresh failure keeps a still-valid access token usable', async () => {
  installWindow();
  const accessToken = jwtWithExpiry(Math.floor(Date.now() / 1000) + 5);
  await setAuthToken(accessToken, 'old-refresh');
  let protectedCalls = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') return new Response('', { status: 503 });
    protectedCalls += 1;
    return new Response('', {
      status: new Headers(options.headers).get('Authorization') === `Bearer ${accessToken}` ? 200 : 401,
    });
  };

  const response = await authFetch('/protected');

  assert.equal(response.status, 200);
  assert.equal(protectedCalls, 1);
  assert.equal(getAuthToken(), accessToken);
  assert.equal(getRefreshToken(), 'old-refresh');
});

test('a proactive refresh network error still attempts the protected request', async () => {
  installWindow();
  const accessToken = jwtWithExpiry(Math.floor(Date.now() / 1000) + 5);
  await setAuthToken(accessToken, 'old-refresh');
  let protectedCalls = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') throw new TypeError('network unavailable');
    protectedCalls += 1;
    return new Response('', {
      status: new Headers(options.headers).get('Authorization') === `Bearer ${accessToken}` ? 200 : 401,
    });
  };

  const response = await authFetch('/protected');

  assert.equal(response.status, 200);
  assert.equal(protectedCalls, 1);
  assert.equal(getAuthToken(), accessToken);
  assert.equal(getRefreshToken(), 'old-refresh');
});

test('concurrent 401 responses share one refresh request and retry once', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
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
  await setAuthToken('expired-access', 'expired-refresh');
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

test('a local login token removes a refresh token left by a previous remote session', async () => {
  installWindow();
  await setAuthToken('remote-access', 'remote-refresh');
  await setAuthToken('local-access');

  assert.equal(getAuthToken(), 'local-access');
  assert.equal(getRefreshToken(), null);
});

test('401 retry keeps the workspace captured by the original request', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
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

test('another tab cannot redirect this tab request to a different workspace', async () => {
  installWindow();
  await setAuthToken('account-a-access', 'account-a-refresh');
  setCurrentWorkspaceId('workspace-a');
  let capturedWorkspace = null;
  globalThis.fetch = async (url, options = {}) => {
    assert.equal(url, '/protected');
    capturedWorkspace = new Headers(options.headers).get('X-Workspace-Id');
    return new Response('', { status: 200 });
  };

  // A different tab may update the persistence hint, but this tab keeps the
  // workspace snapshot that its rendered UI and caches were built for.
  window.localStorage.setItem('onto_agent_workspace_id', 'workspace-b');
  const response = await authFetch('/protected', { method: 'POST' });

  assert.equal(response.status, 200);
  assert.equal(capturedWorkspace, 'workspace-a');
});

test('logout during refresh cannot restore stale credentials', async () => {
  installWindow();
  await setAuthToken('old-access', 'old-refresh');
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
  await clearAuthToken();
  releaseRefresh();
  const response = await request;

  assert.equal(response.status, 401);
  assert.equal(getAuthToken(), null);
  assert.equal(getRefreshToken(), null);
});

test('a new login during refresh cannot be cleared by the stale 401 request', async () => {
  installWindow();
  await setAuthToken('old-token', 'old-refresh');
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
  await setAuthToken('new-login-token', 'new-login-refresh');
  releaseRefresh();

  const response = await request;
  assert.equal(response.status, 401);
  assert.equal(getAuthToken(), 'new-login-token');
  assert.equal(getRefreshToken(), 'new-login-refresh');
});

test('a request from a new login uses its own refresh and survives an older failed refresh', async () => {
  installWindow();
  await setAuthToken('old-token', 'old-refresh');
  let releaseRefresh;
  const refreshReady = new Promise((resolve) => { releaseRefresh = resolve; });
  let refreshStarted;
  const started = new Promise((resolve) => { refreshStarted = resolve; });
  let protectedCalls = 0;

  const refreshTokens = [];
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/auth/refresh') {
      const refreshToken = JSON.parse(options.body).refresh_token;
      refreshTokens.push(refreshToken);
      if (refreshToken === 'old-refresh') {
        refreshStarted();
        await refreshReady;
        return new Response('', { status: 401 });
      }
      return new Response(JSON.stringify({
        token: 'new-refreshed-token',
        refresh_token: 'new-refreshed-refresh',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    if (url === '/protected') {
      protectedCalls += 1;
      const authorization = new Headers(options.headers).get('Authorization');
      return new Response('', { status: authorization === 'Bearer new-refreshed-token' ? 200 : 401 });
    }
    throw new Error(`Unexpected URL: ${url}`);
  };

  const oldRequest = authFetch('/protected');
  await started;
  await setAuthToken('new-login-token', 'new-login-refresh');
  const newRequest = authFetch('/protected');
  releaseRefresh();

  const [oldResponse, newResponse] = await Promise.all([oldRequest, newRequest]);
  assert.equal(oldResponse.status, 401);
  assert.equal(newResponse.status, 200);
  assert.equal(protectedCalls, 3);
  assert.deepEqual(refreshTokens.sort(), ['new-login-refresh', 'old-refresh']);
  assert.equal(getAuthToken(), 'new-refreshed-token');
  assert.equal(getRefreshToken(), 'new-refreshed-refresh');
});
