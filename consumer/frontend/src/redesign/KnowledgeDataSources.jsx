import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  DATA_SOURCE_MODE,
  buildDataSourcePayload,
  dataSourceScopeKey,
  isCurrentDataSourceRequest,
  normalizeDataSourceDraft,
  providerDefaultConfig,
  validateDataSourceDraft,
} from '../features/knowledge/import/dataSourceDraft';
import { authFetch } from '../shared/api/auth';
import { friendlyRequestError } from '../utils/requestErrors';
import { useAuthContext } from './useAuthContext';
import { EmptyState, FieldRow, SectionCard, StatusTag } from './ui';

export default function KnowledgeDataSources({ ontologyId }) {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.kb.write');
  const [connections, setConnections] = useState([]);
  const [providers, setProviders] = useState([]);
  const [mode, setMode] = useState(DATA_SOURCE_MODE.CONNECTION_REF);
  const [connectionId, setConnectionId] = useState('');
  const [provider, setProvider] = useState('');
  const [config, setConfig] = useState({});
  const [configured, setConfigured] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const requestSequenceRef = useRef(0);
  const loadAbortRef = useRef(null);
  const operationAbortRef = useRef(null);
  const persistedDirectRef = useRef({ provider: '', config: {} });
  const scopeKey = dataSourceScopeKey(workspaceId, ontologyId);
  const currentRequestRef = useRef({ scopeKey, sequence: 0 });
  currentRequestRef.current = { scopeKey, sequence: requestSequenceRef.current };

  const selectedProvider = useMemo(
    () => providers.find((item) => item.name === provider) || null,
    [provider, providers],
  );

  const load = useCallback(async () => {
    if (!ontologyId) return;
    loadAbortRef.current?.abort();
    const controller = new AbortController();
    loadAbortRef.current = controller;
    const request = { scopeKey, sequence: ++requestSequenceRef.current };
    currentRequestRef.current = request;
    const isCurrent = () => (
      !controller.signal.aborted
      && isCurrentDataSourceRequest(request, currentRequestRef.current)
    );
    setLoading(true);
    setLoadError('');
    try {
      const responses = await Promise.all([
        authFetch('/agentic/data-connections', { signal: controller.signal }),
        authFetch('/agentic/business-data/provider-schemas', { signal: controller.signal }),
        authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`, { signal: controller.signal }),
      ]);
      const payloads = await Promise.all(responses.map((response) => response.json().catch(() => ({}))));
      if (!isCurrent()) return;
      const failedIndex = responses.findIndex((response) => !response.ok);
      if (failedIndex >= 0) {
        const failedResponse = responses[failedIndex];
        const failedPayload = payloads[failedIndex];
        throw new Error(failedPayload.detail || failedPayload.error || `数据源配置加载失败（${failedResponse.status}）`);
      }
      const draft = normalizeDataSourceDraft({
        connectionsPayload: payloads[0],
        providersPayload: payloads[1],
        sourcePayload: payloads[2],
      });
      setConnections(draft.connections);
      setProviders(draft.providers);
      setConfigured(draft.configured);
      setMode(draft.mode);
      setConnectionId(draft.connectionId);
      setProvider(draft.provider);
      setConfig(draft.config);
      persistedDirectRef.current = draft.mode === DATA_SOURCE_MODE.DIRECT_PROVIDER
        ? { provider: draft.provider, config: draft.config }
        : { provider: '', config: {} };
    } catch (error) {
      if (!isCurrent() || error?.name === 'AbortError') return;
      setLoadError(friendlyRequestError(error, '数据源配置加载失败'));
    } finally {
      if (isCurrent()) setLoading(false);
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
    }
  }, [ontologyId, scopeKey]);

  useEffect(() => {
    loadAbortRef.current?.abort();
    operationAbortRef.current?.abort();
    requestSequenceRef.current += 1;
    currentRequestRef.current = { scopeKey, sequence: requestSequenceRef.current };
    setConnections([]);
    setProviders([]);
    setMode(DATA_SOURCE_MODE.CONNECTION_REF);
    setConnectionId('');
    setProvider('');
    setConfig({});
    persistedDirectRef.current = { provider: '', config: {} };
    setConfigured(false);
    setBusy('');
    setNotice('');
    setLoadError('');
    const timer = setTimeout(load, 0);
    return () => {
      clearTimeout(timer);
      loadAbortRef.current?.abort();
      operationAbortRef.current?.abort();
      requestSequenceRef.current += 1;
    };
  }, [load, scopeKey]);

  const chooseProvider = (nextProvider) => {
    setProvider(nextProvider);
    setConfig(nextProvider === persistedDirectRef.current.provider
      ? { ...persistedDirectRef.current.config }
      : providerDefaultConfig(providers.find((item) => item.name === nextProvider)));
    setNotice('');
  };

  const draft = useMemo(() => ({
    mode,
    connectionId,
    provider,
    config,
    schema: selectedProvider,
  }), [config, connectionId, mode, provider, selectedProvider]);
  const validationError = validateDataSourceDraft(draft);

  const submit = async (action) => {
    if (validationError) {
      setNotice(validationError);
      return;
    }
    operationAbortRef.current?.abort();
    const controller = new AbortController();
    operationAbortRef.current = controller;
    const requestScope = scopeKey;
    const isCurrent = () => !controller.signal.aborted && requestScope === currentRequestRef.current.scopeKey;
    setBusy(action);
    setNotice('');
    try {
      const suffix = action === 'test' ? '/test' : '';
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source${suffix}`,
        {
          method: action === 'test' ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildDataSourcePayload(draft)),
          signal: controller.signal,
        },
      );
      const data = await response.json().catch(() => ({}));
      if (!isCurrent()) return;
      if (!response.ok) {
        setNotice(`操作失败：${data.detail || data.error || response.status}`);
        return;
      }
      if (action === 'test') {
        setNotice(`${data.success ? '连接成功' : '连接失败'}${data.message ? `：${data.message}` : ''}`);
      } else {
        setNotice('数据源配置已保存');
        await load();
      }
    } catch (error) {
      if (isCurrent() && error?.name !== 'AbortError') setNotice(friendlyRequestError(error, '数据源操作失败'));
    } finally {
      if (isCurrent()) setBusy('');
      if (operationAbortRef.current === controller) operationAbortRef.current = null;
    }
  };

  const remove = async () => {
    operationAbortRef.current?.abort();
    const controller = new AbortController();
    operationAbortRef.current = controller;
    const requestScope = scopeKey;
    const isCurrent = () => !controller.signal.aborted && requestScope === currentRequestRef.current.scopeKey;
    setBusy('delete');
    setNotice('');
    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`, {
        method: 'DELETE',
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (!isCurrent()) return;
      if (!response.ok) {
        setNotice(`解除失败：${data.detail || data.error || response.status}`);
        return;
      }
      setNotice('已解除数据源配置');
      await load();
    } catch (error) {
      if (isCurrent() && error?.name !== 'AbortError') setNotice(friendlyRequestError(error, '解除数据源失败'));
    } finally {
      if (isCurrent()) setBusy('');
      if (operationAbortRef.current === controller) operationAbortRef.current = null;
    }
  };

  if (!ontologyId) return <EmptyState title="请先选择知识包" />;

  return (
    <div className="kb-tab-body">
      <SectionCard title="数据源模式" description="共享连接适合统一运维；直连配置只绑定当前知识包。"
        actions={(
          <>
            <StatusTag status={configured ? 'success' : 'warning'}>{configured ? '已配置' : '未配置'}</StatusTag>
            <button type="button" className="btn" disabled={loading || Boolean(busy)} onClick={load}>{loading ? '刷新中…' : '刷新'}</button>
          </>
        )}>
        {!canEdit && <div className="ro-banner">🔒 当前账号仅可查看，修改由平台管理员执行。</div>}
        <div className="data-mode-cards">
          <button type="button" disabled={!canEdit || loading} className={mode === DATA_SOURCE_MODE.CONNECTION_REF ? 'on' : ''}
            onClick={() => { setMode(DATA_SOURCE_MODE.CONNECTION_REF); setNotice(''); }}>
            <strong>共享连接</strong><span>引用工作空间统一管理的连接，密钥只维护一次。</span>
          </button>
          <button type="button" disabled={!canEdit || loading} className={mode === DATA_SOURCE_MODE.DIRECT_PROVIDER ? 'on' : ''}
            onClick={() => { setMode(DATA_SOURCE_MODE.DIRECT_PROVIDER); setNotice(''); }}>
            <strong>知识包直连</strong><span>为当前知识包单独配置 Provider 与连接参数。</span>
          </button>
        </div>
      </SectionCard>

      <SectionCard title={mode === DATA_SOURCE_MODE.CONNECTION_REF ? '选择共享连接' : '直连参数'}>
        {loading && <EmptyState compact title="正在加载数据源配置…" />}
        {!loading && loadError && <EmptyState compact title="数据源配置加载失败" description={loadError}
          action={<button className="btn" type="button" onClick={load}>重新加载</button>} />}
        {!loading && !loadError && mode === DATA_SOURCE_MODE.CONNECTION_REF && (
          connections.length ? (
            <FieldRow label="工作空间连接">
              <select disabled={!canEdit} value={connectionId} onChange={(event) => { setConnectionId(event.target.value); setNotice(''); }}>
                <option value="">请选择连接</option>
                {connections.map((connection) => <option key={connection.id} value={connection.id}>{connection.name}（{connection.provider}）</option>)}
              </select>
            </FieldRow>
          ) : <EmptyState compact title="暂无共享连接" description="请先到 L2 配置中心创建数据连接。" />
        )}
        {!loading && !loadError && mode === DATA_SOURCE_MODE.DIRECT_PROVIDER && (
          providers.length ? (
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
                      autoComplete={field.secret ? 'new-password' : undefined}
                      value={config[field.name] ?? ''}
                      placeholder={field.secret && config[`has_${field.name}`] ? '••••••（留空保留）' : ''}
                      onChange={(event) => setConfig((current) => ({ ...current, [field.name]: event.target.value }))} />
                  )}
                </FieldRow>
              ))}
            </>
          ) : <EmptyState compact title="暂无可用 Provider" description="服务端未返回业务数据 Provider 定义。" />
        )}
        {notice && <div className="kb-opnotice" role="status">{notice}</div>}
        {canEdit && !loading && !loadError && (
          <div className="foot">
            {configured && <button className="btn btn-danger" disabled={Boolean(busy)} onClick={remove}>{busy === 'delete' ? '解除中…' : '解除配置'}</button>}
            <button className="btn" disabled={Boolean(busy)} onClick={() => submit('test')}>{busy === 'test' ? '测试中…' : '测试连接'}</button>
            <button className="btn btn-p" disabled={Boolean(busy)} onClick={() => submit('save')}>{busy === 'save' ? '保存中…' : '保存配置'}</button>
          </div>
        )}
      </SectionCard>
    </div>
  );
}
