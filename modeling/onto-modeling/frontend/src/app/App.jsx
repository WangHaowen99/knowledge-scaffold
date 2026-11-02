// 应用入口：启动（C-01、A-07、无感刷新）、登录态与角色、路由
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { PAGE_TITLES } from '../shell/CommandPalette.jsx';
import { ShellG, ShellP } from '../shell/Shell.jsx';
import { Button, Empty } from '../ui/index.jsx';
import { AppCtx } from './context.js';
import { PopLayer } from './PopLayer.jsx';
import { ProjectArea } from './ProjectArea.jsx';
import { navigate, pagePath, useRoute } from './router.js';
import { createServices } from './services.js';
import { ToastHost } from './toast.jsx';
// 这几份样式原来随工作台一起进主入口；工作台拆包后仍留在主入口，先后不变（层叠顺序不变，别的页面也用到其中的类）
import '../styles/autobuild.css';
import '../styles/more.css';
import '../styles/incremental.css';
import '../styles/materials.css';
import '../styles/knowledge.css';
import '../styles/workbench-extra.css';

const GLOBAL_PAGES = ['plugins', 'plugin-detail', 'plugin-upload', 'me', 'sys', 'ops', 'ops-obs', 'ops-ctl', 'ops-users'];
// 登录、入口页、工作台（含 Markdown 渲染）也按路由拆包，主入口只留外壳、数据层与组件库
const LoginPage = lazy(() => import('../pages/login/LoginPage.jsx'));
const ProjectsPage = lazy(() => import('../pages/projects/ProjectsPage.jsx'));
const CreatePage = lazy(() => import('../pages/create/CreatePage.jsx'));
const ImportPage = lazy(() => import('../pages/import/ImportPage.jsx'));
const WorkbenchPage = lazy(() => import('../pages/workbench/WorkbenchPage.jsx'));
const ChatStatesPage = lazy(() => import('../pages/chat-states/ChatStatesPage.jsx'));
// 已实现的页面；其余页面先显示「后续版本接入」
const ENTRY_PAGES = { projects: ProjectsPage, create: CreatePage, import: ImportPage };
// Studio（含 WebGL 图谱）、报告、待处理按路由拆包
const StudioPage = lazy(() => import('../pages/studio/StudioPage.jsx'));
const StudioTablePage = lazy(() => import('../pages/studio/StudioTablePage.jsx'));
const ReportsPage = lazy(() => import('../pages/reports/ReportsPage.jsx'));
const InboxPage = lazy(() => import('../pages/inbox/InboxPage.jsx'));
// F3：插件页（知识沉淀、场景走查、代码生成）与项目页（版本、发布、资料与证据源、项目设置）
const KnowledgePage = lazy(() => import('../pages/knowledge/KnowledgePage.jsx'));
const ScenariosPage = lazy(() => import('../pages/scenarios/ScenariosPage.jsx'));
const ScriptsPage = lazy(() => import('../pages/scripts/ScriptsPage.jsx'));
const VersionsPage = lazy(() => import('../pages/versions/VersionsPage.jsx'));
const PublishPage = lazy(() => import('../pages/publish/PublishPage.jsx'));
const ResourcesPage = lazy(() => import('../pages/resources/ResourcesPage.jsx'));
const SourcesPage = lazy(() => import('../pages/evidence/SourcesPage.jsx'));
const SnapshotsPage = lazy(() => import('../pages/evidence/SnapshotsPage.jsx'));
const SchemaPage = lazy(() => import('../pages/evidence/SchemaPage.jsx'));
const ProjectSettingsPage = lazy(() => import('../pages/settings/ProjectSettingsPage.jsx'));
// F4：运维中心、插件中心、个人与系统设置（全局页，都按路由拆包）
const GLOBAL_PAGE_COMPONENTS = {
  ops: lazy(() => import('../pages/ops/OpsOverviewPage.jsx')),
  'ops-obs': lazy(() => import('../pages/ops/OpsObsPage.jsx')),
  'ops-ctl': lazy(() => import('../pages/ops/OpsCtlPage.jsx')),
  'ops-users': lazy(() => import('../pages/ops/OpsUsersPage.jsx')),
  plugins: lazy(() => import('../pages/plugins/PluginsPage.jsx')),
  'plugin-detail': lazy(() => import('../pages/plugins/PluginDetailPage.jsx')),
  'plugin-upload': lazy(() => import('../pages/plugins/PluginUploadPage.jsx')),
  me: lazy(() => import('../pages/me/MePage.jsx')),
  sys: lazy(() => import('../pages/sys/SysPage.jsx')),
};
// 工作台的几个阶段（资料分析、切片规划、陪伴、流程图弹窗、自主构建、增量更新）是同一个对话页，显示什么由对话和流程数据决定
const PROJECT_PAGES = {
  'wb-l2': WorkbenchPage,
  'wb-materials': WorkbenchPage,
  'wb-blueprint': WorkbenchPage,
  'l4-run': WorkbenchPage,
  flowchart: WorkbenchPage,
  incremental: WorkbenchPage,
  'chat-states': ChatStatesPage,
  inbox: InboxPage,
  studio: StudioPage,
  'studio-table': StudioTablePage,
  reports: ReportsPage,
  knowledge: KnowledgePage,
  scenarios: ScenariosPage,
  scripts: ScriptsPage,
  versions: VersionsPage,
  publish: PublishPage,
  resources: ResourcesPage,
  sources: SourcesPage,
  snapshots: SnapshotsPage,
  schema: SchemaPage,
  'proj-settings': ProjectSettingsPage,
};

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
    page = user ? <Splash /> : <Suspense fallback={<Splash />}><LoginPage /></Suspense>;
  } else if (route.name === 'project') {
    const P = PROJECT_PAGES[route.page];
    page = (
      <ProjectArea key={route.projectId} pid={route.projectId}>
        {P ? <Suspense fallback={<Splash />}><P route={route} /></Suspense> : <Pending project projectId={route.projectId} page={route.page} />}
      </ProjectArea>
    );
  } else if (GLOBAL_PAGES.includes(route.name)) {
    const G = GLOBAL_PAGE_COMPONENTS[route.name];
    page = G ? <Suspense fallback={<Splash />}><G route={route} /></Suspense> : <Pending page={route.name} />;
  }
  else {
    const E = ENTRY_PAGES[route.name] || ENTRY_PAGES.projects;
    page = E ? <Suspense fallback={<Splash />}><E /></Suspense> : <Pending page={route.name} />;
  }

  return (
    <AppCtx.Provider value={ctx}>
      {page}
      <PopLayer />
      <ToastHost />
    </AppCtx.Provider>
  );
}
