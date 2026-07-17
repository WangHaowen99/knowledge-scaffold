import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import {
  ChevronDown, Compass, FolderKanban, LogOut, Menu, MessageSquarePlus,
  Gauge, PackageOpen, PanelLeftClose, PanelLeftOpen, Plus, Settings2, SlidersHorizontal, Upload, X,
} from 'lucide-react';
import useStreamManager from '../features/chat/hooks/useStreamManager';
import useChatSessions from '../features/chat/hooks/useChatSessions';
import { ForceChangePassword } from '../features/auth';
import { APP_VERSION } from './version';
import { useAuthContext } from './useAuthContext';
import LoginPage from './LoginPage';
import OnboardingWizard from './OnboardingWizard';
import { wizardSeen } from './wizardState';
import ChatWorkspace from './ChatWorkspace';
import ChatHistorySidebar from './ChatHistorySidebar';
import KbSidebarTree from './KbSidebarTree';
import useKbBrowser from './useKbBrowser';
import { SkeletonState, ToastNotice } from './UiFeedback';
import { clearWorkspaceRuntime } from '../app/providers/workspaceRuntime';

const KnowledgeWorkbench = lazy(() => import('./KnowledgeWorkbench'));
const ConfigCenter = lazy(() => import('./ConfigCenter'));
const ObservabilityFeature = lazy(() => import('../features/observability/index.jsx'));

const SURFACES = { chat: '新建对话', kb: '知识包工作台', config: '配置中心', observability: '可观测性' };
const EMPTY_TRACE_METADATA = Object.freeze({});

export default function AppShell() {
  const auth = useAuthContext();
  const { status, user, logout, changePassword, isAdmin, workspaces, workspaceId, defaultWorkspaceId, workspacesError, reloadWorkspaces, switchWorkspace, can } = auth;
  const [surface, setSurface] = useState('chat');
  const [cfgScope, setCfgScope] = useState('global');
  const [cfgSec, setCfgSec] = useState('llm');
  const [observabilityView, setObservabilityView] = useState('session');
  const [wsMenu, setWsMenu] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [sideCollapsed, setSideCollapsed] = useState(() => {
    try { return localStorage.getItem('mw_sidebar_collapsed') === '1'; } catch { return false; }
  });
  const [kbFocus, setKbFocus] = useState(null);
  const streamManager = useStreamManager();
  const chat = useChatSessions(streamManager);
  const kb = useKbBrowser();
  const currentTraceMetadata = chat.sessions.find((session) => session.id === chat.currentChatId)?.metadata
    || EMPTY_TRACE_METADATA;
  const wsMenuRef = useRef(null);
  const userMenuRef = useRef(null);
  const kbImportRef = useRef(null);
  const openMobileNavigation = useCallback(() => setMobileNavOpen(true), []);
  const toggleDesktopSidebar = () => setSideCollapsed((current) => {
    const next = !current;
    try { localStorage.setItem('mw_sidebar_collapsed', next ? '1' : '0'); } catch { /* ignore */ }
    return next;
  });

  // 每次新登录弹出操作向导（会话级标记，跳过/完成后本次不再弹）
  useEffect(() => {
    if (status !== 'authenticated' || user?.password_change_required || wizardSeen()) return undefined;
    const timer = setTimeout(() => setWizardOpen(true), 0);
    return () => clearTimeout(timer);
  }, [status, user?.password_change_required]);

  useEffect(() => {
    clearWorkspaceRuntime({ streamManager, chat });
    if (status !== 'authenticated') return undefined;
    const t = setTimeout(() => chat.loadSessions(false), 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, workspaceId, chat.loadSessions]);

  useEffect(() => {
    const onDocClick = (e) => {
      if (wsMenuRef.current && !wsMenuRef.current.contains(e.target)) setWsMenu(false);
      if (userMenuRef.current && !userMenuRef.current.contains(e.target)) setUserMenu(false);
    };
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  }, []);

  useEffect(() => {
    const openKnowledgeSource = (event) => {
      const detail = event.detail || {};
      if (!detail.path) return;
      if (detail.ontologyId) kb.selectPkg(detail.ontologyId);
      setKbFocus({ ontologyId: detail.ontologyId || '', path: detail.path, token: Date.now() });
      setSurface('kb');
    };
    window.addEventListener('mwr-open-kb-source', openKnowledgeSource);
    return () => window.removeEventListener('mwr-open-kb-source', openKnowledgeSource);
  }, [kb]);

  if (status === 'checking') {
    return <div className="mwr" style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', color: '#898781' }}>加载中…</div>;
  }
  if (status !== 'authenticated') return <LoginPage />;
  if (user?.password_change_required) {
    return <ForceChangePassword user={user} onChangePassword={changePassword} onLogout={logout} />;
  }

  const navigate = ({ surface: sf, scope, sec }) => {
    if (sf) setSurface(sf);
    if (scope) setCfgScope(scope);
    if (sec) setCfgSec(sec);
    setMobileNavOpen(false);
  };

  const navBtn = (id, Icon, label, wiz) => (
    <button className={surface === id ? 'on' : ''} data-wiz={wiz} title={label}
      onClick={() => { setSurface(id); setMobileNavOpen(false); }}>
      <Icon className="nav-icon" size={17} strokeWidth={1.9} /><span className="nav-label">{label}</span>
    </button>
  );
  const openObservability = (view = 'session') => {
    setObservabilityView(view === 'global' ? 'global' : 'session');
    setSurface('observability');
    setMobileNavOpen(false);
  };
  const isFullSurface = surface === 'chat' || surface === 'kb';

  return (
    <div className={`mwr shell ${sideCollapsed ? 'side-collapsed' : ''}`}>
      <aside className={`side ${mobileNavOpen ? 'side-open' : ''}`} aria-label="主导航">
        <div className="logo">
          <img className="mk" src="/brand-mark.png" alt="" aria-hidden="true" />
          <span className="logo-title">知识工程工具</span>
          <button className="mobile-side-close" type="button" aria-label="关闭导航"
            onClick={() => setMobileNavOpen(false)}>
            <X size={18} />
          </button>
        </div>
        <div className="nav">
          <div className="grp">工作</div>
          <button className={surface === 'chat' ? 'on' : ''} data-wiz="nav-chat" title="新建对话"
            onClick={() => { setSurface('chat'); setMobileNavOpen(false); chat.startNewChat(); }}>
            <MessageSquarePlus className="nav-icon" size={17} strokeWidth={1.9} /><span className="nav-label">新建对话</span>
          </button>
          {navBtn('kb', PackageOpen, '知识包工作台', 'nav-kb')}
          {surface === 'kb' && <KbSidebarTree kb={kb} />}
          <div className="grp">管理</div>
          {navBtn('config', Settings2, '配置中心', 'nav-config')}
          {can('observability.global') && (
            <button className={surface === 'observability' && observabilityView === 'global' ? 'on' : ''}
              title="全局可观测性" onClick={() => openObservability('global')}>
              <Gauge className="nav-icon" size={17} strokeWidth={1.9} /><span className="nav-label">运行观测</span>
            </button>
          )}
        </div>
        <ChatHistorySidebar chat={chat} onOpen={(id) => { setSurface('chat'); chat.selectChat(id); }} />
        <div className="vercard">
          <div className="vn">知识工程 控制台</div>
          <div className="vv">{APP_VERSION} · powered by openJiuwen</div>
        </div>
      </aside>
      {mobileNavOpen && (
        <button className="side-backdrop" type="button" aria-label="关闭导航"
          onClick={() => setMobileNavOpen(false)} />
      )}
      <div className="main">
        <div className="topbar">
          <button className="iconbtn mobile-menu-btn" type="button" aria-label="打开导航"
            onClick={() => setMobileNavOpen(true)}>
            <Menu size={18} />
          </button>
          <button className="iconbtn desktop-sidebar-toggle" type="button"
            aria-label={sideCollapsed ? '展开侧栏' : '折叠侧栏'} title={sideCollapsed ? '展开侧栏' : '折叠侧栏'}
            onClick={toggleDesktopSidebar}>
            {sideCollapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
          </button>
          <span className="crumb">{SURFACES[surface]}</span>
          <div className="wswrap" ref={wsMenuRef}>
            <button className="wschip" type="button" aria-haspopup="menu" aria-expanded={wsMenu}
              onClick={() => setWsMenu((v) => !v)}>
              <FolderKanban size={15} strokeWidth={1.9} />
              <span className="wschip-label">{workspaces.find((w) => w.id === workspaceId)?.name || workspaceId || '选择工作空间'}</span>
              <ChevronDown size={14} strokeWidth={1.9} />
            </button>
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
                      <span><Plus size={14} /> 新建工作空间</span>
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
              <button className="btn btn-p" data-wiz="kb-import" disabled={kb.importLocked}
                onClick={() => kbImportRef.current?.click()}>
                <Upload size={14} />{kb.importing ? '导入中…' : kb.importReconciling ? '核对结果中…' : '导入知识包'}
              </button>
              <input ref={kbImportRef} type="file" accept=".zip" style={{ display: 'none' }}
                onCancel={kb.cancelImportSelection}
                onChange={(e) => { const f = e.target.files?.[0]; kb.importZip(f); e.target.value = ''; }} />
            </>
          )}
          <button className="iconbtn" title="操作向导" aria-label="打开操作向导"
            onClick={() => setWizardOpen(true)}>
            <Compass size={16} strokeWidth={2} />
          </button>
          <div className="uwrap" ref={userMenuRef}>
            <button className="uchip" onClick={() => setUserMenu((v) => !v)}>
              <span className="uav">{(user.username || '?')[0].toUpperCase()}</span>
              <span className="uname">{user.username}</span>
              <ChevronDown className="user-chevron" size={13} />
            </button>
            {userMenu && (
              <div className="wsmenu" style={{ width: 220 }}>
                <div style={{ padding: '10px 12px 8px', borderBottom: '1px solid #efeee9', marginBottom: 4 }}>
                  <div style={{ fontWeight: 650, fontSize: 13 }}>{user.username}</div>
                  <span className={`tag ${isAdmin ? 'tag-ok' : 'tag-lang'}`} style={{ marginTop: 5, display: 'inline-block' }}>
                    {isAdmin ? '平台管理员' : '普通用户'}
                  </span>
                </div>
                <button onClick={() => { setUserMenu(false); navigate({ surface: 'config', scope: 'personal', sec: 'prefs' }); }}><SlidersHorizontal size={14} />个人设置</button>
                <button onClick={() => { setUserMenu(false); setWizardOpen(true); }}><Compass size={14} />操作向导</button>
                <div className="div" />
                <button className="menu-danger" onClick={logout}><LogOut size={14} />退出登录</button>
              </div>
            )}
          </div>
        </div>
        <div className={isFullSurface ? 'content content-full' : 'content'}
          style={isFullSurface ? { display: 'flex', flexDirection: 'column' } : undefined}>
          <div key={surface} className={`surface-stage surface-${surface}`}>
            {surface === 'chat' && <ChatWorkspace chat={chat} streamManager={streamManager} onOpenObservability={openObservability} />}
            {surface === 'kb' && <Suspense fallback={<div className="surface-loading"><SkeletonState rows={7} label="正在加载知识包工作台" /></div>}><KnowledgeWorkbench kb={kb} focusRequest={kbFocus} traceMessages={chat.messages} traceMetadata={currentTraceMetadata} /></Suspense>}
            {surface === 'config' && <Suspense fallback={<div className="surface-loading"><SkeletonState rows={7} label="正在加载配置中心" /></div>}><ConfigCenter scope={cfgScope} section={cfgSec} onNavigate={navigate} /></Suspense>}
            {surface === 'observability' && (
              <Suspense fallback={<div className="surface-loading"><SkeletonState rows={7} label="正在加载可观测性" /></div>}>
                <ObservabilityFeature view={observabilityView} sessionId={chat.currentChatId} user={user}
                  can={can} workspaces={workspaces} onBack={() => setSurface('chat')} />
              </Suspense>
            )}
          </div>
        </div>
      </div>
      {wizardOpen && <OnboardingWizard onNavigate={navigate} onOpenNavigation={openMobileNavigation}
        onClose={() => setWizardOpen(false)} />}
      <ToastNotice message={kb.notice} tone={kb.importLocked ? 'info' : undefined}
        duration={kb.importing ? 0 : 5200} />
      <ToastNotice message={workspacesError} tone="error" duration={0} actionLabel="重新加载" onAction={reloadWorkspaces} />
    </div>
  );
}
