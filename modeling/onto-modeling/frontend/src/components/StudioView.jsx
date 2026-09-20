import {
  ArrowLeftOutlined,
  EditOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import { Button, Empty, Spin, Table, Tag, Typography, message } from 'antd';
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import { Markdown } from './CenterPane.jsx';
import StudioEntryEditor from './StudioEntryEditor.jsx';

// 图模式 = 原版 modeling Studio 图整体搬迁（含 WebGL 力导运行时），按需加载
const LegacyStudioGraphHost = lazy(() => import('./studio/LegacyStudioGraphHost.jsx'));

// 表格视图的 kind 视觉沿用原版 KIND_META 的对象/操作用色
const KIND_LABEL = { object: '对象', operation: '操作', logic: '操作', rule: '规则', metric: '指标', script: '脚本' };

export default function StudioView({ project, onOpenFile, onCommand, onWorkspaceChanged }) {
  const [view, setView] = useState('graph');
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [entry, setEntry] = useState(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(() => {
    setLoading(true);
    api
      .studioGraph(project.id)
      .then(setGraph)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [project.id]);

  useEffect(() => {
    setEntry(null);
    setView('graph');
    setQuery('');
    refresh();
  }, [refresh]);

  const openEntry = useCallback(
    (path) => {
      api
        .studioElement(project.id, path)
        .then((data) => {
          setEntry(data);
          setView('entry');
        })
        .catch((error) => message.error(error.message));
    },
    [project.id],
  );

  const pathByName = useMemo(() => {
    const map = new Map();
    for (const node of graph.nodes) map.set(node.id, node.path);
    return map;
  }, [graph]);

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
        <div className="studio-search">
          <SearchOutlined />
          <input
            placeholder="搜索元素"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button type="button" onClick={() => setQuery('')}>
              ×
            </button>
          )}
        </div>
        <span className="studio-count">
          {graph.nodes.length} 元素 · {graph.edges.length} 关联
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
              query={query}
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
            dataSource={
              query.trim()
                ? graph.nodes.filter((n) => n.id.includes(query.trim()))
                : graph.nodes
            }
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
              { title: '名称', dataIndex: 'id' },
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
              {entry.name}
            </Typography.Title>
            {entry.draft && <Tag color="warning">有未提交草稿（{entry.draft.author}）</Tag>}
            {entry.lease && <Tag color="processing">{entry.lease.owner} 编辑中</Tag>}
            {entry.schema && (
              <Button
                size="small"
                type="primary"
                icon={<EditOutlined />}
                onClick={() => setView('edit')}
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
                  {name}
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
                  {back.name}
                </Tag>
              ))}
            </div>
          )}
          {entry.intro && <Markdown content={entry.intro} />}
          {entry.sections.map((section) => (
            <div key={section.title} className="studio-section msg-soft">
              <div className="studio-section-title">{section.title}</div>
              <Markdown content={section.content} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
