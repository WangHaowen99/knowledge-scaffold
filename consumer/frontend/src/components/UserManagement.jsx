import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ArrowLeft,
  Loader2,
  Plus,
  Trash2,
  Edit3,
  X,
} from 'lucide-react';
import ChangePasswordModal from './ChangePasswordModal';
import { authFetch } from '../utils/auth';
import {
  getAccountUserAction,
  getVisibleAccountUsers,
} from '../utils/accountAccess';
import { evaluatePasswordRules } from '../utils/passwordPolicy';

function PasswordRuleList({ rules }) {
  return (
    <div className="password-rule-list password-rule-list-compact">
      {rules.map((rule) => (
        <div className={`password-rule ${rule.met ? 'met' : ''}`} key={rule.id}>
          <span className="password-rule-dot" />
          <span>{rule.label}</span>
        </div>
      ))}
    </div>
  );
}

function UserManagement({
  onBack,
  currentUser = null,
  onChangePassword = null,
  mode = 'admin',
}) {
  const isAdminMode = mode === 'admin';
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);

  const [showCreate, setShowCreate] = useState(false);
  const [editUser, setEditUser] = useState(null);
  const [deleteConfirm, setDeleteConfirm] = useState(null);
  const [showSelfPasswordModal, setShowSelfPasswordModal] = useState(false);

  const [formUsername, setFormUsername] = useState('');
  const [formPassword, setFormPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const passwordRules = useMemo(
    () => evaluatePasswordRules(formPassword, {
      username: showCreate ? formUsername : editUser?.username,
    }),
    [editUser?.username, formPassword, formUsername, showCreate],
  );
  const passwordPolicyMet = passwordRules.every((rule) => rule.met);
  const passwordSubmitDisabled = (showCreate || editUser) && (!formPassword || !passwordPolicyMet);
  const visibleUsers = useMemo(
    () => getVisibleAccountUsers({ mode, currentUser, users }),
    [currentUser, mode, users],
  );

  const loadUsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    if (!isAdminMode) {
      setUsers(currentUser ? [currentUser] : []);
      setLoading(false);
      return;
    }
    try {
      const res = await authFetch('/auth/users');
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || '加载用户列表失败');
      }
      const data = await res.json();
      setUsers(data.users || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [currentUser, isAdminMode]);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  useEffect(() => {
    if (success) {
      const timer = setTimeout(() => setSuccess(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [success]);

  const resetForm = () => {
    setFormUsername('');
    setFormPassword('');
    setSubmitting(false);
  };

  const handleCreate = async () => {
    if (!formUsername.trim() || !formPassword) {
      setError('用户名和密码不能为空');
      return;
    }
    if (!passwordPolicyMet) {
      setError('密码复杂度不满足要求');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await authFetch('/auth/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: formUsername.trim(), password: formPassword, role: 'user' }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || '创建失败');
      setShowCreate(false);
      resetForm();
      setSuccess(`用户 ${formUsername.trim()} 创建成功`);
      loadUsers();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleUpdate = async () => {
    if (!formPassword) {
      setError('新密码不能为空');
      return;
    }
    if (!passwordPolicyMet) {
      setError('密码复杂度不满足要求');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await authFetch(`/auth/users/${encodeURIComponent(editUser.username)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: formPassword }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || '更新失败');
      setEditUser(null);
      resetForm();
      setSuccess(`用户 ${editUser.username} 密码已重置`);
      loadUsers();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (username) => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await authFetch(`/auth/users/${encodeURIComponent(username)}`, {
        method: 'DELETE',
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || '删除失败');
      setDeleteConfirm(null);
      setSuccess(`用户 ${username} 已删除`);
      loadUsers();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const openEdit = (user) => {
    resetForm();
    setEditUser(user);
    setError(null);
  };

  const closeModal = () => {
    setShowCreate(false);
    setEditUser(null);
    setDeleteConfirm(null);
    setShowSelfPasswordModal(false);
    resetForm();
    setError(null);
  };

  return (
    <div className="system-settings-page">
      <div className="system-settings-header">
        <div className="system-settings-heading">
          <button className="system-settings-back" onClick={onBack} type="button" aria-label="返回对话" title="返回对话">
            <ArrowLeft size={16} />
          </button>
          <div>
            <h2>{isAdminMode ? '用户管理' : '账号设置'}</h2>
            <p>{isAdminMode ? '管理系统用户账号。仅管理员可操作。' : '查看当前账号并修改密码。'}</p>
          </div>
        </div>
        {isAdminMode && (
          <div className="settings-header-actions">
            <button className="btn btn-primary settings-action-btn" onClick={() => { resetForm(); setShowCreate(true); setError(null); }} type="button">
              <Plus size={16} />
              <span>新增用户</span>
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="skill-message error" style={{ margin: '0 24px 16px' }}>
          <span>{error}</span>
          <button onClick={() => setError(null)} style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', color: '#dc2626' }}><X size={14} /></button>
        </div>
      )}
      {success && (
        <div className="skill-message success" style={{ margin: '0 24px 16px' }}>
          <span>{success}</span>
        </div>
      )}

      <div className="user-management-table-wrap">
        <table className="user-management-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>角色</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={3} className="user-management-empty">加载中...</td></tr>
            ) : visibleUsers.length === 0 ? (
              <tr><td colSpan={3} className="user-management-empty">暂无用户</td></tr>
            ) : (
              visibleUsers.map((user) => {
                const isAdmin = user.role === 'admin';
                const action = getAccountUserAction({ mode, currentUser, targetUser: user });
                const isSelfPasswordChange = action === 'change_password';
                const isCurrentAccount = action === 'current_account';
                return (
                  <tr key={user.username}>
                    <td className="user-management-username">{user.username}</td>
                    <td>
                      <span className={`user-management-role-badge ${user.role}`}>
                        {isAdmin ? '管理员' : '普通用户'}
                      </span>
                    </td>
                    <td className="user-management-actions">
                      <button
                        className="btn btn-secondary user-action-btn"
                        onClick={() => {
                          if (isCurrentAccount) {
                            return;
                          }
                          if (isSelfPasswordChange) {
                            setShowSelfPasswordModal(true);
                            setError(null);
                          } else {
                            openEdit(user);
                          }
                        }}
                        type="button"
                        title={isCurrentAccount ? '当前账号请到账号设置修改密码' : (isSelfPasswordChange ? '修改密码' : '重置密码')}
                        disabled={isCurrentAccount}
                      >
                        <Edit3 size={14} />
                        <span>
                          {isCurrentAccount && '当前账号'}
                          {isSelfPasswordChange && '修改密码'}
                          {action === 'reset_password' && '重置密码'}
                        </span>
                      </button>
                      {isAdminMode && !isAdmin && (
                        <button className="btn btn-secondary user-action-btn user-action-danger" onClick={() => { setDeleteConfirm(user.username); setError(null); }} type="button" title="删除">
                          <Trash2 size={14} />
                          <span>删除</span>
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {isAdminMode && (showCreate || editUser || deleteConfirm) && (
        <div className="modal-overlay" onClick={closeModal}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>
                {showCreate && '新增用户'}
                {editUser && `重置密码：${editUser.username}`}
                {deleteConfirm && '确认删除'}
              </h3>
              <button className="modal-close" onClick={closeModal} type="button"><X size={18} /></button>
            </div>

            {error && (
              <div className="skill-message error">
                <span>{error}</span>
              </div>
            )}

            {showCreate && (
              <div className="modal-body">
                <label className="workspace-field">
                  <span>用户名</span>
                  <input value={formUsername} onChange={(e) => setFormUsername(e.target.value)} placeholder="请输入用户名" disabled={submitting} />
                </label>
                <label className="workspace-field">
                  <span>密码</span>
                  <input type="password" value={formPassword} onChange={(e) => setFormPassword(e.target.value)} placeholder="请输入密码" disabled={submitting} />
                </label>
                <PasswordRuleList rules={passwordRules} />
              </div>
            )}

            {editUser && (
              <div className="modal-body">
                <label className="workspace-field">
                  <span>临时密码</span>
                  <input type="password" value={formPassword} onChange={(e) => setFormPassword(e.target.value)} placeholder="请输入临时密码" disabled={submitting} />
                </label>
                <PasswordRuleList rules={passwordRules} />
              </div>
            )}

            {deleteConfirm && (
              <div className="modal-body">
                <p style={{ color: '#64748b', lineHeight: 1.6 }}>
                  确定要删除用户 <strong>{deleteConfirm}</strong> 吗？此操作不可撤销。
                </p>
              </div>
            )}

            <div className="modal-footer">
              <button className="btn btn-secondary" onClick={closeModal} disabled={submitting} type="button">取消</button>
              <button
                className="btn btn-primary"
                disabled={submitting || passwordSubmitDisabled}
                onClick={() => {
                  if (showCreate) handleCreate();
                  else if (editUser) handleUpdate();
                  else if (deleteConfirm) handleDelete(deleteConfirm);
                }}
                type="button"
              >
                {submitting && <Loader2 size={14} className="spin" />}
                <span>
                  {showCreate && '创建'}
                  {editUser && '确认重置'}
                  {deleteConfirm && '确认删除'}
                </span>
              </button>
            </div>
          </div>
        </div>
      )}
      {showSelfPasswordModal && (
        <ChangePasswordModal
          currentUser={currentUser}
          onChangePassword={onChangePassword}
          onClose={() => setShowSelfPasswordModal(false)}
        />
      )}
    </div>
  );
}

export default UserManagement;
