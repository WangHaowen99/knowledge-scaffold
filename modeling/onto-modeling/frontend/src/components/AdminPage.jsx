import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd';
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

function OverviewTab({ data, loading, refresh }) {
  const [backingUp, setBackingUp] = useState(false);

  const tiles = data
    ? [
        { label: '在线用户', value: data.online_count, tone: 'tone-blue' },
        { label: '活跃会话', value: data.sessions.length, tone: 'tone-violet' },
        { label: '建模任务', value: data.projects.length, tone: 'tone-teal' },
        { label: '累计成本', value: `$${data.total_cost_usd.toFixed(2)}`, tone: 'tone-amber' },
        { label: '磁盘可用', value: formatBytes(data.system.disk_free), tone: 'tone-red' },
      ]
    : [];

  return (
    <Spin spinning={loading && !data}>
      <div className="stat-grid" style={{ marginBottom: 16 }}>
        {tiles.map((tile, index) => (
          <div
            key={tile.label}
            className={`stat-tile msg-soft ${tile.tone}`}
            style={{ animationDelay: `${index * 50}ms` }}
          >
            <div className="stat-num">{tile.value}</div>
            <div className="stat-label">{tile.label}</div>
          </div>
        ))}
      </div>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        负载 {data ? data.system.load.map((v) => v.toFixed(2)).join(' / ') : '—'} · Agent SDK{' '}
        {data?.system.sdk_version || '—'} · 每 30 秒自动刷新
      </Typography.Paragraph>

      <Typography.Title level={5}>按日用量（最近 14 天）</Typography.Title>
      <Table
        size="small"
        rowKey="date"
        pagination={false}
        dataSource={data?.usage_daily || []}
        locale={{ emptyText: '暂无调用记录' }}
        columns={[
          { title: '日期', dataIndex: 'date' },
          { title: '调用', dataIndex: 'calls' },
          {
            title: '成本',
            dataIndex: 'cost',
            render: (v) => `$${v.toFixed(2)}`,
          },
          {
            title: '输出 tokens',
            dataIndex: 'output_tokens',
            render: (v) => v.toLocaleString(),
          },
        ]}
        style={{ marginBottom: 20 }}
      />

      <Typography.Title level={5}>用户活跃</Typography.Title>
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
            render: (online) => (online ? <Tag color="success">在线</Tag> : <Tag>离线</Tag>),
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
            render: (_, row) => `${row.stages_done}/${row.stages_total}`,
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
          { title: '最近活动', render: (_, row) => timeAgo(row.usage.last_activity) },
        ]}
        style={{ marginBottom: 20 }}
      />

      <Typography.Title level={5}>
        <DatabaseOutlined /> 工作区备份
      </Typography.Title>
      <Button
        size="small"
        loading={backingUp}
        onClick={async () => {
          setBackingUp(true);
          try {
            const result = await api.adminBackup();
            message.success(`备份完成：${result.created}`);
            refresh();
          } catch (error) {
            message.error(error.message);
          } finally {
            setBackingUp(false);
          }
        }}
      >
        立即备份
      </Button>
      <Table
        size="small"
        rowKey="name"
        pagination={false}
        dataSource={data?.backups || []}
        locale={{ emptyText: '暂无备份' }}
        columns={[
          { title: '文件', dataIndex: 'name' },
          { title: '大小', dataIndex: 'size', render: formatBytes },
          { title: '时间', dataIndex: 'ts', render: (v) => new Date(v * 1000).toLocaleString() },
        ]}
        style={{ margin: '8px 0 20px' }}
      />

      <Typography.Title level={5}>原始错误（最近 100 条）</Typography.Title>
      {data?.errors?.length ? (
        <Table
          size="small"
          rowKey={(row) => `${row.ts}-${row.project_id}`}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          dataSource={data.errors}
          expandable={{
            expandedRowRender: (row) => <pre className="admin-raw-error">{row.raw}</pre>,
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
            { title: '摘要', dataIndex: 'raw', ellipsis: true },
          ]}
        />
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无错误记录" />
      )}
    </Spin>
  );
}

function UsersTab({ available }) {
  const [users, setUsers] = useState([]);
  const [resetting, setResetting] = useState(null); // {username, password}

  const refresh = useCallback(() => {
    api
      .adminUsers()
      .then((data) => setUsers(data.users || []))
      .catch((error) => message.error(error.message));
  }, []);

  useEffect(() => {
    if (available) refresh();
  }, [available, refresh]);

  if (!available) {
    return (
      <Alert
        type="warning"
        showIcon
        message="用户管理不可用"
        description="需要在服务端配置认证服务管理令牌（ONTO_WEB_AUTH_ADMIN_TOKEN 与 AUTH_SERVER_ADMIN_TOKEN）。"
      />
    );
  }

  return (
    <>
      <Table
        size="small"
        rowKey="username"
        pagination={false}
        dataSource={users}
        columns={[
          { title: '用户', dataIndex: 'username' },
          {
            title: '角色',
            dataIndex: 'role',
            render: (role, row) => (
              <Select
                size="small"
                value={role}
                disabled={row.username === 'admin'}
                style={{ width: 110 }}
                options={[
                  { value: 'user', label: '建模者' },
                  { value: 'admin', label: '管理员' },
                ]}
                onChange={async (next) => {
                  try {
                    await api.adminUpdateUser(row.username, { role: next });
                    message.success('角色已更新');
                    refresh();
                  } catch (error) {
                    message.error(error.message);
                  }
                }}
              />
            ),
          },
          {
            title: '操作',
            render: (_, row) => (
              <>
                <Button
                  size="small"
                  type="link"
                  onClick={() => setResetting({ username: row.username, password: '' })}
                >
                  重置密码
                </Button>
                {row.username !== 'admin' && (
                  <Popconfirm
                    title={`删除用户 ${row.username}？`}
                    onConfirm={async () => {
                      try {
                        await api.adminDeleteUser(row.username);
                        message.success('已删除');
                        refresh();
                      } catch (error) {
                        message.error(error.message);
                      }
                    }}
                  >
                    <Button size="small" type="link" danger>
                      删除
                    </Button>
                  </Popconfirm>
                )}
              </>
            ),
          },
        ]}
      />
      <Modal
        title={`重置密码：${resetting?.username || ''}`}
        open={!!resetting}
        onCancel={() => setResetting(null)}
        onOk={async () => {
          try {
            await api.adminUpdateUser(resetting.username, { password: resetting.password });
            message.success('密码已重置');
            setResetting(null);
          } catch (error) {
            message.error(error.message);
          }
        }}
      >
        <Input.Password
          placeholder="新密码（至少 8 位，含三类字符）"
          value={resetting?.password}
          onChange={(e) => setResetting({ ...resetting, password: e.target.value })}
        />
      </Modal>
    </>
  );
}

// 路径说明不了的操作要点：指定所有者的原值与新值、迁移时移除的成员表
function auditDetail(detail) {
  if (!detail) return '';
  if (detail.action === 'set_owner') return `所有者：${detail.from || '无主'} → ${detail.to}`;
  if (detail.action === 'remove_members') {
    return `单人所有权迁移，移除成员：${Object.keys(detail.members || {}).join('、') || '无'}`;
  }
  return JSON.stringify(detail);
}

function AuditTab() {
  const [entries, setEntries] = useState([]);

  useEffect(() => {
    api.adminAudit().then((data) => setEntries(data.entries)).catch(() => {});
  }, []);

  return (
    <Table
      size="small"
      rowKey={(row) => `${row.ts}-${row.path}`}
      pagination={{ pageSize: 20, hideOnSinglePage: true }}
      dataSource={entries}
      locale={{ emptyText: '暂无审计记录' }}
      columns={[
        {
          title: '时间',
          dataIndex: 'ts',
          width: 180,
          render: (ts) => new Date(ts * 1000).toLocaleString(),
        },
        { title: '用户', dataIndex: 'user', width: 140 },
        { title: '操作', dataIndex: 'method', width: 90 },
        { title: '路径', dataIndex: 'path' },
        { title: '说明', dataIndex: 'detail', render: auditDetail },
      ]}
    />
  );
}

// 会话池：活跃/排队一览 + 手动回收（续接点保留，用户下次发消息自动恢复）
function SessionPoolTab({ data, refresh }) {
  const pool = data?.session_pool;
  const sessions = data?.sessions || [];
  if (!pool) return null;
  return (
    <div>
      <Descriptions column={3} size="small" bordered style={{ marginBottom: 12 }}>
        <Descriptions.Item label="活跃会话">
          {pool.active} / {pool.max}
        </Descriptions.Item>
        <Descriptions.Item label="排队中">{pool.waiting.length}</Descriptions.Item>
        <Descriptions.Item label="空闲回收阈值">
          {pool.idle_ttl_seconds > 0 ? `${Math.round(pool.idle_ttl_seconds / 60)} 分钟` : '关闭'}
        </Descriptions.Item>
      </Descriptions>
      {pool.waiting.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={`排队中的任务：${pool.waiting.join('、')}`}
        />
      )}
      <Table
        size="small"
        rowKey="project_id"
        dataSource={sessions}
        pagination={false}
        columns={[
          { title: '任务', dataIndex: 'project_id' },
          {
            title: '状态',
            dataIndex: 'state',
            render: (state, row) => (
              <Tag color={state === 'running' ? 'processing' : state === 'error' ? 'error' : 'default'}>
                {state}
                {row.connected ? '' : '（无子进程）'}
              </Tag>
            ),
          },
          { title: '事件数', dataIndex: 'events' },
          {
            title: '最近活动',
            dataIndex: 'last_activity',
            render: (ts) => (ts ? timeAgo(ts) : '—'),
          },
          {
            title: '',
            render: (_, row) => (
              <Popconfirm
                title="回收该会话子进程？"
                description="模型上下文经 resume 保留，用户下次发消息自动恢复"
                onConfirm={async () => {
                  await api.adminRecycleSession(row.project_id).catch(() => {});
                  message.success('已回收');
                  refresh();
                }}
              >
                <Button size="small" disabled={!row.connected}>
                  回收
                </Button>
              </Popconfirm>
            ),
          },
        ]}
        locale={{ emptyText: '当前没有活跃会话' }}
      />
    </div>
  );
}

// 健康页：SDK/CLI 版本、磁盘水位告警、负载、备份调度
function HealthTab() {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    api.adminHealth().then(setHealth).catch(() => {});
  }, []);

  if (!health) return null;
  return (
    <div style={{ maxWidth: 640 }}>
      {health.disk.warn && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`磁盘水位告警：可用仅 ${formatBytes(health.disk.free)}（${health.disk.free_pct}%）`}
          description="建议清理旧备份/回收站，或扩容数据盘。"
        />
      )}
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="Agent SDK 版本">{health.sdk_version || '未安装'}</Descriptions.Item>
        <Descriptions.Item label="捆绑 CLI 版本">{health.cli_version || '未检测到'}</Descriptions.Item>
        <Descriptions.Item label="磁盘">
          可用 {formatBytes(health.disk.free)} / 共 {formatBytes(health.disk.total)}（
          {health.disk.free_pct}% 可用）
        </Descriptions.Item>
        <Descriptions.Item label="负载（1/5/15 分钟）">
          {health.load.map((v) => v.toFixed(2)).join(' / ')}
        </Descriptions.Item>
        <Descriptions.Item label="会话池">
          活跃 {health.pool.active}/{health.pool.max} · 排队 {health.pool.waiting.length}
        </Descriptions.Item>
        <Descriptions.Item label="定期备份">
          {health.backup.interval_hours > 0
            ? `每 ${health.backup.interval_hours} 小时`
            : '已关闭'}
          {health.backup.last &&
            ` · 最近：${new Date(health.backup.last.ts * 1000).toLocaleString()}`}
        </Descriptions.Item>
      </Descriptions>
    </div>
  );
}

// 回收站：旧版软删除留下的存量项目的可视化恢复（删除已改为永久删除，不再产生新条目）
function TrashTab() {
  const [entries, setEntries] = useState(null);

  const refresh = () => {
    api.adminTrash().then((d) => setEntries(d.entries)).catch(() => {});
  };
  useEffect(refresh, []);

  if (!entries) return <Spin />;
  return (
    <Table
      size="small"
      rowKey="name"
      dataSource={entries}
      pagination={false}
      locale={{ emptyText: '回收站为空' }}
      columns={[
        { title: '任务', dataIndex: 'project_name' },
        { title: '所有者', dataIndex: 'owner', render: (v) => v || '—' },
        {
          title: '删除时间',
          dataIndex: 'deleted_at',
          render: (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—'),
        },
        {
          title: '',
          render: (_, row) => (
            <Popconfirm
              title={`恢复「${row.project_name}」？`}
              onConfirm={async () => {
                try {
                  await api.adminRestoreTrash(row.name);
                  message.success('已恢复到项目列表');
                  refresh();
                } catch (error) {
                  message.error(error.message);
                }
              }}
            >
              <Button size="small">恢复</Button>
            </Popconfirm>
          ),
        },
      ]}
    />
  );
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

  return (
    <div className="admin-page">
      <div className="pj-head">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>
          返回
        </Button>
        <h2 style={{ margin: 0 }}>运维中心</h2>
        <span style={{ flex: 1 }} />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={refresh}>
          刷新
        </Button>
      </div>
      <Tabs
        items={[
          {
            key: 'overview',
            label: '总览',
            children: <OverviewTab data={data} loading={loading} refresh={refresh} />,
          },
          {
            key: 'users',
            label: '用户管理',
            children: <UsersTab available={!!data?.user_admin_available} />,
          },
          {
            key: 'pool',
            label: '会话池',
            children: <SessionPoolTab data={data} refresh={refresh} />,
          },
          { key: 'health', label: '健康', children: <HealthTab /> },
          { key: 'trash', label: '回收站', children: <TrashTab /> },
          { key: 'audit', label: '审计日志', children: <AuditTab /> },
        ]}
      />
    </div>
  );
}
