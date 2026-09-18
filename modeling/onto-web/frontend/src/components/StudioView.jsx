import { ArrowLeftOutlined, EditOutlined } from '@ant-design/icons';
import dagre from '@dagrejs/dagre';
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { Button, Empty, Segmented, Space, Table, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api.js';
import { Markdown } from './CenterPane.jsx';

const KIND_META = {
  object: { label: '对象', color: '#256fd0', bg: '#e8f1fd' },
  logic: { label: '逻辑', color: '#7048c8', bg: '#eae2f9' },
};

function layout(nodes, edges) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 24, ranksep: 70 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const node of nodes) g.setNode(node.id, { width: 150, height: 40 });
  for (const edge of edges) g.setEdge(edge.source, edge.target);
  dagre.layout(g);
  return nodes.map((node) => {
    const pos = g.node(node.id);
    return { ...node, position: { x: pos.x - 75, y: pos.y - 20 } };
  });
}

export default function StudioView({ project, onOpenFile }) {
  const [view, setView] = useState('graph');
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [entry, setEntry] = useState(null);

  useEffect(() => {
    setEntry(null);
    setView('graph');
    api.studioGraph(project.id).then(setGraph).catch(() => {});
  }, [project.id]);

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

  const flowNodes = useMemo(
    () =>
      layout(
        graph.nodes.map((node) => {
          const meta = KIND_META[node.kind] || KIND_META.object;
          return {
            id: node.id,
            data: { label: node.id },
            style: {
              background: meta.bg,
              border: `1.5px solid ${meta.color}`,
              borderRadius: 10,
              fontSize: 12,
              color: 'var(--text-primary)',
              padding: '6px 10px',
            },
          };
        }),
        graph.edges,
      ),
    [graph],
  );

  const flowEdges = useMemo(
    () =>
      graph.edges.map((edge, index) => ({
        id: `e${index}`,
        source: edge.source,
        target: edge.target,
        animated: false,
        style: { stroke: 'var(--border-default)' },
      })),
    [graph],
  );

  if (graph.nodes.length === 0) {
    return (
      <Empty
        description="还没有可视化的本体元素——先完成对象/逻辑提取"
        style={{ marginTop: 48 }}
      />
    );
  }

  return (
    <div className="studio">
      <Space style={{ marginBottom: 10 }}>
        {view === 'entry' && (
          <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => setView('graph')}>
            返回
          </Button>
        )}
        <Segmented
          size="small"
          value={view === 'entry' ? 'graph' : view}
          onChange={(v) => setView(v)}
          options={[
            { label: '图', value: 'graph' },
            { label: '表', value: 'table' },
          ]}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {graph.nodes.length} 个元素 · {graph.edges.length} 条关联
        </Typography.Text>
      </Space>

      {view === 'graph' && (
        <div className="studio-graph center-anim">
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            fitView
            proOptions={{ hideAttribution: true }}
            onNodeClick={(_, node) => {
              const path = pathByName.get(node.id);
              if (path) openEntry(path);
            }}
            nodesConnectable={false}
            nodesDraggable
          >
            <Background gap={18} size={1} />
            <MiniMap pannable zoomable />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      )}

      {view === 'table' && (
        <div className="center-anim">
          <Table
            size="small"
            rowKey="id"
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            dataSource={graph.nodes}
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
                  <Tag color={kind === 'object' ? 'blue' : 'purple'}>
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

      {view === 'entry' && entry && (
        <div className="center-anim">
          <Space align="center" style={{ marginBottom: 8 }}>
            <Typography.Title level={5} style={{ margin: 0 }}>
              {entry.name}
            </Typography.Title>
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() => onOpenFile(entry.path)}
            >
              在编辑器中打开
            </Button>
          </Space>
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
                <Tag key={back.name} style={{ cursor: 'pointer' }} onClick={() => openEntry(back.path)}>
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
