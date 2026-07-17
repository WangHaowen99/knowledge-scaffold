import { useEffect, useState, useCallback, useRef, useMemo } from 'react';

import TopBar from './components/TopBar';
import ChatSidebar from './components/ChatSidebar';
import AgenticChat from './components/AgenticChat';
import TaskDAGSidebar from './components/TaskDAGSidebar';
import OperationEvolution from './components/OperationEvolution';
import LoginPage from './components/LoginPage';
import ForceChangePasswordPage from './components/ForceChangePasswordPage';
import ConfigurationCenter from './components/ConfigurationCenter';
import UserManagement from './components/UserManagement';
import LocalObservabilityDashboard from './components/LocalObservabilityDashboard';
import useChatSessions from './features/chat/hooks/useChatSessions';
import useStreamManager from './features/chat/hooks/useStreamManager';
import useAuth from './features/auth/useAuth';
import createLogger from './shared/lib/logger';
import { authFetch, getCurrentWorkspaceId, setCurrentWorkspaceId } from './shared/api/auth';
import './index.css';

const logger = createLogger('App');

function AppContent({ currentUser, onLogout, onChangePassword }) {
  const isAdmin = currentUser?.role === 'admin';
  // Stream manager for multi-session support
  const streamManager = useStreamManager();
  const streamManagerRef = useRef(streamManager);
  useEffect(() => {
    streamManagerRef.current = streamManager;
  }, [streamManager]);

  const {
    sessions,
    currentChatId,
    messages,
    setMessages,
    loadingSessions,
    loadSessions,
    createChat,
    deleteChat,
    selectChat,
    startNewChat,
    resetSessionsState,
    generateSessionTitle,
  } = useChatSessions(streamManager);

  // Get loading state for current session
  const currentStreamState = streamManager.getStreamState(currentChatId);
  const isLoading = currentStreamState.isLoading;
  // Get list of sessions with active streams (for sidebar indicators)
  const activeSessionIds = streamManager.activeSessionIds;
  const currentSessionSummary = useMemo(
    () => sessions.find((session) => session.id === currentChatId) || null,
    [sessions, currentChatId]
  );

  // Stop function for current session
  const stop = useCallback(() => {
    if (currentChatId) {
      streamManager.stopStreamForSession(currentChatId);
    }
  }, [currentChatId, streamManager]);

  const [input, setInput] = useState('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [activeNavItem, setActiveNavItem] = useState('chat');
  const effectiveActiveNavItem = !isAdmin && ['local-observability', 'user-management'].includes(activeNavItem)
    ? 'chat'
    : activeNavItem;
  const [activeConfigSection, setActiveConfigSection] = useState('system');
  const [activeOntologySubItem, setActiveOntologySubItem] = useState('ontology-management');
  const [isDAGSidebarPinnedOpen, setIsDAGSidebarPinnedOpen] = useState(true);
  const [isSessionPerformanceOpen, setIsSessionPerformanceOpen] = useState(false);
  const [reasoningSelection, setReasoningSelection] = useState({ chatId: null, segment: null });
  const [ontologyOptions, setOntologyOptions] = useState([]);
  const [selectedOntologyId, setSelectedOntologyId] = useState('');
  const [workspaces, setWorkspaces] = useState([]);
  const [currentWorkspaceId, setWorkspaceIdState] = useState(() => getCurrentWorkspaceId());
  const currentWorkspaceIdRef = useRef(currentWorkspaceId);
  const currentWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === currentWorkspaceId) || null,
    [currentWorkspaceId, workspaces]
  );

  useEffect(() => {
    currentWorkspaceIdRef.current = currentWorkspaceId;
  }, [currentWorkspaceId]);

  const loadWorkspaces = useCallback(async (preferredWorkspaceId = null) => {
    const res = await authFetch('/agentic/workspaces');
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data?.detail || '加载工作空间失败');
    }

    const nextWorkspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
    const defaultWorkspaceId = typeof data.default_workspace_id === 'string' ? data.default_workspace_id : '';
    setWorkspaces(nextWorkspaces);

    const candidateIds = [
      preferredWorkspaceId,
      currentWorkspaceIdRef.current,
      getCurrentWorkspaceId(),
      defaultWorkspaceId,
      nextWorkspaces.find((item) => item.is_default)?.id,
      nextWorkspaces[0]?.id,
    ].filter(Boolean);

    const nextWorkspaceId = candidateIds.find((id) => nextWorkspaces.some((item) => item.id === id)) || '';
    setCurrentWorkspaceId(nextWorkspaceId);
    setWorkspaceIdState(nextWorkspaceId);
    return nextWorkspaces;
  }, []);

  const loadOntologies = useCallback(async () => {
    try {
      const res = await authFetch('/agentic/ontologies');
      if (!res.ok) return;
      const data = await res.json();
      const ontologies = Array.isArray(data.ontologies) ? data.ontologies : [];
      setOntologyOptions(ontologies);
      const ids = ontologies.map((ontology) => ontology.id);
      setSelectedOntologyId((currentSelectedId) => {
        if (ids.length > 0 && !ids.includes(currentSelectedId)) {
          return ids[0];
        }
        return ids.length > 0 ? currentSelectedId : '';
      });
    } catch (error) {
      if (error?.name !== 'AbortError') {
        logger.warn('Failed to load ontologies', error);
      }
    }
  }, []);

  useEffect(() => {
    // Defer ontology loading until after workspace/auth state settles for this render.
    const timeoutId = window.setTimeout(() => {
      loadOntologies();
    }, 0);
    return () => window.clearTimeout(timeoutId);
  }, [currentUser?.token, currentWorkspaceId, loadOntologies]);

  // Pending staged-patch count for the sidebar badge. Polled gently — patcher
  // is event-driven so 60s is plenty.
  const [pendingPatchCount, setPendingPatchCount] = useState(0);
  const refreshPendingPatchCount = useCallback(async () => {
    try {
      const res = await authFetch('/agentic/operations/pending');
      if (!res.ok) return;
      const body = await res.json();
      setPendingPatchCount(Number(body?.count || 0));
    } catch (error) {
      if (error?.name !== 'AbortError') {
        logger.warn('Failed to refresh pending patches', error);
      }
    }
  }, []);
  useEffect(() => {
    if (!currentUser?.token) return undefined;
    // Defer initial fetch off the effect tick to keep React's
    // set-state-in-effect lint happy and avoid a redundant render in StrictMode.
    const initial = window.setTimeout(refreshPendingPatchCount, 0);
    const id = window.setInterval(refreshPendingPatchCount, 60_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(id);
    };
  }, [currentUser?.token, currentWorkspaceId, refreshPendingPatchCount]);

  const [isCompactLayout, setIsCompactLayout] = useState(false);
  const layoutRef = useRef(null);
  const compactLayoutRef = useRef(false);

  const shouldShowDAGSidebar = effectiveActiveNavItem === 'chat' && !isCompactLayout && !isSessionPerformanceOpen;
  const isDAGSidebarOpen = shouldShowDAGSidebar && isDAGSidebarPinnedOpen;
  const activeReasoningSegment = reasoningSelection.chatId === currentChatId
    ? reasoningSelection.segment
    : null;

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      logger.info('App mounted, loading workspaces and sessions');
      loadWorkspaces()
        .then(() => loadSessions(false))
        .catch((error) => logger.warn('Failed to load workspaces on mount', error));
    }, 0);
    return () => window.clearTimeout(timeoutId);
  }, [loadSessions, loadWorkspaces]);

  const handleWorkspaceChange = useCallback(async (workspaceId) => {
    if (!workspaceId || workspaceId === currentWorkspaceId) return;
    setCurrentWorkspaceId(workspaceId);
    setWorkspaceIdState(workspaceId);
    streamManager.stopAllStreams();
    if (streamManager.clearAllCaches) {
      streamManager.clearAllCaches();
    }
    resetSessionsState();
    setOntologyOptions([]);
    setSelectedOntologyId('');
    setReasoningSelection({ chatId: null, segment: null });
    await loadSessions(false);
  }, [currentWorkspaceId, loadSessions, resetSessionsState, streamManager]);

  useEffect(() => {
    const stopAllStreams = () => {
      const sm = streamManagerRef.current;
      if (sm?.stopAllStreams) {
        sm.stopAllStreams();
      }
    };

    const handleBeforeUnload = () => stopAllStreams();
    const handlePageHide = () => stopAllStreams();

    window.addEventListener('beforeunload', handleBeforeUnload);
    window.addEventListener('pagehide', handlePageHide);

    return () => {
      stopAllStreams();
      window.removeEventListener('beforeunload', handleBeforeUnload);
      window.removeEventListener('pagehide', handlePageHide);
    };
  }, []);

  useEffect(() => {
    const layoutEl = layoutRef.current;
    if (!layoutEl || typeof ResizeObserver === 'undefined') {
      return undefined;
    }

    const updateCompactState = (width) => {
      const nextCompact = width < 1100;
      if (compactLayoutRef.current === nextCompact) {
        return;
      }
      compactLayoutRef.current = nextCompact;
      setIsCompactLayout(nextCompact);
      if (nextCompact) {
        setIsSidebarCollapsed(true);
      }
    };

    updateCompactState(layoutEl.getBoundingClientRect().width);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      updateCompactState(entry.contentRect.width);
    });

    observer.observe(layoutEl);

    return () => observer.disconnect();
  }, []);

  // Start new chat interface (no backend record until first message)
  const handleCreateChat = useCallback(() => {
    setActiveNavItem('chat');
    setIsSessionPerformanceOpen(false);
    startNewChat();
  }, [startNewChat]);

  // Select chat - no longer stops background streams
  const handleSelectChat = useCallback(
    (id) => {
      setActiveNavItem('chat');
      setReasoningSelection({ chatId: null, segment: null });
      selectChat(id);
    },
    [selectChat]
  );

  const handleDeleteChat = useCallback(
    (id) => {
      const shouldSelectLatest = id === currentChatId;
      deleteChat(id, () => {
        if (shouldSelectLatest) {
          loadSessions(true);
        }
      });
    },
    [currentChatId, deleteChat, loadSessions]
  );

  const openConfigurationCenter = useCallback((section = 'system') => {
    setActiveConfigSection(section);
    setActiveNavItem('configuration');
  }, []);

  return (
    <div className="page">
      <TopBar
        workspaces={workspaces}
        currentWorkspaceId={currentWorkspaceId}
        currentUser={currentUser}
        onWorkspaceChange={handleWorkspaceChange}
        onWorkspaceManagementClick={() => openConfigurationCenter('workspace')}
        onSettingsClick={() => openConfigurationCenter('system')}
        onConfigurationClick={openConfigurationCenter}
        onAccountSettingsClick={() => setActiveNavItem('account-settings')}
        onUserManagementClick={isAdmin ? () => setActiveNavItem('user-management') : null}
        onLocalObservabilityClick={isAdmin ? () => setActiveNavItem('local-observability') : null}
        onLogout={onLogout}
        onToggleSidebar={() => setIsSidebarCollapsed((prev) => !prev)}
        isSidebarCollapsed={isSidebarCollapsed}
        isWorkspaceManagementActive={effectiveActiveNavItem === 'configuration' && activeConfigSection !== 'system'}
        isSettingsActive={effectiveActiveNavItem === 'configuration' && activeConfigSection === 'system'}
        isConfigurationActive={effectiveActiveNavItem === 'configuration'}
        isAccountSettingsActive={effectiveActiveNavItem === 'account-settings'}
        isUserManagementActive={isAdmin && effectiveActiveNavItem === 'user-management'}
        isLocalObservabilityActive={isAdmin && effectiveActiveNavItem === 'local-observability'}
      />
      <main
        ref={layoutRef}
        className={`layout ${isSidebarCollapsed ? 'layout-collapsed' : ''} ${shouldShowDAGSidebar ? 'layout-with-dag' : ''}`}
      >
        <ChatSidebar
          sessions={sessions}
          currentChatId={currentChatId}
          loadingSessions={loadingSessions}
          isCollapsed={isSidebarCollapsed}
          currentUser={currentUser}
          activeSessionIds={activeSessionIds}
          onSelectChat={handleSelectChat}
          onDeleteChat={handleDeleteChat}
          onCreateChat={handleCreateChat}
          onRefresh={() => loadSessions(true)}
          onOpenSettings={currentUser?.role === 'admin' ? () => openConfigurationCenter('system') : null}
          onLogout={onLogout}
          onToggleSidebar={() => setIsSidebarCollapsed((prev) => !prev)}
          onNavItemChange={(navItemId, subItemId) => {
            setActiveNavItem(navItemId);
            if (navItemId === 'ontology') {
              const nextSubItemId = subItemId || 'ontology-management';
              setActiveOntologySubItem(nextSubItemId);
              if (nextSubItemId === 'ontology-management') {
                openConfigurationCenter('ontology');
              }
            }
          }}
          navBadges={{ operationEvolution: pendingPatchCount }}
        />

        <div className="main-content">
          <div className="content-area">
            {effectiveActiveNavItem === 'chat' && (
              <AgenticChat
                sessionId={currentChatId}
                sessionMetadata={currentSessionSummary?.metadata || null}
                sessionMessageCount={currentSessionSummary?.message_count || 0}
                messages={messages}
                setMessages={setMessages}
                input={input}
                setInput={setInput}
                streamManager={streamManager}
                isLoading={isLoading}
                onStop={stop}
                onGenerateTitle={generateSessionTitle}
                onCreateChat={createChat}
                activeReasoningSegment={activeReasoningSegment}
                ontologyOptions={ontologyOptions}
                currentWorkspaceId={currentWorkspaceId}
                currentWorkspaceName={currentWorkspace?.name || currentWorkspaceId}
                onNavigateToWorkspace={() => openConfigurationCenter('workspace')}
                onOpenSettings={currentUser?.role === 'admin' ? () => openConfigurationCenter('system') : null}
                onOpenOntologyConfig={() => openConfigurationCenter('ontology-bindings')}
                onPerformanceViewChange={setIsSessionPerformanceOpen}
                onSegmentClick={(segment) => {
                  setIsDAGSidebarPinnedOpen(true);
                  setReasoningSelection({ chatId: currentChatId, segment });
                }}
              />
            )}
            
            {effectiveActiveNavItem === 'configuration' && (
              <ConfigurationCenter
                activeSection={activeConfigSection}
                onSectionChange={setActiveConfigSection}
                onBack={() => setActiveNavItem('chat')}
                workspaces={workspaces}
                currentWorkspaceId={currentWorkspaceId}
                onWorkspacesRefresh={loadWorkspaces}
                onOntologiesRefresh={loadOntologies}
                onWorkspaceChange={handleWorkspaceChange}
                currentUser={currentUser}
                ontologyId={selectedOntologyId}
                ontologyOptions={ontologyOptions}
                onOntologyIdChange={setSelectedOntologyId}
              />
            )}

            {isAdmin && effectiveActiveNavItem === 'user-management' && (
              <UserManagement
                onBack={() => setActiveNavItem('chat')}
                currentUser={currentUser}
                onChangePassword={onChangePassword}
                mode="admin"
              />
            )}

            {effectiveActiveNavItem === 'account-settings' && (
              <UserManagement
                onBack={() => setActiveNavItem('chat')}
                currentUser={currentUser}
                onChangePassword={onChangePassword}
                mode="account"
              />
            )}

            {isAdmin && effectiveActiveNavItem === 'local-observability' && (
              <LocalObservabilityDashboard
                workspaces={workspaces}
                onBack={() => setActiveNavItem('chat')}
              />
            )}

            {effectiveActiveNavItem === 'ontology' && activeOntologySubItem === 'operation-evolution' && (
              <OperationEvolution
                currentUser={currentUser}
                ontologyId={selectedOntologyId}
                ontologyOptions={ontologyOptions}
                onOntologyIdChange={setSelectedOntologyId}
              />
            )}
          </div>
        </div>

        {shouldShowDAGSidebar && (
          <TaskDAGSidebar
            isOpen={isDAGSidebarOpen}
            onToggle={() => setIsDAGSidebarPinnedOpen((prev) => !prev)}
            messages={messages}
            ontologyOptions={ontologyOptions}
            activeReasoningSegment={activeReasoningSegment}
            onSegmentClick={(segment) => {
              setReasoningSelection({ chatId: currentChatId, segment });
            }}
          />
        )}
      </main>
    </div>
  );
}

function App() {
  const { status, user, error, login, changePassword, logout } = useAuth();
  const [loginPending, setLoginPending] = useState(false);

  const handleLogin = useCallback(async (username, password) => {
    setLoginPending(true);
    const result = await login(username, password);
    setLoginPending(false);
    return result;
  }, [login]);

  if (status === 'checking') {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-header">
            <div className="login-brand">本体消费智能体</div>
            <p className="login-subtitle">正在验证登录状态...</p>
          </div>
        </div>
      </div>
    );
  }

  if (status !== 'authenticated') {
    return (
      <LoginPage
        onLogin={handleLogin}
        loading={loginPending}
        error={error}
      />
    );
  }

  if (user?.password_change_required) {
    return (
      <ForceChangePasswordPage
        user={user}
        onChangePassword={changePassword}
        onLogout={logout}
        error={error}
      />
    );
  }

  return (
    <AppContent
      key={user?.username || 'user'}
      currentUser={user}
      onLogout={logout}
      onChangePassword={changePassword}
    />
  );
}

export default App;
