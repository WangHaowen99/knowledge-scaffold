import { useMemo, useState } from 'react';
import { Loader2, LogOut } from 'lucide-react';
import { evaluatePasswordRules } from '../utils/passwordPolicy';

function ForceChangePasswordPage({ user, onChangePassword, onLogout, error = null }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState(null);

  const rules = useMemo(
    () => evaluatePasswordRules(newPassword, {
      username: user?.username,
      oldPassword,
      includeSameAsOld: true,
    }),
    [newPassword, oldPassword, user?.username],
  );
  const passwordMatches = newPassword && confirmPassword && newPassword === confirmPassword;
  const canSubmit = oldPassword && newPassword && confirmPassword && passwordMatches && rules.every((rule) => rule.met);

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setLocalError(null);
    const result = await onChangePassword(oldPassword, newPassword);
    setSubmitting(false);
    if (!result.success) {
      setLocalError(result.error || '修改密码失败');
    }
  };

  return (
    <div className="login-page">
      <div className="login-card force-password-card">
        <div className="login-header">
          <div className="login-brand">首次登录修改密码</div>
          <p className="login-subtitle">账号 {user?.username || ''} 需要设置新的登录密码。</p>
        </div>
        <form className="login-form" onSubmit={handleSubmit}>
          <label className="login-field">
            <span>当前密码</span>
            <input
              type="password"
              value={oldPassword}
              onChange={(event) => setOldPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <label className="login-field">
            <span>新密码</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label className="login-field">
            <span>确认新密码</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>
          <div className="password-rule-list">
            {rules.map((rule) => (
              <div className={`password-rule ${rule.met ? 'met' : ''}`} key={rule.id}>
                <span className="password-rule-dot" />
                <span>{rule.label}</span>
              </div>
            ))}
            {confirmPassword && !passwordMatches && (
              <div className="password-rule failed">
                <span className="password-rule-dot" />
                <span>两次输入的新密码一致</span>
              </div>
            )}
          </div>
          {(localError || error) && <div className="login-error">{localError || error}</div>}
          <button className="btn login-submit" type="submit" disabled={!canSubmit || submitting}>
            {submitting && <Loader2 size={14} className="spin" />}
            <span>{submitting ? '提交中...' : '修改密码并重新登录'}</span>
          </button>
          <button className="btn login-secondary" type="button" onClick={onLogout} disabled={submitting}>
            <LogOut size={14} />
            <span>退出登录</span>
          </button>
        </form>
      </div>
    </div>
  );
}

export default ForceChangePasswordPage;
