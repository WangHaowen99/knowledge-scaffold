// 应用入口：启动（C-01、A-07、无感刷新）、登录态与角色、路由
import {
  Component, Suspense, useCallback, useEffect, useMemo, useState,
} from 'react';
import { ShellG, ShellP } from '../shell/Shell.jsx';
import { can } from '../state/roles.js';
import { Button, Empty } from '../ui/index.jsx';
import { AppCtx } from './context.js';
import { PopLayer } from './PopLayer.jsx';
import { ProjectArea } from './ProjectArea.jsx';
import { prefetchWhenIdle } from './prefetch.js';
import { navigate, pagePath, parseHash, useRouteState } from './router.js';
import { routePage } from './routeChunks.js';
import { primeProject } from './earlyData.js';
import { createServices } from './services.js';
import { ToastHost } from './toast.jsx';
// 这几份样式原来随工作台一起进主入口；工作台拆包后仍留在主入口，先后不变（层叠顺序不变，别的页面也用到其中的类）
import '../styles/autobuild.css';
import '../styles/more.css';
import '../styles/incremental.css';
import '../styles/materials.css';
import '../styles/workbench-extra.css';

const GLOBAL_PAGES = ['plugins', 'plugin-detail', 'plugin-upload', 'me', 'sys', 'ops', 'ops-obs', 'ops-ctl', 'ops-users'];
// 登录、入口页、工作台（含 Markdown 渲染）也按路由拆包，主入口只留外壳、数据层与组件库；
// 首屏之后在空闲时预取（PREFETCH），换页在 transition 里做，代码块没到之前保留上一页（F2：第一次切换页面白屏几秒）
const LoginPage = routePage(() => import('../pages/login/LoginPage.jsx'));
const ProjectsPage = routePage(() => import('../pages/projects/ProjectsPage.jsx'));
const CreatePage = routePage(() => import('../pages/create/CreatePage.jsx'));
const ImportPage = routePage(() => import('../pages/import/ImportPage.jsx'));
const WorkbenchPage = routePage(() => import('../pages/workbench/WorkbenchPage.jsx'));
// 对话卡片与状态：设计稿给评审看的卡片目录，不是产品页面（R6）。只在开发与 mock 构建里注册，生产构建摇掉
const DEV_PAGES = import.meta.env.DEV || __API_MOCK__
  ? { 'chat-states': routePage(() => import('../pages/chat-states/ChatStatesPage.jsx')) }
  : {};
// 入口页；地址不认识时显示「页面不存在」
const ENTRY_PAGES = { projects: ProjectsPage, create: CreatePage, import: ImportPage };
// Studio（含 WebGL 图谱）、报告、待处理按路由拆包
const StudioPage = routePage(() => import('../pages/studio/StudioPage.jsx'));
const StudioTablePage = routePage(() => import('../pages/studio/StudioTablePage.jsx'));
const ReportsPage = routePage(() => import('../pages/reports/ReportsPage.jsx'));
const InboxPage = routePage(() => import('../pages/inbox/InboxPage.jsx'));
// F3：插件页（场景模拟、代码生成）与项目页（版本、发布、资料、项目设置）
const ScenariosPage = routePage(() => import('../pages/scenarios/ScenariosPage.jsx'));
// 插件自己的页面（ADR-0103）：页面 id 是「包名:页面 id」，不在内置页表里
const PluginFramePage = routePage(() => import('../pages/plugin/PluginFramePage.jsx'));
const projectPage = (page) => PROJECT_PAGES[page] || (String(page || '').includes(':') ? PluginFramePage : null);
const ScriptsPage = routePage(() => import('../pages/scripts/ScriptsPage.jsx'));
const VersionsPage = routePage(() => import('../pages/versions/VersionsPage.jsx'));
const PublishPage = routePage(() => import('../pages/publish/PublishPage.jsx'));
const ResourcesPage = routePage(() => import('../pages/resources/ResourcesPage.jsx'));
const ProjectSettingsPage = routePage(() => import('../pages/settings/ProjectSettingsPage.jsx'));
// F4：运维中心、插件中心、个人与系统设置（全局页，都按路由拆包）
const GLOBAL_PAGE_COMPONENTS = {
  ops: routePage(() => import('../pages/ops/OpsOverviewPage.jsx')),
  'ops-obs': routePage(() => import('../pages/ops/OpsObsPage.jsx')),
  'ops-ctl': routePage(() => import('../pages/ops/OpsCtlPage.jsx')),
  'ops-users': routePage(() => import('../pages/ops/OpsUsersPage.jsx')),
  plugins: routePage(() => import('../pages/plugins/PluginsPage.jsx')),
  'plugin-detail': routePage(() => import('../pages/plugins/PluginDetailPage.jsx')),
  'plugin-upload': routePage(() => import('../pages/plugins/PluginUploadPage.jsx')),
  me: routePage(() => import('../pages/me/MePage.jsx')),
  sys: routePage(() => import('../pages/sys/SysPage.jsx')),
};
// 工作台的几个阶段（资料分析、建模蓝图、陪伴、流程图弹窗、自主构建、增量更新）是同一个对话页，显示什么由对话和流程数据决定
const PROJECT_PAGES = {
  'wb-l2': WorkbenchPage,
  'wb-materials': WorkbenchPage,
  'wb-blueprint': WorkbenchPage,
  'l4-run': WorkbenchPage,
  flowchart: WorkbenchPage,
  incremental: WorkbenchPage,
  inbox: InboxPage,
  studio: StudioPage,
  'studio-er': StudioPage,
  'studio-table': StudioTablePage,
  reports: ReportsPage,
  scenarios: ScenariosPage,
  scripts: ScriptsPage,
  versions: VersionsPage,
  publish: PublishPage,
  resources: ResourcesPage,
  'proj-settings': ProjectSettingsPage,
  ...DEV_PAGES,
};

// 首屏之后预取：项目里的页面在前（工作台、左栏内核页、设置、插件页），全局页随后，运维与管理页按角色
const PREFETCH = [
  WorkbenchPage, StudioPage, ResourcesPage, PublishPage, InboxPage, ProjectSettingsPage, ScenariosPage, ScriptsPage,
  VersionsPage, StudioTablePage, ReportsPage, ProjectsPage, CreatePage,
  GLOBAL_PAGE_COMPONENTS.plugins, GLOBAL_PAGE_COMPONENTS['plugin-detail'], GLOBAL_PAGE_COMPONENTS.me,
];
const PREFETCH_OPS = ['ops', 'ops-obs', 'ops-ctl', 'ops-users'].map((k) => GLOBAL_PAGE_COMPONENTS[k]);
const PREFETCH_ADMIN = [GLOBAL_PAGE_COMPONENTS.sys, GLOBAL_PAGE_COMPONENTS['plugin-upload']];

// 直接打开或刚进项目时，页面代码块还没到：先画外壳（顶栏、左栏）和骨架
function ProjectSkeleton({ page }) {
  return (
    <ShellP active={page}>
      <div className="pg-skel" data-skeleton><i /><i /><i /></div>
    </ShellP>
  );
}

// 页面渲染或代码块加载出错（断网、刷新后仍取不到）：不让整页变白；换页后重新渲染
class PageBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidUpdate(prev) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="app" style={{ alignItems: 'center', justifyContent: 'center' }}>
        <Empty icon="alert" title="页面没加载出来" actions={<Button icon="refresh" onClick={() => window.location.reload()}>重新加载</Button>} />
      </div>
    );
  }
}

// 首屏页面的代码块和启动请求同时下载，不等登录态确认完再取
function preloadInitialRoute() {
  if (typeof window === 'undefined') return;
  const r = parseHash(window.location.hash);
  let P;
  if (r.name === 'project') P = projectPage(r.page);
  else if (r.name === 'login') P = LoginPage;
  else if (GLOBAL_PAGES.includes(r.name)) P = GLOBAL_PAGE_COMPONENTS[r.name];
  else P = ENTRY_PAGES[r.name] || ENTRY_PAGES.projects;
  P?.preload?.().catch(() => {});
}
preloadInitialRoute();

function Pending({ project, page, projectId }) {
  const body = (
    <div className="card" style={{ padding: 30, maxWidth: 640, margin: '40px auto' }}>
      <Empty
        icon="layout"
        title="页面不存在"
        actions={<Button icon="arrowl" onClick={() => navigate(project ? pagePath('wb-l2', projectId) : '/projects')}>{project ? '回到工作台' : '回到我的项目'}</Button>}
      />
    </div>
  );
  if (project) return <ShellP active={page}>{body}</ShellP>;
  return <ShellG active={page.startsWith('ops') ? 'ops' : page.startsWith('plugin') ? 'plugins' : ''}>{body}</ShellG>;
}

function Splash() {
  return <div className="app" style={{ alignItems: 'center', justifyContent: 'center' }}><span className="spin" /></div>;
}

export default function App() {
  const [route, routePending] = useRouteState();
  const [services, setServices] = useState(null);
  const [config, setConfig] = useState(null);
  const [user, setUser] = useState(undefined);
  const [maintenance, setMaintenance] = useState(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      let me = null;
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
      // 带着令牌就和 C-01 同时取 A-07，省掉一次往返；令牌失效时客户端照常无感刷新
      if (s.tokens.access) {
        me = s.api.me();
        me.catch(() => {});
      }
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
          const u = await (me || s.api.me());
          // 登录态一确认就把项目首屏的请求发出去，不等页面渲染完再由各组件自己取（这两个轻请求先回来，不和它们挤）
          const r = parseHash(window.location.hash);
          if (r.name === 'project') primeProject(s.api, r.projectId);
          setUser(u);
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
    // 只在还停在登录页时才去项目列表：取 A-07 期间用户（或地址）已经去了别的页面就不再拽回来
    if (parseHash(window.location.hash).name === 'login') navigate('/projects');
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

  const hasUser = !!user;
  const opsReader = can(user, 'ops.read');
  const admin = can(user, 'admin');
  useEffect(() => {
    if (!hasUser) return undefined;
    const pages = [...PREFETCH, ...(opsReader ? PREFETCH_OPS : []), ...(admin ? PREFETCH_ADMIN : [])];
    return prefetchWhenIdle(pages.map((P) => P.preload));
  }, [hasUser, opsReader, admin]);

  if (!ctx || user === undefined) return <Splash />;

  let page;
  if (!user || route.name === 'login') {
    page = user ? <Splash /> : <LoginPage />;
  } else if (route.name === 'project') {
    const P = projectPage(route.page);
    page = (
      <ProjectArea key={route.projectId} pid={route.projectId}>
        {P ? <Suspense fallback={<ProjectSkeleton page={route.page} />}><P route={route} /></Suspense> : <Pending project projectId={route.projectId} page={route.page} />}
      </ProjectArea>
    );
  } else if (GLOBAL_PAGES.includes(route.name)) {
    const G = GLOBAL_PAGE_COMPONENTS[route.name];
    page = G ? <G route={route} /> : <Pending page={route.name} />;
  }
  else {
    const E = ENTRY_PAGES[route.name] || ENTRY_PAGES.projects;
    page = E ? <E /> : <Pending page={route.name} />;
  }

  return (
    <AppCtx.Provider value={ctx}>
      {/* 全局页、入口页共用这一个 Suspense：换页时它已经显示着内容，transition 里不会退回 Splash */}
      <PageBoundary resetKey={`${route.name}/${route.projectId || ''}/${route.page || ''}`}>
        <Suspense fallback={<Splash />}>{page}</Suspense>
      </PageBoundary>
      {routePending ? <div className="route-pending" /> : null}
      <PopLayer />
      <ToastHost />
    </AppCtx.Provider>
  );
}
