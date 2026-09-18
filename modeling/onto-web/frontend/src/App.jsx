import {
  DownOutlined,
  ImportOutlined,
  MoreOutlined,
  PlusOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import {
  Button,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Modal,
  Segmented,
  Splitter,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import AdminPage from './components/AdminPage.jsx';
import CenterPane from './components/CenterPane.jsx';
import ChatPane from './components/ChatPane.jsx';
import LeftPane, { stageProgress } from './components/LeftPane.jsx';
import LoginPage from './components/LoginPage.jsx';
import McpSettings from './components/McpSettings.jsx';
import { api, authStore } from './lib/api.js';
import { ThemeCtx } from './lib/theme-context.js';
import { ACCENTS } from './theme.js';

const ACCENT_LABELS = {
  blue: '默认蓝',
  violet: '紫',
  teal: '青',
  orange: '橙',
  graphite: '石墨',
};

export default function App() {
  const [auth, setAuth] = useState('unknown'); // unknown -> disabled | required | ok
  const [projects, setProjects] = useState([]);
  const [project, setProject] = useState(null);
  const [tree, setTree] = useState([]);
  const [versions, setVersions] = useState([]);
  const [usage, setUsage] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [focus, setFocus] = useState('overview');
  const [diffRev, setDiffRev] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [renaming, setRenaming] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [adminOpen, setAdminOpen] = useState(false);
  const chatSendRef = useRef(null);
  const theme = useContext(ThemeCtx);

  const refreshProjects = useCallback(() => {
    api.listProjects().then((data) => setProjects(data.projects)).catch(() => {});
  }, []);

  const refreshWorkspace = useCallback(() => {
    if (!project) return;
    api.fileTree(project.id).then((data) => setTree(data.tree)).catch(() => {});
    api.versions(project.id).then((data) => setVersions(data.versions)).catch(() => {});
    api.usage(project.id).then(setUsage).catch(() => {});
  }, [project]);

  useEffect(() => {
    api
      .authStatus()
      .then((data) => {
        if (!data.enabled) setAuth('disabled');
        else setAuth(authStore.token ? 'ok' : 'required');
      })
      .catch(() => setAuth('disabled'));
    const onAuthRequired = () => {
      setProject(null);
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
    setFocus('overview');
    setDiffRev(null);
    refreshWorkspace();
  }, [refreshWorkspace]);

  const createProject = async (template = 'blank') => {
    const name = template === 'example' ? newName.trim() || '外卖订单示例' : newName;
    try {
      const created = await api.createProject(name, template);
      setNewName('');
      setCreating(false);
      refreshProjects();
      setProject(created);
      if (template === 'example') message.success('示例领域已注入，可直接体验各阶段');
    } catch (error) {
      message.error(error.message);
    }
  };

  const importProject = async (file) => {
    const name = file.name.replace(/\.zip$/i, '');
    try {
      const created = await api.importProject(name, file);
      refreshProjects();
      setProject(created);
      message.success('导入完成');
    } catch (error) {
      message.error(error.message);
    }
    return false;
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
        if (project?.id === item.id) setProject(null);
      },
    });
  };

  const projectMenu = (item) => ({
    items: [
      { key: 'open', label: '打开' },
      { key: 'rename', label: '重命名' },
      { key: 'export-onto', label: '导出产物包' },
      { key: 'export-ws', label: '导出完整工作区' },
      { type: 'divider' },
      { key: 'delete', label: '删除', danger: true },
    ],
    onClick: ({ key, domEvent }) => {
      domEvent.stopPropagation();
      if (key === 'open') setProject(item);
      if (key === 'rename') setRenaming({ id: item.id, name: item.name });
      if (key === 'export-onto') window.open(api.exportUrl(item.id, 'ontology'));
      if (key === 'export-ws') window.open(api.exportUrl(item.id, 'workspace'));
      if (key === 'delete') removeProject(item);
    },
  });

  const settingsDrawer = (
    <Drawer
      title="设置"
      open={settingsOpen}
      onClose={() => setSettingsOpen(false)}
      width={400}
    >
      <Tabs
        items={[
          {
            key: 'appearance',
            label: '外观',
            children: (
              <>
                <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                  明暗模式
                </Typography.Paragraph>
                <Segmented
                  block
                  value={theme.mode}
                  onChange={theme.setMode}
                  options={[
                    { label: '浅色', value: 'light' },
                    { label: '深色', value: 'dark' },
                  ]}
                  style={{ marginBottom: 24 }}
                />
                <Typography.Paragraph strong style={{ marginBottom: 8 }}>
                  强调色
                </Typography.Paragraph>
                <div className="accent-row">
                  {Object.keys(ACCENTS).map((key) => (
                    <Tooltip key={key} title={ACCENT_LABELS[key] || key}>
                      <button
                        type="button"
                        className={`accent-swatch${theme.accent === key ? ' active' : ''}`}
                        style={{ background: ACCENTS[key].primary }}
                        onClick={() => theme.setAccent(key)}
                        aria-label={ACCENT_LABELS[key] || key}
                      />
                    </Tooltip>
                  ))}
                </div>
              </>
            ),
          },
          {
            key: 'mcp',
            label: 'MCP 服务器',
            children: <McpSettings project={project} />,
          },
        ]}
      />
    </Drawer>
  );

  if (auth === 'unknown') return null;
  if (auth === 'required') {
    return <LoginPage onLogin={() => setAuth('ok')} />;
  }
  if (adminOpen) {
    return <AdminPage onBack={() => setAdminOpen(false)} />;
  }

  const isAdmin = auth === 'disabled' || authStore.role === 'admin';

  if (!project) {
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
          <Upload accept=".zip" showUploadList={false} beforeUpload={importProject}>
            <Button icon={<ImportOutlined />}>导入</Button>
          </Upload>
          {isAdmin && <Button onClick={() => setAdminOpen(true)}>运维</Button>}
          <Button icon={<SettingOutlined />} onClick={() => setSettingsOpen(true)} />
          {auth === 'ok' && (
            <Button
              onClick={() => {
                authStore.clear();
                setAuth('required');
              }}
            >
              退出（{authStore.username}）
            </Button>
          )}
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
          {projects.map((item, index) => (
            <div
              key={item.id}
              className="pj-card msg-soft"
              role="button"
              tabIndex={0}
              style={{ animationDelay: `${Math.min(index + 1, 8) * 40}ms` }}
              onClick={() => setProject(item)}
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
          ))}
        </div>
        {projects.length === 0 && (
          <Empty description="还没有任务" style={{ marginTop: 48 }} />
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
        {settingsDrawer}
      </div>
    );
  }

  const progress = stageProgress(tree);

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <span className="brand-dot" />
        <span className="brand">本体建模工作台</span>
        <span className="project-name">{project.name}</span>
        {progress.current ? (
          <Tag color="processing" style={{ marginInlineStart: 4 }}>
            当前阶段：{progress.current}
          </Tag>
        ) : (
          <Tag color="success">主链全部完成</Tag>
        )}
        <span style={{ flex: 1 }} />
        {usage && usage.calls > 0 && (
          <Tooltip title={`${usage.calls} 次调用 · 输出 ${usage.output_tokens} tokens`}>
            <span className="cost-widget">${usage.total_cost_usd.toFixed(2)}</span>
          </Tooltip>
        )}
        <Button
          size="small"
          icon={<SettingOutlined />}
          onClick={() => setSettingsOpen(true)}
        />
        <Button size="small" icon={<DownOutlined rotate={90} />} onClick={() => setProject(null)}>
          切换任务
        </Button>
      </div>
      <div className="app-body">
        <Splitter>
          <Splitter.Panel defaultSize="22%" min="14%" max="35%">
            <div className="pane pane-left">
              <LeftPane
                project={project}
                tree={tree}
                versions={versions}
                onSelectFile={(path) => {
                  setSelectedFile(path);
                  setFocus('file');
                }}
                onRefresh={refreshWorkspace}
                onCommand={(command) => chatSendRef.current?.(command)}
                onCompare={(commit) => {
                  setDiffRev(commit);
                  setFocus('diff');
                }}
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
                onFocus={setFocus}
                diffRev={diffRev}
                onOpenFile={(path) => {
                  setSelectedFile(path);
                  setFocus('file');
                }}
              />
            </div>
          </Splitter.Panel>
          <Splitter.Panel defaultSize="32%" min="24%" max="48%">
            <div className="pane pane-right">
              <ChatPane
                project={project}
                onWorkspaceChanged={refreshWorkspace}
                registerSend={(fn) => {
                  chatSendRef.current = fn;
                }}
              />
            </div>
          </Splitter.Panel>
        </Splitter>
      </div>
      {settingsDrawer}
    </div>
  );
}
