export const WORKSPACE_ACCESS_STATE = Object.freeze({
  READY: 'ready',
  LOADING: 'loading',
  ERROR: 'error',
  EMPTY: 'empty',
  UNAVAILABLE: 'unavailable',
});

export function configSectionRequiresWorkspace(scope, section) {
  return (
    scope === 'workspace' || (scope === 'personal' && section === 'memory')
  );
}

export function resolveWorkspaceAccess({
  workspaceDataReady = false,
  workspacesLoaded = false,
  workspacesLoading = false,
  workspacesError = '',
  workspaceId = '',
  workspaces = [],
} = {}) {
  if (!workspacesLoaded || workspacesLoading) {
    return WORKSPACE_ACCESS_STATE.LOADING;
  }
  if (workspacesError) {
    return WORKSPACE_ACCESS_STATE.ERROR;
  }
  if (!workspaceId || !workspaces.length) {
    return WORKSPACE_ACCESS_STATE.EMPTY;
  }
  if (
    !workspaceDataReady ||
    !workspaces.some((workspace) => workspace.id === workspaceId)
  ) {
    return WORKSPACE_ACCESS_STATE.UNAVAILABLE;
  }
  return WORKSPACE_ACCESS_STATE.READY;
}

export function configSectionDisabled(scope, section, workspaceAccess) {
  return (
    configSectionRequiresWorkspace(scope, section) &&
    workspaceAccess !== WORKSPACE_ACCESS_STATE.READY
  );
}
