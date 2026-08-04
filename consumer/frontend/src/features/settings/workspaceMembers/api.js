import { authFetch } from '../../../shared/api/auth.js';

async function requestWorkspaceMemberJson(request, path, options, fallback) {
  const response = await request(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.error || `${fallback}（HTTP ${response.status}）`);
    error.status = response.status;
    error.code = payload.code || '';
    error.username = payload.username || '';
    error.usernames = Array.isArray(payload.usernames) ? payload.usernames : [];
    error.reconciliationId = payload.reconciliation_id || '';
    throw error;
  }
  return payload;
}

const workspaceMembersPath = (workspaceId, suffix = '') => (
  `/agentic/workspaces/${encodeURIComponent(String(workspaceId || ''))}/members${suffix}`
);

export function validateWorkspaceMember(
  workspaceId,
  username,
  { request = authFetch, signal } = {},
) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  return requestWorkspaceMemberJson(
    request,
    workspaceMembersPath(workspaceId, '/validate'),
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: String(username || '').trim() }),
      signal,
    },
    '校验成员失败',
  );
}

export function saveWorkspaceMembers(
  workspaceId,
  members,
  { request = authFetch, signal } = {},
) {
  if (!workspaceId) return Promise.reject(new Error('缺少工作空间标识'));
  if (!Array.isArray(members)) return Promise.reject(new TypeError('members 必须是数组'));
  return requestWorkspaceMemberJson(
    request,
    workspaceMembersPath(workspaceId),
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ members }),
      signal,
    },
    '保存成员失败',
  );
}
