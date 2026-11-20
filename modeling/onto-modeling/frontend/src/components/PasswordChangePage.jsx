import { useState } from 'react';
import { api, authStore } from '../lib/api.js';

const VIOLATIONS = {
  min_length: '至少 8 位',
  max_length: '不超过 128 位',
  character_classes: '需包含大写、小写、数字、符号中至少三类',
  contains_username: '不能包含用户名',
  common_password: '密码过于常见',
  surrounding_whitespace: '首尾不能有空格',
  same_as_old: '不能与旧密码相同',
};

export default function PasswordChangePage({ onComplete, onLogout }) {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    if (busy) return;
    if (newPassword !== confirm) {
      setError('两次输入的新密码不一致');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api.changePassword(authStore.username, oldPassword, newPassword);
      onComplete();
    } catch (cause) {
      const violations = cause.data?.violations || [];
      setError(violations.length
        ? violations.map((code) => VIOLATIONS[code] || code).join('；')
        : cause.message || '修改密码失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-split">
      <div className="lform">
        <form className="lcard" onSubmit={submit}>
          <h2>请先修改初始密码</h2>
          <div className="ld">完成修改后，请使用新密码重新登录。</div>
          <div className="lfield">
            <label htmlFor="old-password">当前密码</label>
            <input id="old-password" type="password" autoComplete="current-password"
              value={oldPassword} onChange={(event) => setOldPassword(event.target.value)} required />
          </div>
          <div className="lfield">
            <label htmlFor="new-password">新密码</label>
            <input id="new-password" type="password" autoComplete="new-password"
              value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required />
          </div>
          <div className="lfield">
            <label htmlFor="confirm-password">确认新密码</label>
            <input id="confirm-password" type="password" autoComplete="new-password"
              value={confirm} onChange={(event) => setConfirm(event.target.value)} required />
          </div>
          {error && <div className="lerr" role="alert">{error}</div>}
          <button className="lbtn" type="submit" disabled={busy}>
            {busy ? '修改中…' : '修改密码'}
          </button>
          <div className="lswitch"><button className="llink" type="button" onClick={onLogout}>退出登录</button></div>
        </form>
      </div>
    </div>
  );
}
