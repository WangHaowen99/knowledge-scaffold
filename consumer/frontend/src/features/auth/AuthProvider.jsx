import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import useAuth from './useAuth';
import { authFetch, getCurrentWorkspaceId, setCurrentWorkspaceId } from '../../shared/api/auth';
import { friendlyRequestError } from '../../shared/api/errors';
import { can as canCheck } from './permissions';
import { canLoadProtectedData, canLoadWorkspaceData, resolveWorkspaceSelection } from './accessState';
import { AuthCtx } from './useAuthContext';

// 统一的认证 + 工作空间上下文：替代 props 层层下传。
// can(perm) 只做 UI 门控；服务端是唯一强制点。
export default function AuthProvider({ children }) {
  const auth = useAuth();
  const [workspaces, setWorkspaces] = useState([]);
  const [defaultWorkspaceId, setDefaultWorkspaceId] = useState(null);
  const [workspacesError, setWorkspacesError] = useState('');
  const [workspacesLoaded, setWorkspacesLoaded] = useState(false);
  const [workspacesLoading, setWorkspacesLoading] = useState(false);
  const [workspaceId, setWorkspaceIdState] = useState(() => getCurrentWorkspaceId());
  // 由列表接口的 role 字段派生 {wsId: 'admin'|'member'}，供 permissions.can 的 wsRoles 分支消费
  const [wsRoles, setWsRoles] = useState({});
  const workspaceLoadEpochRef = useRef(0);
  // 当前登录用户变化（登出/换号）时，在渲染阶段重置空间与角色，避免窗口期沿用上个用户的权限。
  // 这是 React 官方“identity 变化时调整 state”模式（渲染中 setState，非 effect，lint 安全）。
  const currentUsername = auth.user?.username ?? null;
  const protectedDataReady = canLoadProtectedData(auth.status, auth.user);
  const workspaceDataReady = canLoadWorkspaceData(
    protectedDataReady,
    { loaded: workspacesLoaded, loading: workspacesLoading, error: workspacesError },
    workspaceId,
    workspaces,
  );
  const [rolesOwner, setRolesOwner] = useState(currentUsername);
  if (rolesOwner !== currentUsername) {
    workspaceLoadEpochRef.current += 1;
    setRolesOwner(currentUsername);
    setWorkspaces([]);
    setWsRoles({});
    setDefaultWorkspaceId(null);
    setWorkspacesError('');
    setWorkspacesLoaded(false);
    setWorkspacesLoading(false);
    setWorkspaceIdState(currentUsername ? getCurrentWorkspaceId(currentUsername) : '');
  }

  const loadWorkspaces = useCallback(async () => {
    if (!protectedDataReady) return;
    const loadEpoch = workspaceLoadEpochRef.current + 1;
    workspaceLoadEpochRef.current = loadEpoch;
    setWorkspacesLoading(true);
    setWorkspacesError('');
    try {
      const res = await authFetch('/agentic/workspaces');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `工作空间加载失败（${res.status}）`);
      if (workspaceLoadEpochRef.current !== loadEpoch) return;
      const list = data.workspaces || [];
      setWorkspaces(list);
      setWsRoles(Object.fromEntries(list.filter((w) => w.role).map((w) => [w.id, w.role])));
      setDefaultWorkspaceId(data.default_workspace_id || null);
      const nextWorkspaceId = resolveWorkspaceSelection({
        workspaces: list,
        storedWorkspaceId: getCurrentWorkspaceId(currentUsername),
        preferredWorkspaceId: data.preferred_workspace_id,
        defaultWorkspaceId: data.default_workspace_id,
      });
      setCurrentWorkspaceId(nextWorkspaceId, currentUsername);
      setWorkspaceIdState(nextWorkspaceId);
    } catch (error) {
      if (workspaceLoadEpochRef.current !== loadEpoch) return;
      setWorkspacesError(friendlyRequestError(error, '工作空间加载失败'));
    } finally {
      if (workspaceLoadEpochRef.current === loadEpoch) {
        setWorkspacesLoaded(true);
        setWorkspacesLoading(false);
      }
    }
  }, [protectedDataReady, currentUsername]);

  useEffect(() => {
    if (!protectedDataReady) return undefined;
    const timer = setTimeout(() => { loadWorkspaces(); }, 0);
    return () => clearTimeout(timer);
  }, [protectedDataReady, loadWorkspaces]);

  const switchWorkspace = useCallback((id) => {
    setCurrentWorkspaceId(id, currentUsername);
    setWorkspaceIdState(id);
  }, [currentUsername]);

  const value = useMemo(() => ({
    ...auth,
    protectedDataReady,
    workspacesLoaded,
    workspacesLoading,
    workspaceDataReady,
    isAdmin: auth.user?.role === 'admin',
    can: (perm, ctx = {}) => canCheck(
      auth.user ? { ...auth.user, workspace_roles: wsRoles } : auth.user,
      perm,
      { workspaceId, ...ctx },
    ),
    workspaces,
    workspaceId,
    defaultWorkspaceId,
    workspacesError,
    switchWorkspace,
    reloadWorkspaces: loadWorkspaces,
  }), [
    auth, protectedDataReady, workspacesLoaded, workspacesLoading,
    workspaceDataReady, workspaces, wsRoles, workspaceId, defaultWorkspaceId,
    workspacesError, switchWorkspace, loadWorkspaces,
  ]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
