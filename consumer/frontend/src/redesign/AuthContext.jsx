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
  // 由列表接口的 role 字段派生 {wsId: 'admin'|'member'}，供 permissions.can 的 wsRoles 分支消费
  const [wsRoles, setWsRoles] = useState({});
  // 当前登录用户变化（登出/换号）时，在渲染阶段重置空间与角色，避免窗口期沿用上个用户的权限。
  // 这是 React 官方“identity 变化时调整 state”模式（渲染中 setState，非 effect，lint 安全）。
  const currentUsername = auth.user?.username ?? null;
  const [rolesOwner, setRolesOwner] = useState(currentUsername);
  if (rolesOwner !== currentUsername) {
    setRolesOwner(currentUsername);
    setWorkspaces([]);
    setWsRoles({});
    setDefaultWorkspaceId(null);
  }

  const loadWorkspaces = useCallback(async () => {
    try {
      const res = await authFetch('/agentic/workspaces');
      if (!res.ok) return;
      const data = await res.json();
      const list = data.workspaces || [];
      setWorkspaces(list);
      setWsRoles(Object.fromEntries(list.filter((w) => w.role).map((w) => [w.id, w.role])));
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
    can: (perm, ctx = {}) => canCheck(
      auth.user ? { ...auth.user, workspace_roles: wsRoles } : auth.user,
      perm,
      { workspaceId, ...ctx },
    ),
    workspaces,
    workspaceId,
    defaultWorkspaceId,
    switchWorkspace,
    reloadWorkspaces: loadWorkspaces,
  }), [auth, workspaces, wsRoles, workspaceId, defaultWorkspaceId, switchWorkspace, loadWorkspaces]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
