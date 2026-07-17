import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AUTH_CLEARED_EVENT,
  authFetch,
  clearAuthToken,
  getAuthToken,
  getRefreshToken,
  setAuthToken,
} from '../../shared/api/auth';
import createLogger from '../../shared/lib/logger';

const logger = createLogger('Auth');

const useAuth = () => {
  const [status, setStatus] = useState('checking');
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);
  const authOperationEpochRef = useRef(0);

  const applyAuthPayload = useCallback((data) => {
    if (!data.token) {
      throw new Error('缺少登录令牌');
    }
    // remote 模式登录响应携带 refresh_token（local 模式无此字段，行为不变）
    setAuthToken(data.token, data.refresh_token);
    setUser(data.user || null);
    setStatus('authenticated');
    setError(null);
  }, []);

  const verify = useCallback(async () => {
    const operationEpoch = authOperationEpochRef.current;
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
    } catch (err) {
      if (authOperationEpochRef.current !== operationEpoch) return;
      logger.warn('Auth check failed', { error: err.message });
      clearAuthToken();
      setUser(null);
      setStatus('unauthenticated');
    }
  }, []);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void verify();
    }, 0);
    return () => window.clearTimeout(timeoutId);
  }, [verify]);

  useEffect(() => {
    const handleCleared = () => {
      authOperationEpochRef.current += 1;
      setUser(null);
      setStatus('unauthenticated');
    };
    window.addEventListener(AUTH_CLEARED_EVENT, handleCleared);
    return () => window.removeEventListener(AUTH_CLEARED_EVENT, handleCleared);
  }, []);

  const login = useCallback(async (username, password) => {
    const operationEpoch = authOperationEpochRef.current + 1;
    authOperationEpochRef.current = operationEpoch;
    setError(null);
    try {
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });

      const data = await res.json().catch(() => ({}));
      if (authOperationEpochRef.current !== operationEpoch) return { success: false, stale: true };

      if (!res.ok) {
        throw new Error(data.error || '登录失败');
      }

      applyAuthPayload(data);
      return { success: true };
    } catch (err) {
      if (authOperationEpochRef.current !== operationEpoch) return { success: false, stale: true };
      logger.warn('Login failed', { error: err.message });
      setError(err.message || '登录失败');
      setStatus('unauthenticated');
      return { success: false, error: err.message };
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
      clearAuthToken();
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
    const refreshToken = getRefreshToken();
    authOperationEpochRef.current += 1;
    clearAuthToken();
    setUser(null);
    setStatus('unauthenticated');
    try {
      if (refreshToken) {
        await fetch('/auth/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      }
    } catch (err) {
      logger.warn('Logout revoke request failed', { error: err.message });
    }
  }, []);

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
