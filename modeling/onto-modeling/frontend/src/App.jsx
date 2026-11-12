import {
  ArrowLeftOutlined,
  FileTextOutlined,
  ExclamationCircleOutlined,
  InboxOutlined,
  LogoutOutlined,
  MessageOutlined,
  MoreOutlined,
  SearchOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Collapse,
  Checkbox,
  Descriptions,
  Dropdown,
  Empty,
  Input,
  Modal,
  Radio,
  Splitter,
  Steps,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AdminPage from './components/AdminPage.jsx';
import CenterPane from './components/CenterPane.jsx';
import ChatPane from './components/ChatPane.jsx';
import LeftPane from './components/LeftPane.jsx';
import LoginPage from './components/LoginPage.jsx';
import HubPackagesDialog from './components/HubPackagesDialog.jsx';
import { MAX_NAME_LENGTH, useNameConflict } from './components/NameConflictDialog.jsx';
import { usePackagePublish } from './components/PublishCheckDialog.jsx';
import CreatePage from './components/home/CreatePage.jsx';
import './components/home/modeling-home.css';
import '../src/components/studio/legacy/studio-tokens.css';
import SettingsPage from './components/SettingsPage.jsx';
import { api, authStore } from './lib/api.js';
import { clearWorkspaceDirty } from './lib/workspaceDirty.js';

function parseHash() {
  const hash = window.location.hash || '#/';
  const [path, queryString = ''] = hash.split('?');
  const query = new URLSearchParams(queryString);
  if (path === '#/settings') {
    return {
      name: 'settings',
      projectId: query.get('project') || null,
      section: query.get('section') || 'account',
    };
  }
  if (path === '#/admin') return { name: 'admin' };
  if (path === '#/create') return { name: 'create' };
  if (path.startsWith('#/p/')) return { name: 'project', projectId: path.slice(4) };
  return { name: 'home' };
}

export default function App() {
  const [auth, setAuth] = useState('unknown'); // unknown -> disabled | required | ok
  const [route, setRoute] = useState(parseHash);
  const [projects, setProjects] = useState([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState(null);
  const projectsRequestIdRef = useRef(0);
  const [tree, setTree] = useState([]);
  const [versions, setVersions] = useState([]);
  const [versionTotal, setVersionTotal] = useState(0);
  const [versionNextOffset, setVersionNextOffset] = useState(null);
  const [workspaceRevision, setWorkspaceRevision] = useState(0);
  const [studioResetRevision, setStudioResetRevision] = useState(0);
  const [usage, setUsage] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  // 打开文件时附带的定位：{path, anchor, nonce, origin}；nonce 让重复点击同一位置也重新滚动
  const [fileTarget, setFileTarget] = useState(null);
  const [focusStack, setFocusStack] = useState(['overview']);
  const [diffRev, setDiffRev] = useState(null);
  const [renaming, setRenaming] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchHits, setSearchHits] = useState(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [drawioAvailable, setDrawioAvailable] = useState(false);
  const [importWiz, setImportWiz] = useState(null); // {step,file,name,summary,importing}
  const [memberWiz, setMemberWiz] = useState(null); // {project, members, newUser, newRole}
  const [engineWiz, setEngineWiz] = useState(null); // {project, engine, available}
  const [initialFiles, setInitialFiles] = useState(null); // {projectId, files} 新建项目待发送的初始资料
  const homePublish = usePackagePublish(); // 首页卡片菜单发布：无聊天区，只给表级修复与降级
  const nameConflict = useNameConflict(); // 导入 / 重命名 / 克隆的同名改名
  // 移动端：≤768px 时工作台切换为单栏 + 底部页签
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches,
  );
  const [mobilePane, setMobilePane] = useState('chat'); // left | center | chat
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 768px)');
    const onChange = (e) => setIsMobile(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  const [queryPreset, setQueryPreset] = useState(null); // 反查预置 {kind, term, nonce}
  const [diffFiles, setDiffFiles] = useState(null); // 变更卡带入对比页的文件清单
  const [recentFiles, setRecentFiles] = useState([]);
  const chatSendRef = useRef(null);
  const [homeMenuOpen, setHomeMenuOpen] = useState(false);
  const [activeStage, setActiveStage] = useState(null); // 会话正在执行的阶段（左栏高亮）
  const [transferBusy, setTransferBusy] = useState(false);
  const transferInputRef = useRef(null);
  const importInputRef = useRef(null);
  const [hubPackagesOpen, setHubPackagesOpen] = useState(false);
  // 删除确认与提示用 hook 实例（随首页渲染）：静态 Modal.confirm / message.* 拿不到
  // ConfigProvider 的主题与语言配置
  const [modal, modalContext] = Modal.useModal();
  const [messageApi, messageContext] = message.useMessage();

  // ---- 手写 hash 路由：刷新/深链回到原页面 ----
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const nav = (hash) => {
    if (window.location.hash === hash) setRoute(parseHash());
    else window.location.hash = hash;
  };

  const projectId = route.projectId || null;
  const project = useMemo(
    () => (projectId ? projects.find((p) => p.id === projectId) || null : null),
    [projects, projectId],
  );

  const focus = focusStack[focusStack.length - 1];
  const pushFocus = useCallback((next) => {
    setFocusStack((stack) =>
      stack[stack.length - 1] === next ? stack : [...stack, next].slice(-12),
    );
    setMobilePane('center'); // 移动端：下钻动作自动切到内容栏（桌面态无感）
  }, []);
  const popFocus = () => setFocusStack((stack) => (stack.length > 1 ? stack.slice(0, -1) : stack));

  // 打开文件 = 下钻（入返回栈）+ 记录最近文件；options.anchor 定位到资料内的位置，
  // options.origin（{path, focus}）记录从哪份文档点进来，预览上方据此提供返回
  const openFile = useCallback(
    (path, options = {}) => {
      setSelectedFile(path);
      setFileTarget({ path, anchor: options.anchor || '', nonce: Date.now(), origin: options.origin || null });
      setRecentFiles((prev) => [path, ...prev.filter((p) => p !== path)].slice(0, 6));
      pushFocus('file');
    },
    [pushFocus],
  );

  // 从 Studio 详情点进来的回到 Studio（详情保持挂载），从文件预览点进来的重新打开原文件
  const backToOrigin = () => {
    const origin = fileTarget?.origin;
    if (!origin) return;
    if (origin.focus === 'file') openFile(origin.path);
    else {
      setFileTarget(null);
      popFocus();
    }
  };

  const resetProjects = useCallback(() => {
    projectsRequestIdRef.current += 1;
    setProjects([]);
    setProjectsLoaded(false);
    setProjectsLoading(true);
    setProjectsError(null);
  }, []);

  const refreshProjects = useCallback(() => {
    const requestId = ++projectsRequestIdRef.current;
    setProjectsLoading(true);
    setProjectsError(null);
    return api
      .listProjects()
      .then((data) => {
        if (requestId !== projectsRequestIdRef.current) return;
        setProjects(data.projects);
        setProjectsLoaded(true);
      })
      .catch((error) => {
        if (requestId === projectsRequestIdRef.current) {
          setProjectsError(error.message || '项目列表加载失败');
        }
      })
      .finally(() => {
        if (requestId === projectsRequestIdRef.current) setProjectsLoading(false);
      });
  }, []);

  const refreshUsage = useCallback(() => {
    if (!projectId) return;
    api.usage(projectId).then(setUsage).catch(() => {});
  }, [projectId]);

  const refreshWorkspace = useCallback(() => {
    if (!projectId) return;
    setWorkspaceRevision((value) => value + 1);
    api.fileTree(projectId).then((data) => setTree(data.tree)).catch(() => {});
    api.versions(projectId).then((data) => {
      setVersions(data.versions);
      setVersionTotal(data.total ?? data.versions.length);
      setVersionNextOffset(data.next_offset);
    }).catch(() => {});
    refreshUsage();
    refreshProjects(); // 项目 meta（资料更新时间/统计）同步刷新
  }, [projectId, refreshProjects, refreshUsage]);

  const refreshAfterRevert = useCallback(() => {
    clearWorkspaceDirty();
    setSelectedFile(null);
    setFocusStack(['overview']);
    setDiffRev(null);
    setDiffFiles(null);
    setQueryPreset(null);
    setWorkspaceRevision((value) => value + 1);
    setStudioResetRevision((value) => value + 1);
    if (!projectId) return;
    api.fileTree(projectId).then((data) => setTree(data.tree)).catch(() => {});
    api.versions(projectId).then((data) => {
      setVersions(data.versions);
      setVersionTotal(data.total ?? data.versions.length);
      setVersionNextOffset(data.next_offset);
    }).catch(() => {});
    refreshUsage();
    refreshProjects();
  }, [projectId, refreshProjects, refreshUsage]);

  useEffect(() => {
    api
      .authStatus()
      .then((data) => {
        if (!data.enabled) setAuth('disabled');
        else setAuth(authStore.token ? 'ok' : 'required');
      })
      .catch(() => setAuth('disabled'));
    api.health().then((data) => setDrawioAvailable(!!data.drawio)).catch(() => {});
    const onAuthRequired = () => {
      resetProjects();
      nav('#/');
      setAuth('required');
    };
    window.addEventListener('onto-auth-required', onAuthRequired);
    return () => window.removeEventListener('onto-auth-required', onAuthRequired);
  }, [resetProjects]);

  useEffect(() => {
    if (auth === 'ok' || auth === 'disabled') refreshProjects();
  }, [auth, refreshProjects]);

  useEffect(() => {
    setTree([]);
    setVersions([]);
    setVersionTotal(0);
    setVersionNextOffset(null);
    setUsage(null);
    setSelectedFile(null);
    setFocusStack(['overview']);
    setDiffRev(null);
    setPendingCount(0);
    refreshWorkspace();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // 深链的项目不存在（被删/无权限）时回首页
  useEffect(() => {
    if (projectId && projectsLoaded && !project) {
      message.warning('任务不存在或无权访问');
      nav('#/');
    }
  }, [projectId, projectsLoaded, project]);

  // ---- 导入向导：选文件 → 内容确认（dry-run 摘要）→ 导入 ----
  const startImport = (file) => {
    setImportWiz({
      step: 0,
      file,
      name: file.name.replace(/\.zip$/i, ''),
      summary: null,
      importing: false,
    });
    return false;
  };

  // 名称作参数传入：同名冲突改名重试时，向导状态里还是旧名称。失败抛出，由调用方分流
  const inspectImportAs = async (name) => {
    const { file } = importWiz;
    setImportWiz((w) => w && { ...w, name, importing: true });
    try {
      const summary = await api.importProject(name, file, true);
      setImportWiz((w) => w && { ...w, step: 1, summary, importing: false });
    } catch (error) {
      setImportWiz((w) => w && { ...w, importing: false });
      throw error;
    }
  };

  const importInspect = async () => {
    if (!importWiz.name.trim()) {
      messageApi.warning('请输入任务名称');
      return;
    }
    try {
      await inspectImportAs(importWiz.name.trim());
    } catch (error) {
      const texts = { confirmText: '改名并检查', busyText: '检查中…' };
      if (!nameConflict.offer(error, inspectImportAs, texts)) messageApi.error(error.message);
    }
  };

  const confirmImportAs = async (name) => {
    const { file } = importWiz;
    setImportWiz((w) => w && { ...w, name, importing: true });
    try {
      const created = await api.importProject(name, file);
      setImportWiz(null);
      // 先等列表刷新拿到新任务再进入，否则 project 解析为空会被深链守卫送回首页
      await refreshProjects();
      nav(`#/p/${created.id}`);
      message.success('导入完成');
    } catch (error) {
      setImportWiz((w) => w && { ...w, importing: false });
      throw error;
    }
  };

  const importConfirm = async () => {
    try {
      await confirmImportAs(importWiz.name.trim());
    } catch (error) {
      const texts = { confirmText: '改名并导入', busyText: '导入中…' };
      if (!nameConflict.offer(error, confirmImportAs, texts)) messageApi.error(error.message);
    }
  };

  const transferImportAs = async (file, projectName = '') => {
    const created = await api.transferImport(file, projectName);
    await refreshProjects(); // 同上：先拿到新任务再进入
    nav(`#/p/${created.id}`);
    message.success('迁移导入完成（迭代历史已保留）');
  };

  // 重命名 / 克隆：名称作参数传入，同名冲突改名后用新名称重试。失败抛出，由调用方分流
  const renameAs = async (id, name) => {
    await api.renameProject(id, name);
    setRenaming(null);
    refreshProjects();
  };

  const submitRename = async () => {
    const { id } = renaming;
    const name = renaming.name.trim();
    if (!name) {
      messageApi.warning('请输入任务名称');
      return;
    }
    try {
      await renameAs(id, name);
    } catch (error) {
      const texts = { confirmText: '改名并保存', busyText: '保存中…' };
      const retry = (nextName) => renameAs(id, nextName);
      if (!nameConflict.offer(error, retry, texts)) messageApi.error(error.message);
    }
  };

  const cloneAs = async (item, name = '') => {
    const clone = await api.cloneProject(item.id, name);
    refreshProjects();
    messageApi.success(`已克隆为「${clone.name}」`);
  };

  const cloneProject = async (item) => {
    try {
      await cloneAs(item);
    } catch (error) {
      const texts = { confirmText: '改名并克隆', busyText: '克隆中…' };
      const retry = (name) => cloneAs(item, name);
      if (!nameConflict.offer(error, retry, texts)) messageApi.error(error.message);
    }
  };

  const removeProject = async (item) => {
    let preview;
    try {
      preview = await api.hubDeletePreview(item.id);
    } catch (error) {
      preview = { published: false, error: error.message };
    }
    let deleteHub = false;
    const perform = async (withHub) => {
      try {
        await api.deleteProject(item.id, withHub);
        messageApi.success(`已删除「${item.name}」`);
        refreshProjects();
        if (projectId === item.id) nav('#/');
      } catch (error) {
        messageApi.error(error.message || '删除失败');
        throw error;
      }
    };
    modal.confirm({
      title: `删除任务「${item.name}」？`,
      content: <div>
        <p>进行中的建模会话和评测将终止。工作区文件、对话和迭代历史将永久删除，无法恢复。</p>
        <Checkbox disabled={!preview.published} onChange={(e) => { deleteHub = e.target.checked; }}>
          同时删除本项目发布的知识包
        </Checkbox>
        {!preview.published && <p>{preview.error || '本项目尚未发布知识包'}</p>}
        {preview.state && <p>Hub 删除进度：{preview.state === 'deleted' ? '知识包已删除，待完成项目删除' : '结果待确认，请勾选后重试'}</p>}
      </div>,
      okText: '确认删除',
      okButtonProps: { danger: true },
      onOk: () => {
        if (!deleteHub) return perform(false);
        let acknowledged = false;
        const confirmation = modal.confirm({
          title: '确认删除项目及知识包',
          content: <div>
            <p>{preview.name}（{preview.ontology_id}）</p>
            <p>请确保消费和评测不再使用这些知识包。将物理删除全部正式版本、评测快照及共享资料，使全部用户授权失效，包括其他服务发布到同一本体的版本。可能导致依赖任务失败，无法恢复。已安装或下载的副本不会删除；系统未自动核实下游使用情况。</p>
            <Checkbox onChange={(e) => {
              acknowledged = e.target.checked;
              confirmation.update({ okButtonProps: { danger: true, disabled: !acknowledged } });
            }}>已确认消费和评测不再使用这些知识包</Checkbox>
          </div>,
          okText: '删除项目及知识包',
          okButtonProps: { danger: true, disabled: true },
          onOk: () => acknowledged ? perform(true) : Promise.reject(new Error('请先确认使用情况')),
        });
      },
    });
  };

  const projectMenu = (item) => ({
    items: [
      { key: 'open', label: '打开' },
      { key: 'rename', label: '重命名' },
      { key: 'clone', label: '克隆' },
      { key: 'members', label: '成员共享' },
      { key: 'engine', label: '会话引擎' },
      { key: 'archive', label: item.archived ? '恢复' : '归档' },
      { type: 'divider' },
      { key: 'export-onto', label: '导出知识包（原始产物）' },
      { key: 'export-pkg', label: '发布为知识包（供消费端导入）' },
      { key: 'published-versions', label: '已发布版本（知识包评测）' },
      { key: 'export-ws', label: '导出迁移包（完整工作区）' },
      { type: 'divider' },
      { key: 'delete', label: '删除', danger: true },
    ],
    onClick: async ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'open') nav(`#/p/${item.id}`);
      if (key === 'rename') setRenaming({ id: item.id, name: item.name });
      if (key === 'clone') cloneProject(item);
      if (key === 'engine') {
        try {
          const data = await api.getProjectEngine(item.id);
          setEngineWiz({ project: item, engine: data.engine, available: data.available });
        } catch (error) {
          message.error(error.message);
        }
      }
      if (key === 'members') {
        try {
          const data = await api.getMembers(item.id);
          setMemberWiz({ project: item, members: data.members, hubSync: data.hub_sync, newUser: '', newRole: 'editor' });
        } catch (error) {
          message.error(error.message);
        }
      }
      if (key === 'archive') {
        await api.archiveProject(item.id, !item.archived).catch((e) => message.error(e.message));
        refreshProjects();
      }
      if (key === 'export-onto') window.open(api.exportUrl(item.id, 'ontology'));
      if (key === 'export-pkg') homePublish.publish(item.id);
      if (key === 'published-versions') homePublish.showVersions(item);
      if (key === 'export-ws') window.open(api.exportUrl(item.id, 'workspace'));
      if (key === 'delete') removeProject(item);
    },
  });

  const isAdmin = auth === 'disabled' || authStore.role === 'admin';

  const logout = () => {
    authStore.clear();
    resetProjects();
    nav('#/');
    setAuth('required');
  };

  const avatarNode = auth === 'ok' && (
    <Dropdown
      trigger={['click']}
      menu={{
        items: [
          { key: 'user', label: authStore.username, disabled: true },
          { type: 'divider' },
          { key: 'logout', label: '退出登录', icon: <LogoutOutlined />, danger: true },
        ],
        onClick: ({ key }) => {
          if (key === 'logout') logout();
        },
      }}
    >
      <Avatar className="user-avatar" size={30}>
        {(authStore.username || '?').slice(0, 1).toUpperCase()}
      </Avatar>
    </Dropdown>
  );

  if (auth === 'unknown') return null;
  if (auth === 'required') {
    return <LoginPage onLogin={() => setAuth('ok')} />;
  }
  if (route.name === 'admin') {
    return <AdminPage onBack={() => window.history.back()} />;
  }
  if (route.name === 'settings') {
    return (
      <SettingsPage
        project={project}
        isAdmin={isAdmin}
        authEnabled={auth === 'ok'}
        initialSection={route.section || 'account'}
        onBack={() => (projectId ? nav(`#/p/${encodeURIComponent(projectId)}`) : window.history.back())}
        onOpenAdmin={() => nav('#/admin')}
      />
    );
  }

  if (route.name === 'create') {
    return (
      <CreatePage
        onBack={() => nav('#/')}
        onCreated={async (created, files) => {
          if (files?.length) setInitialFiles({ projectId: created.id, files });
          // 先等列表刷新拿到新项目，再进入项目页（否则 project 解析为空会落回列表）
          await refreshProjects();
          nav(`#/p/${created.id}`);
        }}
      />
    );
  }

  // 深链项目：列表未加载完成前不闪首页
  if (projectId && !projectsLoaded && !projectsError) return null;

  if (route.name === 'home' || !project) {
    const activeProjects = projects.filter((p) => !p.archived);
    const archivedProjects = projects.filter((p) => p.archived);
    const stageLabel = (item) => {
      const base =
        (item.stages_done ?? 0) >= (item.stages_total ?? 9)
          ? '主链完成'
          : `阶段 ${item.stages_done ?? 0}/${item.stages_total ?? 9}`;
      return item.eval_score != null ? `${base} · 评测 ${item.eval_score}` : base;
    };
    // 卡片结构逐节点复刻 modeling ProjectList.ProjectCard
    const renderCard = (item) => (
      <div
        key={item.id}
        className="pj-card"
        role="button"
        tabIndex={0}
        aria-label={`打开项目：${item.name}`}
        onClick={() => nav(`#/p/${item.id}`)}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget || !['Enter', ' '].includes(event.key)) return;
          event.preventDefault();
          nav(`#/p/${item.id}`);
        }}
      >
        <button
          type="button"
          className="pj-share"
          aria-label={`共享项目：${item.name}`}
          title="管理项目成员"
          onClick={async (event) => {
            event.stopPropagation();
            try {
              const data = await api.getMembers(item.id);
              setMemberWiz({ project: item, members: data.members, hubSync: data.hub_sync, newUser: '', newRole: 'editor' });
            } catch (error) {
              message.error(error.message);
            }
          }}
        >
          成员
        </button>
        <Dropdown menu={projectMenu(item)} trigger={['click']}>
          <button
            type="button"
            className="pj-share pj-more"
            aria-label="更多操作"
            title="更多操作"
            onClick={(e) => e.stopPropagation()}
          >
            ⋯
          </button>
        </Dropdown>
        <button
          type="button"
          className="pj-delete"
          aria-label={`删除项目：${item.name}`}
          title="删除项目"
          onClick={(event) => {
            event.stopPropagation();
            removeProject(item);
          }}
        >
          ×
        </button>
        <h3 title={item.name}>{item.name}</h3>
        <div className={`pd ${item.background ? '' : 'pd-empty'}`}>
          {item.background || `对象 ${item.objects ?? 0} · 操作 ${item.operations ?? 0}`}
        </div>
        <div className="pj-prog" title={stageLabel(item)}>
          <div
            className="pj-prog-in"
            style={{ width: `${((item.stages_done || 0) / (item.stages_total || 9)) * 100}%` }}
          />
        </div>
        <div className="pm">
          <span className="tag tag-lang">{stageLabel(item)}</span>
          {(item.stages_done ?? 0) >= (item.stages_total ?? 9) && <span className="tag tag-ok">可导出</span>}
          <span className="pj-date">{new Date(item.created_at * 1000).toLocaleDateString()}</span>
        </div>
      </div>
    );

    return (
      <div className="mwr shell">
        {modalContext}
        {messageContext}
        <HubPackagesDialog open={hubPackagesOpen} onClose={() => setHubPackagesOpen(false)}
          onImported={async (created) => { await refreshProjects(); nav(`#/p/${created.id}`); }} />
        <div className="topbar">
          <div className="logo"><span className="mk">知</span>知识工程建模工作台</div>
          <span className="crumb">项目</span>
          <div className="right">
            <button
              className="tb-btn"
              disabled={transferBusy}
              title="从其他环境导入完整项目迁移包（含迭代历史）"
              onClick={() => transferInputRef.current?.click()}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5"/><line x1="10" y1="12" x2="14" y2="12"/></svg>
              {transferBusy ? '导入项目…' : '导入项目'}
            </button>
            <input
              ref={transferInputRef}
              type="file"
              accept=".zip"
              hidden
              onChange={async (event) => {
                const file = event.target.files?.[0];
                event.target.value = '';
                if (!file) return;
                setTransferBusy(true);
                try {
                  await transferImportAs(file);
                } catch (error) {
                  const retry = (name) => transferImportAs(file, name);
                  const texts = { confirmText: '改名并导入', busyText: '导入中…' };
                  if (!nameConflict.offer(error, retry, texts)) messageApi.error(error.message);
                } finally {
                  setTransferBusy(false);
                }
              }}
            />
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  { key: 'published', label: '已发布知识包' },
                  { key: 'local', label: '本地知识包' },
                ],
                onClick: ({ key }) => {
                  if (key === 'published') setHubPackagesOpen(true);
                  if (key === 'local') importInputRef.current?.click();
                },
              }}
            >
              <button
                type="button"
                className="tb-btn"
                title="选择已发布知识包或本地知识包"
              >
                导入已有知识包 ▾
              </button>
            </Dropdown>
            <input
              ref={importInputRef}
              type="file"
              accept=".zip"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = '';
                if (file) startImport(file);
              }}
            />
            {isAdmin && (
              <button className="tb-btn" onClick={() => nav('#/admin')}>运维</button>
            )}
            <button className="tb-btn" onClick={() => nav('#/settings')}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              配置中心
            </button>
            <span className="tb-sep" />
            <div className="tb-user">
              <button
                type="button"
                className={`tb-chip ${homeMenuOpen ? 'open' : ''}`}
                title="账号菜单"
                onClick={() => setHomeMenuOpen((v) => !v)}
              >
                <span className="tb-avatar">{(authStore.username || '?').slice(0, 1).toUpperCase()}</span>
                <span className="tb-name">{authStore.username || '本地'}</span>
                <span className={`tb-caret ${homeMenuOpen ? 'up' : ''}`}>▾</span>
              </button>
              {homeMenuOpen && (
                <div className="tb-menu">
                  <div className="tb-menu-head">
                    <span className="tb-avatar lg">{(authStore.username || '?').slice(0, 1).toUpperCase()}</span>
                    <div>
                      <div className="tb-menu-name">{authStore.username || '本地模式'}</div>
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
        <div className="main">
          <div className="pj-head">
            <div>
              <h2>建模任务与知识包</h2>
              <p>资料分析 → 对象/操作提取 → 规则与指标 → 质量检查 → 场景验证 → 导出交付</p>
            </div>
            {activeProjects.length > 0 && <span className="pj-count">{activeProjects.length} 个任务</span>}
          </div>
          {projectsLoading && (
            <div className="empty" role="status" aria-live="polite">项目列表加载中…</div>
          )}
          {projectsError && (
            <Alert
              type="error"
              showIcon
              message={`项目列表加载失败：${projectsError}`}
              action={<Button size="small" onClick={refreshProjects}>重试</Button>}
              style={{ maxWidth: 1200, marginBottom: 18 }}
            />
          )}
          {projectsLoaded && !projectsLoading && !projectsError && projects.length === 0 && (
            <div className="pj-hero">
              <span className="pj-hero-ic">✦</span>
              <div>
                <div className="pj-hero-t">暂无任务</div>
                <div className="pj-hero-d">创建任务并说明建模目标，AI 将围绕目标分析资料。</div>
              </div>
            </div>
          )}
          {projectsLoaded && (projects.length > 0 || (!projectsLoading && !projectsError)) && (
            <div className="pj-grid">
              {activeProjects.map(renderCard)}
              <button className="pj-card pj-new" onClick={() => nav('#/create')}>
                <span className="pj-new-ic">＋</span>
                <span>新建任务</span>
              </button>
            </div>
          )}
          {archivedProjects.length > 0 && (
            <Collapse
              ghost
              style={{ marginTop: 24, maxWidth: 1200 }}
              items={[
                {
                  key: 'archived',
                  label: <span><InboxOutlined /> 归档区（{archivedProjects.length}）</span>,
                  children: <div className="pj-grid">{archivedProjects.map(renderCard)}</div>,
                },
              ]}
            />
          )}
        </div>
        <Modal
          open={!!renaming}
          title="重命名任务"
          onCancel={() => setRenaming(null)}
          onOk={() => submitRename()}
        >
          <Input
            value={renaming?.name}
            maxLength={MAX_NAME_LENGTH}
            onChange={(e) => setRenaming({ ...renaming, name: e.target.value })}
          />
        </Modal>
        {homePublish.dialog}
        {nameConflict.dialog}
        <Modal
          title={engineWiz ? `会话引擎 · ${engineWiz.project.name}` : ''}
          open={!!engineWiz}
          onCancel={() => setEngineWiz(null)}
          okText="切换"
          onOk={async () => {
            try {
              await api.setProjectEngine(engineWiz.project.id, engineWiz.engine);
              message.success('已切换引擎；当前会话已重启，下一条消息用新引擎启动');
              setEngineWiz(null);
            } catch (error) {
              message.error(error.message);
            }
          }}
          width={460}
        >
          {engineWiz && (
            <div>
              <Radio.Group
                value={engineWiz.engine}
                onChange={(e) => setEngineWiz({ ...engineWiz, engine: e.target.value })}
                style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
              >
                <Radio value="claude">
                  稳定引擎（默认）— 功能最完整
                </Radio>
                <Radio value="jiuwen" disabled={!engineWiz.available?.jiuwen}>
                  内测引擎 — OpenAI 兼容模型直连，低内存
                  {!engineWiz.available?.jiuwen && '（当前部署未安装）'}
                </Radio>
              </Radio.Group>
              <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 12 }}>
                切换即重启会话：界面历史保留可回放，模型上下文按新引擎的续接方式重建。
                内测引擎需管理员先在「配置中心 → 模型接入」配置 OpenAI 兼容模型。
              </Typography.Paragraph>
            </div>
          )}
        </Modal>
        <Modal
          title={memberWiz ? `成员共享 · ${memberWiz.project.name}` : ''}
          open={!!memberWiz}
          onCancel={() => setMemberWiz(null)}
          okText="保存"
          onOk={async () => {
            try {
              const result = await api.saveMembers(memberWiz.project.id, memberWiz.members);
              if (result.hub_sync?.status === 'failed') {
                setMemberWiz({ ...memberWiz, hubSync: result.hub_sync });
                message.warning('项目成员已更新，Hub 授权同步失败；移除的成员可能仍可访问知识包');
                return;
              }
              setMemberWiz(null);
              message.success('成员已更新');
            } catch (error) {
              message.error(error.message);
            }
          }}
        >
          {memberWiz && (
            <>
              <Alert type="info" message="建模者将获得 Hub 全部版本读写、授权管理和整包删除权限。移除或降级会撤销该本体的整体授权，包括其他渠道授予的权限。" />
              {Object.keys(memberWiz.members).length === 0 && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未共享给其他成员" />
              )}
              {Object.entries(memberWiz.members).map(([user, role]) => (
                <div key={user} className="member-row">
                  <span className="member-name">{user}</span>
                  <Tag color={role === 'editor' ? 'processing' : 'default'}>
                    {role === 'editor' ? '建模者（可编辑）' : '只读评审'}
                  </Tag>
                  {memberWiz.hubSync?.pending?.[user] && <>
                    <Tooltip trigger={['hover', 'focus', 'click']} title={memberWiz.hubSync.results?.[user]?.error || '授权变更尚未同步，请重试'}>
                      <Button type="text" size="small" danger aria-label="查看授权失败原因" icon={<ExclamationCircleOutlined />} />
                    </Tooltip>
                    <Button size="small" loading={memberWiz.retrying === user} disabled={!!memberWiz.retrying && memberWiz.retrying !== user}
                      onClick={async () => {
                        const pid = memberWiz.project.id;
                        setMemberWiz((current) => ({ ...current, retrying: user }));
                        try {
                          const hubSync = await api.retryHubMembers(pid, user);
                          setMemberWiz((current) => current?.project.id === pid ? { ...current, hubSync, retrying: null } : current);
                        } catch (error) {
                          message.error(error.message);
                          setMemberWiz((current) => current?.project.id === pid ? { ...current, retrying: null } : current);
                        }
                      }}>
                      {memberWiz.hubSync.pending[user] === 'revoke' ? '重试撤权' : '重试授权'}
                    </Button>
                  </>}
                  <Button
                    size="small"
                    type="text"
                    danger
                    disabled={!!memberWiz.retrying}
                    onClick={() => {
                      const next = { ...memberWiz.members };
                      delete next[user];
                      setMemberWiz({ ...memberWiz, members: next });
                    }}
                  >
                    移除
                  </Button>
                </div>
              ))}
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <Input
                  placeholder="用户名"
                  value={memberWiz.newUser}
                  onChange={(e) => setMemberWiz({ ...memberWiz, newUser: e.target.value })}
                  style={{ flex: 1 }}
                />
                <select
                  className="member-role-select"
                  value={memberWiz.newRole}
                  onChange={(e) => setMemberWiz({ ...memberWiz, newRole: e.target.value })}
                >
                  <option value="editor">建模者（可编辑）</option>
                  <option value="viewer">只读评审</option>
                </select>
                <Button
                  onClick={() => {
                    const user = memberWiz.newUser.trim();
                    if (!user) return;
                    setMemberWiz({
                      ...memberWiz,
                      members: { ...memberWiz.members, [user]: memberWiz.newRole },
                      newUser: '',
                    });
                  }}
                >
                  添加
                </Button>
              </div>
            </>
          )}
        </Modal>
        <Modal
          title="导入建模任务"
          open={!!importWiz}
          onCancel={() => setImportWiz(null)}
          footer={null}
          width={560}
        >
          {importWiz && (
            <>
              <Steps
                size="small"
                current={importWiz.step}
                items={[{ title: '选择与命名' }, { title: '内容确认' }]}
                style={{ margin: '8px 0 16px' }}
              />
              {importWiz.step === 0 && (
                <>
                  <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
                    <Descriptions.Item label="压缩包">{importWiz.file.name}</Descriptions.Item>
                  </Descriptions>
                  <Input
                    addonBefore="任务名称"
                    value={importWiz.name}
                    maxLength={MAX_NAME_LENGTH}
                    onChange={(e) => setImportWiz({ ...importWiz, name: e.target.value })}
                    style={{ marginBottom: 16 }}
                  />
                  <Button type="primary" loading={importWiz.importing} onClick={() => importInspect()}>
                    检查内容
                  </Button>
                </>
              )}
              {importWiz.step === 1 && importWiz.summary && (
                <>
                  <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
                    <Descriptions.Item label="识别格式">
                      {importWiz.summary.kind_label}
                    </Descriptions.Item>
                    <Descriptions.Item label="文件数">{importWiz.summary.files}</Descriptions.Item>
                    <Descriptions.Item label="业务对象">{importWiz.summary.objects}</Descriptions.Item>
                    <Descriptions.Item label="业务操作">{importWiz.summary.operations}</Descriptions.Item>
                    <Descriptions.Item label="规则 / 指标">
                      {importWiz.summary.rules} / {importWiz.summary.metrics}
                    </Descriptions.Item>
                    <Descriptions.Item label="脚本">{importWiz.summary.scripts}</Descriptions.Item>
                    <Descriptions.Item label="总大小">
                      {(importWiz.summary.total_size / 1048576).toFixed(1)} MB
                    </Descriptions.Item>
                  </Descriptions>
                  {importWiz.summary.kind === 'knowledge_package' && (
                    <div style={{ marginBottom: 12, fontSize: 12, color: '#888' }}>
                      知识包「{importWiz.summary.package_name || '未命名'}」 · 版本{' '}
                      {importWiz.summary.package_version || '未声明'} · 包根{' '}
                      {importWiz.summary.package_root || '压缩包根目录'}
                    </div>
                  )}
                  <div style={{ marginBottom: 12 }}>
                    {importWiz.summary.top_dirs.map((d) => (
                      <Tag key={d.name}>
                        {d.name}/（{d.files}）
                      </Tag>
                    ))}
                  </div>
                  {/* 阻断项必须独立展示并禁用导入：合并成一条提示就是"显示成功但是空任务"的来源 */}
                  {importWiz.summary.blockers?.length > 0 && (
                    <Alert
                      type="error"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message="这个压缩包不能从这里导入"
                      description={(
                        <ul style={{ paddingLeft: 18, margin: 0 }}>
                          {importWiz.summary.blockers.map((text) => (
                            <li key={text}>{text}</li>
                          ))}
                        </ul>
                      )}
                    />
                  )}
                  {importWiz.summary.kind === 'knowledge_package'
                    && !importWiz.summary.blockers?.length && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message="已发布知识包只含交付物：对象、操作、规则、指标与脚本会落到工作区继续建模，资料与报告等过程资产为空，阶段进度从头计算。原包会完整留底，映射与引用不会丢失。"
                    />
                  )}
                  {importWiz.summary.skipped.length > 0 && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message={`将跳过：${importWiz.summary.skipped.join('、')}（资产由本服务重新挂载，git 历史不迁移）`}
                    />
                  )}
                  <Button
                    type="primary"
                    loading={importWiz.importing}
                    disabled={importWiz.summary.blockers?.length > 0}
                    onClick={() => importConfirm()}
                  >
                    确认导入
                  </Button>{' '}
                  <Button onClick={() => setImportWiz({ ...importWiz, step: 0 })}>上一步</Button>
                </>
              )}
            </>
          )}
        </Modal>
      </div>
    );
  }


  const leftPaneNode = (
    <div className="pane pane-left">
              <LeftPane
                activeStage={activeStage}
                project={project}
                meta={project}
                tree={tree}
                versions={versions}
                versionNextOffset={versionNextOffset}
                onLoadMoreVersions={async () => {
                  if (versionNextOffset == null) return;
                  const data = await api.versions(project.id, versionNextOffset);
                  setVersions((current) => [
                    ...current,
                    ...data.versions.filter(
                      (entry) => !current.some((item) => item.commit === entry.commit),
                    ),
                  ]);
                  setVersionNextOffset(data.next_offset);
                }}
                onSelectFile={openFile}
                onRefresh={refreshWorkspace}
                onReverted={refreshAfterRevert}
                onCommand={(command) => chatSendRef.current?.(command)}
                onDiff={(rev) => {
                  setDiffRev(rev);
                  setDiffFiles(null);
                  pushFocus('diff');
                }}
                onQuery={(kind, term) => {
                  setQueryPreset({ kind, term, nonce: Date.now() });
                  pushFocus('query');
                }}
                onKnowledge={() => pushFocus('knowledge')}
                onSolver={() => pushFocus('solver')}
              />
            </div>
  );
  const centerPaneNode = (
    <div className="pane pane-center">
              <CenterPane
                project={project}
                tree={tree}
                versions={versions}
                versionTotal={versionTotal}
                usage={usage}
                file={selectedFile}
                fileTarget={fileTarget?.path === selectedFile ? fileTarget : null}
                onBackToOrigin={backToOrigin}
                focus={focus}
                onFocus={pushFocus}
                canBack={focusStack.length > 1}
                onBack={popFocus}
                diffRev={diffRev}
                queryPreset={queryPreset}
                onFocusPlain={(next) => setFocusStack([next])}
                diffFiles={diffFiles}
                onPickFile={setSelectedFile}
                recentFiles={recentFiles}
                workspaceRevision={workspaceRevision}
                studioResetRevision={studioResetRevision}
                drawioAvailable={drawioAvailable}
                onWorkspaceChanged={refreshWorkspace}
                onOpenFile={openFile}
                onCommand={(command) => chatSendRef.current?.(command)}
              />
            </div>
  );
  const chatPaneNode = (
    <div className="pane pane-right">
              <ChatPane
                onActiveStage={setActiveStage}
                initialFiles={initialFiles?.projectId === project.id ? initialFiles.files : null}
                onInitialFilesConsumed={() => setInitialFiles(null)}
                project={project}
                tree={tree}
                meta={project}
                versions={versions}
                usage={usage}
                onWorkspaceChanged={refreshWorkspace}
                onUsageChanged={refreshUsage}
                onPendingChange={setPendingCount}
                onOpenFile={openFile}
                onOpenDiff={(rev, files) => {
                  setDiffRev(rev);
                  setDiffFiles(files?.length ? files : null);
                  pushFocus('diff');
                }}
                registerSend={(fn) => {
                  chatSendRef.current = fn;
                }}
              />
            </div>
  );

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <img src="/favicon.svg" alt="" width="28" height="28" />
        <span className="brand">知识工程建模工作台</span>
        <span className="project-name">{project.name}</span>
        <span style={{ flex: 1 }} />
        {pendingCount > 0 && (
          <Tooltip title="对话里有待你确认的操作">
            <Badge count={pendingCount} size="small">
              <span className="pending-widget">待处理</span>
            </Badge>
          </Tooltip>
        )}
        <Button
          size="small"
          icon={<SearchOutlined />}
          onClick={() => {
            setSearchHits(null);
            setSearchOpen(true);
          }}
        />
        <Button
          size="small"
          icon={<SettingOutlined />}
          onClick={() => nav(`#/settings?project=${encodeURIComponent(project.id)}&section=mcp`)}
        />
        <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => nav('#/')}>
          返回项目列表
        </Button>
        {avatarNode}
      </div>
      <div className="app-body">
        {isMobile ? (
          <div className="mobile-body">
            <div className="mobile-panes">
              <div className={`mobile-pane${mobilePane === 'left' ? '' : ' mobile-pane-hidden'}`}>
                {leftPaneNode}
              </div>
              <div className={`mobile-pane${mobilePane === 'center' ? '' : ' mobile-pane-hidden'}`}>
                {centerPaneNode}
              </div>
              <div className={`mobile-pane${mobilePane === 'chat' ? '' : ' mobile-pane-hidden'}`}>
                {chatPaneNode}
              </div>
            </div>
            <div className="mobile-nav">
              <button
                type="button"
                className={mobilePane === 'left' ? 'active' : ''}
                onClick={() => setMobilePane('left')}
              >
                <UnorderedListOutlined />
                进度
              </button>
              <button
                type="button"
                className={mobilePane === 'center' ? 'active' : ''}
                onClick={() => setMobilePane('center')}
              >
                <FileTextOutlined />
                内容
              </button>
              <button
                type="button"
                className={mobilePane === 'chat' ? 'active' : ''}
                onClick={() => setMobilePane('chat')}
              >
                <Badge count={pendingCount} size="small" offset={[8, -2]}>
                  <MessageOutlined />
                </Badge>
                聊天
              </button>
            </div>
          </div>
        ) : (
          <Splitter>
            <Splitter.Panel defaultSize="22%" min="14%" max="35%" collapsible>
              {leftPaneNode}
            </Splitter.Panel>
            <Splitter.Panel>{centerPaneNode}</Splitter.Panel>
            <Splitter.Panel defaultSize="32%" min="24%" max="48%" collapsible>
              {chatPaneNode}
            </Splitter.Panel>
          </Splitter>
        )}
      </div>
      <Modal
        title="全文搜索"
        open={searchOpen}
        onCancel={() => setSearchOpen(false)}
        footer={null}
        centered
        width={640}
      >
        <Input.Search
          placeholder="搜索知识包、资料分析、报告与知识"
          autoFocus
          enterButton
          onSearch={async (q) => {
            if (!q.trim()) return;
            try {
              const data = await api.search(project.id, q.trim());
              setSearchHits(data.hits);
            } catch (error) {
              message.error(error.message);
            }
          }}
          style={{ marginBottom: 12 }}
        />
        {searchHits && searchHits.length === 0 && <Empty description="没有命中" />}
        {searchHits?.map((hit, index) => (
          <div
            key={`${hit.path}-${hit.line}-${index}`}
            className="search-hit msg-soft"
            onClick={() => {
              openFile(hit.path);
              setSearchOpen(false);
            }}
          >
            <div className="search-hit-path">
              {hit.path}:{hit.line}
            </div>
            <div className="search-hit-text">{hit.text}</div>
          </div>
        ))}
      </Modal>
    </div>
  );
}
