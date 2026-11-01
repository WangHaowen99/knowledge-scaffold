// 顶栏：全局页（品牌 + 我的项目 / 插件中心 / 运维中心 + 铃铛）与项目页（面包屑 + 知识包版本）两种
import { useRef, useState } from 'react';
import { useApp, useProject } from '../app/context.js';
import { navigate, pagePath } from '../app/router.js';
import { Avatar, Icon, Kbd, Menu, cx, useOutside } from '../ui/index.jsx';
import { can } from '../state/roles.js';
import { openPalette } from './CommandPalette.jsx';

function UserButton() {
  const { user, logout } = useApp();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  const initial = (user?.display_name || user?.username || '?').slice(0, 1);
  return (
    <div className="tb-user" ref={ref}>
      <button type="button" className="row g4" style={{ padding: '3px 4px 3px 3px', borderRadius: 20 }} onClick={() => setOpen((v) => !v)} title="个人设置">
        <Avatar text={initial} />
        <Icon name="chevd" size={12} />
      </button>
      {open ? (
        <div className="menu">
          <div className="who">
            <div className="b">{user?.display_name || user?.username}</div>
            <div className="xs muted">{user?.username} · {user?.role_label}{user?.auth_source === 'local' ? ' · 本地模式' : ''}</div>
          </div>
          <div className="mi" onClick={() => { setOpen(false); navigate('/me'); }}><Icon name="user" size={14} /><span>个人设置</span></div>
          {can(user, 'admin') ? (
            <div className="mi" onClick={() => { setOpen(false); navigate('/sys'); }}><Icon name="gear" size={14} /><span>系统设置</span></div>
          ) : null}
          {user?.auth_source === 'local' ? null : (
            <div className="mi" onClick={() => { setOpen(false); logout(); }}><Icon name="logout" size={14} /><span>退出登录</span></div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function SearchBox() {
  return (
    <div className="tb-search" onClick={openPalette} role="button">
      <Icon name="search" size={14} />
      <span className="q">搜索、跳转或输入命令</span>
      <Kbd>⌘</Kbd><Kbd>K</Kbd>
    </div>
  );
}

export function GlobalTopbar({ active, unread, onBell }) {
  const { user } = useApp();
  const items = [['projects', '我的项目', 'grid'], ['plugins', '插件中心', 'puzzle']];
  // 运维中心只对管理员（只读）和运维开放（契约 1.3）
  if (can(user, 'ops.read')) items.push(['ops', '运维中心', 'activity']);
  return (
    <header className="topbar">
      <div className="brand"><div className="logo">本</div><span>本体建模工作台</span></div>
      <nav className="gnav">
        {items.map(([k, t, i]) => (
          <a key={k} className={active === k ? 'on' : ''} href={`#${pagePath(k)}`}>
            <Icon name={i} size={14} />{t}
          </a>
        ))}
      </nav>
      <span className="sp" />
      <SearchBox />
      <button type="button" className="icon-btn" title="通知" onClick={onBell}>
        <Icon name="bell" size={18} />
        {unread ? <span className="dot">{unread > 99 ? '99+' : unread}</span> : null}
      </button>
      <UserButton />
    </header>
  );
}

export function ProjectTopbar() {
  const { meta, recentProjects } = useProject();
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useOutside(ref, () => setOpen(false), open);
  return (
    <header className="topbar">
      <div className="brand"><div className="logo">本</div></div>
      <div className="crumb" ref={ref}>
        <a href="#/projects" style={{ color: 'var(--ink3)' }}>我的项目</a>
        <span className="sep">/</span>
        <span className="row" style={{ cursor: 'pointer', gap: 6 }} onClick={() => setOpen((v) => !v)}>
          <b>{meta?.name || '…'}</b>
          <Icon name="chevd" size={12} />
        </span>
        {meta?.version_label ? (
          <span className="chip line sm" style={{ marginLeft: 4 }}><Icon name="pkg" size={12} />知识包 {meta.version_label}</span>
        ) : null}
        {open ? (
          <Menu
            className="tb-crumb-menu"
            items={[
              ...(recentProjects || []).filter((p) => p.id !== meta?.id).slice(0, 6).map((p) => ({
                label: p.name, icon: 'cube', onClick: () => navigate(pagePath(p.landing_page || 'wb-l2', p.id)),
              })),
              '-',
              { label: '全部项目', icon: 'grid', onClick: () => navigate('/projects') },
            ]}
            onPick={() => setOpen(false)}
          />
        ) : null}
      </div>
      <span className="sp" />
      <SearchBox />
      <UserButton />
    </header>
  );
}

export function MaintenanceBanner() {
  const { maintenance } = useApp();
  if (!maintenance?.active) return null;
  const ro = maintenance.mode === 'read_only';
  return (
    <div className={cx('mnt-bar', ro && 'ro')} role="alert">
      <Icon name="wrench" size={14} />
      <b>{ro ? '平台维护中 · 只读' : '平台维护中 · 暂停新会话'}</b>
      <span className="grow ell">{maintenance.message}</span>
      {ro ? <span className="xs muted">维护期间所有修改都会被拒绝，正在跑的这一轮会跑完</span> : <span className="xs muted">已有会话可以跑完当前这一轮，网页可浏览、可编辑</span>}
    </div>
  );
}
