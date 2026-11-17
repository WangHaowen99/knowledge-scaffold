import {
  ArrowLeftOutlined,
  EditOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Button, Empty, Spin, Table, Tag, Typography, message } from 'antd';
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { workspaceDirtySources } from '../lib/workspaceDirty.js';
import { compareUpdatedAt, formatUpdatedAt, withUpdatedAt } from '../lib/studioUpdates.js';
import { Markdown } from './CenterPane.jsx';
import StudioEntryEditor from './StudioEntryEditor.jsx';
import { studioTextMatchesQuery } from './studio/legacy/studio-model.js';

// 图模式 = 原版 modeling Studio 图整体搬迁（含 WebGL 力导运行时），按需加载
const LegacyStudioGraphHost = lazy(() => import('./studio/LegacyStudioGraphHost.jsx'));

// 表格视图的 kind 视觉沿用原版 KIND_META 的对象/操作用色
const KIND_LABEL = { object: '对象', operation: '操作', logic: '操作', rule: '规则', metric: '指标', script: '脚本' };

export default function StudioView({
  project,
  onOpenFile,
  knownPaths,
  onCommand,
  onWorkspaceChanged,
  active = false,
  workspaceRevision = 0,
}) {
  const [view, setView] = useState('graph');
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [entry, setEntry] = useState(null);
  const [selectedElementId, setSelectedElementId] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [updates, setUpdates] = useState({});
  const [updatesRequest, setUpdatesRequest] = useState(0);
  const [openingEditor, setOpeningEditor] = useState(false);
  const previousProjectId = useRef(project.id);
  const previousWorkspaceRevision = useRef(workspaceRevision);

  const refreshGraph = useCallback(() => {
    setLoading(true);
    api
      .studioGraph(project.id)
      .then(setGraph)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [project.id]);

  useEffect(() => {
    const projectChanged = previousProjectId.current !== project.id;
    previousProjectId.current = project.id;
    if (projectChanged) {
      setEntry(null);
      setView('graph');
      setQuery('');
      setSelectedElementId('');
      setUpdates({});
    }
    if (active) refreshGraph();
  }, [active, project.id, refreshGraph, workspaceRevision]);

  // Agent review can update the selected document while Studio stays mounted. Refresh the
  // detail on the result event; an open editor remains untouched and relies on revision checks.
  useEffect(() => {
    const changed = previousWorkspaceRevision.current !== workspaceRevision;
    previousWorkspaceRevision.current = workspaceRevision;
    if (!changed || !active || view === 'edit' || !entry?.path) return undefined;
    let cancelled = false;
    api
      .studioElement(project.id, entry.path)
      .then((data) => !cancelled && setEntry(data))
      .catch((error) => !cancelled && message.error(error.message));
    return () => {
      cancelled = true;
    };
  }, [active, entry?.path, project.id, view, workspaceRevision]);

  // 最后更新时间要查 git 历史，只在表视图读取，图视图不发这个请求
  useEffect(() => {
    if (!active || view !== 'table') return undefined;
    let cancelled = false;
    api
      .studioUpdates(project.id)
      .then((data) => !cancelled && setUpdates(data.updates || {}))
      .catch(() => !cancelled && setUpdates({}));
    return () => {
      cancelled = true;
    };
  }, [active, view, project.id, workspaceRevision, updatesRequest]);

  const tableRows = useMemo(() => {
    const keyword = query.trim();
    const nodes = keyword
      ? graph.nodes.filter((node) => studioTextMatchesQuery(`${node.id} ${node.label || ''} ${node.description || ''}`, keyword))
      : graph.nodes;
    return withUpdatedAt(nodes, updates);
  }, [graph, query, updates]);

  const openEntry = useCallback(
    (path, elementId = '') => {
      api
        .studioElement(project.id, path)
        .then((data) => {
          setEntry(data);
          const matching = graph.nodes.find((node) => node.path === path);
          setSelectedElementId(elementId || matching?.id || '');
          setView('entry');
        })
        .catch((error) => message.error(error.message));
    },
    [graph.nodes, project.id],
  );

  const refresh = useCallback(() => {
    refreshGraph();
    setUpdatesRequest((count) => count + 1);
    if (!entry?.path) return;
    if (view === 'edit' && workspaceDirtySources().includes('studio-editor')) {
      message.warning('当前编辑表单有未保存修改，已刷新图数据但保留表单内容');
      return;
    }
    const path = entry.path;
    const elementId = selectedElementId;
    setEntry(null);
    openEntry(path, elementId);
  }, [entry, openEntry, refreshGraph, selectedElementId, view]);

  const startEditing = useCallback(() => {
    if (!entry?.path || openingEditor) return;
    setOpeningEditor(true);
    api
      .studioElement(project.id, entry.path)
      .then((data) => {
        setEntry(data);
        setView('edit');
      })
      .catch((error) => message.error(error.message))
      .finally(() => setOpeningEditor(false));
  }, [entry?.path, openingEditor, project.id]);

  // 详情里的资料芯片：打开原文时记下当前条目，预览上方的「返回」回到这里
  const openFromEntry = useCallback(
    (path, options) => onOpenFile?.(path, { ...options, origin: { path: entry?.path, focus: 'studio' } }),
    [entry?.path, onOpenFile],
  );

  const pathByName = useMemo(() => {
    const map = new Map();
    for (const node of graph.nodes) map.set(node.id, node.path);
    return map;
  }, [graph]);

  const labelByName = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node.label || node.id])),
    [graph],
  );

  if (!loading && graph.nodes.length === 0) {
    return (
      <Empty
        description="还没有可视化的本体元素——先完成对象/逻辑提取"
        style={{ marginTop: 48 }}
      />
    );
  }

  return (
    <div className="studio-root">
      <div className="studio-toolbar">
        {(view === 'entry' || view === 'edit') && (
          <Button
            size="small"
            icon={<ArrowLeftOutlined />}
            onClick={() => setView(view === 'edit' ? 'entry' : 'graph')}
          >
            返回
          </Button>
        )}
        <div className="seg studio-seg">
          <button
            type="button"
            className={view === 'graph' ? 'on' : ''}
            onClick={() => setView('graph')}
          >
            图
          </button>
          <button
            type="button"
            className={view === 'table' ? 'on' : ''}
            onClick={() => setView('table')}
          >
            表
          </button>
        </div>
        {view === 'table' && <div className="studio-search">
          <SearchOutlined />
          <input
            placeholder="搜索元素"
            aria-label="搜索表格元素"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button type="button" onClick={() => setQuery('')}>
              ×
            </button>
          )}
        </div>}
        <span className="studio-count">
          {view === 'table' && query.trim()
            ? `匹配 ${tableRows.length} / ${graph.nodes.length} 元素`
            : `${graph.nodes.length} 元素`} · {graph.edges.length} 关联
        </span>
        <Button
          size="small"
          type="text"
          className="studio-refresh"
          icon={<ReloadOutlined spin={loading} />}
          onClick={refresh}
        />
      </div>

      {view === 'graph' && (
        <div className="studio-graph-slot">
          <Suspense fallback={<Spin style={{ margin: 48 }} />}>
            <LegacyStudioGraphHost
              project={project}
              graph={graph}
              selectedElementId={selectedElementId}
              onOpenEntry={openEntry}
            />
          </Suspense>
        </div>
      )}

      {view === 'table' && (
        <div className="center-anim studio-table">
          <Table
            size="small"
            rowKey="id"
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            dataSource={tableRows}
            onRow={(row) => ({ onClick: () => openEntry(row.path), style: { cursor: 'pointer' } })}
            columns={[
              {
                title: '类型',
                dataIndex: 'kind',
                width: 80,
                filters: [
                  { text: '对象', value: 'object' },
                  { text: '操作', value: 'operation' },
                  { text: '规则', value: 'rule' },
                  { text: '指标', value: 'metric' },
                  { text: '脚本', value: 'script' },
                ],
                onFilter: (value, row) => row.kind === value,
                render: (kind) => (
                  <Tag
                    color={{ object: 'blue', operation: 'green', logic: 'green', rule: 'gold', metric: 'purple', script: 'magenta' }[kind] || 'default'}
                  >
                    {KIND_LABEL[kind] || kind}
                  </Tag>
                ),
              },
              { title: '名称', dataIndex: 'label', render: (label, row) => label || row.id },
              {
                title: '最后更新时间',
                dataIndex: 'updatedAt',
                width: 170,
                sorter: compareUpdatedAt,
                sortDirections: ['descend', 'ascend'],
                render: formatUpdatedAt,
              },
              { title: '引用', dataIndex: 'out', width: 70, sorter: (a, b) => a.out - b.out },
              { title: '被引', dataIndex: 'in', width: 70, sorter: (a, b) => a.in - b.in },
            ]}
          />
        </div>
      )}

      {view === 'edit' && entry && (
        <StudioEntryEditor
          project={project}
          entry={entry}
          onCommand={onCommand}
          onExit={() => {
            openEntry(entry.path);
            onWorkspaceChanged?.();
          }}
          onCommitted={onWorkspaceChanged}
        />
      )}

      {view === 'entry' && entry && (
        <div className="center-anim studio-entry">
          <div className="studio-entry-head">
            <Typography.Title level={5} style={{ margin: 0 }}>
              {entry.label || entry.name}
            </Typography.Title>
            {entry.draft && (
              <Tag color="warning">
                {entry.draft.state === 'stale' ? '旧版本草稿已失效' : '有未提交草稿'}
                （{entry.draft.author}）
              </Tag>
            )}
            {entry.lease && <Tag color="processing">{entry.lease.owner} 编辑中</Tag>}
            {entry.schema && (
              <Button
                size="small"
                type="primary"
                icon={<EditOutlined />}
                loading={openingEditor}
                onClick={startEditing}
              >
                结构化编辑
              </Button>
            )}
          </div>
          {entry.links.length > 0 && (
            <div className="studio-links">
              关联：
              {entry.links.map((name) => (
                <Tag
                  key={name}
                  style={{ cursor: pathByName.has(name) ? 'pointer' : 'default' }}
                  color={pathByName.has(name) ? 'blue' : 'default'}
                  onClick={() => {
                    const path = pathByName.get(name);
                    if (path) openEntry(path);
                  }}
                >
                  {labelByName.get(name) || name}
                </Tag>
              ))}
            </div>
          )}
          {entry.backlinks.length > 0 && (
            <div className="studio-links">
              被引用：
              {entry.backlinks.map((back) => (
                <Tag
                  key={back.name}
                  style={{ cursor: 'pointer' }}
                  onClick={() => openEntry(back.path)}
                >
                  {back.label || back.name}
                </Tag>
              ))}
            </div>
          )}
          {entry.intro && (
            <Markdown content={entry.intro} onOpenFile={openFromEntry} knownPaths={knownPaths} />
          )}
          {entry.sections.map((section) => (
            <div key={section.title} className="studio-section msg-soft">
              <div className="studio-section-title">{section.title}</div>
              <Markdown content={section.content} onOpenFile={openFromEntry} knownPaths={knownPaths} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
