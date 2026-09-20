import { CopyOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Collapse,
  Empty,
  Input,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

const EMPTY_DRAFT = { name: '', transport: 'http', url: '', command: '' };

function copyText(text, label) {
  navigator.clipboard?.writeText(text).then(
    () => message.success(`${label}已复制`),
    () => message.warning('复制失败，请手动选择复制'),
  );
}

// 对外暴露的 MCP 服务：状态 / 令牌管理 / 客户端配置（移植自 knowledge scaffold 的 MCP 接入模式）
function ExposedService() {
  const [status, setStatus] = useState(null);
  const [tokenName, setTokenName] = useState('');
  const [freshToken, setFreshToken] = useState(null); // 一次性明文

  const refresh = () => {
    api.mcpService().then(setStatus).catch(() => {});
  };
  useEffect(refresh, []);

  if (!status) return null;
  const serviceUrl = `${window.location.origin}${status.path}`;
  const clientConfig = JSON.stringify(
    {
      mcpServers: {
        'onto-modeling': {
          type: 'http',
          url: serviceUrl,
          headers: { Authorization: 'Bearer <你的令牌>' },
        },
      },
    },
    null,
    2,
  );

  const createToken = async () => {
    try {
      const data = await api.mcpServiceCreateToken(tokenName || '未命名');
      setFreshToken(data.token);
      setTokenName('');
      refresh();
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div>
      <div className="mcp-status-line">
        <Tag color="success">streamable-http</Tag>
        <Typography.Text code copyable={{ text: serviceUrl }}>
          {serviceUrl}
        </Typography.Text>
      </div>
      <Collapse
        ghost
        size="small"
        items={[
          {
            key: 'tools',
            label: `提供 ${status.tools.length} 个只读工具`,
            children: status.tools.map((tool) => (
              <div key={tool.name} className="mcp-tool-line">
                <Typography.Text code>{tool.name}</Typography.Text>{' '}
                <Typography.Text type="secondary">{tool.description}</Typography.Text>
              </div>
            )),
          },
          {
            key: 'client',
            label: '客户端配置示例',
            children: (
              <>
                <pre className="mcp-client-config">{clientConfig}</pre>
                <Button
                  size="small"
                  icon={<CopyOutlined />}
                  onClick={() => copyText(clientConfig, '配置')}
                >
                  复制配置
                </Button>
              </>
            ),
          },
        ]}
      />
      <Space.Compact style={{ width: '100%', margin: '12px 0 8px' }}>
        <Input
          placeholder="令牌用途，如：外部检索代理"
          value={tokenName}
          onChange={(e) => setTokenName(e.target.value)}
        />
        <Button type="primary" icon={<PlusOutlined />} onClick={createToken}>
          生成令牌
        </Button>
      </Space.Compact>
      {freshToken && (
        <Alert
          type="success"
          showIcon
          closable
          onClose={() => setFreshToken(null)}
          message="令牌已生成，仅此一次可见，请立即保存"
          description={
            <Space>
              <Typography.Text code>{freshToken}</Typography.Text>
              <Button
                size="small"
                icon={<CopyOutlined />}
                onClick={() => copyText(freshToken, '令牌')}
              />
            </Space>
          }
          style={{ marginBottom: 8 }}
        />
      )}
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={status.tokens}
        locale={{ emptyText: '尚未生成令牌' }}
        columns={[
          { title: '用途', dataIndex: 'name' },
          { title: '令牌', dataIndex: 'masked', render: (v) => <code>{v}</code> },
          {
            title: '创建',
            dataIndex: 'created_at',
            render: (v) => new Date(v * 1000).toLocaleDateString(),
          },
          {
            title: '',
            render: (_, row) => (
              <Button
                size="small"
                type="text"
                danger
                icon={<DeleteOutlined />}
                onClick={async () => {
                  await api.mcpServiceRevokeToken(row.id).catch(() => {});
                  refresh();
                  message.success('已吊销');
                }}
              />
            ),
          },
        ]}
      />
    </div>
  );
}

// 消费外部 MCP 服务器的配置（会话装配用）
function ExternalServers({ project }) {
  const [servers, setServers] = useState([]);
  const [draft, setDraft] = useState(EMPTY_DRAFT);

  useEffect(() => {
    api.getMcp().then((data) => setServers(data.servers)).catch(() => {});
  }, []);

  const persist = async (next) => {
    try {
      const saved = await api.saveMcp(next);
      setServers(saved.servers);
      return true;
    } catch (error) {
      message.error(error.message);
      return false;
    }
  };

  return (
    <div>
      {servers.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未配置外部服务器" />
      ) : (
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          {servers.map((server, index) => (
            <div key={server.name} className="mcp-row">
              <Tag color="processing">{server.transport}</Tag>
              <div className="mcp-meta">
                <div className="mcp-name">{server.name}</div>
                <div className="mcp-target">{server.url || server.command}</div>
              </div>
              <Switch
                size="small"
                checked={server.enabled}
                onChange={(checked) =>
                  persist(servers.map((s, i) => (i === index ? { ...s, enabled: checked } : s)))
                }
              />
              <Button
                size="small"
                type="text"
                onClick={async () => {
                  try {
                    const result = await api.mcpTest(server);
                    (result.ok ? message.success : message.warning)(
                      `${server.name}：${result.detail}`,
                    );
                  } catch (error) {
                    message.error(error.message);
                  }
                }}
              >
                测试
              </Button>
              <Button
                size="small"
                type="text"
                icon={<DeleteOutlined />}
                onClick={() => persist(servers.filter((_, i) => i !== index))}
              />
            </div>
          ))}
        </Space>
      )}
      <div className="mcp-add">
        <Typography.Text strong>添加服务器</Typography.Text>
        <Space.Compact style={{ width: '100%', marginTop: 8 }}>
          <Input
            placeholder="名称"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            style={{ width: '38%' }}
          />
          <Select
            value={draft.transport}
            onChange={(transport) => setDraft({ ...draft, transport })}
            options={[
              { value: 'http', label: 'HTTP' },
              { value: 'sse', label: 'SSE' },
              { value: 'stdio', label: 'stdio' },
            ]}
            style={{ width: '28%' }}
          />
        </Space.Compact>
        <Input
          style={{ marginTop: 8 }}
          placeholder={draft.transport === 'stdio' ? '启动命令（含参数）' : '服务地址 http(s)://…'}
          value={draft.transport === 'stdio' ? draft.command : draft.url}
          onChange={(e) =>
            setDraft(
              draft.transport === 'stdio'
                ? { ...draft, command: e.target.value }
                : { ...draft, url: e.target.value },
            )
          }
        />
        <Button
          type="primary"
          size="small"
          icon={<PlusOutlined />}
          style={{ marginTop: 8 }}
          onClick={async () => {
            if (await persist([...servers, { ...draft, enabled: true }])) setDraft(EMPTY_DRAFT);
          }}
        >
          添加
        </Button>
      </div>
      <Typography.Paragraph type="secondary" style={{ marginTop: 12, fontSize: 12 }}>
        配置在新会话生效。
        {project && (
          <Typography.Link
            onClick={async () => {
              await api.closeSession(project.id).catch(() => {});
              message.success('会话已重置，下次发消息按最新配置启动');
            }}
          >
            {' '}
            重启当前任务的会话
          </Typography.Link>
        )}
      </Typography.Paragraph>
    </div>
  );
}

// 项目级启用开关：全局启用的服务器可在单个任务里停用（新会话生效）
export function ProjectMcpToggle({ project }) {
  const [data, setData] = useState(null);

  useEffect(() => {
    if (!project) return;
    api.getProjectMcp(project.id).then(setData).catch(() => {});
  }, [project]);

  if (!project || !data || data.servers.length === 0) return null;

  const toggle = async (name, enabled) => {
    const disabled = new Set(data.disabled);
    if (enabled) disabled.delete(name);
    else disabled.add(name);
    try {
      await api.saveProjectMcp(project.id, [...disabled]);
      setData(await api.getProjectMcp(project.id));
      message.success('已保存，本任务新会话生效');
    } catch (error) {
      message.error(error.message);
    }
  };

  return (
    <div style={{ marginTop: 16 }}>
      <Typography.Text strong>本任务启用状态（{project.name}）</Typography.Text>
      <Space direction="vertical" style={{ width: '100%', marginTop: 8 }} size={4}>
        {data.servers.map((server) => (
          <div key={server.name} className="mcp-row">
            <Tag color="processing">{server.transport}</Tag>
            <div className="mcp-meta">
              <div className="mcp-name">{server.name}</div>
            </div>
            <Switch
              size="small"
              disabled={!server.enabled_globally}
              checked={server.enabled}
              onChange={(checked) => toggle(server.name, checked)}
            />
          </div>
        ))}
      </Space>
    </div>
  );
}

export default function McpSettings({ project }) {
  return (
    <div>
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        对外 MCP 服务
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        把本工作台的本体产物以只读工具集暴露给外部 MCP 客户端（IDE / 其他 Agent）。
      </Typography.Paragraph>
      <ExposedService />
      <Typography.Title level={5} style={{ marginTop: 24 }}>
        外部 MCP 服务器
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        为建模助手接入外部工具能力。
      </Typography.Paragraph>
      <ExternalServers project={project} />
      <ProjectMcpToggle project={project} />
    </div>
  );
}
