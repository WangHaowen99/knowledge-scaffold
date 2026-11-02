// 应用入口：启动（C-01、A-07、无感刷新）、登录态与角色、路由
import { useCallback, useEffect, useMemo, useState } from 'react';
import ChatStatesPage from '../pages/chat-states/ChatStatesPage.jsx';
import CreatePage from '../pages/create/CreatePage.jsx';
import ImportPage from '../pages/import/ImportPage.jsx';
import LoginPage from '../pages/login/LoginPage.jsx';
import ProjectsPage from '../pages/projects/ProjectsPage.jsx';
import WorkbenchPage from '../pages/workbench/WorkbenchPage.jsx';
import { PAGE_TITLES } from '../shell/CommandPalette.jsx';
import { ShellG, ShellP } from '../shell/Shell.jsx';
import { Button, Empty } from '../ui/index.jsx';
import { AppCtx } from './context.js';
import { PopLayer } from './PopLayer.jsx';
import { ProjectArea } from './ProjectArea.jsx';
import { navigate, pagePath, useRoute } from './router.js';
import { createServices } from './services.js';
import { ToastHost } from './toast.jsx';

const GLOBAL_PAGES = ['plugins', 'plugin-detail', 'plugin-upload', 'me', 'sys', 'ops', 'ops-obs', 'ops-ctl', 'ops-users'];
// 已实现的页面；其余页面先显示「后续版本接入」
const ENTRY_PAGES = { projects: ProjectsPage, create: CreatePage, import: ImportPage };
const PROJECT_PAGES = { 'wb-l2': WorkbenchPage, 'chat-states': ChatStatesPage };

function Pending({ project, page, projectId }) {
  const body = (
    <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
      <Empty
        icon="layout"
        title={PAGE_TITLES[page] || '页面'}
        actions={<Button icon="arrowl" onClick={() => navigate(project ? pagePath('wb-l2', projectId) : '/projects')}>{project ? '回到工作台' : '回到我的项目'}</Button>}
      >
        这一页在后续版本接入。
      </Empty>
    </div>
  );
  if (project) return <ShellP active={page}>{body}</ShellP>;
  return <ShellG active={page.startsWith('ops') ? 'ops' : page.startsWith('plugin') ? 'plugins' : ''}>{body}</ShellG>;
}

function Splash() {
  return <div className="app" style={{ alignItems: 'center', justifyContent: 'center' }}><span className="spin" /></div>;
}

export default function App() {
  const route = useRoute();
  const [services, setServices] = useState(null);
  const [config, setConfig] = useState(null);
  const [user, setUser] = useState(undefined);
  const [maintenance, setMaintenance] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const s = await createServices({
        onAuthRequired: () => {
          s.tokens.clear();
          setUser(null);
          navigate('/login');
        },
        onMaintenance: (d) => setMaintenance((m) => ({ ...(m || {}), active: true, mode: m?.mode || 'read_only', message: d.message || m?.message || '' })),
      });
      if (!alive) return;
      setServices(s);
      let cfg;
      try {
        cfg = await s.api.clientConfig();
      } catch {
        cfg = { auth: { enabled: true, registration_enabled: null }, maintenance: null, ops_base_url: '' };
      }
      if (!alive) return;
      s.setOpsBase(cfg.ops_base_url);
      setConfig(cfg);
      setMaintenance(cfg.maintenance || null);
      const localMode = cfg.auth?.enabled === false;
      if (!localMode && s.tokens.localMode) s.tokens.clear();
      if ((localMode && s.tokens.localMode) || (!localMode && s.tokens.access)) {
        try {
          setUser(await s.api.me());
          return;
        } catch {
          s.tokens.clear();
        }
      }
      setUser(null);
    })();
    return () => { alive = false; };
  }, []);

  const signedIn = useCallback(async (u) => {
    setUser(u);
    try { setUser(await services.api.me()); } catch { /* 用登录响应里的资料 */ }
    navigate('/projects');
  }, [services]);

  const logout = useCallback(async () => {
    try { await services.api.logout(services.tokens.refresh); } catch { /* 令牌已失效也照常退出 */ }
    services.tokens.clear();
    setUser(null);
    navigate('/login');
  }, [services]);

  const ctx = useMemo(() => (services ? {
    services, api: services.api, config, user, maintenance, setMaintenance, signedIn, logout,
  } : null), [services, config, user, maintenance, signedIn, logout]);

  useEffect(() => {
    if (user === null && route.name !== 'login') navigate('/login');
    if (user && route.name === 'login') navigate('/projects');
  }, [user, route.name]);

  if (!ctx || user === undefined) return <Splash />;

  let page;
  if (!user || route.name === 'login') {
    page = user ? <Splash /> : <LoginPage />;
  } else if (route.name === 'project') {
    const P = PROJECT_PAGES[route.page];
    page = (
      <ProjectArea key={route.projectId} pid={route.projectId}>
        {P ? <P route={route} /> : <Pending project projectId={route.projectId} page={route.page} />}
      </ProjectArea>
    );
  } else if (GLOBAL_PAGES.includes(route.name)) page = <Pending page={route.name} />;
  else {
    const E = ENTRY_PAGES[route.name] || ENTRY_PAGES.projects;
    page = E ? <E /> : <Pending page={route.name} />;
  }

  return (
    <AppCtx.Provider value={ctx}>
      {page}
      <PopLayer />
      <ToastHost />
    </AppCtx.Provider>
  );
}
