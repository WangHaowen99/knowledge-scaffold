import { useCallback, useEffect, useRef, useState } from 'react';
import { getToken, getRefreshToken, clearSession, me, importPackageWorkspace } from './api.js';
import LoginPage from './LoginPage.jsx';
import ProjectList from './ProjectList.jsx';
import ProjectWorkbenchRouter from './ProjectWorkbenchRouter.jsx';
import SettingsModal from './SettingsModal.jsx';
import Tour, { PROJECT_STEPS } from './Tour.jsx';
import {
  confirmUnsavedNavigation,
  protectUnsavedBeforeUnload,
} from './flowchartNavigationGuard.js';

function initialWorkspaceRoute() {
  if (typeof window === 'undefined') return { projectId: null, projectKind: 'modeling' };
  const params = new URLSearchParams(window.location.search);
  const projectId = params.get('project');
  const projectKind = params.get('kind') === 'package' ? 'package' : 'modeling';
  return { projectId, projectKind };
}

function replaceWorkspaceRoute(projectId, projectKind) {
  if (typeof window === 'undefined') return;
  const url = new URL(window.location.href);
  for (const key of ['project', 'kind', 'view']) url.searchParams.delete(key);
  if (projectId) {
    url.searchParams.set('project', projectId);
    url.searchParams.set('kind', projectKind);
  }
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}

export default function App() {
  const [initialRoute] = useState(initialWorkspaceRoute);
  const [status, setStatus] = useState('checking'); // checking | out | in
  const [user, setUser] = useState(null);
  const [projectId, setProjectId] = useState(initialRoute.projectId);
  const [projectKind, setProjectKind] = useState(initialRoute.projectKind);
  const [importingPackage, setImportingPackage] = useState(false);
  const [importError, setImportError] = useState('');
  const [importNotice, setImportNotice] = useState('');
  const [projectAccessNotice, setProjectAccessNotice] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState('llm');
  const [menuOpen, setMenuOpen] = useState(false);
  const [workspaceDirty, setWorkspaceDirty] = useState(false);
  const userRef = useRef(null);
  const packageInputRef = useRef(null);

  useEffect(() => {
    if (!getToken()) { setStatus('out'); return; }
    me().then((d) => { setUser(d.user); setStatus('in'); })
      .catch((e) => {
        // 只在明确鉴权拒绝且静默续期链已放弃（refresh token 已被 authFetch 清掉/不存在）时清凭据；
        // 瞬时故障（网络错、5xx、认证服务暂不可用）保留凭据，服务恢复后仍可续期
        if ((e.status === 401 || e.status === 403) && !getRefreshToken()) clearSession();
        setStatus('out');
      });
  }, []);

  // 点击菜单外任意处 / 按 Esc 关闭用户菜单。
  // 用捕获阶段 + contains 判断：页面里有组件对 click 调 stopPropagation（如优化建议卡的勾选），冒泡监听会被吞
  useEffect(() => {
    if (!menuOpen) return;
    const close = (e) => { if (!userRef.current?.contains(e.target)) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('pointerdown', close, true);
    window.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('pointerdown', close, true); window.removeEventListener('keydown', onKey); };
  }, [menuOpen]);

  useEffect(() => {
    const beforeUnload = (event) => { protectUnsavedBeforeUnload(event, workspaceDirty); };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [workspaceDirty]);

  const logout = () => {
    if (!confirmUnsavedNavigation(workspaceDirty)) return;
    clearSession(); setStatus('out'); setUser(null); setProjectId(null); setMenuOpen(false);
    setWorkspaceDirty(false);
  };

  const closeProject = () => {
    if (!confirmUnsavedNavigation(workspaceDirty)) return;
    setProjectId(null);
    setProjectKind('modeling');
    setImportError('');
    setImportNotice('');
    setWorkspaceDirty(false);
    replaceWorkspaceRoute(null, 'modeling');
  };

  const openProject = (project) => {
    const id = typeof project === 'string' ? project : project.id;
    const kind = typeof project === 'string' ? 'modeling' : (project.kind || 'modeling');
    setProjectAccessNotice('');
    setProjectId(id);
    setProjectKind(kind);
    replaceWorkspaceRoute(id, kind);
  };

  const handleProjectUnavailable = useCallback(() => {
    setProjectId(null);
    setProjectKind('modeling');
    setWorkspaceDirty(false);
    setProjectAccessNotice('项目已删除或当前账号无权访问，已返回项目列表');
    replaceWorkspaceRoute(null, 'modeling');
  }, []);

  const handleAuthenticationRequired = useCallback(() => {
    if (!getRefreshToken()) clearSession();
    setStatus('out');
    setUser(null);
    setProjectId(null);
    setProjectKind('modeling');
    setMenuOpen(false);
    setWorkspaceDirty(false);
    setProjectAccessNotice('');
    replaceWorkspaceRoute(null, 'modeling');
  }, []);

  const importPackage = async (file) => {
    if (!file) return;
    setImportingPackage(true); setImportError(''); setImportNotice('');
    try {
      const result = await importPackageWorkspace(file);
      setImportNotice((result.warnings ?? []).join('；'));
      openProject(result.project);
    } catch (error) {
      setImportError(error instanceof Error ? error.message : '知识包导入失败');
    } finally {
      setImportingPackage(false);
    }
  };

  // 操作引导联动：引导步骤可打开配置中心并切换 tab；引导结束时收起弹窗
  useEffect(() => {
    const open = (e) => { setSettingsTab(e.detail?.tab || 'llm'); setSettingsOpen(true); };
    const end = () => setSettingsOpen(false);
    window.addEventListener('mw-settings-open', open);
    window.addEventListener('mw-tour-end', end);
    return () => { window.removeEventListener('mw-settings-open', open); window.removeEventListener('mw-tour-end', end); };
  }, []);

  if (status === 'checking') {
    return <div className="mwr" style={{ display: 'flex', height: '100vh', alignItems: 'center', justifyContent: 'center', color: '#898781' }}>加载中…</div>;
  }
  if (status === 'out') {
    return <LoginPage onLogin={(u) => { setUser(u); setStatus('in'); }} />;
  }

  return (
    <div className="mwr shell">
      <div className="topbar">
        <div className="logo"><span className="mk">知</span>知识包建模工具</div>
        <span className="crumb">
          {projectId
            ? <><button type="button" className="crumb-link" onClick={closeProject}>项目</button> / 统一建模工作台</>
            : '项目'}
        </span>
        <div className="right">
          {!projectId && (
            <>
              <button className="tb-btn" disabled={importingPackage} onClick={() => packageInputRef.current?.click()}>
                {importingPackage ? '导入中…' : '导入已有知识包'}
              </button>
              <input ref={packageInputRef} type="file" accept=".zip" hidden
                onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; void importPackage(file); }} />
            </>
          )}
          <button className="tb-btn" title="功能引导"
            onClick={() => window.dispatchEvent(new CustomEvent('mw-tour-start', { detail: { scene: projectId ? 'workbench' : 'projects' } }))}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            引导
          </button>
          <button className="tb-btn" data-tour="config" onClick={() => setSettingsOpen(true)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
            配置中心
          </button>
          <span className="tb-sep" />
          <div className="tb-user" ref={userRef}>
            <button className={`tb-chip ${menuOpen ? 'open' : ''}`} title="账号菜单"
              onClick={() => setMenuOpen((v) => !v)}>
              <span className="tb-avatar">{(user?.username || '?').slice(0, 1).toUpperCase()}</span>
              <span className="tb-name">{user?.username}</span>
              <span className={`tb-caret ${menuOpen ? 'up' : ''}`}>▾</span>
            </button>
            {menuOpen && (
              <div className="tb-menu">
                <div className="tb-menu-head">
                  <span className="tb-avatar lg">{(user?.username || '?').slice(0, 1).toUpperCase()}</span>
                  <div>
                    <div className="tb-menu-name">{user?.username}</div>
                    <div className="tb-menu-sub">当前登录账号</div>
                  </div>
                </div>
                <button className="tb-menu-item" onClick={logout}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
                  退出登录
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      {importError && <div className="app-global-error">{importError}<button onClick={() => setImportError('')}>✕</button></div>}
      {importNotice && <div className="app-global-notice">{importNotice}<button onClick={() => setImportNotice('')}>✕</button></div>}
      {projectAccessNotice && <div className="app-global-notice">{projectAccessNotice}<button onClick={() => setProjectAccessNotice('')}>✕</button></div>}
      <div className="main">
        {projectId
          ? <ProjectWorkbenchRouter projectId={projectId} onUnsavedChange={setWorkspaceDirty}
              onProjectUnavailable={handleProjectUnavailable}
              onAuthenticationRequired={handleAuthenticationRequired} />
          : <ProjectList onOpen={openProject} />}
      </div>
      {!projectId && <Tour scene="projects" steps={PROJECT_STEPS} />}
      {settingsOpen && <SettingsModal user={user} tab={settingsTab} onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
