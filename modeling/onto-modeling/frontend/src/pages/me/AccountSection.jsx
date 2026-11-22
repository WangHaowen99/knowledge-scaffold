// 个人设置 · 账号：资料卡（A-07）、退出登录（A-05）、修改密码（A-06）。
// 改密成功后所有设备（含当前这台）的令牌都作废（B2 第 4 条），提示后回登录页。
// 本地模式没有登录、没有密码：不放退出登录，也不出改密卡。
import { useEffect, useState } from 'react';
import { useApp } from '../../app/context.js';
import { PageHead } from '../../shell/Shell.jsx';
import { Avatar, Button, Card, Chip } from '../../ui/index.jsx';
import {
  avatarText, isLocalMode, lastLoginText, profileChips, usageView,
} from './meModel.js';
import { PasswordForm } from './PasswordForm.jsx';

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
  return (
    <Card title="修改密码" icon="lock">
      <PasswordForm username={username} aside={<span className="xs muted">保存后所有设备需重新登录</span>} />
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
