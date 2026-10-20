import { CopyOutlined, DeleteOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
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
import { useEffect, useRef, useState } from 'react';
import { api, authStore } from '../lib/api.js';

const EMPTY_DRAFT = { name: '', transport: 'http', url: '', command: '' };

function copyText(text, label) {
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(
      () => message.success(`${label}已复制`),
      () => message.warning('复制失败，请手动选择复制'),
    );
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  copied ? message.success(`${label}已复制`) : message.warning('复制失败，请手动选择复制');
}

function mcpUrl(runtime, toolset) {
  return (runtime?.urls || []).find(
    (item) => item.toolset === toolset && item.transport === 'streamable-http',
  )?.url || '';
}

function buildClientConfigs(runtime, token, scopes) {
  const selected = Array.isArray(scopes) ? scopes : [
    'ontology.read', 'ontology.change.validate', 'ontology.change.apply',
  ];
  const auth = { Authorization: `Bearer ${token || '<MCP_TOKEN>'}` };
  const read = selected.includes('ontology.read') && mcpUrl(runtime, 'read')
    ? { type: 'http', url: mcpUrl(runtime, 'read'), headers: auth }
    : null;
  const change = selected.some((scope) => scope.startsWith('ontology.change.')) && mcpUrl(runtime, 'change')
    ? { type: 'http', url: mcpUrl(runtime, 'change'), headers: auth }
    : null;
  const json = (servers) => Object.keys(servers).length
    ? JSON.stringify({ mcpServers: servers }, null, 2)
    : '';
  return {
    read: read ? json({ 'modeling-read': read }) : '',
    change: change ? json({ 'modeling-change': change }) : '',
    combined: json({
      ...(read ? { 'modeling-read': read } : {}),
      ...(change ? { 'modeling-change': change } : {}),
    }),
  };
}

function formatMcpTime(value) {
  if (!value) return '永不过期';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}

function isLoopbackUrl(value) {
  return /:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::|\/|$)/u.test(String(value || ''));
}

function toolsetScopes(toolset) {
  const scopes = toolset?.accepted_scopes?.length
    ? toolset.accepted_scopes
    : toolset?.required_scopes || [];
  return scopes.length ? `${toolset.scope_requirement === 'any' ? '任一' : '全部'}：${scopes.join('、')}` : '无';
}

// 管理 Modeling 服务的项目级 MCP：实际服务和 Token 均由 5188 统一提供。
function ExposedService({ project }) {
  const [status, setStatus] = useState(null);
  const [tokens, setTokens] = useState([]);
  const [tokenName, setTokenName] = useState('');
  const [freshToken, setFreshToken] = useState(null);
  const [scopes, setScopes] = useState(['ontology.read']);
  const [expiresIn, setExpiresIn] = useState('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revokingId, setRevokingId] = useState('');
  const [loadError, setLoadError] = useState('');
  const [tokenError, setTokenError] = useState('');
  const requestVersion = useRef(0);

  const username = authStore.username;
  const isOwner = project?.owner === username || authStore.role === 'admin';
  const canManage = isOwner || project?.members?.[username] === 'editor';

  const load = async (mode = 'refresh') => {
    const version = ++requestVersion.current;
    if (mode === 'initial') setLoading(true);
    else setRefreshing(true);
    try {
      const results = await Promise.allSettled([
        api.getModelingMcp(project.id),
        ...(canManage ? [api.getModelingMcpTokens(project.id)] : []),
      ]);
      if (results[0].status === 'rejected') throw results[0].reason;
      if (version !== requestVersion.current) return;
      setStatus(results[0].value);
      setLoadError('');
      if (canManage && results[1]) {
        if (results[1].status === 'fulfilled') {
          setTokens(results[1].value.tokens || []);
          setTokenError('');
        } else {
          setTokens([]);
          setTokenError(results[1].reason?.message || '当前账号无权管理 MCP Token');
        }
      } else {
        setTokens([]);
        setTokenError('当前角色仅可查看 MCP 运行状态');
      }
    } catch (error) {
      if (version !== requestVersion.current) return;
      setLoadError(error.message || 'MCP 配置加载失败');
    } finally {
      if (version === requestVersion.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  };

  useEffect(() => {
    requestVersion.current += 1;
    setStatus(null);
    setTokens([]);
    setFreshToken(null);
    setTokenName('');
    setScopes(['ontology.read']);
    setExpiresIn('');
    setLoadError('');
    setTokenError('');
    void load('initial');
  }, [project.id, canManage]);

  const runtime = status?.runtime || null;
  const clientConfigs = buildClientConfigs(
    runtime,
    freshToken?.token || '<MCP_TOKEN>',
    freshToken ? freshToken.token_summary?.scopes || [] : undefined,
  );

  const createToken = async () => {
    if (!tokenName.trim()) return message.warning('Token 名称必填');
    if (!scopes.length) return message.warning('至少选择一个 scope');
    setCreating(true);
    try {
      const body = { name: tokenName.trim(), scopes };
      if (expiresIn === 'never') body.expires_in_seconds = null;
      else if (expiresIn) body.expires_in_seconds = Number(expiresIn);
      const data = await api.createModelingMcpToken(project.id, body);
      setFreshToken(data);
      setTokenName('');
      setTokens((current) => [data.token_summary, ...current.filter((item) => item.id !== data.token_summary.id)]);
      message.success('Token 已创建，明文只会展示这一次');
    } catch (error) {
      message.error(error.message);
    } finally {
      setCreating(false);
    }
  };

  if (loading && !runtime) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="MCP 配置加载中…" />;
  if (!runtime) {
    return (
      <Alert
        type="error"
        showIcon
        message="MCP 配置加载失败"
        description={loadError || '未知错误'}
        action={<Button size="small" onClick={() => void load('initial')}>重试</Button>}
      />
    );
  }
  if (runtime.configured === false) {
    return <Alert type="warning" showIcon message={runtime.detail || '当前项目尚未关联 Modeling 项目'} />;
  }

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} wrap>
        <Space size={[6, 6]} wrap>
          <Tag color={runtime.running ? 'success' : 'warning'}>{runtime.running ? '运行中' : '未运行'}</Tag>
          <Tag>{runtime.transport || 'streamable-http'}</Tag>
          <Tag>{runtime.server_name}@{runtime.server_version}</Tag>
          {(runtime.protocol_versions || []).map((version) => <Tag key={version}>{version}</Tag>)}
        </Space>
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void load()}>
          刷新
        </Button>
      </Space>

      {loadError && <Alert type="warning" showIcon message={`最近一次刷新失败：${loadError}`} />}
      {status?.package?.synced === false && <Alert type="warning" showIcon message="知识包尚未同步到 MCP" description={status.package.detail} />}
      {!runtime.running && <Alert type="error" showIcon message={runtime.detail || 'MCP 服务当前不可用'} />}
      {isLoopbackUrl(runtime.base_url) && (
        <Alert type="warning" showIcon message="当前 MCP 地址仅限本机访问" description="请将 MODELING_MCP_PUBLIC_BASE_URL 配置为其他机器可访问的 HTTPS 域名或 IP。" />
      )}

      <div>
        <Typography.Text strong>endpoint ID</Typography.Text>
        <Input.Group compact style={{ marginTop: 8 }}>
          <Input value={runtime.endpoint_id || ''} readOnly style={{ width: 'calc(100% - 88px)' }} />
          <Button icon={<CopyOutlined />} onClick={() => copyText(runtime.endpoint_id, 'endpoint ID')}>复制</Button>
        </Input.Group>
      </div>

      {(runtime.urls || []).map((item) => {
        const toolset = (runtime.toolsets || []).find((candidate) => candidate.name === item.toolset);
        return (
          <div className="mcp-toolset-card" key={`${item.toolset}:${item.transport}`}>
            <Space size={6} wrap><Typography.Text strong>{item.toolset}</Typography.Text><Tag>{item.transport}</Tag></Space>
            <Input.Group compact style={{ marginTop: 8 }}>
              <Input value={item.url} readOnly style={{ width: 'calc(100% - 88px)' }} />
              <Button icon={<CopyOutlined />} onClick={() => copyText(item.url, `${item.toolset} URL`)}>复制</Button>
            </Input.Group>
            <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>
              工具：{toolset?.tools?.length ? toolset.tools.join('、') : '当前没有可用工具'}
            </Typography.Paragraph>
            <Typography.Paragraph type="secondary" style={{ margin: 0 }}>scope：{toolsetScopes(toolset)}</Typography.Paragraph>
          </div>
        );
      })}

      <Collapse size="small" items={[{
        key: 'scopes',
        label: '支持的权限范围',
        children: (runtime.scope_catalog || []).map((item) => (
          <div key={item.scope} className="mcp-tool-line"><Typography.Text code>{item.scope}</Typography.Text>{' '}<Typography.Text type="secondary">{item.description}</Typography.Text></div>
        )),
      }]} />

      {tokenError && <Alert type="info" showIcon message={tokenError} />}
      {canManage && !tokenError && (
        <div className="mcp-toolset-card">
          <Typography.Title level={5} style={{ marginTop: 0 }}>新建 Token</Typography.Title>
          <Typography.Paragraph type="secondary">默认过期 {runtime.token_policy?.default_ttl_seconds ?? 86400} 秒，限时 Token 最大 {runtime.token_policy?.max_ttl_seconds ?? 2592000} 秒；永不过期 Token 仍可随时吊销。</Typography.Paragraph>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Input placeholder="例如：业务 Agent 读写联调" value={tokenName} onChange={(event) => setTokenName(event.target.value)} />
            <Select mode="multiple" value={scopes} onChange={setScopes} options={(runtime.scope_catalog || []).map((item) => ({ value: item.scope, label: item.scope }))} placeholder="选择 Token 权限" style={{ width: '100%' }} />
            <Select value={expiresIn} onChange={setExpiresIn} style={{ width: '100%' }} options={[
              { value: '', label: `使用默认值（${runtime.token_policy?.default_ttl_seconds ?? 86400} 秒）` },
              { value: '86400', label: '有效期：1 天' },
              { value: '2592000', label: '有效期：30 天' },
              ...(runtime.token_policy?.supports_non_expiring === false ? [] : [{ value: 'never', label: '永不过期' }]),
            ]} />
            <Button type="primary" icon={<PlusOutlined />} loading={creating} disabled={!tokenName.trim() || !scopes.length} onClick={() => void createToken()}>创建 Token</Button>
          </Space>
        </div>
      )}
      {freshToken && (
        <Alert
          type="success"
          showIcon
          closable
          onClose={() => setFreshToken(null)}
          message="刚创建的 Token"
          description={<Space direction="vertical" style={{ width: '100%' }}><Typography.Text type="secondary">明文只回显一次，关闭后无法再次查看。</Typography.Text><Input.Group compact><Input value={freshToken.token} readOnly style={{ width: 'calc(100% - 88px)' }} /><Button icon={<CopyOutlined />} onClick={() => copyText(freshToken.token, 'Token')}>复制</Button></Input.Group><Typography.Text type="secondary">Token ID：{freshToken.token_summary?.id}</Typography.Text></Space>}
        />
      )}
      {(clientConfigs.read || clientConfigs.change) && (
        <div className="mcp-toolset-card">
          <Typography.Title level={5} style={{ marginTop: 0 }}>客户端 JSON 配置</Typography.Title>
          <Typography.Paragraph type="secondary">{freshToken ? '已按本次 Token 的 scope 生成可用配置；明文仅在当前页面会话中保留。' : 'Token 使用 <MCP_TOKEN> 占位符，请替换为具备对应 scope 的 Token。'}</Typography.Paragraph>
          {[['Read 配置', clientConfigs.read], ['Change 配置', clientConfigs.change], ['完整配置', clientConfigs.combined]].map(([title, value]) => value && <div key={title} style={{ marginBottom: 16 }}><Typography.Text strong>{title}</Typography.Text><pre className="mcp-client-config">{value}</pre><Button size="small" icon={<CopyOutlined />} onClick={() => copyText(value, title)}>复制{title}</Button></div>)}
        </div>
      )}
      {canManage && !tokenError && <div><Typography.Title level={5}>现有 Token</Typography.Title><Typography.Paragraph type="secondary">仅展示未吊销 Token；调用成功后会更新最近使用时间。</Typography.Paragraph><Table size="small" rowKey="id" pagination={false} dataSource={tokens} locale={{ emptyText: '当前还没有可用 Token' }} columns={[
        { title: '用途', dataIndex: 'name', render: (value, row) => <Space direction="vertical" size={0}><Typography.Text strong>{value}</Typography.Text><Typography.Text type="secondary">ID：{row.id}</Typography.Text><Space size={[4, 4]} wrap>{(row.scopes || []).map((scope) => <Tag key={scope}>{scope}</Tag>)}</Space></Space> },
        { title: '签发信息', responsive: ['md'], render: (_, row) => <Space direction="vertical" size={0}><Typography.Text type="secondary">创建人：{row.subjectUsername || '-'}</Typography.Text><Typography.Text type="secondary">签发人：{row.issuedBy || '-'}</Typography.Text></Space> },
        { title: '生命周期', responsive: ['lg'], render: (_, row) => <Space direction="vertical" size={0}><Typography.Text type="secondary">创建：{formatMcpTime(row.createdAt)}</Typography.Text><Typography.Text type="secondary">过期：{formatMcpTime(row.expiresAt)}</Typography.Text><Typography.Text type="secondary">最近使用：{row.lastUsedAt ? formatMcpTime(row.lastUsedAt) : '尚未使用'}</Typography.Text></Space> },
        { title: '操作', width: 76, render: (_, row) => <Button size="small" danger type="text" icon={<DeleteOutlined />} loading={revokingId === row.id} onClick={async () => { if (!window.confirm(`确认吊销 Token「${row.name}」？`)) return; setRevokingId(row.id); try { await api.revokeModelingMcpToken(project.id, row.id); await load(); message.success('Token 已吊销'); } catch (error) { message.error(error.message); } finally { setRevokingId(''); } }}>吊销</Button> },
      ]} /></div>}
      <Alert type="info" showIcon message="外部 Agent 推荐连接顺序" description="server/discover → tools/list → tools/call。旧客户端如果仍走 initialize，当前服务也兼容。" />
    </Space>
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
        Modeling MCP 服务
      </Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        当前项目复用 Modeling 的 5188 MCP 服务，Read 和 Change 地址、Token 及权限由 Modeling 统一管理。
      </Typography.Paragraph>
      <ExposedService project={project} />
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
