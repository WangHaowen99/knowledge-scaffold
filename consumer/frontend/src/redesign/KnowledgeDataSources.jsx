import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import { EmptyState, FieldRow, SectionCard, StatusTag } from './ui';

const configDefaults = (provider) => {
  const defaults = {};
  (provider?.fields || []).forEach((field) => {
    if (field.default !== undefined && field.default !== null) defaults[field.name] = field.default;
  });
  return defaults;
};

const sameScope = (left, right) => (
  left.ontologyId === right.ontologyId && left.workspaceId === right.workspaceId
);

export default function KnowledgeDataSources({ ontologyId, onDataSourceChanged }) {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.kb.write');
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState('');
  const [config, setConfig] = useState({});
  const [configured, setConfigured] = useState(false);
  const [loadingProviders, setLoadingProviders] = useState(false);
  const [loadingSource, setLoadingSource] = useState(false);
  const [sourceLoadError, setSourceLoadError] = useState(false);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const loadRequestRef = useRef(0);
  const operationRequestRef = useRef(0);
  const currentScopeRef = useRef({ ontologyId, workspaceId });
  currentScopeRef.current = { ontologyId, workspaceId };

  const selectedProvider = useMemo(
    () => providers.find((item) => item.name === provider) || null,
    [provider, providers],
  );
  const canSubmit = (
    canEdit
    && Boolean(selectedProvider)
    && !loadingProviders
    && !loadingSource
    && !sourceLoadError
  );

  const load = useCallback(async () => {
    const targetScope = { ontologyId, workspaceId };
    const requestId = ++loadRequestRef.current;
    const isCurrentRequest = () => (
      requestId === loadRequestRef.current
      && sameScope(currentScopeRef.current, targetScope)
    );
    if (!ontologyId) {
      if (isCurrentRequest()) {
        setProvider('');
        setConfig({});
        setConfigured(false);
        setSourceLoadError(false);
      }
      return false;
    }
    setNotice('');
    setLoadingProviders(true);
    setLoadingSource(true);
    setConfigured(false);
    setProvider('');
    setConfig({});
    setSourceLoadError(false);

    let nextProviders = [];
    try {
      const providersResponse = await authFetch('/agentic/business-data/provider-schemas');
      const providersPayload = await providersResponse.json().catch(() => ({}));
      if (!isCurrentRequest()) return false;
      if (!providersResponse.ok) throw new Error(providersPayload.detail || '加载 Provider 失败');
      nextProviders = Array.isArray(providersPayload.providers) ? providersPayload.providers : [];
      setProviders(nextProviders);
    } catch (error) {
      if (!isCurrentRequest()) return false;
      setProviders([]);
      setProvider('');
      setConfig({});
      setSourceLoadError(true);
      setLoadingSource(false);
      setNotice(error?.message || '加载 Provider 失败');
      return false;
    } finally {
      if (isCurrentRequest()) setLoadingProviders(false);
    }

    try {
      const sourceResponse = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`);
      const sourcePayload = await sourceResponse.json().catch(() => ({}));
      if (!isCurrentRequest()) return false;
      if (!sourceResponse.ok) throw new Error(sourcePayload.detail || '加载数据源失败');
      const source = sourcePayload.source || null;
      const nextProviderName = source?.provider || nextProviders[0]?.name || '';
      const nextProvider = nextProviders.find((item) => item.name === nextProviderName) || null;
      setConfigured(Boolean(sourcePayload.configured));
      setProvider(nextProviderName);
      setConfig(source?.config || configDefaults(nextProvider));
      return true;
    } catch (error) {
      if (!isCurrentRequest()) return false;
      setConfigured(false);
      setProvider('');
      setConfig({});
      setSourceLoadError(true);
      setNotice(error?.message || '加载数据源失败');
      return false;
    } finally {
      if (isCurrentRequest()) setLoadingSource(false);
    }
  }, [ontologyId, workspaceId]);

  useEffect(() => {
    operationRequestRef.current += 1;
    setBusy('');
    const timer = setTimeout(load, 0);
    return () => {
      clearTimeout(timer);
      loadRequestRef.current += 1;
      operationRequestRef.current += 1;
    };
  }, [load]);

  const chooseProvider = (nextProviderName) => {
    const nextProvider = providers.find((item) => item.name === nextProviderName) || null;
    setProvider(nextProviderName);
    setConfig(configDefaults(nextProvider));
    setNotice('');
  };

  const buildPayload = () => {
    const providerConfig = {};
    (selectedProvider?.fields || []).forEach((field) => {
      const value = config[field.name];
      // GET 中的 has_* 只用于提示；密钥留空时由服务端保留原值。
      if (field.secret && (value === '' || value === null || value === undefined)) return;
      if (value !== undefined) providerConfig[field.name] = value;
    });
    return { mode: 'direct_provider', provider, config: providerConfig };
  };

  const submit = async (action) => {
    const targetScope = { ontologyId, workspaceId };
    const operationId = ++operationRequestRef.current;
    const isCurrentOperation = () => (
      operationId === operationRequestRef.current
      && sameScope(currentScopeRef.current, targetScope)
    );
    setBusy(action);
    setNotice('');
    try {
      const suffix = action === 'test' ? '/test' : '';
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source${suffix}`,
        {
          method: action === 'test' ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(buildPayload()),
        },
      );
      const data = await response.json().catch(() => ({}));
      if (!isCurrentOperation()) return;
      if (!response.ok) throw new Error(data.detail || data.error || `请求失败（${response.status}）`);
      if (action === 'test') {
        setNotice(`${data.success ? '连接成功' : '连接失败'}${data.message ? `：${data.message}` : ''}`);
      } else {
        const reloaded = await load();
        if (isCurrentOperation() && reloaded) {
          await Promise.resolve(onDataSourceChanged?.()).catch(() => undefined);
          if (isCurrentOperation()) setNotice('数据源配置已保存');
        }
      }
    } catch (error) {
      if (isCurrentOperation()) setNotice(`操作失败：${error?.message || '未知错误'}`);
    } finally {
      if (isCurrentOperation()) setBusy('');
    }
  };

  const remove = async () => {
    const targetScope = { ontologyId, workspaceId };
    const operationId = ++operationRequestRef.current;
    const isCurrentOperation = () => (
      operationId === operationRequestRef.current
      && sameScope(currentScopeRef.current, targetScope)
    );
    setBusy('delete');
    setNotice('');
    try {
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`,
        { method: 'DELETE' },
      );
      const data = await response.json().catch(() => ({}));
      if (!isCurrentOperation()) return;
      if (!response.ok) throw new Error(data.detail || data.error || `请求失败（${response.status}）`);
      const reloaded = await load();
      if (isCurrentOperation() && reloaded) {
        await Promise.resolve(onDataSourceChanged?.()).catch(() => undefined);
        if (isCurrentOperation()) setNotice('数据源配置已清除');
      }
    } catch (error) {
      if (isCurrentOperation()) setNotice(`清除失败：${error?.message || '未知错误'}`);
    } finally {
      if (isCurrentOperation()) setBusy('');
    }
  };

  if (!ontologyId) return <EmptyState title="请先选择知识包" />;

  return (
    <div className="kb-tab-body" data-wiz="kb-data-source">
      <SectionCard
        title="数据源配置"
        description="当前配置仅属于这个知识包，保存时覆盖原配置。"
        actions={(
          <StatusTag status={sourceLoadError ? 'error' : configured ? 'success' : 'warning'}>
            {loadingSource ? '加载中' : sourceLoadError ? '加载失败' : configured ? '已配置' : '未配置'}
          </StatusTag>
        )}
      >
        {!canEdit && <div className="ro-banner">🔒 当前账号仅可查看，修改由平台管理员执行。</div>}
        <FieldRow label="Provider" hint={selectedProvider?.description || ''}>
          <select
            disabled={!canEdit || loadingProviders || sourceLoadError || !providers.length}
            value={provider}
            onChange={(event) => chooseProvider(event.target.value)}
          >
            <option value="">请选择 Provider</option>
            {providers.map((item) => (
              <option key={item.name} value={item.name}>{item.label || item.name}</option>
            ))}
          </select>
        </FieldRow>
        {(selectedProvider?.fields || []).map((field) => (
          <FieldRow
            key={field.name}
            label={`${field.label || field.name}${field.required ? ' *' : ''}`}
            hint={field.secret && config[`has_${field.name}`] ? '已配置，留空保留原值' : ''}
          >
            {field.type === 'bool' ? (
              <input
                type="checkbox"
                disabled={!canEdit}
                checked={Boolean(config[field.name])}
                onChange={(event) => setConfig((current) => ({
                  ...current,
                  [field.name]: event.target.checked,
                }))}
              />
            ) : (
              <input
                type={field.secret ? 'password' : field.type === 'int' ? 'number' : 'text'}
                disabled={!canEdit}
                value={field.secret ? (config[field.name] || '') : (config[field.name] ?? '')}
                placeholder={field.secret && config[`has_${field.name}`] ? '••••••（留空保留）' : ''}
                onChange={(event) => setConfig((current) => ({
                  ...current,
                  [field.name]: field.type === 'int' ? Number(event.target.value) : event.target.value,
                }))}
              />
            )}
          </FieldRow>
        ))}
        {notice && <div className="kb-opnotice">{notice}</div>}
        {canEdit && (
          <div className="foot">
            {sourceLoadError && (
              <button className="btn" disabled={loadingProviders || loadingSource} onClick={load}>
                重新加载
              </button>
            )}
            {configured && (
              <button className="btn btn-danger" disabled={Boolean(busy)} onClick={remove}>
                {busy === 'delete' ? '清除中…' : '清除配置'}
              </button>
            )}
            <button className="btn" disabled={Boolean(busy) || !canSubmit} onClick={() => submit('test')}>
              {busy === 'test' ? '测试中…' : '测试连接'}
            </button>
            <button className="btn btn-p" disabled={Boolean(busy) || !canSubmit} onClick={() => submit('save')}>
              {busy === 'save' ? '保存中…' : '保存配置'}
            </button>
          </div>
        )}
      </SectionCard>
    </div>
  );
}
