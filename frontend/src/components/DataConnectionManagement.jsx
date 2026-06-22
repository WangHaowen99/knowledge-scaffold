import { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCircle, Database, Loader2, Plus, RefreshCw, Save, Trash2, X } from 'lucide-react';

import { authFetch } from '../utils/auth';

const EMPTY_FORM = {
  id: '',
  name: '',
  provider: 'nl2sql_http',
  config: {},
  set_as_default: false,
};

function buildConfigFromProvider(provider) {
  const config = {};
  (provider?.fields || []).forEach((field) => {
    if (field.secret) return;
    config[field.name] = field.default ?? '';
  });
  return config;
}

function getProviderLabel(providers, providerId) {
  return providers.find((provider) => provider.name === providerId)?.label || providerId || '-';
}

function maskUrl(url = '') {
  if (!url) return '未配置地址';
  if (url.length <= 42) return url;
  return `${url.slice(0, 28)}...${url.slice(-10)}`;
}

const FULL_WIDTH_FIELD_NAMES = ['url', 'endpoint', 'token', 'schema', 'prompt', 'query', 'json', 'headers', 'sql'];

const PROVIDER_FIELD_LAYOUT = {
  mindsdb: {
    fullWidth: ['url'],
  },
  nl2sql_http: {
    fullWidth: ['url', 'token'],
  },
};

function shouldUseFullWidthField(providerId, field) {
  const fieldName = String(field?.name || '').toLowerCase();
  const providerLayout = PROVIDER_FIELD_LAYOUT[providerId] || {};
  if ((providerLayout.fullWidth || []).includes(fieldName)) return true;
  if (field?.type === 'text' && fieldName.includes('json')) return true;
  if (FULL_WIDTH_FIELD_NAMES.some((key) => fieldName.includes(key))) return true;
  return false;
}

function isConnectionConfigured(connection, provider) {
  if (!provider) return false;
  const config = connection?.config || {};
  const requiredFields = (provider?.fields || []).filter((field) => field.required);
  if (requiredFields.length === 0) return true;
  return requiredFields.every((field) => {
    if (field.secret) return Boolean(config[`has_${field.name}`] || config[field.name]);
    return Boolean(String(config[field.name] ?? '').trim());
  });
}

function DataConnectionManagement({
  currentWorkspaceId,
  canEdit = false,
  view = 'connections',
  onOntologiesChange,
  }) {
  const [providers, setProviders] = useState([]);
  const [connections, setConnections] = useState([]);
  const [defaultConnectionId, setDefaultConnectionId] = useState('');
  const [ontologies, setOntologies] = useState([]);
  const [selectedConnectionId, setSelectedConnectionId] = useState('');
  const [formState, setFormState] = useState(EMPTY_FORM);
  const [isCreating, setIsCreating] = useState(false);
  const [showConnectionModal, setShowConnectionModal] = useState(false);
  const [bindingDrafts, setBindingDrafts] = useState({});
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState('');
  const [message, setMessage] = useState({ type: '', text: '' });
  const [modalMessage, setModalMessage] = useState({ type: '', text: '' });

  const providerMap = useMemo(() => {
    const map = {};
    providers.forEach((provider) => {
      map[provider.name] = provider;
    });
    return map;
  }, [providers]);

  const selectedConnection = useMemo(
    () => connections.find((connection) => connection.id === selectedConnectionId) || null,
    [connections, selectedConnectionId],
  );

  const bindingCounts = useMemo(() => {
    const counts = {};
    ontologies.forEach((ontology) => {
      const connectionId = ontology?.business_data?.connection_id || '';
      if (!connectionId) return;
      counts[connectionId] = (counts[connectionId] || 0) + 1;
    });
    return counts;
  }, [ontologies]);

  const loadData = useCallback(async ({ clearMessage = false } = {}) => {
    if (!currentWorkspaceId) return;
    setLoading(true);
    if (clearMessage) {
      setMessage({ type: '', text: '' });
      setModalMessage({ type: '', text: '' });
    }
    try {
      const [providerResponse, connectionResponse, ontologyResponse] = await Promise.all([
        authFetch('/agentic/business-data/provider-schemas'),
        authFetch('/agentic/data-connections'),
        authFetch('/agentic/ontologies'),
      ]);
      const providerPayload = await providerResponse.json().catch(() => ({}));
      const connectionPayload = await connectionResponse.json().catch(() => ({}));
      const ontologyPayload = await ontologyResponse.json().catch(() => ({}));
      if (!providerResponse.ok) throw new Error(providerPayload?.detail || '加载 Provider 失败');
      if (!connectionResponse.ok) throw new Error(connectionPayload?.detail || '加载数据连接失败');
      if (!ontologyResponse.ok) throw new Error(ontologyPayload?.detail || '加载本体列表失败');

      const nextProviders = Array.isArray(providerPayload.providers) ? providerPayload.providers : [];
      const nextConnections = Array.isArray(connectionPayload.connections) ? connectionPayload.connections : [];
      const nextOntologies = Array.isArray(ontologyPayload.ontologies) ? ontologyPayload.ontologies : [];
      setProviders(nextProviders);
      setConnections(nextConnections);
      setDefaultConnectionId(connectionPayload.default_connection_id || '');
      setOntologies(nextOntologies);
      setBindingDrafts(
        nextOntologies.reduce((acc, ontology) => {
          acc[ontology.id] = ontology?.business_data?.connection_id || '';
          return acc;
        }, {}),
      );
      setSelectedConnectionId((current) => {
        if (current && nextConnections.some((connection) => connection.id === current)) return current;
        return nextConnections[0]?.id || '';
      });
      if (nextConnections.length === 0) {
        const provider = nextProviders[0] || { name: 'nl2sql_http', fields: [] };
        setIsCreating(true);
        setShowConnectionModal(false);
        setFormState({
          ...EMPTY_FORM,
          provider: provider.name,
          config: buildConfigFromProvider(provider),
        });
      }
    } catch (error) {
      setMessage({ type: 'error', text: error?.message || '加载数据连接失败' });
    } finally {
      setLoading(false);
    }
  }, [currentWorkspaceId]);

  useEffect(() => {
    loadData({ clearMessage: true });
  }, [loadData]);

  useEffect(() => {
    setMessage({ type: '', text: '' });
    setModalMessage({ type: '', text: '' });
  }, [currentWorkspaceId, view]);

  useEffect(() => {
    if (message.type !== 'success' || !message.text) return undefined;
    const timer = window.setTimeout(() => {
      setMessage({ type: '', text: '' });
    }, 3500);
    return () => window.clearTimeout(timer);
  }, [message]);

  const refreshDataAndOntologies = async () => {
    await loadData();
    if (onOntologiesChange) {
      await onOntologiesChange();
    }
  };

  const closeConnectionModal = () => {
    setShowConnectionModal(false);
    setModalMessage({ type: '', text: '' });
  };

  const setFeedbackMessage = (nextMessage, target = 'global') => {
    if (target === 'modal') {
      setModalMessage(nextMessage);
      return;
    }
    setMessage(nextMessage);
  };

  useEffect(() => {
    if (isCreating) return;
    if (!selectedConnection) return;
    setFormState({
      id: selectedConnection.id,
      name: selectedConnection.name || '',
      provider: selectedConnection.provider || 'nl2sql_http',
      config: { ...(selectedConnection.config || {}) },
      set_as_default: selectedConnection.id === defaultConnectionId,
    });
  }, [defaultConnectionId, isCreating, selectedConnection]);

  const beginCreate = () => {
    const provider = providers[0] || { name: 'nl2sql_http', fields: [] };
    setIsCreating(true);
    setSelectedConnectionId('');
    setShowConnectionModal(true);
    setFormState({
      ...EMPTY_FORM,
      provider: provider.name,
      config: buildConfigFromProvider(provider),
    });
    setMessage({ type: '', text: '' });
    setModalMessage({ type: '', text: '' });
  };

  const selectConnection = (connectionId) => {
    const connection = connections.find((item) => item.id === connectionId);
    if (!connection) return;
    setIsCreating(false);
    setSelectedConnectionId(connectionId);
    setShowConnectionModal(true);
    setFormState({
      id: connection.id,
      name: connection.name || '',
      provider: connection.provider || 'nl2sql_http',
      config: { ...(connection.config || {}) },
      set_as_default: connection.id === defaultConnectionId,
    });
    setMessage({ type: '', text: '' });
    setModalMessage({ type: '', text: '' });
  };

  const updateConfigField = (fieldName, value) => {
    setFormState((prev) => ({
      ...prev,
      config: {
        ...(prev.config || {}),
        [fieldName]: value,
      },
    }));
  };

  const handleProviderChange = (providerId) => {
    const provider = providerMap[providerId] || providers[0];
    setModalMessage({ type: '', text: '' });
    setFormState((prev) => ({
      ...prev,
      provider: providerId,
      config: buildConfigFromProvider(provider),
    }));
  };

  const buildSavePayload = () => {
    const provider = providerMap[formState.provider];
    const config = {};
    (provider?.fields || []).forEach((field) => {
      const value = formState.config?.[field.name];
      if (field.secret && !value) return;
      config[field.name] = value ?? '';
    });
    return {
      id: isCreating ? formState.id.trim() || undefined : formState.id.trim(),
      name: formState.name.trim(),
      provider: formState.provider,
      config,
      set_as_default: Boolean(formState.set_as_default),
    };
  };

  const saveConnection = async () => {
    if (!canEdit) return;
    setMessage({ type: '', text: '' });
    setModalMessage({ type: '', text: '' });
    if (!formState.name.trim()) {
      setModalMessage({ type: 'error', text: '连接名称不能为空' });
      return;
    }
    setBusyAction('save-connection');
    try {
      const payload = buildSavePayload();
      const url = isCreating
        ? '/agentic/data-connections'
        : `/agentic/data-connections/${encodeURIComponent(formState.id)}`;
      const method = isCreating ? 'POST' : 'PATCH';
      const response = await authFetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '保存数据连接失败');
      setIsCreating(false);
      setSelectedConnectionId(data?.connection?.id || payload.id);
      closeConnectionModal();
      await refreshDataAndOntologies();
      setMessage({ type: 'success', text: '数据连接已保存' });
    } catch (error) {
      setModalMessage({ type: 'error', text: error?.message || '保存数据连接失败' });
    } finally {
      setBusyAction('');
    }
  };

  const testConnection = async (connectionId = formState.id, { target = 'global' } = {}) => {
    if (!connectionId || isCreating) {
      setFeedbackMessage({ type: 'error', text: '请先保存连接后再测试' }, target);
      return;
    }
    setBusyAction(`test-${connectionId}`);
    setFeedbackMessage({ type: '', text: '' }, target);
    try {
      const response = await authFetch(`/agentic/data-connections/${encodeURIComponent(connectionId)}/test`, {
        method: 'POST',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) throw new Error(data?.detail || data?.message || '测试连接失败');
      setFeedbackMessage({ type: 'success', text: data.message || '连接测试通过' }, target);
    } catch (error) {
      setFeedbackMessage({ type: 'error', text: error?.message || '测试连接失败' }, target);
    } finally {
      setBusyAction('');
    }
  };

  const setDefaultConnection = async (connectionId) => {
    if (!canEdit || !connectionId) return;
    setBusyAction(`default-${connectionId}`);
    setMessage({ type: '', text: '' });
    try {
      const response = await authFetch(`/agentic/data-connections/${encodeURIComponent(connectionId)}/default`, {
        method: 'POST',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '设置默认连接失败');
      setDefaultConnectionId(data.default_connection_id || connectionId);
      await loadData();
      setMessage({ type: 'success', text: '已设为新本体默认连接' });
    } catch (error) {
      setMessage({ type: 'error', text: error?.message || '设置默认连接失败' });
    } finally {
      setBusyAction('');
    }
  };

  const deleteConnection = async (connectionId) => {
    if (!canEdit || !connectionId) return;
    if (!window.confirm('确认删除该数据连接吗？')) return;
    setBusyAction(`delete-${connectionId}`);
    setMessage({ type: '', text: '' });
    try {
      const response = await authFetch(`/agentic/data-connections/${encodeURIComponent(connectionId)}`, {
        method: 'DELETE',
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const suffix = Array.isArray(data.bound_ontologies) && data.bound_ontologies.length
          ? `：${data.bound_ontologies.join('、')}`
          : '';
        throw new Error(`${data?.detail || '删除数据连接失败'}${suffix}`);
      }
      await loadData();
      setMessage({ type: 'success', text: '数据连接已删除' });
    } catch (error) {
      setMessage({ type: 'error', text: error?.message || '删除数据连接失败' });
    } finally {
      setBusyAction('');
    }
  };

  const saveBinding = async (ontologyId) => {
    if (!canEdit) return;
    const connectionId = bindingDrafts[ontologyId] || '';
    if (!connectionId) {
      setMessage({ type: 'error', text: '请选择要绑定的数据连接' });
      return;
    }
    setBusyAction(`bind-${ontologyId}`);
    setMessage({ type: '', text: '' });
    try {
      const response = await authFetch(`/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-binding`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ connection_id: connectionId }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data?.detail || '绑定数据源失败');
      await refreshDataAndOntologies();
      setMessage({ type: 'success', text: '本体数据源绑定已更新' });
    } catch (error) {
      setMessage({ type: 'error', text: error?.message || '绑定数据源失败' });
    } finally {
      setBusyAction('');
    }
  };

  const renderStatus = (ready, fallbackText = '未使用') => {
    if (ready) return <span className="data-status ready">已就绪</span>;
    return <span className="data-status muted">{fallbackText}</span>;
  };

  const renderConnectionRows = () => {
    if (!connections.length) {
      return (
        <tr>
          <td colSpan={6} className="data-empty">当前工作区暂无数据连接</td>
        </tr>
      );
    }
    return connections.map((connection) => {
      const providerLabel = getProviderLabel(providers, connection.provider);
      const provider = providerMap[connection.provider];
      const config = connection.config || {};
      const boundCount = bindingCounts[connection.id] || 0;
      const isDefault = connection.id === defaultConnectionId;
      const isReady = isConnectionConfigured(connection, provider);
      return (
        <tr
          key={connection.id}
          className={selectedConnectionId === connection.id ? 'is-selected' : ''}
          onClick={() => selectConnection(connection.id)}
        >
          <td>
            <div className="data-title-cell">
              <strong>{connection.name}</strong>
              <div className="data-title-meta">
                <span>{connection.id}</span>
                {isDefault && <span className="data-default-badge">新本体默认</span>}
              </div>
            </div>
          </td>
          <td>{providerLabel}</td>
          <td>{maskUrl(config.url)}{config.scene ? ` · scene: ${config.scene}` : ''}</td>
          <td>{boundCount} 个本体</td>
          <td>{renderStatus(isReady, boundCount ? '已绑定' : '未使用')}</td>
          <td>
            <div className="data-row-actions">
              <button className="btn btn-secondary btn-compact" type="button" onClick={(event) => { event.stopPropagation(); testConnection(connection.id); }}>
                {busyAction === `test-${connection.id}` ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
                <span>测试</span>
              </button>
              {canEdit && !isDefault && (
                <button className="btn btn-secondary btn-compact" type="button" onClick={(event) => { event.stopPropagation(); setDefaultConnection(connection.id); }}>
                  <span>设为新本体默认</span>
                </button>
              )}
              {canEdit && boundCount === 0 && (
                <button className="btn btn-secondary btn-compact data-danger-action" type="button" onClick={(event) => { event.stopPropagation(); deleteConnection(connection.id); }}>
                  <Trash2 size={13} />
                </button>
              )}
            </div>
          </td>
        </tr>
      );
    });
  };

  const renderConnectionForm = () => {
    const provider = providerMap[formState.provider] || providers[0];
    return (
      <div className="data-connection-detail">
        <div className="data-section-heading data-connection-detail-header">
          <div>
            <h4>{isCreating ? '新建连接' : '连接详情'}</h4>
            <p>{isCreating ? '创建当前工作区可复用的业务数据连接。' : formState.name || selectedConnection?.name}</p>
          </div>
          <button className="modal-close" type="button" onClick={closeConnectionModal} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        {modalMessage.text && (
          <div className={`workspace-message data-modal-message ${modalMessage.type}`}>{modalMessage.text}</div>
        )}
        <div className="data-connection-detail-scroll">
          <section className="data-form-section">
            <div className="data-form-section-heading">
              <h5>通用信息</h5>
              <p>所有数据连接共享的基础标识。</p>
            </div>
            <div className="data-form-grid data-form-grid-two-column">
              <label className="data-field">
                <span>连接 ID（可选）</span>
                <input
                  value={formState.id}
                  onChange={(event) => setFormState((prev) => ({ ...prev, id: event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '') }))}
                  placeholder="留空自动根据名称生成"
                  disabled={!canEdit || !isCreating}
                />
                <small className="data-help-inline">用于脚本和接口引用；创建后不可修改。</small>
              </label>
              <label className="data-field">
                <span>名称 <span className="data-required-mark">*</span></span>
                <input
                  value={formState.name}
                  onChange={(event) => setFormState((prev) => ({ ...prev, name: event.target.value }))}
                  placeholder="示例制造 NL2SQL"
                  disabled={!canEdit}
                />
              </label>
              <label className="data-field data-field-span-2">
                <span>Provider</span>
                <select
                  value={formState.provider}
                  onChange={(event) => handleProviderChange(event.target.value)}
                  disabled={!canEdit}
                >
                  {providers.map((item) => (
                    <option value={item.name} key={item.name}>{item.label}</option>
                  ))}
                </select>
              </label>
            </div>
          </section>

          <section className="data-form-section">
            <div className="data-form-section-heading">
              <h5>Provider 配置</h5>
              <p>根据所选 Provider 动态显示定制参数。</p>
            </div>
            <div className="data-form-grid data-form-grid-two-column">
              {(provider?.fields || []).length === 0 ? (
                <div className="data-empty data-provider-empty">当前 Provider 无额外配置项</div>
              ) : (provider?.fields || []).map((field) => {
                const value = formState.config?.[field.name] ?? '';
                const secretConfigured = Boolean(formState.config?.[`has_${field.name}`]);
                const isFullWidth = shouldUseFullWidthField(formState.provider, field);
                return (
                  <label className={`data-field${isFullWidth ? ' data-field-span-2' : ''}`} key={field.name}>
                    <span>{field.label}{field.required && <span className="data-required-mark"> *</span>}</span>
                    {field.type === 'bool' ? (
                      <select
                        value={String(Boolean(value))}
                        onChange={(event) => updateConfigField(field.name, event.target.value === 'true')}
                        disabled={!canEdit}
                      >
                        <option value="false">否</option>
                        <option value="true">是</option>
                      </select>
                    ) : (
                      <input
                        type={field.secret ? 'password' : field.type === 'int' ? 'number' : 'text'}
                        value={field.secret ? (formState.config?.[field.name] || '') : value}
                        onChange={(event) => updateConfigField(field.name, event.target.value)}
                        placeholder={field.secret && secretConfigured ? '已配置，留空保持不变' : ''}
                        disabled={!canEdit}
                      />
                    )}
                    {field.name === 'url' && (
                      <small className="data-help-inline">填写 IP 时需要带上 http://。</small>
                    )}
                  </label>
                );
              })}
            </div>
          </section>

          <section className="data-form-section data-form-section-compact">
            <div className="data-form-section-heading">
              <h5>默认策略</h5>
              <p>影响当前工作区中新导入本体的默认连接选择。</p>
            </div>
            <label className="data-toggle">
              <input
                type="checkbox"
                checked={Boolean(formState.set_as_default)}
                onChange={(event) => setFormState((prev) => ({ ...prev, set_as_default: event.target.checked }))}
                disabled={!canEdit}
              />
              <span>作为新本体默认连接</span>
            </label>
            <p className="data-help">导入新本体时默认选择此连接；运行时仍以本体绑定为准。</p>
          </section>
        </div>
        <div className="data-detail-actions data-connection-detail-footer">
          <button className="btn btn-secondary" type="button" onClick={closeConnectionModal}>
            <span>取消</span>
          </button>
          <button className="btn btn-primary" type="button" onClick={saveConnection} disabled={!canEdit || busyAction === 'save-connection'}>
            {busyAction === 'save-connection' ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
            <span>保存</span>
          </button>
          <button className="btn btn-secondary" type="button" onClick={() => testConnection(formState.id, { target: 'modal' })} disabled={isCreating || busyAction.startsWith('test-')}>
            <RefreshCw size={16} />
            <span>测试连接</span>
          </button>
        </div>
      </div>
    );
  };

  const renderBindingTable = () => (
    <section className="data-management-section data-binding-section">
      <div className="data-table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>本体</th>
              <th>业务数据源</th>
              <th>Provider</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {ontologies.length === 0 ? (
              <tr><td colSpan={5} className="data-empty">当前工作区暂无本体</td></tr>
            ) : ontologies.map((ontology) => {
              const businessData = ontology.business_data || {};
              const currentBinding = businessData.connection_id || '';
              const draftValue = bindingDrafts[ontology.id] ?? currentBinding;
              const isReady = Boolean(businessData.ready);
              return (
                <tr key={ontology.id}>
                  <td>
                    <div className="data-title-cell">
                      <strong>{ontology.name || ontology.id}</strong>
                      <span>{ontology.id}</span>
                    </div>
                  </td>
                  <td>{businessData.connection_name || '未绑定'}</td>
                  <td>{getProviderLabel(providers, businessData.provider) || '-'}</td>
                  <td>{isReady ? <span className="data-status ready">可查询</span> : <span className="data-status warning">不可查询</span>}</td>
                  <td>
                    <div className="data-binding-actions">
                      <select
                        value={draftValue}
                        onChange={(event) => setBindingDrafts((prev) => ({ ...prev, [ontology.id]: event.target.value }))}
                        disabled={!canEdit || connections.length === 0}
                      >
                        <option value="">选择数据连接</option>
                        {connections.map((connection) => (
                          <option value={connection.id} key={connection.id}>{connection.name}</option>
                        ))}
                      </select>
                      <button
                        className="btn btn-primary btn-compact"
                        type="button"
                        onClick={() => saveBinding(ontology.id)}
                        disabled={!canEdit || !draftValue || draftValue === currentBinding || busyAction === `bind-${ontology.id}`}
                      >
                        {busyAction === `bind-${ontology.id}` ? <Loader2 size={13} className="spin" /> : <CheckCircle size={13} />}
                        <span>{currentBinding ? '更新' : '绑定'}</span>
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );

  return (
    <div className="data-management">
      {message.text && <div className={`workspace-message ${message.type}`}>{message.text}</div>}
      {view !== 'bindings' && (
      <section className="data-management-section">
        <div className="data-section-heading">
          <div className="data-section-actions">
            <button className="btn btn-secondary" type="button" onClick={() => loadData({ clearMessage: true })} disabled={loading}>
              {loading ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
              <span>刷新</span>
            </button>
            {canEdit && (
              <button className="btn btn-primary" type="button" onClick={beginCreate}>
                <Plus size={16} />
                <span>新建连接</span>
              </button>
            )}
          </div>
        </div>
        <div className="data-table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>Provider</th>
                <th>地址 / 场景</th>
                <th>绑定本体</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>{renderConnectionRows()}</tbody>
          </table>
        </div>
      </section>
      )}
      {view === 'bindings' && renderBindingTable()}
      {showConnectionModal && (
        <div
          className="modal-overlay"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              closeConnectionModal();
            }
          }}
        >
          <div className="modal-content data-connection-modal" role="dialog" aria-modal="true" aria-labelledby="data-connection-modal-title">
            <div className="modal-body">
              <div id="data-connection-modal-title" className="sr-only">{isCreating ? '新建连接' : '连接详情'}</div>
              {renderConnectionForm()}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default DataConnectionManagement;
