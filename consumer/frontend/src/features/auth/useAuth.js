import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUTH_CLEARED_EVENT,
  AUTH_STATE_STORAGE_KEY,
  LEGACY_AUTH_STORAGE_KEYS,
  authFetch,
  clearAuthToken,
  getAuthToken,
  loginWithPassword,
  logoutAuthSession,
  migrateLegacyAuthState,
  synchronizeAuthStateFromStorage,
} from '../../shared/api/auth';
import createLogger from '../../shared/lib/logger';
import { getLoginErrorMessage } from './loginErrors.js';

const logger = createLogger('Auth');

const sameAuthState = (left, right) => Boolean(left && right)
  && left.accessToken === right.accessToken
  && left.refreshToken === right.refreshToken
  && left.sessionId === right.sessionId
  && left.tokenVersion === right.tokenVersion;

const useAuth = () => {
  const [status, setStatus] = useState('checking');
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);
  const authOperationEpochRef = useRef(0);
  const localAuthOperationRef = useRef({ id: 0, kind: 'idle' });
  const crossTabSyncTimerRef = useRef(null);

  const applyAuthPayload = useCallback((data) => {
    if (!data.token) {
      throw new Error('缺少登录令牌');
    }
    setUser(data.user || null);
    setStatus('authenticated');
    setError(null);
  }, []);

  const verify = useCallback(async () => {
    const operationEpoch = authOperationEpochRef.current;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const token = getAuthToken();
      if (!token) {
        if (authOperationEpochRef.current === operationEpoch) {
          setStatus('unauthenticated');
          setUser(null);
        }
        return;
      }

      try {
        const res = await authFetch('/auth/me');
        if (!res.ok) {
          throw new Error(`Auth check failed: ${res.status}`);
        }
        const data = await res.json();
        if (authOperationEpochRef.current !== operationEpoch) return;
        setUser(data.user || null);
        setStatus('authenticated');
        setError(null);
        return;
      } catch (err) {
        if (authOperationEpochRef.current !== operationEpoch) return;
        logger.warn('Auth check failed', { error: err.message });
        const cleared = await clearAuthToken();
        if (authOperationEpochRef.current !== operationEpoch) return;
        if (!cleared && attempt === 0) {
          const synchronized = synchronizeAuthStateFromStorage();
          if (synchronized.nextState.accessToken) {
            setUser(null);
            setStatus('checking');
            setError(null);
            continue;
          }
        }
        setUser(null);
        setStatus('unauthenticated');
        return;
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const timeoutId = window.setTimeout(() => {
      void migrateLegacyAuthState()
        .catch((migrationError) => {
          logger.warn('Legacy auth migration failed', { error: migrationError.message });
        })
        .then(() => {
          if (!cancelled) return verify();
          return undefined;
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [verify]);

  useEffect(() => {
    const handleCleared = () => {
      authOperationEpochRef.current += 1;
      if (crossTabSyncTimerRef.current) window.clearTimeout(crossTabSyncTimerRef.current);
      setUser(null);
      setStatus('unauthenticated');
    };
    const handleStorage = (event) => {
      if (event.storageArea !== window.localStorage) return;
      if (LEGACY_AUTH_STORAGE_KEYS.includes(event.key)) {
        // Old application tabs only know the two legacy token keys. A non-null
        // write means login/refresh; migrate the final pair after that task and
        // freeze the current identity until it has been verified.
        if (event.newValue === null) return;
        const operationEpoch = authOperationEpochRef.current + 1;
        authOperationEpochRef.current = operationEpoch;
        if (crossTabSyncTimerRef.current) window.clearTimeout(crossTabSyncTimerRef.current);
        setUser(null);
        setError(null);
        setStatus('checking');
        crossTabSyncTimerRef.current = window.setTimeout(() => {
          crossTabSyncTimerRef.current = null;
          void migrateLegacyAuthState()
            .catch((migrationError) => {
              logger.warn('Legacy auth migration failed', { error: migrationError.message });
            })
            .then(() => {
              if (authOperationEpochRef.current === operationEpoch) return verify();
              return undefined;
            });
        }, 0);
        return;
      }
      // localStorage.clear() dispatches a storage event with a null key.
      if (event.key !== AUTH_STATE_STORAGE_KEY && event.key !== null) return;
      const synchronized = synchronizeAuthStateFromStorage();
      if (!synchronized.identityChanged) return;

      const operationEpoch = authOperationEpochRef.current + 1;
      authOperationEpochRef.current = operationEpoch;
      if (crossTabSyncTimerRef.current) window.clearTimeout(crossTabSyncTimerRef.current);
      setUser(null);
      setError(null);
      if (!synchronized.nextState.accessToken) {
        setStatus('unauthenticated');
        return;
      }

      // Freeze the old account immediately. Verification runs in the next task,
      // after the storage event's atomic credential commit is fully observable.
      setStatus('checking');
      crossTabSyncTimerRef.current = window.setTimeout(() => {
        crossTabSyncTimerRef.current = null;
        if (authOperationEpochRef.current === operationEpoch) void verify();
      }, 0);
    };
    window.addEventListener(AUTH_CLEARED_EVENT, handleCleared);
    window.addEventListener('storage', handleStorage);
    return () => {
      if (crossTabSyncTimerRef.current) window.clearTimeout(crossTabSyncTimerRef.current);
      window.removeEventListener(AUTH_CLEARED_EVENT, handleCleared);
      window.removeEventListener('storage', handleStorage);
    };
  }, [verify]);

  const login = useCallback(async (username, password) => {
    const operationEpoch = authOperationEpochRef.current + 1;
    authOperationEpochRef.current = operationEpoch;
    const localOperation = {
      id: localAuthOperationRef.current.id + 1,
      kind: 'login',
    };
    localAuthOperationRef.current = localOperation;
    setError(null);
    try {
      const result = await loginWithPassword(username, password);
      if (
        localAuthOperationRef.current.id !== localOperation.id
        || localAuthOperationRef.current.kind !== 'login'
      ) return { success: false, stale: true };

      if (!result.ok) {
        if (authOperationEpochRef.current !== operationEpoch) {
          localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
          return { success: false, stale: true };
        }
        const authError = new Error(getLoginErrorMessage({
          status: result.status,
          error: result.data?.error,
          code: result.data?.code,
        }));
        authError.status = result.status;
        authError.code = result.data?.code;
        throw authError;
      }

      const synchronized = synchronizeAuthStateFromStorage();
      if (!result.state || !sameAuthState(synchronized.nextState, result.state)) {
        localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
        return { success: false, stale: true };
      }
      authOperationEpochRef.current += 1;
      if (crossTabSyncTimerRef.current) {
        window.clearTimeout(crossTabSyncTimerRef.current);
        crossTabSyncTimerRef.current = null;
      }
      localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
      applyAuthPayload(result.data);
      return { success: true };
    } catch (err) {
      const ownsLocalOperation = localAuthOperationRef.current.id === localOperation.id
        && localAuthOperationRef.current.kind === 'login';
      if (!ownsLocalOperation) return { success: false, stale: true };
      if (authOperationEpochRef.current !== operationEpoch) {
        localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
        return { success: false, stale: true };
      }
      localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
      const message = getLoginErrorMessage(err);
      logger.warn('Login failed', { error: err?.message, status: err?.status });
      setError(message);
      setStatus('unauthenticated');
      return {
        success: false,
        error: message,
        status: err?.status,
        code: err?.code,
      };
    }
  }, [applyAuthPayload]);

  const changePassword = useCallback(async (oldPassword, newPassword) => {
    setError(null);
    try {
      const res = await authFetch('/auth/change-password', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = new Error(data.error || '修改密码失败');
        err.violations = data.violations || [];
        throw err;
      }
      await clearAuthToken();
      setUser(null);
      setStatus('unauthenticated');
      setError(null);
      return { success: true };
    } catch (err) {
      logger.warn('Change password failed', { error: err.message });
      setError(err.message || '修改密码失败');
      return { success: false, error: err.message, violations: err.violations || [] };
    }
  }, []);

  const logout = useCallback(async () => {
    authOperationEpochRef.current += 1;
    const localOperation = {
      id: localAuthOperationRef.current.id + 1,
      kind: 'logout',
    };
    localAuthOperationRef.current = localOperation;
    setUser(null);
    setError(null);
    // Keep every tab out of the login UI until the shared lock completes the
    // old refresh-token revoke and atomically clears the envelope.
    setStatus('checking');
    const { cleared, revokeError, state } = await logoutAuthSession();
    if (revokeError) logger.warn('Logout revoke request failed', { error: revokeError.message });
    if (
      localAuthOperationRef.current.id !== localOperation.id
      || localAuthOperationRef.current.kind !== 'logout'
    ) return;
    localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
    if (!cleared && state.accessToken) {
      setStatus('checking');
      void verify();
      return;
    }
    setStatus('unauthenticated');
  }, [verify]);

  return {
    status,
    user,
    error,
    login,
    changePassword,
    logout,
    refresh: verify,
  };
};

export default useAuth;
