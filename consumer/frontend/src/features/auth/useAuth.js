// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  authFetch,
  clearAuthToken,
  getAuthToken,
  loginWithPassword,
  logoutAuthSession,
  migrateLegacyAuthState,
  subscribeAuthEvents,
} from '../../shared/api/auth';
import createLogger from '../../shared/lib/logger';
import { getLoginErrorMessage } from './loginErrors.js';

const logger = createLogger('Auth');

const sameAuthState = (left, right) => Boolean(left && right) &&
  left.accessToken === right.accessToken &&
  left.refreshToken === right.refreshToken &&
  left.sessionId === right.sessionId &&
  left.tokenVersion === right.tokenVersion;

// eslint-disable-next-line max-lines-per-function -- Preserve established boundary.
const useAuth = () => {
  const [status, setStatus] = useState('checking');
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);
  const authOperationEpochRef = useRef(0);
  const localAuthOperationRef = useRef({ id: 0, kind: 'idle' });

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
      try {
        const res = await authFetch('/auth/me');
        if (!res.ok) {
          throw new Error(`Auth check failed: ${res.status}`);
        }
        const data = await res.json();
        if (authOperationEpochRef.current !== operationEpoch) {
          return;
        }
        setUser(data.user || null);
        setStatus('authenticated');
        setError(null);
        return;
      } catch (err) {
        if (authOperationEpochRef.current !== operationEpoch) {
          return;
        }
        logger.warn('Auth check failed', { error: err.message });
        const cleared = await clearAuthToken(undefined, { broadcast: false });
        if (authOperationEpochRef.current !== operationEpoch) {
          return;
        }
        if (!cleared && attempt === 0 && getAuthToken()) {
          continue;
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
          if (!cancelled) {
            return verify();
          }
          return undefined;
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timeoutId);
    };
  }, [verify]);

  useEffect(() => {
    const handleAuthEvent = (type) => {
      if (type !== 'login' && type !== 'logout') {
        return;
      }
      const operationEpoch = authOperationEpochRef.current + 1;
      authOperationEpochRef.current = operationEpoch;
      setUser(null);
      setError(null);
      setStatus('checking');
      void clearAuthToken(undefined, { broadcast: false }).then(() => {
        if (authOperationEpochRef.current !== operationEpoch) {
          return;
        }
        if (type === 'logout') {
          setStatus('unauthenticated');
          return;
        }
        void verify();
      });
    };
    return subscribeAuthEvents(handleAuthEvent);
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
        localAuthOperationRef.current.id !== localOperation.id ||
        localAuthOperationRef.current.kind !== 'login'
      ) {
        return { success: false, stale: true };
      }

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

      if (!result.state || !sameAuthState({
        accessToken: getAuthToken(),
        refreshToken: null,
        sessionId: result.state.sessionId,
        tokenVersion: result.state.tokenVersion,
      }, result.state)) {
        localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
        return { success: false, stale: true };
      }
      authOperationEpochRef.current += 1;
      localAuthOperationRef.current = { id: localOperation.id, kind: 'idle' };
      applyAuthPayload(result.data);
      return { success: true };
    } catch (err) {
      const ownsLocalOperation = localAuthOperationRef.current.id === localOperation.id &&
        localAuthOperationRef.current.kind === 'login';
      if (!ownsLocalOperation) {
        return { success: false, stale: true };
      }
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
    if (revokeError) {
      logger.warn('Logout revoke request failed', { error: revokeError.message });
    }
    if (
      localAuthOperationRef.current.id !== localOperation.id ||
      localAuthOperationRef.current.kind !== 'logout'
    ) {
      return;
    }
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
