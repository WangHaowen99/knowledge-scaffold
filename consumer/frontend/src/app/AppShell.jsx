// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  ChevronDown,
  Compass,
  FolderKanban,
  LogOut,
  Menu,
  MessageSquarePlus,
  Gauge,
  PackageOpen,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings2,
  SlidersHorizontal,
  Upload,
  X,
} from 'lucide-react';
import useStreamManager from '../features/chat/hooks/useStreamManager';
import useChatSessions from '../features/chat/hooks/useChatSessions';
import useAgenticStream from '../features/chat/hooks/useAgenticStream';
import ChatWorkspace from '../features/chat/ChatWorkspace';
import ChatHistorySidebar from '../features/chat/ChatHistorySidebar';
import LoginPage from '../features/auth/LoginPage';
import { requiresWorkspaceData } from '../features/auth/accessState';
import { useAuthContext } from '../features/auth/useAuthContext';
import { useNativeFilePicker } from '../features/knowledge/lib/browserInterop';
import useKbBrowser from '../features/knowledge/hooks/useKbBrowser';
import { SkeletonState, ToastNotice } from '../shared/ui/feedback';
import { EmptyState } from '../shared/ui/primitives';
import { APP_VERSION } from './appMeta';
import OnboardingWizard from './onboarding/OnboardingWizard';
import { wizardSeen } from './onboarding/wizardState';
import {
  clearOntologyResourcePrincipal,
  ontologyResource,
} from './providers/ontologyResource';
import { ontologyRequestPolicy } from './providers/ontologyRequestPolicy';
import { clearWorkspaceRuntime } from './providers/workspaceRuntime';
import './appShell.css';

function importActionLabel(importing, reconciling) {
  if (importing) {
    return '导入中…';
  }
  if (reconciling) {
    return '核对结果中…';
  }
  return '导入知识包';
}

const KnowledgeWorkbench = lazy(
  () => import('../features/knowledge/KnowledgeWorkbench'),
);
const ConfigCenter = lazy(() => import('../features/settings/ConfigCenter'));
const ObservabilityFeature = lazy(
  () => import('../features/observability/index.jsx'),
);
const SolverCoordinationWorkbench = lazy(
  () => import('../features/solver/SolverCoordinationWorkbench'),
);

const SURFACES = {
  chat: '新建对话',
  kb: '知识包工作台',
  solver: '求解器协同',
  config: '配置中心',
  observability: '可观测性',
};
const EMPTY_TRACE_METADATA = Object.freeze({});

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function AppShell() {
  const auth = useAuthContext();
  const {
    status,
    user,
    workspacesLoaded,
    workspacesLoading,
    workspaceDataReady,
    logout,
    isAdmin,
    workspaces,
    workspaceId,
    defaultWorkspaceId,
    workspacesError,
    reloadWorkspaces,
    switchWorkspace,
    can,
  } = auth;
  const [surface, setSurface] = useState('chat');
  const [cfgScope, setCfgScope] = useState('global');
  const [cfgSec, setCfgSec] = useState('llm');
  const [observabilityView, setObservabilityView] = useState('session');
  const [wsMenu, setWsMenu] = useState(false);
  const [userMenu, setUserMenu] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [isNarrowViewport, setIsNarrowViewport] = useState(
    () =>
      typeof window !== 'undefined' &&
      window.matchMedia?.('(max-width: 900px)').matches,
  );
  const [sideCollapsed, setSideCollapsed] = useState(() => {
    try {
      return localStorage.getItem('mw_sidebar_collapsed') === '1';
    } catch {
      return false;
    }
  });
  const [kbFocus, setKbFocus] = useState(null);
  const [solverResetToken, setSolverResetToken] = useState(0);
  const principalId = String(user?.username || '');
  const ontologyPolicy = ontologyRequestPolicy({
    surface,
    configScope: cfgScope,
    configSection: cfgSec,
  });
  const streamManager = useStreamManager();
  const chat = useChatSessions(streamManager);
  const agenticStream = useAgenticStream({
    sessionId: chat.currentChatId,
    setMessages: chat.setMessages,
    streamManager,
    scopeKey: workspaceId,
  });
  const kb = useKbBrowser({
    listEnabled: ontologyPolicy.listEnabled,
    treeEnabled: ontologyPolicy.treeEnabled,
    ontologyResource,
  });
  const kbImportPicker = useNativeFilePicker({
    onSelect: kb.importZip,
    onCancel: kb.cancelImportSelection,
  });
  const currentTraceMetadata =
    chat.sessions.find((session) => session.id === chat.currentChatId)
      ?.metadata || EMPTY_TRACE_METADATA;
  const wsMenuRef = useRef(null);
  const userMenuRef = useRef(null);
  const mobileNavRef = useRef(null);
  const mobileMenuButtonRef = useRef(null);
  const wizardOpenRef = useRef(wizardOpen);
  wizardOpenRef.current = wizardOpen;
  const openMobileNavigation = useCallback(() => setMobileNavOpen(true), []);
  const toggleDesktopSidebar = () =>
    setSideCollapsed((current) => {
      const next = !current;
      try {
        localStorage.setItem('mw_sidebar_collapsed', next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });
  const resetSolver = useCallback(
    () => setSolverResetToken((value) => value + 1),
    [],
  );
  const handleLogout = useCallback(() => {
    clearOntologyResourcePrincipal(principalId);
    return logout();
  }, [logout, principalId]);

  // 每次新登录弹出操作向导（会话级标记，跳过/完成后本次不再弹）
  useEffect(() => {
    const wizardUnavailable =
      status !== 'authenticated' ||
      !workspacesLoaded ||
      workspacesLoading ||
      workspacesError ||
      wizardSeen();
    if (wizardUnavailable) {
      return undefined;
    }
    const timer = setTimeout(() => setWizardOpen(true), 0);
    return () => clearTimeout(timer);
  }, [status, workspacesLoaded, workspacesLoading, workspacesError]);

  useEffect(() => {
    const media = window.matchMedia?.('(max-width: 900px)');
    if (!media) {
      return undefined;
    }
    const syncViewport = () => {
      setIsNarrowViewport(media.matches);
      if (!media.matches) {
        setMobileNavOpen(false);
      }
    };
    syncViewport();
    media.addEventListener?.('change', syncViewport);
    return () => media.removeEventListener?.('change', syncViewport);
  }, []);

  useEffect(() => {
    clearWorkspaceRuntime({ streamManager, chat, resetSolver });
    if (!workspaceDataReady) {
      return undefined;
    }
    const t = setTimeout(() => chat.loadSessions(false), 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceDataReady, workspaceId, chat.loadSessions]);

  useEffect(() => {
    const onDocClick = (e) => {
      if (wsMenuRef.current && !wsMenuRef.current.contains(e.target)) {
        setWsMenu(false);
      }
      if (userMenuRef.current && !userMenuRef.current.contains(e.target)) {
        setUserMenu(false);
      }
    };
    document.addEventListener('click', onDocClick);
    return () => document.removeEventListener('click', onDocClick);
  }, []);

  useEffect(() => {
    if (!mobileNavOpen || !isNarrowViewport || wizardOpen) {
      return undefined;
    }
    const navigation = mobileNavRef.current;
    const previouslyFocused = document.activeElement;
    const fallbackTrigger = mobileMenuButtonRef.current;
    const focusable = () =>
      Array.from(
        navigation?.querySelectorAll(
          [
            'button:not([disabled])',
            'input:not([disabled])',
            'select:not([disabled])',
            'textarea:not([disabled])',
            'a[href]',
            '[tabindex]:not([tabindex="-1"])',
          ].join(', '),
        ) || [],
      ).filter((element) => {
        const style = window.getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden';
      });
    focusable()[0]?.focus();
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setMobileNavOpen(false);
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const items = focusable();
      if (!items.length) {
        return;
      }
      const [first, last] = [items[0], items.at(-1)];
      if (!navigation?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else {
        return;
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      if (wizardOpenRef.current) {
        return;
      }
      if (
        previouslyFocused instanceof HTMLElement &&
        previouslyFocused !== document.body &&
        previouslyFocused.isConnected
      ) {
        previouslyFocused.focus();
      } else {
        fallbackTrigger?.focus();
      }
    };
  }, [mobileNavOpen, isNarrowViewport, wizardOpen]);

  useEffect(() => {
    const openKnowledgeSource = (event) => {
      const detail = event.detail || {};
      if (!detail.path) {
        return;
      }
      if (detail.ontologyId) {
        kb.selectPkg(detail.ontologyId);
      }
      setKbFocus({
        ontologyId: detail.ontologyId || '',
        path: detail.path,
        token: Date.now(),
      });
      setSurface('kb');
    };
    window.addEventListener('mwr-open-kb-source', openKnowledgeSource);
    return () =>
      window.removeEventListener('mwr-open-kb-source', openKnowledgeSource);
  }, [kb]);

  if (status === 'checking') {
    return (
      <div
        className="mwr"
        style={{
          display: 'flex',
          height: '100vh',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#898781',
        }}
      >
        加载中…
      </div>
    );
  }
  if (status !== 'authenticated') {
    return <LoginPage />;
  }
  const navigate = ({ surface: sf, scope, sec, tab, ontologyId }) => {
    if (sf) {
      setSurface(sf);
    }
    if (scope) {
      setCfgScope(scope);
    }
    if (sec) {
      setCfgSec(sec);
    }
    if (sf === 'kb' && ontologyId) {
      kb.selectPkg(ontologyId);
    }
    if (sf === 'kb' && tab) {
      setKbFocus({
        ontologyId: ontologyId || kb.pkgId || '',
        tab,
        token: Date.now(),
      });
    }
    setMobileNavOpen(false);
  };

  const navBtn = (id, Icon, label, wiz) => (
    <button
      className={surface === id ? 'on' : ''}
      data-wiz={wiz}
      title={label}
      onClick={() => {
        setSurface(id);
        setMobileNavOpen(false);
      }}
    >
      <Icon className="nav-icon" size={17} strokeWidth={1.9} />
      <span className="nav-label">{label}</span>
    </button>
  );
  const openObservability = (view = 'session') => {
    setObservabilityView(view === 'global' ? 'global' : 'session');
    setSurface('observability');
    setMobileNavOpen(false);
  };
  const isFullSurface =
    surface === 'chat' || surface === 'kb' || surface === 'solver';
  const workspaceSurfaceBlocked =
    requiresWorkspaceData(surface, observabilityView) && !workspaceDataReady;
  const canCreateWorkspace = can('workspace.create');
  const mobileNavigationInert =
    isNarrowViewport && (!mobileNavOpen || wizardOpen);
  const mainContentInert = isNarrowViewport && mobileNavOpen;
  let workspaceBlockedView;
  if (!workspacesLoaded || workspacesLoading) {
    workspaceBlockedView = <SkeletonState rows={4} label="正在加载工作空间" />;
  } else if (workspacesError) {
    workspaceBlockedView = (
      <EmptyState
        title="工作空间加载失败"
        description={workspacesError}
        action={
          <button className="btn" type="button" onClick={reloadWorkspaces}>
            重新加载
          </button>
        }
      />
    );
  } else {
    workspaceBlockedView = (
      <EmptyState
        title="尚无可用工作空间"
        description="创建第一个工作空间后，即可配置模型、导入知识包并开始对话。"
        action={canCreateWorkspace ? (
          <button
            className="btn btn-p"
            type="button"
            onClick={() => navigate({ surface: 'config', scope: 'global', sec: 'wsmgmt' })}
          >
            新建工作空间
          </button>
        ) : undefined}
      />
    );
  }

  return (
    <div className={`mwr shell ${sideCollapsed ? 'side-collapsed' : ''}`}>
      <aside
        ref={mobileNavRef}
        className={`side ${mobileNavOpen ? 'side-open' : ''}`}
        aria-label="主导航"
        aria-hidden={mobileNavigationInert ? 'true' : undefined}
        inert={mobileNavigationInert}
      >
        <div className="logo">
          <img className="mk" src="/brand-mark.png" alt="" aria-hidden="true" />
          <span className="logo-title">知识工程工具</span>
          <button
            className="mobile-side-close"
            type="button"
            aria-label="关闭导航"
            onClick={() => setMobileNavOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        <div className="nav">
          <div className="grp">工作</div>
          <button
            className={surface === 'chat' && !chat.currentChatId ? 'on' : ''}
            data-wiz="nav-chat"
            title="新建对话"
            onClick={() => {
              setSurface('chat');
              setMobileNavOpen(false);
              chat.startNewChat();
            }}
          >
            <MessageSquarePlus
              className="nav-icon"
              size={17}
              strokeWidth={1.9}
            />
            <span className="nav-label">新建对话</span>
          </button>
          {navBtn('kb', PackageOpen, '知识包工作台', 'nav-kb')}
          <div className="grp">管理</div>
          {navBtn('config', Settings2, '配置中心', 'nav-config')}
          {can('observability.global') && (
            <button
              className={
                surface === 'observability' && observabilityView === 'global' ?
                  'on' :
                  ''
              }
              title="全局可观测性"
              onClick={() => openObservability('global')}
            >
              <Gauge className="nav-icon" size={17} strokeWidth={1.9} />
              <span className="nav-label">运行观测</span>
            </button>
          )}
        </div>
        {workspaceDataReady && (
          <ChatHistorySidebar
            chat={chat}
            onOpen={(id) => {
              setSurface('chat');
              chat.selectChat(id);
            }}
          />
        )}
        <div className="vercard">
          <div className="vn">知识工程 控制台</div>
          <div className="vv">{APP_VERSION} · powered by openJiuwen</div>
        </div>
      </aside>
      {mobileNavOpen && (
        <button
          className="side-backdrop"
          type="button"
          aria-label="关闭导航"
          aria-hidden={wizardOpen ? 'true' : undefined}
          inert={wizardOpen}
          onClick={() => setMobileNavOpen(false)}
        />
      )}
      <div
        className="main"
        aria-hidden={mainContentInert ? 'true' : undefined}
        inert={mainContentInert}
      >
        <div className="topbar">
          <button
            ref={mobileMenuButtonRef}
            className="iconbtn mobile-menu-btn"
            type="button"
            aria-label="打开导航"
            onClick={openMobileNavigation}
          >
            <Menu size={18} />
          </button>
          <button
            className="iconbtn desktop-sidebar-toggle"
            type="button"
            aria-label={sideCollapsed ? '展开侧栏' : '折叠侧栏'}
            title={sideCollapsed ? '展开侧栏' : '折叠侧栏'}
            onClick={toggleDesktopSidebar}
          >
            {sideCollapsed ? (
              <PanelLeftOpen size={17} />
            ) : (
              <PanelLeftClose size={17} />
            )}
          </button>
          <span className="crumb">{SURFACES[surface]}</span>
          <div className="wswrap" ref={wsMenuRef}>
            <button
              className="wschip"
              type="button"
              aria-haspopup="menu"
              aria-expanded={wsMenu}
              onClick={() => setWsMenu((v) => !v)}
            >
              <FolderKanban size={15} strokeWidth={1.9} />
              <span className="wschip-label">
                {workspaces.find((w) => w.id === workspaceId)?.name ||
                  workspaceId ||
                  '选择工作空间'}
              </span>
              <ChevronDown size={14} strokeWidth={1.9} />
            </button>
            {wsMenu && (
              <div className="wsmenu">
                <div className="wm-h">切换工作空间</div>
                {workspaces.map((w) => (
                  <button
                    key={w.id}
                    onClick={() => {
                      switchWorkspace(w.id);
                      setWsMenu(false);
                    }}
                  >
                    <span className={w.id === workspaceId ? 'cur' : ''}>
                      {w.name}
                      {w.id === workspaceId ? ' ✓' : ''}
                    </span>
                    {(w.is_default || w.id === defaultWorkspaceId) && (
                      <span className="default-flag">默认</span>
                    )}
                  </button>
                ))}
                {(canCreateWorkspace || can('workspace.manage')) && (
                  <>
                    <div className="div" />
                    {canCreateWorkspace && (
                      <button
                        className="new"
                        onClick={() => {
                          setWsMenu(false);
                          navigate({
                            surface: 'config',
                            scope: 'global',
                            sec: 'wsmgmt',
                          });
                        }}
                      >
                        <span>
                          <Plus size={14} /> 新建工作空间
                        </span>
                      </button>
                    )}
                    <button
                      style={{ color: 'var(--ink2)' }}
                      onClick={() => {
                        setWsMenu(false);
                        navigate({
                          surface: 'config',
                          scope: 'global',
                          sec: 'wsmgmt',
                        });
                      }}
                    >
                      管理工作空间…
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
          {surface === 'kb' && workspaceDataReady && kb.canImport && (
            <>
              <button
                className="btn btn-p"
                data-wiz="kb-import"
                disabled={kb.importLocked}
                onClick={() => kbImportPicker.open()}
              >
                <Upload size={14} />
                {importActionLabel(kb.importing, kb.importReconciling)}
              </button>
              <input
                ref={kbImportPicker.inputRef}
                type="file"
                accept=".zip"
                style={{ display: 'none' }}
                onChange={kbImportPicker.handleChange}
              />
            </>
          )}
          <button
            className="iconbtn"
            title="操作向导"
            aria-label="打开操作向导"
            onClick={() => setWizardOpen(true)}
          >
            <Compass size={16} strokeWidth={2} />
          </button>
          <div className="uwrap" ref={userMenuRef}>
            <button className="uchip" onClick={() => setUserMenu((v) => !v)}>
              <span className="uav">
                {(user.username || '?')[0].toUpperCase()}
              </span>
              <span className="uname">{user.username}</span>
              <ChevronDown className="user-chevron" size={13} />
            </button>
            {userMenu && (
              <div className="wsmenu" style={{ width: 220 }}>
                <div
                  style={{
                    padding: '10px 12px 8px',
                    borderBottom: '1px solid #efeee9',
                    marginBottom: 4,
                  }}
                >
                  <div style={{ fontWeight: 650, fontSize: 13 }}>
                    {user.username}
                  </div>
                  <span
                    className={`tag ${isAdmin ? 'tag-ok' : 'tag-lang'}`}
                    style={{ marginTop: 5, display: 'inline-block' }}
                  >
                    {isAdmin ? '平台管理员' : '普通用户'}
                  </span>
                </div>
                <button
                  onClick={() => {
                    setUserMenu(false);
                    navigate({
                      surface: 'config',
                      scope: 'personal',
                      sec: 'prefs',
                    });
                  }}
                >
                  <SlidersHorizontal size={14} />
                  个人设置
                </button>
                <button
                  onClick={() => {
                    setUserMenu(false);
                    setWizardOpen(true);
                  }}
                >
                  <Compass size={14} />
                  操作向导
                </button>
                <div className="div" />
                <button className="menu-danger" onClick={handleLogout}>
                  <LogOut size={14} />
                  退出登录
                </button>
              </div>
            )}
          </div>
        </div>
        <div
          className={isFullSurface ? 'content content-full' : 'content'}
          style={
            isFullSurface ?
              { display: 'flex', flexDirection: 'column' } :
              undefined
          }
        >
          <div key={surface} className={`surface-stage surface-${surface}`}>
            {workspaceSurfaceBlocked && (
              <div className="workspace-empty-state">
                {workspaceBlockedView}
              </div>
            )}
            {surface === 'chat' && workspaceDataReady && (
              <ChatWorkspace
                chat={chat}
                streamManager={streamManager}
                agenticStream={agenticStream}
                ontologies={kb.packages}
                ontologiesLoading={kb.pkgsLoading}
                ontologiesError={kb.packagesError}
                onRetryOntologies={kb.retryPackages}
                onOpenObservability={openObservability}
                onConfigureDataSource={(ontologyId) =>
                  navigate({ surface: 'kb', tab: 'data', ontologyId })
                }
              />
            )}
            {surface === 'kb' && workspaceDataReady && (
              <Suspense
                fallback={
                  <div className="surface-loading">
                    <SkeletonState rows={7} label="正在加载知识包工作台" />
                  </div>
                }
              >
                <KnowledgeWorkbench
                  kb={kb}
                  focusRequest={kbFocus}
                  traceMessages={chat.messages}
                  traceMetadata={currentTraceMetadata}
                />
              </Suspense>
            )}
            {surface === 'solver' && workspaceDataReady && (
              <Suspense
                fallback={
                  <div className="surface-loading">
                    <SkeletonState rows={7} label="正在加载求解器协同" />
                  </div>
                }
              >
                <SolverCoordinationWorkbench
                  key={workspaceId}
                  workspaceId={workspaceId}
                  resetToken={solverResetToken}
                />
              </Suspense>
            )}
            {surface === 'config' && (
              <Suspense
                fallback={
                  <div className="surface-loading">
                    <SkeletonState rows={7} label="正在加载配置中心" />
                  </div>
                }
              >
                <ConfigCenter
                  scope={cfgScope}
                  section={cfgSec}
                  onNavigate={navigate}
                  ontologies={kb.packages}
                  ontologiesLoading={kb.pkgsLoading}
                  ontologiesError={kb.packagesError}
                  onRetryOntologies={kb.retryPackages}
                />
              </Suspense>
            )}
            {surface === 'observability' && !workspaceSurfaceBlocked && (
              <Suspense
                fallback={
                  <div className="surface-loading">
                    <SkeletonState rows={7} label="正在加载可观测性" />
                  </div>
                }
              >
                <ObservabilityFeature
                  view={observabilityView}
                  sessionId={chat.currentChatId}
                  user={user}
                  can={can}
                  workspaceId={workspaceId}
                  workspaces={workspaces}
                  onBack={() => setSurface('chat')}
                />
              </Suspense>
            )}
          </div>
        </div>
      </div>
      {wizardOpen && (
        <OnboardingWizard
          onNavigate={navigate}
          onOpenNavigation={openMobileNavigation}
          hasNoWorkspace={
            workspacesLoaded &&
            !workspacesLoading &&
            !workspacesError &&
            workspaces.length === 0
          }
          onClose={() => setWizardOpen(false)}
        />
      )}
      <ToastNotice
        message={kb.notice}
        tone={kb.noticeTone}
        duration={kb.importing ? 0 : 5200}
      />
      <ToastNotice
        message={workspacesError}
        tone="error"
        duration={0}
        actionLabel="重新加载"
        onAction={reloadWorkspaces}
      />
    </div>
  );
}
