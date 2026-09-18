import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons';
import { Button, Empty, Spin, Table, Tag, Typography } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log2(bytes) / 10), units.length - 1);
  return `${(bytes / 2 ** (10 * index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function timeAgo(ts) {
  if (!ts) return '—';
  const seconds = Date.now() / 1000 - ts;
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return new Date(ts * 1000).toLocaleDateString();
}

export default function AdminPage({ onBack }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(() => {
    setLoading(true);
    api
      .adminOverview()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  const tiles = data
    ? [
        { label: '在线用户', value: data.online_count, color: '#256fd0', bg: '#e8f1fd' },
        { label: '活跃会话', value: data.sessions.length, color: '#7048c8', bg: '#eae2f9' },
        { label: '建模任务', value: data.projects.length, color: '#0f8f7f', bg: '#d5f3ee' },
        {
          label: '累计成本',
          value: `$${data.total_cost_usd.toFixed(2)}`,
          color: '#b77909',
          bg: '#fbf0d7',
        },
        {
          label: '磁盘可用',
          value: formatBytes(data.system.disk_free),
          color: '#c53d4b',
          bg: '#fbe8e8',
        },
      ]
    : [];

  return (
    <div className="admin-page">
      <div className="pj-head">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回
        </Button>
        <div>
          <h2 style={{ margin: 0 }}>运维总览</h2>
          <p style={{ margin: 0 }}>负载 {data ? data.system.load.map((v) => v.toFixed(2)).join(' / ') : '—'} · 每 30 秒自动刷新</p>
        </div>
        <span style={{ flex: 1 }} />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
          刷新
        </Button>
      </div>
      <Spin spinning={loading && !data}>
        <div className="stat-grid" style={{ marginBottom: 16 }}>
          {tiles.map((tile, index) => (
            <div
              key={tile.label}
              className="stat-tile msg-soft"
              style={{ '--tile': tile.color, '--tile-bg': tile.bg, animationDelay: `${index * 50}ms` }}
            >
              <div className="stat-num">{tile.value}</div>
              <div className="stat-label">{tile.label}</div>
            </div>
          ))}
        </div>

        <Typography.Title level={5}>用户</Typography.Title>
        <Table
          size="small"
          rowKey="username"
          pagination={false}
          dataSource={data?.users || []}
          locale={{ emptyText: '开启登录后展示' }}
          columns={[
            { title: '用户', dataIndex: 'username' },
            {
              title: '状态',
              dataIndex: 'online',
              render: (online) =>
                online ? <Tag color="success">在线</Tag> : <Tag>离线</Tag>,
            },
            { title: '最近活跃', dataIndex: 'last_seen', render: timeAgo },
          ]}
          style={{ marginBottom: 20 }}
        />

        <Typography.Title level={5}>建模任务</Typography.Title>
        <Table
          size="small"
          rowKey="id"
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          dataSource={data?.projects || []}
          columns={[
            { title: '所有者', dataIndex: 'owner', render: (v) => v || '—' },
            { title: '任务', dataIndex: 'name' },
            {
              title: '阶段',
              render: (_, row) => (
                <span>
                  {row.stages_done}/{row.stages_total}
                </span>
              ),
            },
            {
              title: '成本',
              render: (_, row) => `$${row.usage.total_cost_usd.toFixed(2)}`,
              sorter: (a, b) => a.usage.total_cost_usd - b.usage.total_cost_usd,
            },
            {
              title: '输出 tokens',
              render: (_, row) => row.usage.output_tokens.toLocaleString(),
            },
            { title: '占用', dataIndex: 'size_bytes', render: formatBytes },
            {
              title: '最近活动',
              render: (_, row) => timeAgo(row.usage.last_activity),
            },
          ]}
          style={{ marginBottom: 20 }}
        />

        <Typography.Title level={5}>原始错误（最近 100 条）</Typography.Title>
        {data?.errors?.length ? (
          <Table
            size="small"
            rowKey={(row) => `${row.ts}-${row.project_id}`}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={data.errors}
            expandable={{
              expandedRowRender: (row) => (
                <pre className="admin-raw-error">{row.raw}</pre>
              ),
            }}
            columns={[
              {
                title: '时间',
                dataIndex: 'ts',
                width: 170,
                render: (ts) => new Date(ts * 1000).toLocaleString(),
              },
              { title: '项目', dataIndex: 'project_id', width: 180 },
              { title: '来源', dataIndex: 'source', width: 120 },
              {
                title: '摘要',
                dataIndex: 'raw',
                ellipsis: true,
              },
            ]}
          />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无错误记录" />
        )}
      </Spin>
    </div>
  );
}
