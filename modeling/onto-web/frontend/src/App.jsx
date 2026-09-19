import {
  ArrowLeftOutlined,
  ImportOutlined,
  InboxOutlined,
  LogoutOutlined,
  MoreOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Collapse,
  Descriptions,
  Dropdown,
  Empty,
  Input,
  Modal,
  Splitter,
  Steps,
  Tag,
  Tooltip,
  Upload,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import AdminPage from './components/AdminPage.jsx';
import CenterPane from './components/CenterPane.jsx';
import ChatPane from './components/ChatPane.jsx';
import LeftPane, { stageProgress } from './components/LeftPane.jsx';
import LoginPage from './components/LoginPage.jsx';
import SettingsPage from './components/SettingsPage.jsx';
import { api, authStore } from './lib/api.js';

function parseHash() {
  const hash = window.location.hash || '#/';
  if (hash.startsWith('#/settings')) return { name: 'settings' };
  if (hash.startsWith('#/admin')) return { name: 'admin' };
  if (hash.startsWith('#/p/')) return { name: 'project', projectId: hash.slice(4) };
  return { name: 'home' };
}

export default function App() {
  const [auth, setAuth] = useState('unknown'); // unknown -> disabled | required | ok
  const [route, setRoute] = useState(parseHash);
  const [projects, setProjects] = useState([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [tree, setTree] = useState([]);
  const [versions, setVersions] = useState([]);
  const [usage, setUsage] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [focusStack, setFocusStack] = useState(['overview']);
  const [diffRev, setDiffRev] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [renaming, setRenaming] = useState(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchHits, setSearchHits] = useState(null);
  const [pendingCount, setPendingCount] = useState(0);
  const [drawioAvailable, setDrawioAvailable] = useState(false);
  const [importWiz, setImportWiz] = useState(null); // {step,file,name,summary,importing}
  const [memberWiz, setMemberWiz] = useState(null); // {project, members, newUser, newRole}
  const [queryPreset, setQueryPreset] = useState(null); // 反查预置 {kind, term, nonce}
  const chatSendRef = useRef(null);

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

  const projectId = route.name === 'project' ? route.projectId : null;
  const project = useMemo(
    () => (projectId ? projects.find((p) => p.id === projectId) || null : null),
    [projects, projectId],
  );

  const focus = focusStack[focusStack.length - 1];
  const pushFocus = useCallback((next) => {
    setFocusStack((stack) =>
      stack[stack.length - 1] === next ? stack : [...stack, next].slice(-12),
    );
  }, []);
  const popFocus = () => setFocusStack((stack) => (stack.length > 1 ? stack.slice(0, -1) : stack));

  const refreshProjects = useCallback(() => {
    api
      .listProjects()
      .then((data) => {
        setProjects(data.projects);
        setProjectsLoaded(true);
      })
      .catch(() => {});
  }, []);

  const refreshWorkspace = useCallback(() => {
    if (!projectId) return;
    api.fileTree(projectId).then((data) => setTree(data.tree)).catch(() => {});
    api.versions(projectId).then((data) => setVersions(data.versions)).catch(() => {});
    api.usage(projectId).then(setUsage).catch(() => {});
    refreshProjects(); // 项目 meta（资料更新时间/统计）同步刷新
  }, [projectId, refreshProjects]);

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
      nav('#/');
      setAuth('required');
    };
    window.addEventListener('onto-auth-required', onAuthRequired);
    return () => window.removeEventListener('onto-auth-required', onAuthRequired);
  }, []);

  useEffect(() => {
    if (auth === 'ok' || auth === 'disabled') refreshProjects();
  }, [auth, refreshProjects]);

  useEffect(() => {
    setTree([]);
    setVersions([]);
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

  const createProject = async (template = 'blank') => {
    const name = template === 'example' ? newName.trim() || '外卖订单示例' : newName;
    try {
      const created = await api.createProject(name, template);
      setNewName('');
      setCreating(false);
      refreshProjects();
      nav(`#/p/${created.id}`);
      if (template === 'example') message.success('示例领域已注入，可直接体验各阶段');
    } catch (error) {
      message.error(error.message);
    }
  };

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

  const importInspect = async () => {
    setImportWiz((w) => ({ ...w, importing: true }));
    try {
      const summary = await api.importProject(importWiz.name, importWiz.file, true);
      setImportWiz((w) => ({ ...w, step: 1, summary, importing: false }));
    } catch (error) {
      message.error(error.message);
      setImportWiz((w) => ({ ...w, importing: false }));
    }
  };

  const importConfirm = async () => {
    setImportWiz((w) => ({ ...w, importing: true }));
    try {
      const created = await api.importProject(importWiz.name, importWiz.file);
      setImportWiz(null);
      refreshProjects();
      nav(`#/p/${created.id}`);
      message.success('导入完成');
    } catch (error) {
      message.error(error.message);
      setImportWiz((w) => ({ ...w, importing: false }));
    }
  };

  const removeProject = (item) => {
    Modal.confirm({
      title: `删除任务「${item.name}」？`,
      content: '将移入回收目录（软删除），数据与版本历史保留，可人工找回。',
      okText: '删除',
      okButtonProps: { danger: true },
      onOk: async () => {
        await api.deleteProject(item.id);
        refreshProjects();
        if (projectId === item.id) nav('#/');
      },
    });
  };

  const projectMenu = (item) => ({
    items: [
      { key: 'open', label: '打开' },
      { key: 'rename', label: '重命名' },
      { key: 'clone', label: '克隆' },
      { key: 'members', label: '成员共享' },
      { key: 'archive', label: item.archived ? '恢复' : '归档' },
      { type: 'divider' },
      { key: 'export-onto', label: '导出知识包（原始产物）' },
      { key: 'export-pkg', label: '发布为知识包（供消费端导入）' },
      { key: 'export-ws', label: '导出完整工作区' },
      { type: 'divider' },
      { key: 'delete', label: '删除', danger: true },
    ],
    onClick: async ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'open') nav(`#/p/${item.id}`);
      if (key === 'rename') setRenaming({ id: item.id, name: item.name });
      if (key === 'clone') {
        try {
          const clone = await api.cloneProject(item.id);
          refreshProjects();
          message.success(`已克隆为「${clone.name}」`);
        } catch (error) {
          message.error(error.message);
        }
      }
      if (key === 'members') {
        try {
          const data = await api.getMembers(item.id);
          setMemberWiz({ project: item, members: data.members, newUser: '', newRole: 'editor' });
        } catch (error) {
          message.error(error.message);
        }
      }
      if (key === 'archive') {
        await api.archiveProject(item.id, !item.archived).catch((e) => message.error(e.message));
        refreshProjects();
      }
      if (key === 'export-onto') window.open(api.exportUrl(item.id, 'ontology'));
      if (key === 'export-pkg') window.open(api.exportUrl(item.id, 'package'));
      if (key === 'export-ws') window.open(api.exportUrl(item.id, 'workspace'));
      if (key === 'delete') removeProject(item);
    },
  });

  const isAdmin = auth === 'disabled' || authStore.role === 'admin';

  const logout = () => {
    authStore.clear();
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
        onBack={() => window.history.back()}
        onOpenAdmin={() => nav('#/admin')}
      />
    );
  }

  // 深链项目：列表未加载完成前不闪首页
  if (projectId && !projectsLoaded) return null;

  if (route.name === 'home' || !project) {
    const activeProjects = projects.filter((p) => !p.archived);
    const archivedProjects = projects.filter((p) => p.archived);
    const renderCard = (item, index) => (
      <div
        key={item.id}
        className="pj-card msg-soft"
        role="button"
        tabIndex={0}
        style={{ animationDelay: `${Math.min(index + 1, 8) * 40}ms` }}
        onClick={() => nav(`#/p/${item.id}`)}
      >
        <Dropdown menu={projectMenu(item)} trigger={['click']}>
          <button
            type="button"
            className="pj-menu"
            aria-label="任务操作"
            onClick={(e) => e.stopPropagation()}
          >
            <MoreOutlined />
          </button>
        </Dropdown>
        <h3 title={item.name}>{item.name}</h3>
        <div className="pd">
          对象 {item.objects ?? 0} · 逻辑 {item.logics ?? 0}
        </div>
        <div className="pj-prog">
          <div
            className="pj-prog-in"
            style={{
              width: `${((item.stages_done || 0) / (item.stages_total || 8)) * 100}%`,
            }}
          />
        </div>
        <div className="pm">
          <span>
            阶段 {item.stages_done ?? 0}/{item.stages_total ?? 8}
          </span>
          <span className="pj-date">
            {new Date(item.created_at * 1000).toLocaleDateString()}
          </span>
        </div>
      </div>
    );

    return (
      <div className="home">
        <div className="pj-head">
          <div>
            <div className="home-title">
              <span className="brand-dot" />
              <h2>我的建模任务</h2>
            </div>
            <p>调研 → 流程图 → 提取对象/逻辑 → 校验 → Mock → 追踪 → 代码生成</p>
          </div>
          <span style={{ flex: 1 }} />
          <Upload accept=".zip" showUploadList={false} beforeUpload={startImport}>
            <Button icon={<ImportOutlined />}>导入</Button>
          </Upload>
          {isAdmin && <Button onClick={() => nav('#/admin')}>运维</Button>}
          <Button icon={<SettingOutlined />} onClick={() => nav('#/settings')} />
          {avatarNode}
        </div>
        <div className="pj-grid">
          <div
            className="pj-card pj-new msg-soft"
            role="button"
            tabIndex={0}
            onClick={() => setCreating(true)}
          >
            <div className="pj-new-icon">
              <PlusOutlined />
            </div>
            <div className="pj-new-t">新建建模任务</div>
            <div className="pj-new-d">从空白开始，或注入示例领域体验全流程</div>
          </div>
          {activeProjects.map(renderCard)}
        </div>
        {projects.length === 0 && (
          <Empty description="还没有任务" style={{ marginTop: 48 }} />
        )}
        {archivedProjects.length > 0 && (
          <Collapse
            ghost
            style={{ marginTop: 24 }}
            items={[
              {
                key: 'archived',
                label: <span><InboxOutlined /> 归档区（{archivedProjects.length}）</span>,
                children: <div className="pj-grid">{archivedProjects.map(renderCard)}</div>,
              },
            ]}
          />
        )}
        <Modal
          open={creating}
          title="新建建模任务"
          onCancel={() => setCreating(false)}
          footer={null}
        >
          <Input
            placeholder="任务名称，如：电商订单建模"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onPressEnter={() => createProject('blank')}
            style={{ margin: '8px 0 16px' }}
            autoFocus
          />
          <Button type="primary" onClick={() => createProject('blank')}>
            创建空白任务
          </Button>{' '}
          <Button onClick={() => createProject('example')}>注入外卖订单示例</Button>
        </Modal>
        <Modal
          open={!!renaming}
          title="重命名任务"
          onCancel={() => setRenaming(null)}
          onOk={async () => {
            await api.renameProject(renaming.id, renaming.name);
            setRenaming(null);
            refreshProjects();
          }}
        >
          <Input
            value={renaming?.name}
            onChange={(e) => setRenaming({ ...renaming, name: e.target.value })}
          />
        </Modal>
        <Modal
          title={memberWiz ? `成员共享 · ${memberWiz.project.name}` : ''}
          open={!!memberWiz}
          onCancel={() => setMemberWiz(null)}
          okText="保存"
          onOk={async () => {
            try {
              await api.saveMembers(memberWiz.project.id, memberWiz.members);
              setMemberWiz(null);
              message.success('成员已更新');
            } catch (error) {
              message.error(error.message);
            }
          }}
        >
          {memberWiz && (
            <>
              {Object.keys(memberWiz.members).length === 0 && (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未共享给其他成员" />
              )}
              {Object.entries(memberWiz.members).map(([user, role]) => (
                <div key={user} className="member-row">
                  <span className="member-name">{user}</span>
                  <Tag color={role === 'editor' ? 'processing' : 'default'}>
                    {role === 'editor' ? '建模者（可编辑）' : '只读评审'}
                  </Tag>
                  <Button
                    size="small"
                    type="text"
                    danger
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
                    onChange={(e) => setImportWiz({ ...importWiz, name: e.target.value })}
                    style={{ marginBottom: 16 }}
                  />
                  <Button type="primary" loading={importWiz.importing} onClick={importInspect}>
                    检查内容
                  </Button>
                </>
              )}
              {importWiz.step === 1 && importWiz.summary && (
                <>
                  <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
                    <Descriptions.Item label="文件数">{importWiz.summary.files}</Descriptions.Item>
                    <Descriptions.Item label="总大小">
                      {(importWiz.summary.total_size / 1048576).toFixed(1)} MB
                    </Descriptions.Item>
                    <Descriptions.Item label="业务对象">{importWiz.summary.objects}</Descriptions.Item>
                    <Descriptions.Item label="业务逻辑">{importWiz.summary.logics}</Descriptions.Item>
                  </Descriptions>
                  <div style={{ marginBottom: 12 }}>
                    {importWiz.summary.top_dirs.map((d) => (
                      <Tag key={d.name}>
                        {d.name}/（{d.files}）
                      </Tag>
                    ))}
                  </div>
                  {importWiz.summary.skipped.length > 0 && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message={`将跳过：${importWiz.summary.skipped.join('、')}（资产由本服务重新挂载，git 历史不迁移）`}
                    />
                  )}
                  <Button type="primary" loading={importWiz.importing} onClick={importConfirm}>
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

  const progress = stageProgress(tree);

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <img src="/favicon.svg" alt="" width="28" height="28" />
        <span className="brand">知识工程建模工作台</span>
        <span className="project-name">{project.name}</span>
        {progress.current ? (
          <Tag color="processing">{progress.current}</Tag>
        ) : (
          <Tag color="success">主链完成</Tag>
        )}
        <span style={{ flex: 1 }} />
        {pendingCount > 0 && (
          <Tooltip title="对话里有待你确认的操作">
            <Badge count={pendingCount} size="small">
              <span className="pending-widget">待处理</span>
            </Badge>
          </Tooltip>
        )}
        {usage && usage.calls > 0 && (
          <Tooltip title={`${usage.calls} 次调用 · 输出 ${usage.output_tokens} tokens`}>
            <span className="cost-widget">${usage.total_cost_usd.toFixed(2)}</span>
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
        <Button size="small" icon={<SettingOutlined />} onClick={() => nav('#/settings')} />
        <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => nav('#/')}>
          返回项目列表
        </Button>
        {avatarNode}
      </div>
      <div className="app-body">
        <Splitter>
          <Splitter.Panel defaultSize="22%" min="14%" max="35%" collapsible>
            <div className="pane pane-left">
              <LeftPane
                project={project}
                meta={project}
                tree={tree}
                versions={versions}
                onSelectFile={(path) => {
                  setSelectedFile(path);
                  pushFocus('file');
                }}
                onRefresh={refreshWorkspace}
                onCommand={(command) => chatSendRef.current?.(command)}
                onDiff={(rev) => {
                  setDiffRev(rev);
                  pushFocus('diff');
                }}
                onQuery={(kind, term) => {
                  setQueryPreset({ kind, term, nonce: Date.now() });
                  pushFocus('query');
                }}
                onKnowledge={() => pushFocus('knowledge')}
              />
            </div>
          </Splitter.Panel>
          <Splitter.Panel>
            <div className="pane pane-center">
              <CenterPane
                project={project}
                tree={tree}
                versions={versions}
                usage={usage}
                file={selectedFile}
                focus={focus}
                onFocus={pushFocus}
                canBack={focusStack.length > 1}
                onBack={popFocus}
                diffRev={diffRev}
                queryPreset={queryPreset}
                drawioAvailable={drawioAvailable}
                onWorkspaceChanged={refreshWorkspace}
                onOpenFile={(path) => {
                  setSelectedFile(path);
                  pushFocus('file');
                }}
                onCommand={(command) => chatSendRef.current?.(command)}
              />
            </div>
          </Splitter.Panel>
          <Splitter.Panel defaultSize="32%" min="24%" max="48%" collapsible>
            <div className="pane pane-right">
              <ChatPane
                project={project}
                onWorkspaceChanged={refreshWorkspace}
                onPendingChange={setPendingCount}
                registerSend={(fn) => {
                  chatSendRef.current = fn;
                }}
              />
            </div>
          </Splitter.Panel>
        </Splitter>
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
          placeholder="搜索知识包、调研、报告与知识"
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
              setSelectedFile(hit.path);
              pushFocus('file');
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
