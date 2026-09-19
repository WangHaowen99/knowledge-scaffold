import {
  ArrowLeftOutlined,
  EditOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import dagre from '@dagrejs/dagre';
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Button, Empty, Spin, Table, Tag, Typography, message } from 'antd';
import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import { Markdown } from './CenterPane.jsx';
import StudioEntryEditor from './StudioEntryEditor.jsx';

// WebGL 大图运行时按需加载（pixi 体积大，不进默认 chunk）
const StudioBigGraph = lazy(() => import('./studio/StudioBigGraph.jsx'));

// 超过阈值自动切 WebGL 大图（Pixi + Worker 力导）；也可手动切换
const BIG_GRAPH_NODES = 150;

// 与 modeling 工作台 Studio 相同的 kind 视觉体系（对象=圆形，逻辑沿用"操作"的绿色圆角矩形）
const KIND_META = {
  object: { label: '对象', color: '#07386f', soft: '#9fc8ff', glow: 'rgba(7, 56, 111, .52)' },
  logic: { label: '逻辑', color: '#07513a', soft: '#a8dfc2', glow: 'rgba(7, 81, 58, .52)' },
};

const NODE_SIZE = {
  object: { width: 112, height: 112 },
  logic: { width: 150, height: 56 },
};

function StudioNode({ data }) {
  const meta = KIND_META[data.kind] || KIND_META.object;
  return (
    <div
      className={`studio-node ${data.kind}${data.dimmed ? ' dimmed' : ''}${data.selected ? ' selected' : ''}`}
      style={{ '--node-soft': meta.soft, '--node-color': meta.color, '--node-glow': meta.glow }}
    >
      <span className="studio-node-meta">
        {meta.label}
        <small>{data.degree}</small>
      </span>
      <strong>{data.name}</strong>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const NODE_TYPES = { studio: StudioNode };

function layout(nodes, edges) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 26, ranksep: 80 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) {
    const size = NODE_SIZE[node.data.kind] || NODE_SIZE.logic;
    g.setNode(node.id, { ...size });
  }
  for (const edge of edges) g.setEdge(edge.source, edge.target);
  dagre.layout(g);
  return nodes.map((node) => {
    const pos = g.node(node.id);
    const size = NODE_SIZE[node.data.kind] || NODE_SIZE.logic;
    return {
      ...node,
      position: { x: pos.x - size.width / 2, y: pos.y - size.height / 2 },
      style: { width: size.width, height: size.height },
    };
  });
}

export default function StudioView({ project, onOpenFile, onCommand, onWorkspaceChanged }) {
  const [view, setView] = useState('graph');
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [entry, setEntry] = useState(null);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(false);
  const [renderMode, setRenderMode] = useState('auto'); // auto | flow | webgl
  const useWebgl =
    renderMode === 'webgl' ||
    (renderMode === 'auto' && graph.nodes.length > BIG_GRAPH_NODES);

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
    setSelected(null);
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

  const kindCounts = useMemo(() => {
    const counts = { object: 0, logic: 0 };
    for (const node of graph.nodes) counts[node.kind] = (counts[node.kind] || 0) + 1;
    return counts;
  }, [graph]);

  const flowNodes = useMemo(() => {
    const trimmed = query.trim();
    return layout(
      graph.nodes.map((node) => ({
        id: node.id,
        type: 'studio',
        data: {
          name: node.id,
          kind: node.kind,
          degree: node.in + node.out,
          dimmed: trimmed ? !node.id.includes(trimmed) : false,
          selected: selected === node.id,
        },
      })),
      graph.edges,
    );
  }, [graph, query, selected]);

  const flowEdges = useMemo(
    () =>
      graph.edges.map((edge, index) => ({
        id: `e${index}`,
        source: edge.source,
        target: edge.target,
        className: 'studio-edge',
      })),
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
        {view === 'graph' && (
          <Button
            size="small"
            type="text"
            title={useWebgl ? '切回标准视图（适合小图精细交互）' : '切到 WebGL 大图（适合上千节点）'}
            onClick={() => setRenderMode(useWebgl ? 'flow' : 'webgl')}
          >
            {useWebgl ? 'WebGL' : '标准'}
          </Button>
        )}
        <Button
          size="small"
          type="text"
          className="studio-refresh"
          icon={<ReloadOutlined spin={loading} />}
          onClick={refresh}
        />
      </div>

      {view === 'graph' && useWebgl && (
        <div className="studio-graph">
          <Suspense fallback={<Spin style={{ margin: 48 }} />}>
            <StudioBigGraph
              graph={graph}
              kindMeta={KIND_META}
              query={query}
              selected={selected}
              onNodeClick={(id) => {
                setSelected(id);
                const path = pathByName.get(id);
                if (path) openEntry(path);
              }}
            />
          </Suspense>
          <div className="studio-graph-legend">
            {Object.entries(KIND_META).map(([kind, meta]) => (
              <button key={kind} type="button">
                <i className={`studio-swatch ${kind}`} style={{ background: meta.soft }} />
                {meta.label}
                <small>{kindCounts[kind] || 0}</small>
              </button>
            ))}
          </div>
        </div>
      )}

      {view === 'graph' && !useWebgl && (
        <div className="studio-graph">
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={NODE_TYPES}
            fitView
            proOptions={{ hideAttribution: true }}
            onlyRenderVisibleElements={graph.nodes.length > BIG_GRAPH_NODES}
            onNodeClick={(_, node) => {
              setSelected(node.id);
              const path = pathByName.get(node.id);
              if (path) openEntry(path);
            }}
            nodesConnectable={false}
            nodesDraggable
          >
            <Background gap={22} size={1} color="var(--border-subtle)" />
            {graph.nodes.length <= BIG_GRAPH_NODES && <MiniMap pannable zoomable />}
            <Controls showInteractive={false} />
          </ReactFlow>
          <div className="studio-graph-legend">
            {Object.entries(KIND_META).map(([kind, meta]) => (
              <button key={kind} type="button">
                <i className={`studio-swatch ${kind}`} style={{ background: meta.soft }} />
                {meta.label}
                <small>{kindCounts[kind] || 0}</small>
              </button>
            ))}
          </div>
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
                  { text: '逻辑', value: 'logic' },
                ],
                onFilter: (value, row) => row.kind === value,
                render: (kind) => (
                  <Tag color={kind === 'object' ? 'blue' : 'green'}>
                    {KIND_META[kind]?.label || kind}
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
            <Button size="small" icon={<EditOutlined />} onClick={() => onOpenFile(entry.path)}>
              在编辑器中打开
            </Button>
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
