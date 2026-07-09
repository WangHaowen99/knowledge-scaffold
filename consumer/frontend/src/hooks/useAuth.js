import { useCallback, useEffect, useState } from 'react';
import { authFetch, clearAuthToken, getAuthToken, setAuthToken } from '../utils/auth';
import createLogger from '../utils/logger';

const logger = createLogger('Auth');

const useAuth = () => {
  const [status, setStatus] = useState('checking');
  const [user, setUser] = useState(null);
  const [error, setError] = useState(null);

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
    const token = getAuthToken();
    if (!token) {
      setStatus('unauthenticated');
      setUser(null);
      return;
    }

    try {
      const res = await authFetch('/auth/me');
      if (!res.ok) {
        throw new Error(`Auth check failed: ${res.status}`);
      }
      const data = await res.json();
      setUser(data.user || null);
      setStatus('authenticated');
      setError(null);
    } catch (err) {
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

  const login = useCallback(async (username, password) => {
    setError(null);
    try {
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });

      const data = await res.json().catch(() => ({}));

      if (!res.ok) {
        throw new Error(data.error || '登录失败');
      }

      applyAuthPayload(data);
      return { success: true };
    } catch (err) {
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

  const logout = useCallback(() => {
    clearAuthToken();
    setUser(null);
    setStatus('unauthenticated');
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
