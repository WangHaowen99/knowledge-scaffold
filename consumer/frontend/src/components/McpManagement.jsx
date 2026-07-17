import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, Copy, Info, KeyRound, Loader2, Plus, RefreshCw, Server, Trash2, X } from 'lucide-react';

import { authFetch } from '../shared/api/auth';
import { friendlyRequestError } from '../utils/requestErrors';

const DEFAULT_SCOPES = ['mcp:search', 'mcp:chat'];

function formatDate(value) {
  if (!value) return '永不过期';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

async function copyText(value) {
  const text = String(value || '');
  if (!text) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'readonly');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  document.body.removeChild(textarea);
}

function getClientConfigType(service) {
  if (service?.client_config_type) return service.client_config_type;
  return service?.transport === 'sse' ? 'sse' : 'http';
}

function getActiveUrl(service) {
  return service?.active_url || service?.url || '';
}

function formatTransportMode(value) {
  const transport = String(value || '').trim().toLowerCase();
  if (transport === 'sse') return 'SSE';
  if (transport === 'streamable-http' || transport === 'http') return 'Streamable HTTP';
  if (transport === 'stdio') return 'stdio';
  return transport || '-';
}

function buildClientConfig(service, tokenPlaceholder = '<MCP_TOKEN>') {
  return JSON.stringify({
    mcpServers: {
      [service?.id || 'meta-wise-mcp']: {
        type: getClientConfigType(service),
        url: getActiveUrl(service),
        headers: {
          Authorization: `Bearer ${tokenPlaceholder}`,
        },
      },
    },
  }, null, 2);
}

function McpManagement({
  currentWorkspaceId,
  currentWorkspaceName,
  canEdit = false,
}) {
  const [status, setStatus] = useState(null);
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busyTokenId, setBusyTokenId] = useState('');
  const [message, setMessage] = useState({ type: '', text: '' });
  const [createdToken, setCreatedToken] = useState('');
  const [copiedKey, setCopiedKey] = useState('');
  const [selectedServiceId, setSelectedServiceId] = useState('');
  const [form, setForm] = useState({
    name: '',
    expires_in: '86400',
    scopes: DEFAULT_SCOPES,
  });

  const services = Array.isArray(status?.services) ? status.services : [];
  const searchService = services.find((item) => item.toolset === 'search') || services[0] || null;
  const chatService = services.find((item) => item.toolset === 'chat') || null;
  const selectedService = services.find((item) => item.id === selectedServiceId) || null;
  const clientConfigToken = createdToken || '<MCP_TOKEN>';
  const isMcpRunning = Boolean(status?.running);
  const transportMode = status?.transport || searchService?.transport || chatService?.transport || '';
  const activeTokenCount = useMemo(
    () => tokens.filter((token) => token.status === 'active').length,
    [tokens],
  );

  const loadData = useCallback(async ({ clearMessage = false } = {}) => {
    if (!currentWorkspaceId) return;
    setLoading(true);
    if (clearMessage) setMessage({ type: '', text: '' });
    try {
      const [statusResponse, tokensResponse] = await Promise.all([
        authFetch('/agentic/mcp/runtime/status'),
        authFetch('/agentic/mcp/tokens'),
      ]);
      const statusPayload = await statusResponse.json().catch(() => ({}));
      const tokensPayload = await tokensResponse.json().catch(() => ({}));
      if (!statusResponse.ok) throw new Error(statusPayload?.detail || statusPayload?.error || '加载 MCP 服务状态失败');
      if (!tokensResponse.ok) throw new Error(tokensPayload?.detail || tokensPayload?.error || '加载 MCP Token 失败');
      setStatus(statusPayload);
      setTokens(Array.isArray(tokensPayload.items) ? tokensPayload.items : []);
    } catch (error) {
      setMessage({ type: 'error', text: friendlyRequestError(error, '加载 MCP 配置失败') });
    } finally {
      setLoading(false);
    }
  }, [currentWorkspaceId]);

  useEffect(() => {
    const timer = window.setTimeout(() => loadData({ clearMessage: true }), 0);
    return () => window.clearTimeout(timer);
  }, [loadData]);

  useEffect(() => {
    if (!copiedKey) return undefined;
    const timer = window.setTimeout(() => setCopiedKey(''), 1600);
    return () => window.clearTimeout(timer);
  }, [copiedKey]);

  const handleScopeToggle = (scope) => {
    setForm((current) => {
      const hasScope = current.scopes.includes(scope);
      const nextScopes = hasScope
        ? current.scopes.filter((item) => item !== scope)
        : [...current.scopes, scope];
      return { ...current, scopes: nextScopes };
    });
  };

  const handleCreate = async () => {
    if (!canEdit) return;
    if (form.scopes.length === 0) {
      setMessage({ type: 'error', text: '至少选择一个权限' });
      return;
    }
    setCreating(true);
    setMessage({ type: '', text: '' });
    try {
      const expiresIn = form.expires_in === 'never' ? null : Number(form.expires_in || 86400);
      const response = await authFetch('/agentic/mcp/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name,
          expires_in: expiresIn,
          scopes: form.scopes,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || payload?.error || '创建 MCP Token 失败');
      setCreatedToken(payload.token || '');
      setMessage({ type: 'success', text: 'Token 已创建，请立即复制明文或客户端配置' });
      setForm({ name: '', expires_in: '86400', scopes: DEFAULT_SCOPES });
      await loadData();
    } catch (error) {
      setMessage({ type: 'error', text: friendlyRequestError(error, '创建 MCP Token 失败') });
    } finally {
      setCreating(false);
    }
  };

  const handleRevoke = async (tokenId) => {
    if (!canEdit || !tokenId) return;
    setBusyTokenId(tokenId);
    setMessage({ type: '', text: '' });
    try {
      const response = await authFetch(`/agentic/mcp/tokens/${encodeURIComponent(tokenId)}`, {
        method: 'DELETE',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || payload?.error || '吊销 Token 失败');
      setCreatedToken('');
      setMessage({ type: 'success', text: 'Token 已吊销' });
      await loadData();
    } catch (error) {
      setMessage({ type: 'error', text: friendlyRequestError(error, '吊销 Token 失败') });
    } finally {
      setBusyTokenId('');
    }
  };

  const handleCopy = async (key, text) => {
    await copyText(text);
    setCopiedKey(key);
  };

  return (
    <div className="mcp-management">
      <div className="card">
        <div className="mcp-card-head">
          <div>
            <h3>MCP 服务</h3>
            <div className="cd">
              {currentWorkspaceName || currentWorkspaceId || '当前工作空间'} 下，当前账号可用的 MCP endpoint、Token 和客户端配置。
            </div>
          </div>
          <button
            className="btn"
            type="button"
            onClick={() => loadData({ clearMessage: true })}
            disabled={loading || !currentWorkspaceId}
          >
            {loading ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />}
            刷新
          </button>
        </div>

        {!canEdit && <div className="mcp-notice">当前账号无法管理 MCP Token。</div>}
        {message.text && <div className={`mcp-notice ${message.type}`}>{message.text}</div>}
        {status && !isMcpRunning && (
          <div className="mcp-notice warning">MCP Server 当前离线；Token 可生成和吊销，但客户端暂时无法连接 endpoint。</div>
        )}

        <div className="mcp-metrics">
          <Metric label="服务状态">
            <span className={`mcp-runtime ${isMcpRunning ? 'online' : 'offline'}`}>
              <i aria-hidden="true" />{isMcpRunning ? '在线' : '离线'}
            </span>
          </Metric>
          <Metric label="Public Base URL">{status?.public_base_url || '-'}</Metric>
          <Metric label="Transport 模式">{formatTransportMode(transportMode)}</Metric>
          <Metric label="工作空间">{currentWorkspaceName || currentWorkspaceId || '-'}</Metric>
          <Metric label="Active Tokens">{activeTokenCount}</Metric>
        </div>
      </div>

      <div className="mcp-service-grid">
        {services.map((service) => (
          <div className="card mcp-service-card" key={service.id}>
            <div className="mcp-service-head">
              <span className="mcp-service-icon"><Server size={16} /></span>
              <div>
                <h3>{service.title}</h3>
                <div className="cd">{service.summary}</div>
              </div>
              <span className={`tag ${isMcpRunning ? 'tag-ok' : 'tag-off'}`}>{isMcpRunning ? '在线' : '离线'}</span>
            </div>
            <div className="mcp-endpoint">
              <span>Endpoint</span>
              <code>{getActiveUrl(service) || '-'}</code>
              <button
                className="iconbtn"
                type="button"
                onClick={() => handleCopy(`endpoint-${service.id}`, getActiveUrl(service))}
                title="复制 endpoint"
                aria-label="复制 endpoint"
                disabled={!getActiveUrl(service)}
              >
                {copiedKey === `endpoint-${service.id}` ? <Check size={15} /> : <Copy size={15} />}
              </button>
            </div>
            <button className="btn mcp-inline-btn" type="button" onClick={() => setSelectedServiceId(service.id)}>
              <Info size={15} />查看说明
            </button>
          </div>
        ))}
        {!services.length && <div className="card"><div className="empty">暂无 MCP 服务目录</div></div>}
      </div>

      <div className="card">
        <h3>创建 Token</h3>
        <div className="cd">Token 绑定当前账号与当前工作空间；客户端调用工具时仍需显式传入 ontology_id。</div>
        <div className="fieldrow">
          <div className="fl"><div className="n">名称</div><div className="h">便于区分客户端用途</div></div>
          <input
            type="text"
            value={form.name}
            onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
            disabled={!canEdit || creating}
            placeholder="本地 IDE 接入"
          />
        </div>
        <div className="fieldrow">
          <div className="fl"><div className="n">有效期</div></div>
          <select
            value={form.expires_in}
            onChange={(event) => setForm((current) => ({ ...current, expires_in: event.target.value }))}
            disabled={!canEdit || creating}
          >
            <option value="3600">1 小时</option>
            <option value="86400">1 天</option>
            <option value="604800">7 天</option>
            <option value="2592000">30 天</option>
            <option value="never">永不过期</option>
          </select>
        </div>
        <div className="fieldrow">
          <div className="fl"><div className="n">权限</div><div className="h">最小化选择客户端需要的能力</div></div>
          <div className="mcp-scope-row">
            {DEFAULT_SCOPES.map((scope) => (
              <label className="mcp-check" key={scope}>
                <input
                  type="checkbox"
                  checked={form.scopes.includes(scope)}
                  onChange={() => handleScopeToggle(scope)}
                  disabled={!canEdit || creating}
                />
                <span>{scope}</span>
              </label>
            ))}
          </div>
        </div>
        {createdToken && (
          <div className="mcp-created-token">
            <div><KeyRound size={16} />Token 明文只在本次生成后展示</div>
            <code>{createdToken}</code>
            <button className="btn" type="button" onClick={() => handleCopy('created-token', createdToken)}>
              {copiedKey === 'created-token' ? <Check size={15} /> : <Copy size={15} />}复制
            </button>
          </div>
        )}
        <div className="foot">
          <button
            className="btn btn-p"
            type="button"
            onClick={handleCreate}
            disabled={!canEdit || creating || !currentWorkspaceId}
          >
            {creating ? <Loader2 size={15} className="spin" /> : <Plus size={15} />}
            生成 Token
          </button>
        </div>
      </div>

      <div className="card">
        <h3>Token 列表</h3>
        <div className="cd">仅显示当前账号在当前工作空间下创建的 Token。</div>
        <table className="dt">
          <thead>
            <tr><th>名称</th><th>Token</th><th>权限</th><th>状态</th><th>到期</th><th style={{ width: 100 }}>操作</th></tr>
          </thead>
          <tbody>
            {tokens.map((token) => (
              <tr key={token.token_id}>
                <td style={{ fontWeight: 600 }}>{token.name || token.token_id}</td>
                <td style={{ color: 'var(--ink2)', overflowWrap: 'anywhere' }}>{token.token_masked || '-'}</td>
                <td>{(token.scopes || []).map((scope) => <span className="tag tag-lang mcp-scope-tag" key={scope}>{scope}</span>)}</td>
                <td><span className={`tag ${token.status === 'active' ? 'tag-ok' : 'tag-off'}`}>{token.status}</span></td>
                <td style={{ color: 'var(--ink2)' }}>{formatDate(token.expires_at)}</td>
                <td>
                  <button
                    className="btn btn-danger"
                    style={{ padding: '3px 10px', fontSize: 12 }}
                    type="button"
                    disabled={!canEdit || token.status !== 'active' || busyTokenId === token.token_id}
                    onClick={() => handleRevoke(token.token_id)}
                  >
                    {busyTokenId === token.token_id ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />}
                    吊销
                  </button>
                </td>
              </tr>
            ))}
            {!tokens.length && <tr><td colSpan={6} className="empty">暂无 MCP Token</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h3>客户端配置</h3>
        <div className="cd">生成 Token 后，下方配置仅在本次页面会话中自动填入明文；刷新后请手动替换 &lt;MCP_TOKEN&gt;。</div>
        <div className="mcp-config-grid">
          {[searchService, chatService].filter(Boolean).map((service) => (
            <div className="mcp-config-block" key={service.id}>
              <div className="mcp-config-head">
                <div>
                  <strong>{service.title}</strong>
                  <span>{getActiveUrl(service) || '-'}</span>
                </div>
                <button
                  className="iconbtn"
                  type="button"
                  onClick={() => handleCopy(`config-${service.id}`, buildClientConfig(service, clientConfigToken))}
                  title="复制客户端配置"
                  aria-label="复制客户端配置"
                >
                  {copiedKey === `config-${service.id}` ? <Check size={15} /> : <Copy size={15} />}
                </button>
              </div>
              <pre>{buildClientConfig(service, clientConfigToken)}</pre>
            </div>
          ))}
          {!searchService && !chatService && <div className="empty">暂无可复制的客户端配置</div>}
        </div>
      </div>

      {selectedService && (
        <div className="mcp-modal-backdrop" role="presentation" onClick={(event) => { if (event.target === event.currentTarget) setSelectedServiceId(''); }}>
          <div className="mcp-modal" role="dialog" aria-modal="true" aria-labelledby="mcp-desc-title">
            <div className="mcp-modal-head">
              <div>
                <h3 id="mcp-desc-title">{selectedService.title}</h3>
                <p>{selectedService.description || selectedService.summary}</p>
              </div>
              <button className="iconbtn" type="button" onClick={() => setSelectedServiceId('')} aria-label="关闭">
                <X size={17} />
              </button>
            </div>
            <div className="mcp-tool-list">
              {(selectedService.tools || []).map((tool) => (
                <div className="mcp-tool-item" key={tool.name}>
                  <strong>{tool.name}</strong>
                  <p>{tool.description}</p>
                </div>
              ))}
              {!(selectedService.tools || []).length && <div className="empty">暂无工具说明</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Metric({ label, children }) {
  return (
    <div className="mcp-metric">
      <span>{label}</span>
      <strong>{children}</strong>
    </div>
  );
}

export default McpManagement;
