export const DEFAULT_WORKSPACE_PAGE_SIZE = 8;

function text(value) {
  return String(value ?? '').trim();
}

export function normalizeWorkspaceList(payload = {}) {
  const workspaces = (
    Array.isArray(payload.workspaces) ? payload.workspaces : []
  )
    .filter((item) => item && typeof item === 'object' && text(item.id))
    .map((item) => ({
      ...item,
      id: text(item.id),
      name: text(item.name) || text(item.id),
      description: text(item.description),
      owner: item.owner == null ? null : text(item.owner),
      members: Array.isArray(item.members) ?
        item.members.map(text).filter(Boolean) :
        [],
      role: item.role == null ? null : text(item.role),
      is_default: Boolean(item.is_default),
    }));
  const defaultWorkspaceId =
    text(payload.default_workspace_id) ||
    workspaces.find((item) => item.is_default)?.id ||
    '';
  return { workspaces, defaultWorkspaceId };
}

export function filterAndPaginateWorkspaces(
  workspaces,
  { query = '', page = 1, pageSize = DEFAULT_WORKSPACE_PAGE_SIZE } = {},
) {
  const normalizedQuery = text(query).toLocaleLowerCase();
  const filtered = (Array.isArray(workspaces) ? workspaces : []).filter(
    (workspace) => {
      if (!normalizedQuery) {
        return true;
      }
      return [
        workspace.id,
        workspace.name,
        workspace.description,
        workspace.owner,
        ...(Array.isArray(workspace.members) ? workspace.members : []),
      ].some((value) =>
        String(value || '')
          .toLocaleLowerCase()
          .includes(normalizedQuery),
      );
    },
  );
  const safePageSize = Math.max(
    1,
    Number(pageSize) || DEFAULT_WORKSPACE_PAGE_SIZE,
  );
  const pageCount = Math.max(1, Math.ceil(filtered.length / safePageSize));
  const currentPage = Math.min(pageCount, Math.max(1, Number(page) || 1));
  const start = (currentPage - 1) * safePageSize;
  return {
    items: filtered.slice(start, start + safePageSize),
    total: filtered.length,
    page: currentPage,
    pageCount,
    pageSize: safePageSize,
  };
}

export function canEditWorkspace(currentUser, workspace) {
  if (!currentUser || !workspace) {
    return false;
  }
  return (
    currentUser.role === 'admin' ||
    workspace.role === 'admin' ||
    workspace.owner === currentUser.username
  );
}

export function canCreateWorkspace(currentUser) {
  return Boolean(currentUser?.username);
}

export function canDeleteWorkspace(currentUser, workspace) {
  return canEditWorkspace(currentUser, workspace);
}

export function canManageWorkspaceCollection(currentUser) {
  return currentUser?.role === 'admin';
}

export function chooseWorkspaceFallback({
  deletedWorkspaceId = '',
  currentWorkspaceId = '',
  defaultWorkspaceId = '',
  workspaces = [],
} = {}) {
  if (
    currentWorkspaceId &&
    currentWorkspaceId !== deletedWorkspaceId &&
    workspaces.some((item) => item.id === currentWorkspaceId)
  ) {
    return currentWorkspaceId;
  }
  if (
    defaultWorkspaceId &&
    workspaces.some((item) => item.id === defaultWorkspaceId)
  ) {
    return defaultWorkspaceId;
  }
  return workspaces[0]?.id || '';
}

export function classifyWorkspaceDeleteResult(status, payload = {}) {
  const message =
    text(payload.detail || payload.error) || `删除工作空间失败（${status}）`;
  if (status >= 200 && status < 300) {
    return { kind: 'deleted', message: '' };
  }
  if (status === 409) {
    return { kind: 'conflict', message };
  }
  if (status === 403) {
    return { kind: 'forbidden', message };
  }
  if (status === 404) {
    return { kind: 'missing', message };
  }
  if (status === 400 && /默认工作空间.*不可删除/.test(message)) {
    return { kind: 'default_protected', message };
  }
  return { kind: 'error', message };
}

export function workspaceMutationPayload({ name = '', description = '' } = {}) {
  return { name: text(name), description: text(description) };
}
