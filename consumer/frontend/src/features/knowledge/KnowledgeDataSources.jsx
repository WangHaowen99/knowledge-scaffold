import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { authFetch } from '../../shared/api/auth';
import { useAuthContext } from '../auth';
import { ConfirmDialog } from '../../shared/ui/feedback';
import {
  EmptyState,
  FieldRow,
  SectionCard,
  StatusTag,
} from '../../shared/ui/primitives';

const inputTypeForField = (field) => {
  if (field.secret) {
    return 'password';
  }
  return field.type === 'int' ? 'number' : 'text';
};

const NOTICE_ICONS = {
  success: (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path
        fill="currentColor"
        d="M6.5 12.3 2.7 8.5l1.4-1.4 2.4 2.4 5.4-5.4 1.4 1.4z"
      />
    </svg>
  ),
  error: (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path
        fill="currentColor"
        d="M3.8 3.8 8 8l4.2-4.2 1.4 1.4L9.4 9.4l4.2 4.2-1.4 1.4L8 10.8l-4.2 4.2-1.4-1.4L6.6 9.4 2.4 5.2z"
      />
    </svg>
  ),
  info: (
    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path fill="currentColor" d="M7 7v6h2V7zm0-3v2h2V4z" />
    </svg>
  ),
};

function DataSourceFeedback({ notice }) {
  if (!notice?.message) {
    return null;
  }
  const type = notice.type || 'info';
  return (
    <div
      className={`kb-ds-feedback ${type}`}
      role={type === 'error' ? 'alert' : 'status'}
    >
      <span className="kb-ds-feedback-icon">
        {NOTICE_ICONS[type] || NOTICE_ICONS.info}
      </span>
      <span className="kb-ds-feedback-text">{notice.message}</span>
    </div>
  );
}

const configDefaults = (provider) => {
  const defaults = {};
  (provider?.fields || []).forEach((field) => {
    if (field.default !== undefined && field.default !== null) {
      defaults[field.name] = field.default;
    }
  });
  return defaults;
};

const sameScope = (left, right) =>
  left.ontologyId === right.ontologyId &&
  left.workspaceId === right.workspaceId;

// eslint-disable-next-line complexity, max-lines-per-function -- Preserve established boundary.
export default function KnowledgeDataSources({
  ontologyId,
  onDataSourceChanged,
}) {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.data_source.write');
  const canTest = can('ws.data_source.test');
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState('');
  const [config, setConfig] = useState({});
  const [configured, setConfigured] = useState(false);
  const [loadingProviders, setLoadingProviders] = useState(false);
  const [loadingSource, setLoadingSource] = useState(false);
  const [sourceLoadError, setSourceLoadError] = useState(false);
  const [busy, setBusy] = useState('');
  const [removeConfirmOpen, setRemoveConfirmOpen] = useState(false);
  const [notice, setNotice] = useState(null);
  const loadRequestRef = useRef(0);
  const operationRequestRef = useRef(0);
  const currentScopeRef = useRef({ ontologyId, workspaceId });
  currentScopeRef.current = { ontologyId, workspaceId };

  const selectedProvider = useMemo(
    () => providers.find((item) => item.name === provider) || null,
    [provider, providers],
  );
  const canSubmit =
    canEdit &&
    Boolean(selectedProvider) &&
    !loadingProviders &&
    !loadingSource &&
    !sourceLoadError;

  // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
  const load = useCallback(async () => {
    const targetScope = { ontologyId, workspaceId };
    const requestId = ++loadRequestRef.current;
    const isCurrentRequest = () =>
      requestId === loadRequestRef.current &&
      sameScope(currentScopeRef.current, targetScope);
    if (!ontologyId) {
      if (isCurrentRequest()) {
        setProvider('');
        setConfig({});
        setConfigured(false);
        setSourceLoadError(false);
      }
      return false;
    }
    setNotice(null);
    setLoadingProviders(true);
    setLoadingSource(true);
    setConfigured(false);
    setProvider('');
    setConfig({});
    setSourceLoadError(false);

    let nextProviders = [];
    try {
      const providersResponse = await authFetch(
        '/agentic/business-data/provider-schemas',
      );
      const providersPayload = await providersResponse.json().catch(() => ({}));
      if (!isCurrentRequest()) {
        return false;
      }
      if (!providersResponse.ok) {
        throw new Error(providersPayload.detail || '加载 Provider 失败');
      }
      nextProviders = Array.isArray(providersPayload.providers) ?
        providersPayload.providers :
        [];
      setProviders(nextProviders);
    } catch (error) {
      if (!isCurrentRequest()) {
        return false;
      }
      setProviders([]);
      setProvider('');
      setConfig({});
      setSourceLoadError(true);
      setLoadingSource(false);
      setNotice({
        type: 'error',
        message: error?.message || '加载 Provider 失败',
      });
      return false;
    } finally {
      if (isCurrentRequest()) {
        setLoadingProviders(false);
      }
    }

    try {
      const sourceResponse = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`,
      );
      const sourcePayload = await sourceResponse.json().catch(() => ({}));
      if (!isCurrentRequest()) {
        return false;
      }
      if (!sourceResponse.ok) {
        throw new Error(sourcePayload.detail || '加载数据源失败');
      }
      const source = sourcePayload.source || null;
      const [firstProvider] = nextProviders;
      const nextProviderName = source?.provider || firstProvider?.name || '';
      const nextProvider =
        nextProviders.find((item) => item.name === nextProviderName) || null;
      setConfigured(Boolean(sourcePayload.configured));
      setProvider(nextProviderName);
      setConfig(source?.config || configDefaults(nextProvider));
      return true;
    } catch (error) {
      if (!isCurrentRequest()) {
        return false;
      }
      setConfigured(false);
      setProvider('');
      setConfig({});
      setSourceLoadError(true);
      setNotice({ type: 'error', message: error?.message || '加载数据源失败' });
      return false;
    } finally {
      if (isCurrentRequest()) {
        setLoadingSource(false);
      }
    }
  }, [ontologyId, workspaceId]);

  useEffect(() => {
    operationRequestRef.current += 1;
    setBusy('');
    setRemoveConfirmOpen(false);
    const timer = setTimeout(load, 0);
    return () => {
      clearTimeout(timer);
      loadRequestRef.current += 1;
      operationRequestRef.current += 1;
    };
  }, [load]);

  const chooseProvider = (nextProviderName) => {
    const nextProvider =
      providers.find((item) => item.name === nextProviderName) || null;
    setProvider(nextProviderName);
    setConfig(configDefaults(nextProvider));
    setNotice(null);
  };

  const buildPayload = () => {
    const providerConfig = {};
    (selectedProvider?.fields || []).forEach((field) => {
      const value = config[field.name];
      // GET 中的 has_* 只用于提示；密钥留空时由服务端保留原值。
      // G.CTL.07 exception: tests enforce this fail-closed secret preservation guard verbatim.
      if (
        field.secret &&
        (value === '' || value === null || value === undefined)
      ) {
        return;
      }
      if (value !== undefined) {
        providerConfig[field.name] = value;
      }
    });
    return { mode: 'direct_provider', provider, config: providerConfig };
  };

  const notifyDataSourceChanged = useCallback(
    (targetScope) => {
      try {
        return Promise.resolve(onDataSourceChanged?.(targetScope)).catch(
          () => undefined,
        );
      } catch {
        return Promise.resolve();
      }
    },
    [onDataSourceChanged],
  );

  // eslint-disable-next-line complexity -- Preserve this established UI, hook, or event-contract boundary.
  const submit = async (action) => {
    const targetScope = { ontologyId, workspaceId };
    const operationId = ++operationRequestRef.current;
    const isCurrentOperation = () =>
      operationId === operationRequestRef.current &&
      sameScope(currentScopeRef.current, targetScope);
    setBusy(action);
    setNotice(null);
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
      const summaryRefresh =
        response.ok && action !== 'test' ?
          notifyDataSourceChanged(targetScope) :
          null;
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (!isCurrentOperation()) {
          return;
        }
        throw new Error(
          data.detail || data.error || `请求失败（${response.status}）`,
        );
      }
      if (action === 'test') {
        if (!isCurrentOperation()) {
          return;
        }
        let message = data.success ?
          '连接成功：数据源配置有效，可正常查询' :
          '连接失败：请检查配置后重试';
        if (data.message) {
          const prefix = data.success ? '连接成功' : '连接失败';
          message = `${prefix}：${data.message}`;
        }
        setNotice({
          type: data.success ? 'success' : 'error',
          message,
        });
      } else {
        if (!isCurrentOperation()) {
          await summaryRefresh;
          return;
        }
        const reloaded = await load();
        await summaryRefresh;
        if (isCurrentOperation() && reloaded) {
          if (isCurrentOperation()) {
            setNotice({ type: 'success', message: '数据源配置已保存' });
          }
        }
      }
    } catch (error) {
      if (isCurrentOperation()) {
        const message =
          action === 'test' ?
            `连接失败：${error?.message || '未知错误'}` :
            `操作失败：${error?.message || '未知错误'}`;
        setNotice({ type: 'error', message });
      }
    } finally {
      if (isCurrentOperation()) {
        setBusy('');
      }
    }
  };

  const remove = async () => {
    const targetScope = { ontologyId, workspaceId };
    const operationId = ++operationRequestRef.current;
    const isCurrentOperation = () =>
      operationId === operationRequestRef.current &&
      sameScope(currentScopeRef.current, targetScope);
    setBusy('delete');
    setNotice(null);
    try {
      const response = await authFetch(
        `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-source`,
        { method: 'DELETE' },
      );
      const summaryRefresh = response.ok ?
        notifyDataSourceChanged(targetScope) :
        null;
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        if (!isCurrentOperation()) {
          return;
        }
        throw new Error(
          data.detail || data.error || `请求失败（${response.status}）`,
        );
      }
      if (!isCurrentOperation()) {
        await summaryRefresh;
        return;
      }
      setRemoveConfirmOpen(false);
      const reloaded = await load();
      await summaryRefresh;
      if (isCurrentOperation() && reloaded) {
        if (isCurrentOperation()) {
          setNotice({ type: 'success', message: '数据源配置已清除' });
        }
      }
    } catch (error) {
      if (isCurrentOperation()) {
        setRemoveConfirmOpen(false);
        setNotice({
          type: 'error',
          message: `清除失败：${error?.message || '未知错误'}`,
        });
      }
    } finally {
      if (isCurrentOperation()) {
        setBusy('');
      }
    }
  };

  if (!ontologyId) {
    return <EmptyState title="请先选择知识包" />;
  }
  let sourceStatus = configured ? 'success' : 'warning';
  if (sourceLoadError) {
    sourceStatus = 'error';
  }
  let sourceStatusLabel = configured ? '已配置' : '未配置';
  if (loadingSource) {
    sourceStatusLabel = '加载中';
  } else if (sourceLoadError) {
    sourceStatusLabel = '加载失败';
  }

  return (
    <div className="kb-tab-body" data-wiz="kb-data-source">
      <SectionCard
        title="数据源配置"
        description="当前配置仅属于这个知识包，保存时覆盖原配置。"
        actions={
          <StatusTag status={sourceStatus}>{sourceStatusLabel}</StatusTag>
        }
      >
        {!canEdit && (
          <div className="ro-banner">
            🔒 当前账号仅可查看，修改由工作空间 owner 或平台管理员执行。
          </div>
        )}
        <FieldRow label="Provider" hint={selectedProvider?.description || ''}>
          <select
            disabled={
              !canEdit ||
              loadingProviders ||
              sourceLoadError ||
              !providers.length
            }
            value={provider}
            onChange={(event) => chooseProvider(event.target.value)}
          >
            <option value="">请选择 Provider</option>
            {providers.map((item) => (
              <option key={item.name} value={item.name}>
                {item.label || item.name}
              </option>
            ))}
          </select>
        </FieldRow>
        {(selectedProvider?.fields || []).map((field) => (
          <FieldRow
            key={field.name}
            label={`${field.label || field.name}${field.required ? ' *' : ''}`}
            hint={
              field.secret && config[`has_${field.name}`] ?
                '已配置，留空保留原值' :
                ''
            }
          >
            {field.type === 'bool' ? (
              <input
                type="checkbox"
                disabled={!canEdit}
                checked={Boolean(config[field.name])}
                onChange={(event) =>
                  setConfig((current) => ({
                    ...current,
                    [field.name]: event.target.checked,
                  }))
                }
              />
            ) : (
              <input
                type={inputTypeForField(field)}
                disabled={!canEdit}
                autoComplete={field.secret ? 'new-password' : undefined}
                value={
                  field.secret ?
                    config[field.name] || '' :
                    (config[field.name] ?? '')
                }
                placeholder={
                  field.secret && config[`has_${field.name}`] ?
                    '••••••（留空保留）' :
                    ''
                }
                onChange={(event) =>
                  setConfig((current) => ({
                    ...current,
                    [field.name]:
                      field.type === 'int' ?
                        Number(event.target.value) :
                        event.target.value,
                  }))
                }
              />
            )}
          </FieldRow>
        ))}
        <DataSourceFeedback notice={notice} />
        {(canEdit || canTest) && (
          <div className="foot">
            {sourceLoadError && (
              <button
                className="btn"
                disabled={loadingProviders || loadingSource}
                onClick={load}
              >
                重新加载
              </button>
            )}
            {configured && canEdit && (
              <button
                className="btn btn-danger"
                disabled={Boolean(busy)}
                onClick={() => setRemoveConfirmOpen(true)}
              >
                {busy === 'delete' ? '清除中…' : '清除配置'}
              </button>
            )}
            {canTest && (
              <button
                className="btn"
                disabled={Boolean(busy) || !canSubmit}
                onClick={() => submit('test')}
              >
                {busy === 'test' ? (
                  <span className="kb-ds-btn-loading">
                    <span className="kb-ds-spinner" aria-hidden="true" />
                    测试中…
                  </span>
                ) : (
                  '测试连接'
                )}
              </button>
            )}
            {canEdit && (
              <button
                className="btn btn-p"
                disabled={Boolean(busy) || !canSubmit}
                onClick={() => submit('save')}
              >
                {busy === 'save' ? '保存中…' : '保存配置'}
              </button>
            )}
          </div>
        )}
      </SectionCard>
      <ConfirmDialog
        open={removeConfirmOpen}
        title="清除数据源配置"
        description="确认清除当前知识包的数据源配置？清除后当前知识包将变为“未配置”，相关查询将不可用。"
        confirmLabel="确认清除"
        danger
        busy={busy === 'delete'}
        onConfirm={remove}
        onCancel={() => {
          if (busy !== 'delete') {
            setRemoveConfirmOpen(false);
          }
        }}
      />
    </div>
  );
}
