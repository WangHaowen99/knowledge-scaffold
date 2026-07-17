import { useMemo, useState } from 'react';

import { Modal } from '../../shared/ui/primitives';
import PasswordRuleList from './PasswordRuleList';
import { passwordFormState } from './passwordPolicy';
import './authSettings.css';

export default function ChangePasswordDialog({ currentUser, onChangePassword, onClose }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [violations, setViolations] = useState([]);
  const formState = useMemo(() => passwordFormState({
    password: newPassword,
    confirmation,
    username: currentUser?.username,
    oldPassword,
    includeSameAsOld: true,
    serverViolations: violations,
  }), [confirmation, currentUser?.username, newPassword, oldPassword, violations]);

  const updatePassword = (setter) => (event) => {
    setter(event.target.value);
    setError('');
    setViolations([]);
  };

  const submit = async () => {
    if (!oldPassword || !formState.canSubmit || submitting) return;
    setSubmitting(true);
    setError('');
    setViolations([]);
    try {
      const result = await onChangePassword?.(oldPassword, newPassword);
      if (!result?.success) {
        setError(result?.error || '修改密码失败');
        setViolations(result?.violations || []);
      }
    } catch (submitError) {
      setError(submitError?.message || '修改密码失败');
      setViolations(submitError?.violations || []);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal title="修改密码" description={`账号：${currentUser?.username || '当前账号'}`}
      onClose={() => { if (!submitting) onClose?.(); }}
      actions={(
        <>
          <button className="btn" type="button" disabled={submitting} onClick={onClose}>取消</button>
          <button className="btn btn-p" type="button" disabled={submitting || !oldPassword || !formState.canSubmit} onClick={submit}>
            {submitting ? '提交中…' : '修改并重新登录'}
          </button>
        </>
      )}>
      <div className="settings-password-form">
        <label>当前密码<input type="password" value={oldPassword} disabled={submitting}
          autoComplete="current-password" onChange={updatePassword(setOldPassword)} /></label>
        <label>新密码<input type="password" value={newPassword} disabled={submitting}
          autoComplete="new-password" onChange={updatePassword(setNewPassword)} /></label>
        <label>确认新密码<input type="password" value={confirmation} disabled={submitting}
          autoComplete="new-password" onChange={updatePassword(setConfirmation)} /></label>
        <PasswordRuleList rules={formState.rules} confirmationEntered={Boolean(confirmation)}
          matches={formState.matches} violations={violations} />
        {error && <div className="settings-auth-error" role="alert">{error}</div>}
      </div>
    </Modal>
  );
}
