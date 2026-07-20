import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { PasswordRuleList, passwordFormState } from '../../auth';
import { Modal } from '../../../shared/ui/primitives';
import { authFetch } from '../../../shared/api/auth';
import {
  buildCreateUserPayload,
  buildResetPasswordPayload,
  canManageUserAccounts,
  classifyUserMutation,
  normalizeUserList,
  userActionPolicy,
} from './userModel';
import './adminSettings.css';

async function responsePayload(response) {
  return response.json().catch(() => ({}));
}

function mutationMessage(result, username) {
  if (result.kind === 'owner_transfer_required') {
    const suffix = result.workspaceIds.length ? `：${result.workspaceIds.join('、')}` : '';
    return `请先转移该用户拥有的工作空间${suffix}`;
  }
  if (result.kind === 'reconciliation_blocked') {
    return result.reconciliationId
      ? `用户删除仍在对账中（${result.reconciliationId}），请稍后刷新`
      : '用户删除仍在对账中，请稍后刷新';
  }
  if (result.kind === 'reconciliation_pending') {
    return result.reconciliationId
      ? `${username} 已停用，后台清理等待对账（${result.reconciliationId}）`
      : `${username} 已停用，后台清理等待对账`;
  }
  return result.message;
}

function PasswordAdminDialog({ type, targetUser, busy, onClose, onSubmit }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [error, setError] = useState('');
  const [violations, setViolations] = useState([]);
  const effectiveUsername = type === 'create' ? username : targetUser?.username || '';
  const formState = useMemo(() => passwordFormState({
    password,
    confirmation,
    username: effectiveUsername,
    serverViolations: violations,
  }), [confirmation, effectiveUsername, password, violations]);

  const clearServerFeedback = () => {
    setError('');
    setViolations([]);
  };

  const submit = async (event) => {
    event.preventDefault();
    if (busy || !formState.canSubmit || (type === 'create' && !username.trim())) return;
    clearServerFeedback();
    const result = await onSubmit({ username: effectiveUsername.trim(), password });
    if (result?.ok) return;
    setError(result?.message || '操作失败');
    setViolations(result?.violations || []);
  };

  return (
    <Modal
      title={type === 'create' ? '新增用户' : `重置 ${targetUser?.username} 的密码`}
      description={type === 'create' ? '新用户首次登录时必须修改初始密码。' : '重置后该用户下次登录必须修改密码。'}
      onClose={busy ? undefined : onClose}
      actions={<>
        <button className="btn" type="button" disabled={busy} onClick={onClose}>取消</button>
        <button className="btn btn-p" type="submit" form="user-password-admin-form"
          disabled={busy || !formState.canSubmit || (type === 'create' && !username.trim())}>
          {busy ? '提交中…' : type === 'create' ? '创建用户' : '重置密码'}
        </button>
      </>}
    >
      <form id="user-password-admin-form" className="settings-admin-form" onSubmit={submit}>
        {type === 'create' && (
          <label>
            用户名
            <input autoFocus required maxLength={64} autoComplete="off" value={username} disabled={busy}
              onChange={(event) => { setUsername(event.target.value); clearServerFeedback(); }} />
          </label>
        )}
        <label>
          {type === 'create' ? '初始密码' : '新密码'}
          <input autoFocus={type !== 'create'} type="password" autoComplete="new-password" value={password} disabled={busy}
            onChange={(event) => { setPassword(event.target.value); clearServerFeedback(); }} />
        </label>
        <label>
          确认密码
          <input type="password" autoComplete="new-password" value={confirmation} disabled={busy}
            onChange={(event) => { setConfirmation(event.target.value); clearServerFeedback(); }} />
        </label>
        <PasswordRuleList rules={formState.rules} confirmationEntered={Boolean(confirmation)}
          matches={formState.matches} violations={violations} />
        {error && <div className="settings-admin-alert is-error" role="alert">{error}</div>}
      </form>
    </Modal>
  );
}

export default function UserAdminPanel({ currentUser, canManage = true, request = authFetch }) {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(null);
  const [dialog, setDialog] = useState(null);
  const loadControllerRef = useRef(null);
  const mutationControllerRef = useRef(null);
  const loadSequenceRef = useRef(0);
  const isAdmin = canManageUserAccounts(currentUser, canManage);

  const loadUsers = useCallback(async () => {
    if (!isAdmin) {
      setUsers([]);
      setLoading(false);
      return;
    }
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    const sequence = ++loadSequenceRef.current;
    loadControllerRef.current = controller;
    setLoading(true);
    setError('');
    try {
      const response = await request('/auth/users', { signal: controller.signal });
      const payload = await responsePayload(response);
      if (!response.ok) throw new Error(payload.error || payload.detail || `加载用户失败（${response.status}）`);
      if (!controller.signal.aborted && sequence === loadSequenceRef.current) setUsers(normalizeUserList(payload));
    } catch (loadError) {
      if (loadError?.name !== 'AbortError' && sequence === loadSequenceRef.current) {
        setError(loadError?.message || '加载用户失败');
      }
    } finally {
      if (sequence === loadSequenceRef.current) setLoading(false);
    }
  }, [isAdmin, request]);

  useEffect(() => {
    loadUsers();
    return () => {
      loadControllerRef.current?.abort();
      mutationControllerRef.current?.abort();
    };
  }, [loadUsers]);

  const closeDialog = () => {
    if (busy) return;
    setDialog(null);
  };

  const submitPasswordMutation = async ({ username, password }) => {
    const isCreate = dialog?.type === 'create';
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    setBusy(true);
    setError('');
    setNotice(null);
    try {
      const response = await request(
        isCreate ? '/auth/users' : `/auth/users/${encodeURIComponent(username)}`,
        {
          method: isCreate ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(isCreate
            ? buildCreateUserPayload(username, password)
            : buildResetPasswordPayload(password)),
          signal: controller.signal,
        },
      );
      const payload = await responsePayload(response);
      const result = classifyUserMutation(response.status, payload, isCreate ? 'create' : 'reset_password');
      if (result.kind !== 'success') {
        return { ok: false, message: mutationMessage(result, username), violations: result.violations };
      }
      setDialog(null);
      setNotice({ tone: 'success', message: isCreate ? `用户 ${username} 已创建` : `${username} 的密码已重置` });
      await loadUsers();
      return { ok: true };
    } catch (mutationError) {
      if (mutationError?.name === 'AbortError') return { ok: false, message: '请求已取消', violations: [] };
      return { ok: false, message: mutationError?.message || '操作失败', violations: [] };
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const deleteUser = async () => {
    const target = dialog?.user;
    if (!target || busy) return;
    const policy = userActionPolicy(currentUser, target);
    if (!policy.canDelete) {
      setError(policy.isSelf ? '不能删除当前登录用户' : '不能删除 admin 管理员');
      setDialog(null);
      return;
    }
    mutationControllerRef.current?.abort();
    const controller = new AbortController();
    mutationControllerRef.current = controller;
    setBusy(true);
    setError('');
    setNotice(null);
    try {
      const response = await request(`/auth/users/${encodeURIComponent(target.username)}`, {
        method: 'DELETE',
        signal: controller.signal,
      });
      const payload = await responsePayload(response);
      const result = classifyUserMutation(response.status, payload, 'delete');
      if (result.kind === 'success' || result.kind === 'reconciliation_pending') {
        setDialog(null);
        setNotice({
          tone: result.kind === 'success' ? 'success' : 'warning',
          message: result.kind === 'success' ? `用户 ${target.username} 已删除` : mutationMessage(result, target.username),
        });
        await loadUsers();
        return;
      }
      setError(mutationMessage(result, target.username));
    } catch (deleteError) {
      if (deleteError?.name !== 'AbortError') setError(deleteError?.message || '删除用户失败');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  if (!isAdmin) {
    return (
      <section className="settings-admin-panel" aria-labelledby="user-admin-title">
        <div className="settings-admin-heading"><div><h3 id="user-admin-title">用户管理</h3><p>仅系统管理员可查看与管理用户。</p></div></div>
        <div className="settings-admin-alert is-error" role="alert">当前账号没有用户管理权限。</div>
      </section>
    );
  }

  return (
    <section className="settings-admin-panel" aria-labelledby="user-admin-title">
      <div className="settings-admin-heading">
        <div><h3 id="user-admin-title">用户管理</h3><p>新建和重置后的密码只用于提交，界面不会回显或持久化明文。</p></div>
        <div className="settings-admin-actions">
          <button className="btn" type="button" disabled={loading || busy} onClick={loadUsers}>{loading ? '刷新中…' : '刷新'}</button>
          <button className="btn btn-p" type="button" disabled={busy} onClick={() => { setDialog({ type: 'create' }); setError(''); }}>新增用户</button>
        </div>
      </div>

      {error && <div className="settings-admin-alert is-error" role="alert">{error}</div>}
      {notice && <div className={`settings-admin-alert is-${notice.tone}`} role="status">{notice.message}</div>}

      {loading ? <div className="settings-admin-state" role="status">正在加载用户…</div> : (
        <div className="settings-admin-table-wrap">
          <table className="dt settings-admin-table">
            <thead><tr><th>用户名</th><th>角色</th><th>密码状态</th><th><span className="sr-only">操作</span></th></tr></thead>
            <tbody>
              {users.map((user) => {
                const policy = userActionPolicy(currentUser, user);
                return (
                  <tr key={user.username}>
                    <td><strong>{user.username}</strong>{policy.isSelf && <small>当前账号</small>}</td>
                    <td><span className={`tag ${user.role === 'admin' ? 'tag-lang' : 'tag-off'}`}>{user.role === 'admin' ? '管理员' : '用户'}</span></td>
                    <td>{user.password_change_required ? <span className="tag tag-draft">首次登录须改密</span> : <span className="tag tag-ok">已更新</span>}</td>
                    <td>
                      <div className="settings-admin-row-actions">
                        <button className="btn btn-sm" type="button" disabled={busy || !policy.canResetPassword}
                          title={policy.isSelf ? '当前账号请使用“修改密码”入口' : undefined}
                          onClick={() => { setDialog({ type: 'reset', user }); setError(''); }}>重置密码</button>
                        <button className="btn btn-sm btn-danger" type="button" disabled={busy || !policy.canDelete}
                          title={!policy.canDelete ? (policy.isSelf ? '不能删除当前账号' : '不能删除 admin 管理员') : undefined}
                          onClick={() => { setDialog({ type: 'delete', user }); setError(''); }}>删除</button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              {!users.length && <tr><td colSpan={4}><div className="settings-admin-state">暂无用户</div></td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {(dialog?.type === 'create' || dialog?.type === 'reset') && (
        <PasswordAdminDialog key={`${dialog.type}:${dialog.user?.username || 'new'}`} type={dialog.type}
          targetUser={dialog.user} busy={busy} onClose={closeDialog} onSubmit={submitPasswordMutation} />
      )}
      {dialog?.type === 'delete' && (
        <Modal
          title={`删除用户 ${dialog.user.username}`}
          description="删除会清理该用户在各工作空间的资源。若用户仍是工作空间所有者，系统会要求先转移所有权。"
          onClose={busy ? undefined : closeDialog}
          actions={<>
            <button className="btn" type="button" disabled={busy} onClick={closeDialog}>取消</button>
            <button className="btn btn-danger" type="button" disabled={busy} onClick={deleteUser}>{busy ? '删除中…' : '确认删除'}</button>
          </>}
        >
          <p className="settings-admin-danger-copy">删除返回 202 时，账号已停用，后台仍需完成资源对账。</p>
        </Modal>
      )}
    </section>
  );
}
