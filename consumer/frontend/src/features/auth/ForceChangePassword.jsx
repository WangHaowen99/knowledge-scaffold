import { useMemo, useState } from 'react';

import PasswordRuleList from './PasswordRuleList';
import { passwordFormState } from './passwordPolicy';
import './authSettings.css';

export default function ForceChangePassword({ user, onChangePassword, onLogout, error = '' }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState('');
  const [violations, setViolations] = useState([]);
  const formState = useMemo(() => passwordFormState({
    password: newPassword,
    confirmation,
    username: user?.username,
    oldPassword,
    includeSameAsOld: true,
    serverViolations: violations,
  }), [confirmation, newPassword, oldPassword, user?.username, violations]);

  const updatePassword = (setter) => (event) => {
    setter(event.target.value);
    setLocalError('');
    setViolations([]);
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!oldPassword || !formState.canSubmit || submitting) return;
    setSubmitting(true);
    setLocalError('');
    setViolations([]);
    try {
      const result = await onChangePassword?.(oldPassword, newPassword);
      if (!result?.success) {
        setLocalError(result?.error || '修改密码失败');
        setViolations(result?.violations || []);
      }
    } catch (submitError) {
      setLocalError(submitError?.message || '修改密码失败');
      setViolations(submitError?.violations || []);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="mwr force-password-shell">
      <section className="force-password-context" aria-labelledby="force-password-title">
        <span className="force-password-mark" aria-hidden="true">MW</span>
        <h1 id="force-password-title">首次登录需要修改密码</h1>
        <p>初始密码只用于首次认证。完成修改后，当前令牌立即失效，需要使用新密码重新登录。</p>
        <ul>
          <li>平台不会显示或回传密码明文。</li>
          <li>服务端密码规则违规会在表单中逐项反馈。</li>
          <li>退出登录不会保留当前表单内容。</li>
        </ul>
      </section>
      <form className="force-password-card" onSubmit={submit}>
        <h2>更新账号凭据</h2>
        <p>账号：<strong>{user?.username || '—'}</strong></p>
        <label>当前密码<input type="password" value={oldPassword} disabled={submitting}
          autoComplete="current-password" onChange={updatePassword(setOldPassword)} /></label>
        <label>新密码<input type="password" value={newPassword} disabled={submitting}
          autoComplete="new-password" onChange={updatePassword(setNewPassword)} /></label>
        <label>确认新密码<input type="password" value={confirmation} disabled={submitting}
          autoComplete="new-password" onChange={updatePassword(setConfirmation)} /></label>
        <PasswordRuleList rules={formState.rules} confirmationEntered={Boolean(confirmation)}
          matches={formState.matches} violations={violations} />
        {(localError || error) && <div className="settings-auth-error" role="alert">{localError || error}</div>}
        <button className="btn btn-p" type="submit" disabled={submitting || !oldPassword || !formState.canSubmit}>
          {submitting ? '提交中…' : '修改密码并重新登录'}
        </button>
        <button className="btn" type="button" disabled={submitting} onClick={onLogout}>退出登录</button>
      </form>
    </main>
  );
}
