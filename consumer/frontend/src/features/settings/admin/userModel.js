function text(value) {
  return String(value ?? '').trim();
}

export function normalizeUserList(payload = {}) {
  return (Array.isArray(payload.users) ? payload.users : [])
    .filter((user) => user && typeof user === 'object' && text(user.username))
    .map((user) => ({
      username: text(user.username),
      role: text(user.role) || 'user',
      password_change_required: Boolean(user.password_change_required),
    }));
}

export function canManageUserAccounts(currentUser, capability = false) {
  return Boolean(capability && currentUser?.role === 'admin');
}

export function userActionPolicy(currentUser, targetUser) {
  const username = text(targetUser?.username);
  const currentUsername = text(currentUser?.username);
  const isManager = currentUser?.role === 'admin';
  const isSelf = Boolean(username) && username === currentUsername;
  const isBootstrapAdmin = username.toLowerCase() === 'admin';
  return {
    isSelf,
    isBootstrapAdmin,
    canResetPassword: isManager && Boolean(username) && !isSelf,
    canDelete: isManager && Boolean(username) && !isSelf && !isBootstrapAdmin,
  };
}

export function classifyUserMutation(
  status,
  payload = {},
  action = 'mutation',
) {
  const message =
    text(payload.error || payload.detail) || `用户操作失败（${status}）`;
  const common = {
    message,
    code: text(payload.code),
    violations: Array.isArray(payload.violations) ?
      payload.violations.map(text).filter(Boolean) :
      [],
    workspaceIds: Array.isArray(payload.workspace_ids) ?
      payload.workspace_ids.map(text).filter(Boolean) :
      [],
    reconciliationId: text(payload.reconciliation_id),
  };
  if (status === 202 && payload.reconciliation_required) {
    return { ...common, kind: 'reconciliation_pending', action };
  }
  if (status >= 200 && status < 300) {
    return { ...common, kind: 'success', action };
  }
  if (status === 409 && payload.code === 'WORKSPACE_OWNER_TRANSFER_REQUIRED') {
    return { ...common, kind: 'owner_transfer_required', action };
  }
  if (
    status === 409 &&
    payload.code === 'USER_DELETION_RECONCILIATION_PENDING'
  ) {
    return { ...common, kind: 'reconciliation_blocked', action };
  }
  if (payload.code === 'PASSWORD_POLICY_FAILED') {
    return { ...common, kind: 'password_policy', action };
  }
  if (status === 403) {
    return { ...common, kind: 'forbidden', action };
  }
  return { ...common, kind: 'error', action };
}

export function buildCreateUserPayload(username, password) {
  return {
    username: text(username),
    password: String(password || ''),
    role: 'user',
  };
}

export function buildResetPasswordPayload(password) {
  return { password: String(password || '') };
}
