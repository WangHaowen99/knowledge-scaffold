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

const normalizeCredentialState = (value) => {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const accessToken =
    typeof value.accessToken === 'string' && value.accessToken ?
      value.accessToken :
      null;
  if (!accessToken) {
    return null;
  }
  const parsedVersion = Number(value.tokenVersion);
  return {
    accessToken,
    refreshToken:
      typeof value.refreshToken === 'string' && value.refreshToken ?
        value.refreshToken :
        null,
    sessionId: typeof value.sessionId === 'string' ? value.sessionId : '',
    tokenVersion:
      Number.isSafeInteger(parsedVersion) && parsedVersion >= 0 ?
        parsedVersion :
        0,
  };
};

const parseCredentialEnvelope = (rawValue) => {
  if (!rawValue) {
    return null;
  }
  try {
    return normalizeCredentialState(JSON.parse(rawValue));
  } catch {
    return null;
  }
};

const readLegacyCredentialState = () => {
  const accessToken = readStorage(LEGACY_TOKEN_KEY);
  if (!accessToken) {
    return null;
  }
  const parsedVersion = Number(readStorage(LEGACY_TOKEN_VERSION_KEY));
  return {
    accessToken,
    refreshToken: readStorage(LEGACY_REFRESH_TOKEN_KEY),
    sessionId: readStorage(LEGACY_AUTH_SESSION_KEY) || '',
    tokenVersion:
      Number.isSafeInteger(parsedVersion) && parsedVersion >= 0 ?
        parsedVersion :
        0,
  };
};

const readStoredCredentialState = () => {
  const envelope = parseCredentialEnvelope(readStorage(AUTH_STATE_STORAGE_KEY));
  if (envelope) {
    return envelope;
  }

  // Synchronous compatibility read for the first render. useAuth immediately
  // migrates this pair under the shared credential lock before verification.
  const legacyState = readLegacyCredentialState();
  return legacyState ?
    { ...legacyState, sessionId: legacyState.sessionId || 'legacy-session' } :
    emptyCredentialState();
};

const sameCredentialState = (left, right) =>
  Boolean(left && right) &&
  left.accessToken === right.accessToken &&
  left.refreshToken === right.refreshToken &&
  left.sessionId === right.sessionId &&
  left.tokenVersion === right.tokenVersion;

const sameCredentialIdentity = (left, right) =>
  Boolean(left && right) &&
  Boolean(left.accessToken) === Boolean(right.accessToken) &&
  left.sessionId === right.sessionId &&
  (left.sessionId !== 'legacy-session' ||
    left.accessToken === right.accessToken);

const createAuthSessionId = () => {
  const randomId = globalThis.crypto?.randomUUID?.();
  return randomId || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

const persistCredentialState = (state) => {
  window.localStorage.setItem(AUTH_STATE_STORAGE_KEY, JSON.stringify(state));
  // The envelope is now the sole authority. Removing the legacy multi-key
  // representation prevents another tab from observing a partially written pair.
  window.localStorage.removeItem(LEGACY_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_REFRESH_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_AUTH_SESSION_KEY);
  window.localStorage.removeItem(LEGACY_TOKEN_VERSION_KEY);
};

const removeStoredCredentialState = () => {
  window.localStorage.removeItem(AUTH_STATE_STORAGE_KEY);
  window.localStorage.removeItem(LEGACY_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_REFRESH_TOKEN_KEY);
  window.localStorage.removeItem(LEGACY_AUTH_SESSION_KEY);
  window.localStorage.removeItem(LEGACY_TOKEN_VERSION_KEY);
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
  const previousState =
    credentialSnapshotOwner === window ? credentialSnapshot : null;
  if (credentialSnapshotOwner !== window) {
    credentialIdentityAbortController.abort();
    credentialIdentityAbortController = new AbortController();
  }
  credentialSnapshotOwner = window;
  credentialSnapshot = { ...state };
  if (
    previousState &&
    !sameCredentialIdentity(previousState, credentialSnapshot)
  ) {
    credentialIdentityAbortController.abort();
    credentialIdentityAbortController = new AbortController();
  }
  return credentialSnapshot;
};

const getCredentialState = () => {
  if (credentialSnapshotOwner !== window || !credentialSnapshot) {
    return setCredentialSnapshot(readStoredCredentialState());
  }

  // A managed refresh in another tab is safe to adopt because it preserves the
  // login identity and commits a strictly newer envelope version. A different
  // login remains quarantined until useAuth handles the storage event and freezes UI.
  const storedState = readStoredCredentialState();
  if (
    credentialSnapshot.sessionId &&
    storedState.sessionId === credentialSnapshot.sessionId &&
    storedState.tokenVersion > credentialSnapshot.tokenVersion
  ) {
    return setCredentialSnapshot(storedState);
  }
  return credentialSnapshot;
};

export const migrateLegacyAuthState = async () =>
  withCredentialLock(() => {
    const envelope = parseCredentialEnvelope(
      readStorage(AUTH_STATE_STORAGE_KEY),
    );
    const legacyState = readLegacyCredentialState();
    let nextState = envelope;

    const legacyStateChanged =
      legacyState &&
      (!envelope ||
        envelope.accessToken !== legacyState.accessToken ||
        envelope.refreshToken !== legacyState.refreshToken);
    if (legacyStateChanged) {
      nextState = {
        ...legacyState,
        sessionId: legacyState.sessionId || createAuthSessionId(),
      };
    }

    if (nextState) {
      // Persisting even an equivalent envelope removes the old two-key shape in
      // the same lock. An old application tab that writes the legacy keys again
      // is detected by useAuth's transitional storage listener and re-migrated.
      if (legacyState) {
        persistCredentialState(nextState);
      }
      setCredentialSnapshot(nextState);
    } else {
      setCredentialSnapshot(emptyCredentialState());
    }

    return {
      migrated: Boolean(legacyState),
      state: { ...(nextState || emptyCredentialState()) },
    };
  });

export const synchronizeAuthStateFromStorage = () => {
  const previousState = { ...getCredentialState() };
  const nextState = setCredentialSnapshot(readStoredCredentialState());
  return {
    previousState,
    nextState: { ...nextState },
    identityChanged: !sameCredentialIdentity(previousState, nextState),
  };
};

export const getAuthToken = () => getCredentialState().accessToken;

export const getRefreshToken = () => getCredentialState().refreshToken;

export const setAuthToken = async (token, refreshToken) => {
  const nextState = {
    accessToken: token,
    refreshToken: refreshToken || null,
    sessionId: createAuthSessionId(),
    tokenVersion: 0,
  };
  await withCredentialLock(() => {
    persistCredentialState(nextState);
    setCredentialSnapshot(nextState);
  });
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
    return await Promise.race([
      callback(controller.signal, timeoutError),
      timeout,
    ]);
  } finally {
    globalThis.clearTimeout(timeoutId);
  }
};

export const loginWithPassword = async (username, password) =>
  withCredentialLock(() =>
    withAuthRequestTimeout(async (signal, timeoutError) => {
      const response = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
        signal,
      });
      const data = await response.json().catch(() => ({}));
      if (signal.aborted) {
        throw timeoutError;
      }
      let committedState = null;
      if (response.ok && data.token) {
        committedState = {
          accessToken: data.token,
          refreshToken: data.refresh_token || null,
          sessionId: createAuthSessionId(),
          tokenVersion: 0,
        };
        persistCredentialState(committedState);
        setCredentialSnapshot(committedState);
      }
      return {
        ok: response.ok,
        status: response.status,
        data,
        state: committedState ? { ...committedState } : null,
      };
    }, '登录请求超时'),
  );

export const clearAuthToken = async (expectedState = getCredentialState()) => {
  const expected = { ...expectedState };
  const cleared = await withCredentialLock(() => {
    if (!sameCredentialState(readStoredCredentialState(), expected)) {
      return false;
    }
    removeStoredCredentialState();
    setCredentialSnapshot(emptyCredentialState());
    return true;
  });
  if (cleared) {
    try {
      window.dispatchEvent(new Event(AUTH_CLEARED_EVENT));
    } catch {
      // ignore event delivery errors
    }
  }
  return cleared;
};

export const logoutAuthSession = async () => {
  let cleared = false;
  let revokeError = null;
  let resultingState = emptyCredentialState();

  await withCredentialLock(async () => {
    // Read after acquiring the lock so login/refresh/logout are linearized by
    // lock acquisition order across every open application tab.
    const currentState = readStoredCredentialState();
    setCredentialSnapshot(currentState);

    if (currentState.refreshToken) {
      try {
        const response = await withAuthRequestTimeout(
          (signal) =>
            fetch('/auth/logout', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                refresh_token: currentState.refreshToken,
              }),
              signal,
            }),
          '登出请求超时',
        );
        if (!response.ok) {
          revokeError = new Error(`登出吊销失败（${response.status}）`);
        }
      } catch (error) {
        revokeError = error;
      }
    }

    const storedAfterRevoke = readStoredCredentialState();
    const legacyAfterRevoke = readLegacyCredentialState();
    const differentLegacySession =
      legacyAfterRevoke &&
      (legacyAfterRevoke.accessToken !== currentState.accessToken ||
        legacyAfterRevoke.refreshToken !== currentState.refreshToken) &&
      legacyAfterRevoke.sessionId &&
      legacyAfterRevoke.sessionId !== currentState.sessionId;
    if (differentLegacySession) {
      // A transitional client that already has an explicit different session
      // marker represents a new login. Unmarked legacy writes are logout-wins
      // and force that old page to reload the current application build.
      resultingState = {
        ...legacyAfterRevoke,
      };
      persistCredentialState(resultingState);
      setCredentialSnapshot(resultingState);
      return;
    }
    if (!sameCredentialState(storedAfterRevoke, currentState)) {
      const storedSessionChanged =
        storedAfterRevoke.accessToken &&
        storedAfterRevoke.sessionId &&
        storedAfterRevoke.sessionId !== 'legacy-session' &&
        storedAfterRevoke.sessionId !== currentState.sessionId;
      if (storedSessionChanged) {
        resultingState = storedAfterRevoke;
        setCredentialSnapshot(resultingState);
        return;
      }
      if (!storedAfterRevoke.accessToken) {
        resultingState = emptyCredentialState();
        setCredentialSnapshot(resultingState);
        cleared = true;
        return;
      }
    }

    // Always finish the local logout when the captured credential is still
    // current, even if the server is offline.
    removeStoredCredentialState();
    resultingState = emptyCredentialState();
    setCredentialSnapshot(resultingState);
    cleared = true;
  });

  if (cleared) {
    try {
      window.dispatchEvent(new Event(AUTH_CLEARED_EVENT));
    } catch {
      // ignore event delivery errors
    }
  }
  return { cleared, revokeError, state: { ...resultingState } };
};

let workspaceSnapshotOwner = null;
let workspaceSnapshot = DEFAULT_WORKSPACE_ID;
let workspaceSnapshotUsername = null;

const normalizeWorkspaceUsername = (username) =>
  typeof username === 'string' && username.trim() ? username.trim() : null;

const workspaceStorageKey = (username) =>
  username ? `${WORKSPACE_KEY}:${encodeURIComponent(username)}` : WORKSPACE_KEY;

const ensureWorkspaceSnapshot = (username) => {
  const normalizedUsername = normalizeWorkspaceUsername(username);
  if (
    workspaceSnapshotOwner !== window ||
    workspaceSnapshotUsername !== normalizedUsername
  ) {
    workspaceSnapshotOwner = window;
    workspaceSnapshotUsername = normalizedUsername;
    workspaceSnapshot =
      readStorage(workspaceStorageKey(normalizedUsername)) ||
      DEFAULT_WORKSPACE_ID;
  }
  return workspaceSnapshot;
};

export const getCurrentWorkspaceId = (username) => {
  if (username !== undefined) {
    return ensureWorkspaceSnapshot(username);
  }
  if (workspaceSnapshotOwner !== window) {
    return ensureWorkspaceSnapshot(null);
  }
  return workspaceSnapshot;
};

export const setCurrentWorkspaceId = (workspaceId, username) => {
  const normalizedUsername =
    username === undefined && workspaceSnapshotOwner === window ?
      workspaceSnapshotUsername :
      normalizeWorkspaceUsername(username);
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
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }
  if (workspaceId) {
    // Headers.set only supports ISO-8859-1, so we encode for safety
    headers.set('X-Workspace-Id', encodeURIComponent(workspaceId));
  }
  return fetch(url, { ...options, headers });
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
  if (
    refreshInFlight &&
    sameCredentialState(refreshInFlight.from, expectedState)
  ) {
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
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: expectedState.refreshToken }),
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
    if (!data.token) {
      throw new RefreshFailure('刷新响应缺少登录令牌');
    }

    return withCredentialLock(() => {
      const storedState = readStoredCredentialState();
      if (!sameCredentialState(storedState, expectedState)) {
        const newerStoredState =
          expectedState.sessionId &&
          storedState.sessionId === expectedState.sessionId &&
          storedState.tokenVersion > expectedState.tokenVersion &&
          storedState.accessToken;
        if (newerStoredState) {
          setCredentialSnapshot(storedState);
          return storedState;
        }
        throw new RefreshFailure('登录状态已变化，忽略过期的令牌刷新结果', {
          stale: true,
        });
      }
      const nextState = {
        accessToken: data.token,
        refreshToken: data.refresh_token || expectedState.refreshToken,
        sessionId: expectedState.sessionId,
        tokenVersion: expectedState.tokenVersion + 1,
      };
      persistCredentialState(nextState);
      setCredentialSnapshot(nextState);
      return nextState;
    });
  })()
    .then((nextState) => {
      refreshRequest.to = nextState;
      refreshRequest.status = 'fulfilled';
      return nextState;
    })
    .catch((error) => {
      const failure =
        error instanceof RefreshFailure ?
          error :
          new RefreshFailure('刷新登录令牌失败');
      refreshRequest.error = failure;
      refreshRequest.status = 'rejected';
      throw failure;
    })
    .finally(() => {
      if (
        refreshRequest.status === 'rejected' &&
        refreshInFlight === refreshRequest
      ) {
        refreshInFlight = null;
      }
    });
  refreshInFlight = refreshRequest;
  return refreshRequest;
};

const REFRESH_EXEMPT_PATHS = ['/auth/refresh', '/auth/login'];
const TOKEN_REFRESH_SKEW_MS = 30_000;
const unauthorizedResponse = () => new Response(null, { status: 401 });

const tokenNeedsRefresh = (token, now = Date.now()) => {
  try {
    const [, payload] = String(token || '').split('.');
    if (!payload) {
      return false;
    }
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
    const expiresAt = Number(JSON.parse(atob(padded)).exp) * 1000;
    return (
      Number.isFinite(expiresAt) && expiresAt <= now + TOKEN_REFRESH_SKEW_MS
    );
  } catch {
    return false;
  }
};

const reconciledCredentialState = (requestState) => {
  const currentState = getCredentialState();
  const storedState = readStoredCredentialState();
  // A new login/logout may be committed before this tab receives its storage
  // event. Reject the response at the commit boundary instead of briefly
  // exposing the old account or starting a stale refresh.
  if (!sameCredentialIdentity(storedState, currentState)) {
    return null;
  }
  if (sameCredentialState(currentState, requestState)) {
    return currentState;
  }
  const currentStateIsNewer =
    requestState.sessionId &&
    currentState.sessionId === requestState.sessionId &&
    currentState.tokenVersion > requestState.tokenVersion &&
    currentState.accessToken;
  if (currentStateIsNewer) {
    return currentState;
  }
  if (
    refreshInFlight?.status === 'fulfilled' &&
    sameCredentialState(refreshInFlight.from, requestState) &&
    sameCredentialState(refreshInFlight.to, currentState)
  ) {
    return currentState;
  }
  return null;
};

class AuthStateChangedError extends Error {
  constructor() {
    super('登录状态已变化，已停止读取旧身份响应');
    this.name = 'AuthStateChangedError';
  }
}

const guardResponseBody = (response, requestState) => {
  if (!response.body || !requestState.accessToken || response.status === 0) {
    return response;
  }
  const identitySignal = credentialIdentityAbortController.signal;
  let reader = null;
  let finished = false;

  const cancelSource = (reason) => {
    const cancellation = reader ?
      reader.cancel(reason) :
      response.body.cancel(reason);
    void cancellation.catch(() => undefined);
  };

  const rejectStaleBody = (controller) => {
    if (finished) {
      return;
    }
    finished = true;
    const error = new AuthStateChangedError();
    controller.error(error);
    // Source cancellation is cleanup and must never delay the credential-boundary
    // signal to a consumer whose SSE read is currently waiting for another chunk.
    cancelSource(error);
  };

  const readWithIdentityGuard = () => {
    if (identitySignal.aborted) {
      return Promise.reject(new AuthStateChangedError());
    }
    if (!reader) {
      reader = response.body.getReader();
    }
    return new Promise((resolve, reject) => {
      const onIdentityChange = () => reject(new AuthStateChangedError());
      identitySignal.addEventListener('abort', onIdentityChange, {
        once: true,
      });
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

  const guardedBody = new ReadableStream(
    {
      async pull(controller) {
        if (!reconciledCredentialState(requestState)) {
          rejectStaleBody(controller);
          return;
        }
        try {
          const chunk = await readWithIdentityGuard();
          if (finished) {
            return;
          }
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
          if (finished) {
            return;
          }
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
        if (reader) {
          return reader.cancel(reason);
        }
        return response.body.cancel(reason);
      },
    },
    { highWaterMark: 0 },
  );

  return new Response(guardedBody, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
};

const clearCredentialsIfCurrent = async (expectedState) => {
  if (!sameCredentialState(getCredentialState(), expectedState)) {
    return false;
  }
  return clearAuthToken(expectedState);
};

const retryWithCredentialState = async (
  url,
  options,
  workspaceId,
  retryState,
) => {
  if (!sameCredentialState(getCredentialState(), retryState)) {
    return unauthorizedResponse();
  }
  const response = await doAuthFetch(
    url,
    options,
    workspaceId,
    retryState.accessToken,
  );
  return reconciledCredentialState(retryState) ?
    guardResponseBody(response, retryState) :
    unauthorizedResponse();
};

export const authFetch = async (url, options = {}) => {
  // Both credentials and workspace are captured from this tab's confirmed UI
  // context. Another tab cannot redirect an in-flight write to its account/scope.
  const workspaceId = getCurrentWorkspaceId();
  const refreshExempt = REFRESH_EXEMPT_PATHS.some((path) =>
    String(url).startsWith(path),
  );
  let requestState = { ...getCredentialState() };

  if (
    !refreshExempt &&
    requestState.refreshToken &&
    tokenNeedsRefresh(requestState.accessToken)
  ) {
    const proactiveRefresh = refreshAuthToken(requestState);
    try {
      await proactiveRefresh.promise;
      const refreshedState = reconciledCredentialState(requestState);
      if (!refreshedState) {
        return unauthorizedResponse();
      }
      requestState = { ...refreshedState };
    } catch (error) {
      const currentState = reconciledCredentialState(requestState);
      if (!currentState) {
        return unauthorizedResponse();
      }
      if (error.permanent) {
        await clearCredentialsIfCurrent(requestState);
        return unauthorizedResponse();
      }
      // Network, 5xx and malformed-response failures are transient. Keep a
      // still-valid access token and let the protected endpoint decide.
      requestState = { ...currentState };
    }
  }

  const response = await doAuthFetch(
    url,
    options,
    workspaceId,
    requestState.accessToken,
  );
  const currentState = reconciledCredentialState(requestState);
  if (!currentState) {
    return unauthorizedResponse();
  }
  if (response.status !== 401 || refreshExempt || !requestState.refreshToken) {
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
    if (!caughtState) {
      return unauthorizedResponse();
    }
    if (!sameCredentialState(caughtState, requestState)) {
      return retryWithCredentialState(url, options, workspaceId, caughtState);
    }
    return guardResponseBody(response, requestState);
  }
  if (!retryState) {
    return unauthorizedResponse();
  }
  return retryWithCredentialState(url, options, workspaceId, retryState);
};
