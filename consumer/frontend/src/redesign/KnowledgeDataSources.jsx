import { useCallback, useEffect, useMemo, useState } from 'react';

import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import { EmptyState, FieldRow, SectionCard, StatusTag } from './ui';

const directPayload = (provider, config) => ({
  mode: 'direct_provider',
  provider,
  config,
});

export default function KnowledgeDataSources({ ontologyId }) {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.kb.write');
  const [connections, setConnections] = useState([]);
  const [providers, setProviders] = useState([]);
  const [mode, setMode] = useState('connection_ref');
  const [connectionId, setConnectionId] = useState('');
  const [provider, setProvider] = useState('');
  const [config, setConfig] = useState({});
  const [configured, setConfigured] = useState(false);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');

  const selectedProvider = useMemo(
    () => providers.find((item) => item.name === provider) || null,
    [provider, providers],
  );

  const load = useCallback(async () => {
    if (!ontologyId) return;
    const [connectionsResponse, providersResponse, sourceResponse] = await Promise.all([
      authFetch('/agentic/data-connections'),
      authFetch('/agentic/business-data/provider-schemas'),
      authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`),
    ]);
    const connectionsPayload = await connectionsResponse.json().catch(() => ({}));
    const providersPayload = await providersResponse.json().catch(() => ({}));
    const sourcePayload = await sourceResponse.json().catch(() => ({}));
    const nextProviders = providersPayload.providers || [];
    const source = sourcePayload.source || null;
    setConnections(connectionsPayload.connections || []);
    setProviders(nextProviders);
    setConfigured(Boolean(sourcePayload.configured));
    setMode(source?.mode || 'connection_ref');
    setConnectionId(source?.connection_id || connectionsPayload.default_connection_id || '');
    setProvider(source?.provider || nextProviders[0]?.name || '');
    setConfig(source?.config || {});
  }, [ontologyId]);

  useEffect(() => {
    const timer = setTimeout(load, 0);
    return () => clearTimeout(timer);
  }, [load, workspaceId]);

  const chooseProvider = (nextProvider) => {
    setProvider(nextProvider);
    const schema = providers.find((item) => item.name === nextProvider);
    const defaults = {};
    (schema?.fields || []).forEach((field) => {
      if (field.default !== undefined && field.default !== null) defaults[field.name] = field.default;
    });
    setConfig(defaults);
  };

  const buildPayload = () => {
    if (mode === 'connection_ref') return { mode: 'connection_ref', connection_id: connectionId };
    const providerConfig = {};
    (selectedProvider?.fields || []).forEach((field) => {
      const value = config[field.name];
      // GET 结果中的 has_* 仅用于展示；空密钥不回传，让服务端保留已有值。
      if (field.secret && (value === '' || value === null || value === undefined)) return;
      if (value !== undefined) providerConfig[field.name] = value;
    });
    return directPayload(provider, providerConfig);
  };

  const submit = async (action) => {
    setBusy(action);
    setNotice('');
    try {
      const payload = buildPayload();
      const suffix = action === 'test' ? '/test' : '';
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source${suffix}`,
        {
          method: action === 'test' ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        },
      );
      const data = await response.json().catch(() => ({}));
      setNotice(response.ok
        ? (action === 'test' ? `${data.success ? '连接成功' : '连接失败'}${data.message ? `：${data.message}` : ''}` : '数据源配置已保存')
        : `操作失败：${data.detail || data.error || response.status}`);
      if (response.ok && action !== 'test') await load();
    } finally {
      setBusy('');
    }
  };

  const remove = async () => {
    setBusy('delete');
    const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`, { method: 'DELETE' });
    setNotice(response.ok ? '已解除数据源配置' : '解除失败');
    if (response.ok) await load();
    setBusy('');
  };

  if (!ontologyId) return <EmptyState title="请先选择知识包" />;

  return (
    <div className="kb-tab-body">
      <SectionCard title="数据源模式" description="共享连接适合统一运维；直连配置仅绑定当前知识包。"
        actions={<StatusTag status={configured ? 'success' : 'warning'}>{configured ? '已配置' : '未配置'}</StatusTag>}>
        {!canEdit && <div className="ro-banner">🔒 当前账号仅可查看，修改由平台管理员执行。</div>}
        <div className="data-mode-cards">
          <button type="button" disabled={!canEdit} className={mode === 'connection_ref' ? 'on' : ''} onClick={() => setMode('connection_ref')}>
            <strong>共享连接</strong><span>引用工作空间统一管理的连接，密钥只维护一次。</span>
          </button>
          <button type="button" disabled={!canEdit} className={mode === 'direct_provider' ? 'on' : ''} onClick={() => setMode('direct_provider')}>
            <strong>知识包直连</strong><span>为当前知识包单独配置 Provider 与连接参数。</span>
          </button>
        </div>
      </SectionCard>

      <SectionCard title={mode === 'connection_ref' ? '选择共享连接' : '直连参数'}>
        {mode === 'connection_ref' ? (
          connections.length ? (
            <FieldRow label="工作空间连接">
              <select disabled={!canEdit} value={connectionId} onChange={(event) => setConnectionId(event.target.value)}>
                <option value="">请选择连接</option>
                {connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.name}（{connection.provider}）</option>)}
              </select>
            </FieldRow>
          ) : <EmptyState compact title="暂无共享连接" description="请先到 L2 配置中心创建数据连接。" />
        ) : (
          <>
            <FieldRow label="Provider" hint={selectedProvider?.description || ''}>
              <select disabled={!canEdit} value={provider} onChange={(event) => chooseProvider(event.target.value)}>
                {providers.map((item) => <option key={item.name} value={item.name}>{item.label || item.name}</option>)}
              </select>
            </FieldRow>
            {(selectedProvider?.fields || []).map((field) => (
              <FieldRow key={field.name} label={`${field.label || field.name}${field.required ? ' *' : ''}`}
                hint={field.secret && config[`has_${field.name}`] ? '已配置，留空保留原值' : ''}>
                {field.type === 'bool' ? (
                  <input type="checkbox" disabled={!canEdit} checked={Boolean(config[field.name])}
                    onChange={(event) => setConfig((current) => ({ ...current, [field.name]: event.target.checked }))} />
                ) : (
                  <input type={field.secret ? 'password' : field.type === 'int' ? 'number' : 'text'} disabled={!canEdit}
                    value={field.secret ? (config[field.name] || '') : (config[field.name] ?? '')}
                    placeholder={field.secret && config[`has_${field.name}`] ? '••••••（留空保留）' : ''}
                    onChange={(event) => setConfig((current) => ({
                      ...current,
                      [field.name]: field.type === 'int' ? Number(event.target.value) : event.target.value,
                    }))} />
                )}
              </FieldRow>
            ))}
          </>
        )}
        {notice && <div className="kb-opnotice">{notice}</div>}
        {canEdit && (
          <div className="foot">
            {configured && <button className="btn btn-danger" disabled={Boolean(busy)} onClick={remove}>解除配置</button>}
            <button className="btn" disabled={Boolean(busy) || (mode === 'connection_ref' ? !connectionId : !provider)} onClick={() => submit('test')}>{busy === 'test' ? '测试中…' : '测试连接'}</button>
            <button className="btn btn-p" disabled={Boolean(busy) || (mode === 'connection_ref' ? !connectionId : !provider)} onClick={() => submit('save')}>{busy === 'save' ? '保存中…' : '保存配置'}</button>
          </div>
        )}
      </SectionCard>
    </div>
  );
}
