import { useMemo, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { evaluatePasswordRules } from '../utils/passwordPolicy';

function ChangePasswordModal({ currentUser, onClose, onChangePassword }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const passwordRules = useMemo(
    () => evaluatePasswordRules(newPassword, {
      username: currentUser?.username,
      oldPassword,
      includeSameAsOld: true,
    }),
    [newPassword, oldPassword, currentUser?.username],
  );
  const passwordMatches = newPassword && confirmPassword && newPassword === confirmPassword;
  const canSubmit = oldPassword && newPassword && confirmPassword && passwordMatches && passwordRules.every((rule) => rule.met);

  const handleSubmit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await onChangePassword(oldPassword, newPassword);
      if (!result.success) {
        setError(result.error || '修改密码失败');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <h3>修改密码</h3>
          <button className="modal-close" onClick={onClose} type="button" disabled={submitting}>
            <X size={18} />
          </button>
        </div>
        <div className="modal-body">
          <label className="workspace-field">
            <span>当前密码</span>
            <input
              type="password"
              value={oldPassword}
              onChange={(event) => setOldPassword(event.target.value)}
              autoComplete="current-password"
              disabled={submitting}
            />
          </label>
          <label className="workspace-field">
            <span>新密码</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
              disabled={submitting}
            />
          </label>
          <label className="workspace-field">
            <span>确认新密码</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              autoComplete="new-password"
              disabled={submitting}
            />
          </label>
          <div className="password-rule-list password-rule-list-compact">
            {passwordRules.map((rule) => (
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
          {error && <div className="login-error">{error}</div>}
        </div>
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onClose} disabled={submitting} type="button">取消</button>
          <button className="btn btn-primary" onClick={handleSubmit} disabled={!canSubmit || submitting} type="button">
            {submitting && <Loader2 size={14} className="spin" />}
            <span>修改密码并重新登录</span>
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChangePasswordModal;
