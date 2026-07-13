import { useMemo, useState } from 'react';
import { KeyRound, Loader2, LogOut, ShieldCheck } from 'lucide-react';
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
    <div className="mwr login force-login">
      <div className="lbrand">
        <img className="mk" src="/brand-mark.png" alt="" aria-hidden="true" />
        <h1>知识工程工具</h1>
        <div className="tg">首次登录需要更新初始密码，以保护账号和工作空间数据安全。</div>
        <div className="feats">
          <div className="feat"><div className="fi"><KeyRound size={17} /></div><div><div className="ft">设置专属密码</div><div className="fd">密码仅由你掌握，平台不会显示或回传明文</div></div></div>
          <div className="feat"><div className="fi"><ShieldCheck size={17} /></div><div><div className="ft">保护工作空间</div><div className="fd">完成改密后重新登录，即可进入知识工程工作台</div></div></div>
        </div>
      </div>
      <div className="lform">
        <div>
        <form className="lcard force-password-form" onSubmit={handleSubmit}>
          <h2>首次登录修改密码</h2>
          <div className="ld">账号 {user?.username || ''} 需要设置新的登录密码</div>
          <label className="lfield">
            <span>当前密码</span>
            <input
              type="password"
              value={oldPassword}
              onChange={(event) => setOldPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <label className="lfield">
            <span>新密码</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label className="lfield">
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
          {(localError || error) && <div className="lerr">{localError || error}</div>}
          <button className="lbtn" type="submit" disabled={!canSubmit || submitting}>
            {submitting && <Loader2 size={14} className="spin" />}
            <span>{submitting ? '提交中...' : '修改密码并重新登录'}</span>
          </button>
          <button className="btn force-password-logout" type="button" onClick={onLogout} disabled={submitting}>
            <LogOut size={14} />
            <span>退出登录</span>
          </button>
        </form>
        </div>
      </div>
    </div>
  );
}

export default ForceChangePasswordPage;
