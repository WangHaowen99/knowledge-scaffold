import { useCallback, useEffect, useMemo, useState } from 'react';
import useAuth from '../hooks/useAuth';
import { authFetch, getCurrentWorkspaceId, setCurrentWorkspaceId } from '../utils/auth';
import { can as canCheck } from './permissions';
import { AuthCtx } from './useAuthContext';

// 统一的认证 + 工作空间上下文：替代 props 层层下传。
// can(perm) 只做 UI 门控；服务端是唯一强制点。
export default function AuthProvider({ children }) {
  const auth = useAuth();
  const [workspaces, setWorkspaces] = useState([]);
  const [defaultWorkspaceId, setDefaultWorkspaceId] = useState(null);
  const [workspaceId, setWorkspaceIdState] = useState(() => getCurrentWorkspaceId());

  const loadWorkspaces = useCallback(async () => {
    try {
      const res = await authFetch('/agentic/workspaces');
      if (!res.ok) return;
      const data = await res.json();
      const list = data.workspaces || [];
      setWorkspaces(list);
      setDefaultWorkspaceId(data.default_workspace_id || null);
      const stored = getCurrentWorkspaceId();
      if (!stored || !list.some((w) => w.id === stored)) {
        const fallback = data.default_workspace_id || list[0]?.id || '';
        if (fallback) {
          setCurrentWorkspaceId(fallback);
          setWorkspaceIdState(fallback);
        }
      }
    } catch {
      // 网络异常时保持现状，页面各自处理空态
    }
  }, []);

  useEffect(() => {
    if (auth.status !== 'authenticated') return undefined;
    const timer = setTimeout(() => { loadWorkspaces(); }, 0);
    return () => clearTimeout(timer);
  }, [auth.status, loadWorkspaces]);

  const switchWorkspace = useCallback((id) => {
    setCurrentWorkspaceId(id);
    setWorkspaceIdState(id);
  }, []);

  const value = useMemo(() => ({
    ...auth,
    isAdmin: auth.user?.role === 'admin',
    can: (perm, ctx = {}) => canCheck(auth.user, perm, { workspaceId, ...ctx }),
    workspaces,
    workspaceId,
    defaultWorkspaceId,
    switchWorkspace,
    reloadWorkspaces: loadWorkspaces,
  }), [auth, workspaces, workspaceId, defaultWorkspaceId, switchWorkspace, loadWorkspaces]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
