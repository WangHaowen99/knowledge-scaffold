const LEGACY_TOKEN_KEY = 'onto_agent_token';
const LEGACY_REFRESH_TOKEN_KEY = 'onto_agent_refresh_token';
const LEGACY_AUTH_SESSION_KEY = 'onto_agent_auth_session';
const LEGACY_TOKEN_VERSION_KEY = 'onto_agent_token_version';
export const LEGACY_AUTH_STORAGE_KEYS = Object.freeze([
  LEGACY_TOKEN_KEY,
  LEGACY_REFRESH_TOKEN_KEY,
  LEGACY_AUTH_SESSION_KEY,
  LEGACY_TOKEN_VERSION_KEY,
]);
export const AUTH_STATE_STORAGE_KEY = 'onto_agent_auth_state_v1';
const AUTH_STORAGE_LOCK = 'onto-agent-auth-state-v1';
const WORKSPACE_KEY = 'onto_agent_workspace_id';
export const AUTH_CLEARED_EVENT = 'onto-auth-cleared';
export const AUTH_EVENT = 'onto-auth-event';
const AUTH_CHANNEL_NAME = 'onto-auth-events-v1';
export const DEFAULT_WORKSPACE_ID = '';

const emptyCredentialState = () => ({
  accessToken: null,
  refreshToken: null,
  sessionId: '',
  tokenVersion: 0,
});

const readStorage = (key) => {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
};

const sameCredentialState = (left, right) => Boolean(left && right)
  && left.accessToken === right.accessToken
  && left.refreshToken === right.refreshToken
  && left.sessionId === right.sessionId
  && left.tokenVersion === right.tokenVersion;

const sameCredentialIdentity = (left, right) => Boolean(left && right)
  && Boolean(left.accessToken) === Boolean(right.accessToken)
  && left.sessionId === right.sessionId
  && (
    left.sessionId !== 'legacy-session'
    || left.accessToken === right.accessToken
  );

const createAuthSessionId = () => {
  const randomId = globalThis.crypto?.randomUUID?.();
  return randomId || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

const AUTH_TAB_ID = createAuthSessionId();

const dispatchAuthEvent = (type) => {
  try {
    const event = typeof CustomEvent === 'function'
      ? new CustomEvent(AUTH_EVENT, { detail: { source: AUTH_TAB_ID, type } })
      : Object.assign(new Event(AUTH_EVENT), { detail: { source: AUTH_TAB_ID, type } });
    window.dispatchEvent(event);
  } catch {
    // ignore event delivery errors
  }
};

const dispatchClearedEvent = () => {
  try {
    window.dispatchEvent(new Event(AUTH_CLEARED_EVENT));
  } catch {
    // ignore event delivery errors
  }
};

export const broadcastAuthEvent = (type) => {
  dispatchAuthEvent(type);
  if (typeof BroadcastChannel !== 'function') return;
  try {
    const channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
    channel.postMessage({ source: AUTH_TAB_ID, type });
    channel.close();
  } catch {
    // Browsers may disable BroadcastChannel in restricted contexts.
  }
};

export const subscribeAuthEvents = (callback) => {
  const handleWindowEvent = (event) => {
    const detail = event.detail;
    if (!detail || detail.source === AUTH_TAB_ID) return;
    callback(detail.type);
  };
  window.addEventListener(AUTH_EVENT, handleWindowEvent);

  let channel = null;
  if (typeof BroadcastChannel === 'function') {
    try {
      channel = new BroadcastChannel(AUTH_CHANNEL_NAME);
      channel.onmessage = (event) => {
        const message = event.data;
        if (message?.source !== AUTH_TAB_ID) callback(message?.type);
      };
    } catch {
      channel = null;
    }
  }
  return () => {
    window.removeEventListener(AUTH_EVENT, handleWindowEvent);
    channel?.close();
  };
};

const removeStoredCredentialState = () => {
  // Remove each historical token key explicitly. Never clear unrelated browser
  // state such as the user's workspace selection.
  window.localStorage.removeItem(AUTH_STATE_STORAGE_KEY);
  for (const key of LEGACY_AUTH_STORAGE_KEYS) window.localStorage.removeItem(key);
};

let fallbackCredentialLockTail = Promise.resolve();

const withCredentialLock = async (callback) => {
  const locks = globalThis.navigator?.locks;
  if (typeof locks?.request === 'function') {
    // Keep synchronous callback failures inside the Promise contract. This is
    // equivalent in browsers and prevents Node's Web Locks implementation from
    // retaining the lock after a synchronous throw in tests/SSR environments.
    return locks.request(AUTH_STORAGE_LOCK, async () => callback());
  }
  // Tests and storage-restricted environments still need real serialization
  // across awaits; the event loop alone does not protect a network transaction.
  const run = fallbackCredentialLockTail.then(callback, callback);
  fallbackCredentialLockTail = run.catch(() => undefined);
  return run;
};

let credentialSnapshotOwner = null;
let credentialSnapshot = null;
let credentialIdentityAbortController = new AbortController();

const setCredentialSnapshot = (state) => {
  const previousState = credentialSnapshotOwner === window ? credentialSnapshot : null;
  if (credentialSnapshotOwner !== window) {
    credentialIdentityAbortController.abort();
    credentialIdentityAbortController = new AbortController();
  }
  credentialSnapshotOwner = window;
  credentialSnapshot = { ...state };
  if (previousState && !sameCredentialIdentity(previousState, credentialSnapshot)) {
    credentialIdentityAbortController.abort();
    credentialIdentityAbortController = new AbortController();
  }
  return credentialSnapshot;
};

const getCredentialState = () => {
  if (credentialSnapshotOwner !== window || !credentialSnapshot) {
    return setCredentialSnapshot(emptyCredentialState());
  }
  return credentialSnapshot;
};

export const migrateLegacyAuthState = async () => withCredentialLock(() => {
  removeStoredCredentialState();
  const state = setCredentialSnapshot(emptyCredentialState());
  return { migrated: true, state: { ...state } };
});

export const getAuthToken = () => getCredentialState().accessToken;

export const getRefreshToken = () => getCredentialState().refreshToken;

export const setAuthToken = async (token, refreshToken) => {
  void refreshToken;
  const nextState = {
    accessToken: token,
    refreshToken: null,
    sessionId: createAuthSessionId(),
    tokenVersion: 0,
  };
  await withCredentialLock(() => setCredentialSnapshot(nextState));
  return nextState;
};

const AUTH_TRANSACTION_TIMEOUT_MS = 10_000;

class AuthRequestTimeoutError extends Error {
  constructor(message) {
    super(message);
    this.name = 'AuthRequestTimeoutError';
  }
}

const withAuthRequestTimeout = async (callback, message) => {
  const controller = new AbortController();
  const timeoutError = new AuthRequestTimeoutError(message);
  let timeoutId;
  const timeout = new Promise((resolve, reject) => {
    void resolve;
    timeoutId = globalThis.setTimeout(() => {
      controller.abort();
      reject(timeoutError);
    }, AUTH_TRANSACTION_TIMEOUT_MS);
  });
  try {
    return await Promise.race([callback(controller.signal, timeoutError), timeout]);
  } finally {
    globalThis.clearTimeout(timeoutId);
  }
};

export const loginWithPassword = async (username, password) => withCredentialLock(
  () => withAuthRequestTimeout(async (signal, timeoutError) => {
    const response = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
      credentials: 'same-origin',
      signal,
    });
    const data = await response.json().catch(() => ({}));
    if (signal.aborted) throw timeoutError;
    let committedState = null;
    if (response.ok && data.token) {
      committedState = {
        accessToken: data.token,
        refreshToken: null,
        sessionId: createAuthSessionId(),
        tokenVersion: 0,
      };
      setCredentialSnapshot(committedState);
      broadcastAuthEvent('login');
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      state: committedState ? { ...committedState } : null,
    };
  }, '登录请求超时'),
);

export const clearAuthToken = async (
  expectedState = getCredentialState(),
  { broadcast = true } = {},
) => {
  const expected = { ...expectedState };
  const cleared = await withCredentialLock(() => {
    if (!sameCredentialState(getCredentialState(), expected)) return false;
    setCredentialSnapshot(emptyCredentialState());
    return true;
  });
  if (cleared) {
    removeStoredCredentialState();
    if (broadcast) broadcastAuthEvent('logout');
    dispatchClearedEvent();
  }
  return cleared;
};

export const logoutAuthSession = async () => {
  let revokeError = null;
  await withCredentialLock(async () => {
    try {
      const response = await withAuthRequestTimeout((signal) => fetch('/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        signal,
      }), '登出请求超时');
      if (!response.ok) revokeError = new Error(`登出吊销失败（${response.status}）`);
    } catch (error) {
      revokeError = error;
    }
    setCredentialSnapshot(emptyCredentialState());
    removeStoredCredentialState();
  });
  broadcastAuthEvent('logout');
  dispatchClearedEvent();
  return { cleared: true, revokeError, state: { ...emptyCredentialState() } };
};

let workspaceSnapshotOwner = null;
let workspaceSnapshot = DEFAULT_WORKSPACE_ID;
let workspaceSnapshotUsername = null;

const normalizeWorkspaceUsername = (username) => (
  typeof username === 'string' && username.trim() ? username.trim() : null
);

const workspaceStorageKey = (username) => (
  username ? `${WORKSPACE_KEY}:${encodeURIComponent(username)}` : WORKSPACE_KEY
);

const ensureWorkspaceSnapshot = (username) => {
  const normalizedUsername = normalizeWorkspaceUsername(username);
  if (workspaceSnapshotOwner !== window || workspaceSnapshotUsername !== normalizedUsername) {
    workspaceSnapshotOwner = window;
    workspaceSnapshotUsername = normalizedUsername;
    workspaceSnapshot = readStorage(workspaceStorageKey(normalizedUsername))
      || DEFAULT_WORKSPACE_ID;
  }
  return workspaceSnapshot;
};

export const getCurrentWorkspaceId = (username) => {
  if (username !== undefined) return ensureWorkspaceSnapshot(username);
  if (workspaceSnapshotOwner !== window) return ensureWorkspaceSnapshot(null);
  return workspaceSnapshot;
};

export const setCurrentWorkspaceId = (workspaceId, username) => {
  const normalizedUsername = username === undefined && workspaceSnapshotOwner === window
    ? workspaceSnapshotUsername
    : normalizeWorkspaceUsername(username);
  workspaceSnapshotOwner = window;
  workspaceSnapshotUsername = normalizedUsername;
  workspaceSnapshot = workspaceId || DEFAULT_WORKSPACE_ID;
  try {
    const storageKey = workspaceStorageKey(normalizedUsername);
    if (workspaceSnapshot) {
      window.localStorage.setItem(storageKey, workspaceSnapshot);
    } else {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // ignore storage errors
  }
};

const doAuthFetch = (
  url,
  options = {},
  workspaceId = getCurrentWorkspaceId(),
  accessToken = null,
) => {
  const headers = new Headers(options.headers || {});
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  if (workspaceId) {
    // Headers.set only supports ISO-8859-1, so we encode for safety
    headers.set('X-Workspace-Id', encodeURIComponent(workspaceId));
  }
  return fetch(url, { credentials: 'same-origin', ...options, headers });
};

class RefreshFailure extends Error {
  constructor(message, { permanent = false, stale = false } = {}) {
    super(message);
    this.name = 'RefreshFailure';
    this.permanent = permanent;
    this.stale = stale;
  }
}

// A fulfilled transition is retained so a delayed concurrent 401 from the same
// old token pair can adopt it without issuing a second refresh request.
let refreshInFlight = null;

const refreshAuthToken = (expectedState) => {
  if (refreshInFlight && sameCredentialState(refreshInFlight.from, expectedState)) {
    return refreshInFlight;
  }

  const refreshRequest = {
    from: { ...expectedState },
    to: null,
    status: 'pending',
    error: null,
    promise: null,
  };
  refreshRequest.promise = (async () => {
    let response;
    try {
      response = await fetch('/auth/refresh', {
        method: 'POST',
        credentials: 'same-origin',
      });
    } catch {
      throw new RefreshFailure('刷新登录令牌时网络异常');
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new RefreshFailure('刷新登录令牌失败', {
        permanent: [400, 401, 403].includes(response.status),
      });
    }
    if (!data.token) throw new RefreshFailure('刷新响应缺少登录令牌');

    return withCredentialLock(() => {
      const storedState = getCredentialState();
      if (!sameCredentialState(storedState, expectedState)) {
        if (
          expectedState.sessionId
          && storedState.sessionId === expectedState.sessionId
          && storedState.tokenVersion > expectedState.tokenVersion
          && storedState.accessToken
        ) {
          setCredentialSnapshot(storedState);
          return storedState;
        }
        throw new RefreshFailure('登录状态已变化，忽略过期的令牌刷新结果', { stale: true });
      }
      const nextState = {
        accessToken: data.token,
        refreshToken: null,
        sessionId: expectedState.sessionId || createAuthSessionId(),
        tokenVersion: expectedState.tokenVersion + 1,
      };
      setCredentialSnapshot(nextState);
      return nextState;
    });
  })().then((nextState) => {
    refreshRequest.to = nextState;
    refreshRequest.status = 'fulfilled';
    return nextState;
  }).catch((error) => {
    const failure = error instanceof RefreshFailure
      ? error
      : new RefreshFailure('刷新登录令牌失败');
    refreshRequest.error = failure;
    refreshRequest.status = 'rejected';
    throw failure;
  }).finally(() => {
    if (refreshRequest.status === 'rejected' && refreshInFlight === refreshRequest) {
      refreshInFlight = null;
    }
  });
  refreshInFlight = refreshRequest;
  return refreshRequest;
};

const REFRESH_EXEMPT_PATHS = ['/auth/refresh', '/auth/login', '/auth/logout'];
const TOKEN_REFRESH_SKEW_MS = 30_000;
const unauthorizedResponse = () => new Response(null, { status: 401 });

const tokenNeedsRefresh = (token, now = Date.now()) => {
  try {
    const payload = String(token || '').split('.')[1];
    if (!payload) return false;
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
    const expiresAt = Number(JSON.parse(atob(padded)).exp) * 1000;
    return Number.isFinite(expiresAt) && expiresAt <= now + TOKEN_REFRESH_SKEW_MS;
  } catch {
    return false;
  }
};

const reconciledCredentialState = (requestState) => {
  const currentState = getCredentialState();
  if (sameCredentialState(currentState, requestState)) return currentState;
  if (
    requestState.sessionId
    && currentState.sessionId === requestState.sessionId
    && currentState.tokenVersion > requestState.tokenVersion
    && currentState.accessToken
  ) return currentState;
  if (
    refreshInFlight?.status === 'fulfilled'
    && sameCredentialState(refreshInFlight.from, requestState)
    && sameCredentialState(refreshInFlight.to, currentState)
  ) return currentState;
  return null;
};

class AuthStateChangedError extends Error {
  constructor() {
    super('登录状态已变化，已停止读取旧身份响应');
    this.name = 'AuthStateChangedError';
  }
}

const guardResponseBody = (response, requestState) => {
  if (!response.body || !requestState.accessToken || response.status === 0) return response;
  const identitySignal = credentialIdentityAbortController.signal;
  let reader = null;
  let finished = false;

  const cancelSource = (reason) => {
    const cancellation = reader
      ? reader.cancel(reason)
      : response.body.cancel(reason);
    void cancellation.catch(() => undefined);
  };

  const rejectStaleBody = (controller) => {
    if (finished) return;
    finished = true;
    const error = new AuthStateChangedError();
    controller.error(error);
    // Source cancellation is cleanup and must never delay the credential-boundary
    // signal to a consumer whose SSE read is currently waiting for another chunk.
    cancelSource(error);
  };

  const readWithIdentityGuard = () => {
    if (identitySignal.aborted) return Promise.reject(new AuthStateChangedError());
    if (!reader) reader = response.body.getReader();
    return new Promise((resolve, reject) => {
      const onIdentityChange = () => reject(new AuthStateChangedError());
      identitySignal.addEventListener('abort', onIdentityChange, { once: true });
      reader.read().then(
        (chunk) => {
          identitySignal.removeEventListener('abort', onIdentityChange);
          resolve(chunk);
        },
        (error) => {
          identitySignal.removeEventListener('abort', onIdentityChange);
          reject(error);
        },
      );
    });
  };

  const guardedBody = new ReadableStream({
    async pull(controller) {
      if (!reconciledCredentialState(requestState)) {
        rejectStaleBody(controller);
        return;
      }
      try {
        const chunk = await readWithIdentityGuard();
        if (finished) return;
        if (!reconciledCredentialState(requestState)) {
          rejectStaleBody(controller);
          return;
        }
        if (chunk.done) {
          finished = true;
          controller.close();
          return;
        }
        controller.enqueue(chunk.value);
      } catch (error) {
        if (finished) return;
        if (error instanceof AuthStateChangedError) {
          rejectStaleBody(controller);
          return;
        }
        finished = true;
        controller.error(error);
      }
    },
    cancel(reason) {
      finished = true;
      if (reader) return reader.cancel(reason);
      return response.body.cancel(reason);
    },
  }, { highWaterMark: 0 });

  return new Response(guardedBody, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
};

const clearCredentialsIfCurrent = async (expectedState) => {
  if (!sameCredentialState(getCredentialState(), expectedState)) return false;
  return clearAuthToken(expectedState);
};

const retryWithCredentialState = async (url, options, workspaceId, retryState) => {
  if (!sameCredentialState(getCredentialState(), retryState)) return unauthorizedResponse();
  const response = await doAuthFetch(url, options, workspaceId, retryState.accessToken);
  return reconciledCredentialState(retryState)
    ? guardResponseBody(response, retryState)
    : unauthorizedResponse();
};

export const authFetch = async (url, options = {}) => {
  // Both credentials and workspace are captured from this tab's confirmed UI
  // context. Another tab cannot redirect an in-flight write to its account/scope.
  const workspaceId = getCurrentWorkspaceId();
  const refreshExempt = REFRESH_EXEMPT_PATHS.some((path) => String(url).startsWith(path));
  let requestState = { ...getCredentialState() };

  if (
    !refreshExempt
    && (!requestState.accessToken || tokenNeedsRefresh(requestState.accessToken))
  ) {
    const proactiveRefresh = refreshAuthToken(requestState);
    try {
      await proactiveRefresh.promise;
      const refreshedState = reconciledCredentialState(requestState);
      if (!refreshedState) return unauthorizedResponse();
      requestState = { ...refreshedState };
    } catch (error) {
      const currentState = reconciledCredentialState(requestState);
      if (!currentState) return unauthorizedResponse();
      if (error.permanent) {
        await clearCredentialsIfCurrent(requestState);
        return unauthorizedResponse();
      }
      // Network, 5xx and malformed-response failures are transient. Keep a
      // still-valid access token and let the protected endpoint decide.
      requestState = { ...currentState };
    }
  }

  const response = await doAuthFetch(url, options, workspaceId, requestState.accessToken);
  const currentState = reconciledCredentialState(requestState);
  if (!currentState) return unauthorizedResponse();
  if (response.status !== 401 || refreshExempt) {
    return guardResponseBody(response, requestState);
  }

  if (!sameCredentialState(currentState, requestState)) {
    return retryWithCredentialState(url, options, workspaceId, currentState);
  }

  const reactiveRefresh = refreshAuthToken(requestState);
  let retryState;
  try {
    await reactiveRefresh.promise;
    retryState = reconciledCredentialState(requestState);
  } catch (error) {
    if (error.permanent) {
      await clearCredentialsIfCurrent(requestState);
      return unauthorizedResponse();
    }
    const caughtState = reconciledCredentialState(requestState);
    if (!caughtState) return unauthorizedResponse();
    if (!sameCredentialState(caughtState, requestState)) {
      return retryWithCredentialState(url, options, workspaceId, caughtState);
    }
    return guardResponseBody(response, requestState);
  }
  if (!retryState) return unauthorizedResponse();
  return retryWithCredentialState(url, options, workspaceId, retryState);
};
