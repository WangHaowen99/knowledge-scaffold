// 改密表单（A-06，规则同 A-03，另加不能与旧密码相同）：个人设置「修改密码」卡与首登改密页（ADR-0107）共用。
// 规则 chip 实时打勾；旧密码不对、服务端退回的 failed_rules 落到对应字段。改密成功后该用户所有设备（含当前这台）
// 的令牌都作废（B2 第 4 条），提示后回登录页。aside 放在提交按钮旁边。
import { useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { Button, Chip, Field, Inp } from '../../ui/index.jsx';
import { passwordError, passwordFormView } from './meModel.js';
// 规则提示（me-rule-bad）的样式；首登改密页不经个人设置页，要自己带上
import '../../styles/me.css';

const CHIP_LOOK = { ok: ['teal', 'check'], fail: ['red', 'x'], idle: ['gray', 'check'] };

export function PasswordForm({ username, aside = null }) {
  const { api, logout } = useApp();
  const [f, setF] = useState({ old: '', next: '', confirm: '' });
  const [serverFailed, setServerFailed] = useState([]);
  const [oldErr, setOldErr] = useState('');
  const [busy, setBusy] = useState(false);
  const v = useMemo(() => passwordFormView(f, username, serverFailed), [f, username, serverFailed]);
  const set = (k) => (e) => {
    const val = e.target.value;
    setF((x) => ({ ...x, [k]: val }));
    if (k === 'old') setOldErr('');
    if (k === 'next') setServerFailed([]);
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!v.canSubmit || busy) return;
    setBusy(true);
    try {
      await api.changePassword(f.old, f.next);
      toast('密码已修改，请用新密码重新登录', 'lock');
      await logout();
    } catch (err) {
      const pe = passwordError(err);
      if (pe?.field === 'old') setOldErr(pe.text);
      else if (pe?.field === 'next') setServerFailed(pe.failed);
      else toast(pe?.text || errorText(err), 'alert');
      setBusy(false);
    }
  };

  return (
    <form className="col" onSubmit={submit}>
      <Field label="当前密码" req hint={oldErr ? <span style={{ color: 'var(--red)' }}>{oldErr}</span> : null}>
        <Inp icon="lock" type="password" name="old_password" value={f.old} onChange={set('old')} error={!!oldErr} autoComplete="current-password" />
      </Field>
      <Field label="新密码" req>
        <Inp icon="key" type="password" name="new_password" value={f.next} onChange={set('next')} error={!!f.next && v.failed.length > 0} autoComplete="new-password" />
      </Field>
      <div className="row g4 xs wrap">
        {v.chips.map((c) => <Chip key={c.key} tone={CHIP_LOOK[c.state][0]} icon={CHIP_LOOK[c.state][1]} sm>{c.text}</Chip>)}
        {v.extra.map((t) => <span key={t} className="me-rule-bad">{t}</span>)}
      </div>
      <Field label="确认新密码" req hint={v.mismatch ? <span style={{ color: 'var(--red)' }}>两次输入不一致</span> : null}>
        <Inp icon="key" type="password" name="confirm_password" value={f.confirm} onChange={set('confirm')} error={v.mismatch} autoComplete="new-password" />
      </Field>
      <div className="row mt4">
        <Button kind="primary" type="submit" disabled={!v.canSubmit || busy}>{busy ? '正在保存…' : '保存新密码'}</Button>
        {aside}
      </div>
    </form>
  );
}

