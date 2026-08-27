export function canLoadProtectedData(status, user) {
  return status === 'authenticated' && Boolean(user);
}

export function canLoadWorkspaceData(
  protectedDataReady,
  { loaded = false, loading = false, error = '' } = {},
  workspaceId,
  workspaces,
) {
  if (!protectedDataReady || !loaded || loading || error || !workspaceId) return false;
  return Array.isArray(workspaces) && workspaces.some((workspace) => workspace.id === workspaceId);
}

export function requiresWorkspaceData(surface, observabilityView = 'session') {
  return surface === 'chat'
    || surface === 'kb'
    || surface === 'solver'
    || (surface === 'observability' && observabilityView === 'session');
}
