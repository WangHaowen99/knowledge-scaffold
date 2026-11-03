// 个人设置 · 账号：资料卡（A-07）、退出登录（A-05）、修改密码（A-06）。
// 改密成功后所有设备（含当前这台）的令牌都作废（B2 第 4 条），提示后回登录页。
// 本地模式没有登录、没有密码：不放退出登录，也不出改密卡。
import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../../app/context.js';
import { toast } from '../../app/toast.jsx';
import { errorText } from '../../api/errors.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Avatar, Button, Card, Chip, Field, Inp } from '../../ui/index.jsx';
import {
  avatarText, isLocalMode, lastLoginText, passwordError, passwordFormView, profileChips, usageView,
} from './meModel.js';

const CHIP_LOOK = { ok: ['teal', 'check'], fail: ['red', 'x'], idle: ['gray', 'check'] };

function ProfileCard({ me, local }) {
  const usage = usageView(me.usage_this_month);
  const login = lastLoginText(me.last_login);
  return (
    <div className="card pad row g16">
      <Avatar text={avatarText(me)} size="lg" />
      <div className="grow">
        <div className="lg b">{me.display_name || me.username} <span className="mono sm muted">{me.username}</span></div>
        <div className="row g4 mt4">
          {profileChips(me, local).map((c) => <Chip key={c.text} tone={c.tone} icon={c.icon}>{c.text}</Chip>)}
          {login ? <span className="xs muted">{login}</span> : null}
        </div>
      </div>
      <div className="col" style={{ gap: 2, textAlign: 'right' }}>
        <span className="xs muted">我的项目</span><b className="lg num">{me.project_count ?? '—'}</b>
      </div>
      <div className="vdiv" />
      <div className="col" style={{ gap: 2, textAlign: 'right' }} title={usage.title}>
        <span className="xs muted">本月用量</span><b className="lg num">{usage.text}</b>
      </div>
    </div>
  );
}

function PasswordCard({ username }) {
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
    <Card title="修改密码" icon="lock">
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
          <span className="xs muted">保存后所有设备需重新登录</span>
        </div>
      </form>
    </Card>
  );
}

export function AccountSection() {
  const { api, user, config, logout } = useApp();
  const [me, setMe] = useState(user);
  const local = isLocalMode(user, config);

  useEffect(() => {
    let alive = true;
    api.me().then((r) => alive && setMe(r)).catch(() => {});
    return () => { alive = false; };
  }, [api]);

  return (
    <>
      <PageHead
        title="账号"
        actions={local ? null : <Button kind="danger" icon="logout" onClick={() => logout()}>退出登录</Button>}
      />
      {me ? <ProfileCard me={me} local={local} /> : null}
      {local ? null : (
        <div className="mt16" style={{ maxWidth: 640 }}>
          <PasswordCard username={me?.username || user?.username} />
        </div>
      )}
    </>
  );
}
