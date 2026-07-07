import { useEffect, useRef, useState } from 'react';
import { Compass } from 'lucide-react';
import useStreamManager from '../hooks/useStreamManager';
import { APP_VERSION, CHANNEL } from './version';
import ForceChangePasswordPage from '../components/ForceChangePasswordPage';
import { useAuthContext } from './useAuthContext';
import LoginPage from './LoginPage';
import OnboardingWizard from './OnboardingWizard';
import { wizardSeen } from './wizardState';
import ChatWorkspace from './ChatWorkspace';
import KnowledgeWorkbench from './KnowledgeWorkbench';
import KbSidebarTree from './KbSidebarTree';
import useKbBrowser from './useKbBrowser';
import ConfigCenter from './ConfigCenter';

const SURFACES = { chat: '新建对话', kb: '知识包工作台', config: '配置中心' };

export default function AppShell() {
  const auth = useAuthContext();
  const { status, user, logout, changePassword, isAdmin, workspaces, workspaceId, defaultWorkspaceId, switchWorkspace, can } = auth;
  const [surface, setSurface] = useState('chat');
  const [cfgScope, setCfgScope] = useState('global');
  const [cfgSec, setCfgSec] = useState('llm');
  const [wsMenu, setWsMenu] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const streamManager = useStreamManager();
  const kb = useKbBrowser();
  const wsMenuRef = useRef(null);
  const userMenuRef = useRef(null);
  const kbImportRef = useRef(null);
  const [chatHistoryEl, setChatHistoryEl] = useState(null);

  // 每次新登录弹出操作向导（会话级标记，跳过/完成后本次不再弹）
  useEffect(() => {
    if (status !== 'authenticated' || user?.password_change_required || wizardSeen()) return undefined;
    const timer = setTimeout(() => setWizardOpen(true), 0);
    return () => clearTimeout(timer);
  }, [status, user?.password_change_required]);

  useEffect(() => {
    const onDocClick = (e) => {
      if (wsMenuRef.current && !wsMenuRef.current.contains(e.target)) setWsMenu(false);
      if (userMenuRef.current && !userMenuRef.current.contains(e.target)) setUserMenu(false);
    };
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  }, []);

  if (status === 'checking') {
    return <div className="mwr" style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', color: '#898781' }}>加载中…</div>;
  }
  if (status !== 'authenticated') return <LoginPage />;
  if (user?.password_change_required) {
    return <ForceChangePasswordPage user={user} onChangePassword={changePassword} onLogout={logout} />;
  }

  const navigate = ({ surface: sf, scope, sec }) => {
    if (sf) setSurface(sf);
    if (scope) setCfgScope(scope);
    if (sec) setCfgSec(sec);
  };

  const navBtn = (id, icon, label, wiz) => (
    <button className={surface === id ? 'on' : ''} data-wiz={wiz} onClick={() => setSurface(id)}>
      <span style={{ width: 17, textAlign: 'center' }}>{icon}</span>{label}
    </button>
  );

  return (
    <div className="mwr shell">
      <div className="side">
        <div className="logo"><span className="mk">知</span>知识工程工具</div>
        <div className="nav">
          <div className="grp">工作</div>
          {navBtn('chat', '💬', '新建对话', 'nav-chat')}
          {surface === 'chat' && <div ref={setChatHistoryEl} className="chat-side-history" />}
          {navBtn('kb', '📦', '知识包工作台', 'nav-kb')}
          {surface === 'kb' && <KbSidebarTree kb={kb} />}
          <div className="grp">管理</div>
          {navBtn('config', '⚙️', '配置中心', 'nav-config')}
        </div>
        <div className="vercard">
          <div className="vn">powered by openjiuwen</div>
          <div className="vv">{APP_VERSION} · {CHANNEL}</div>
        </div>
      </div>
      <div className="main">
        <div className="topbar">
          <span className="crumb">{SURFACES[surface]}</span>
          <div className="wswrap" ref={wsMenuRef}>
            <div className="wschip" onClick={() => setWsMenu((v) => !v)}>
              🗂 {workspaces.find((w) => w.id === workspaceId)?.name || workspaceId || '选择工作空间'} ▾
            </div>
            {wsMenu && (
              <div className="wsmenu">
                <div className="wm-h">切换工作空间</div>
                {workspaces.map((w) => (
                  <button key={w.id} onClick={() => { switchWorkspace(w.id); setWsMenu(false); }}>
                    <span className={w.id === workspaceId ? 'cur' : ''}>{w.name}{w.id === workspaceId ? ' ✓' : ''}</span>
                    {(w.is_default || w.id === defaultWorkspaceId) && <span className="default-flag">默认</span>}
                  </button>
                ))}
                {can('workspace.manage') && (
                  <>
                    <div className="div" />
                    <button className="new" onClick={() => { setWsMenu(false); navigate({ surface: 'config', scope: 'global', sec: 'wsmgmt' }); }}>
                      ＋ 新建工作空间
                    </button>
                    <button style={{ color: 'var(--ink2)' }} onClick={() => { setWsMenu(false); navigate({ surface: 'config', scope: 'global', sec: 'wsmgmt' }); }}>
                      管理工作空间…
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
          {surface === 'kb' && kb.canImport && (
            <>
              <button className="btn btn-p" data-wiz="kb-import" disabled={kb.importing}
                onClick={() => kbImportRef.current?.click()}>
                {kb.importing ? '导入中…' : '＋ 导入知识包'}
              </button>
              <input ref={kbImportRef} type="file" accept=".zip" style={{ display: 'none' }}
                onChange={(e) => { const f = e.target.files?.[0]; if (f) kb.importZip(f); e.target.value = ''; }} />
            </>
          )}
          <button className="iconbtn" title="操作向导" onClick={() => setWizardOpen(true)}>
            <Compass size={16} strokeWidth={2} />
          </button>
          <div className="uwrap" ref={userMenuRef}>
            <button className="uchip" onClick={() => setUserMenu((v) => !v)}>
              <span className="uav">{(user.username || '?')[0].toUpperCase()}</span>
              <span className="uname">{user.username}</span>
              <span style={{ color: 'var(--muted)', fontSize: 11 }}>▾</span>
            </button>
            {userMenu && (
              <div className="wsmenu" style={{ width: 220 }}>
                <div style={{ padding: '10px 12px 8px', borderBottom: '1px solid #efeee9', marginBottom: 4 }}>
                  <div style={{ fontWeight: 650, fontSize: 13 }}>{user.username}</div>
                  <span className={`tag ${isAdmin ? 'tag-ok' : 'tag-lang'}`} style={{ marginTop: 5, display: 'inline-block' }}>
                    {isAdmin ? '平台管理员' : '普通用户'}
                  </span>
                </div>
                <button onClick={() => { setUserMenu(false); navigate({ surface: 'config', scope: 'personal', sec: 'prefs' }); }}>个人设置</button>
                <button onClick={() => { setUserMenu(false); setWizardOpen(true); }}>操作向导</button>
                <div className="div" />
                <button style={{ color: 'var(--crit)' }} onClick={logout}>退出登录</button>
              </div>
            )}
          </div>
        </div>
        <div className="content" style={surface !== 'config' ? { display: 'flex', flexDirection: 'column' } : undefined}>
          {surface === 'chat' && <div style={{ flex: 1, minHeight: 0 }}><ChatWorkspace streamManager={streamManager} historyContainer={chatHistoryEl} /></div>}
          {surface === 'kb' && <div style={{ flex: 1, minHeight: 0 }}><KnowledgeWorkbench kb={kb} /></div>}
          {surface === 'config' && <ConfigCenter scope={cfgScope} section={cfgSec} onNavigate={navigate} />}
        </div>
      </div>
      {wizardOpen && <OnboardingWizard onNavigate={navigate} onClose={() => setWizardOpen(false)} />}
    </div>
  );
}
