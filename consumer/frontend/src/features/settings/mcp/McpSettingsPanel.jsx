import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, Copy, Info, KeyRound, Plus, RefreshCw, Save, Trash2 } from 'lucide-react';

import { authFetch } from '../../../shared/api/auth';
import { Modal } from '../../../shared/ui/primitives';
import { copyMcpText } from './clipboard';
import {
  MCP_SCOPES,
  MCP_STREAM_USER_CONCURRENCY_DEFAULT,
  acceptsMcpResponse,
  buildMcpClientConfig,
  buildMcpTokenRequest,
  canUseMcpSelfService,
  createMcpScopeState,
  createdMcpToken,
  mcpScopeKey,
  mcpOntologyEligibility,
  normalizeMcpOntologies,
  normalizeMcpStreamConcurrency,
  normalizeMcpStatus,
  normalizeMcpTokens,
  resolveMcpOntologySelection,
} from './model';
import './mcpSettings.css';

function payloadMessage(payload, fallback) {
  return payload?.detail || payload?.error || fallback;
}

function formatDate(value) {
  if (!value) return '永不过期';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

export default function McpSettingsPanel({
  workspaceId,
  workspaceName = '',
  authenticated = false,
  canManageTokens = true,
  canManageConcurrency = false,
  ontologies: sharedOntologies = [],
  ontologiesLoading = false,
  ontologiesError = '',
  onRetryOntologies,
  request = authFetch,
}) {
  const scopeKey = mcpScopeKey(workspaceId);
  const scopeRef = useRef(scopeKey);
  const loadSequenceRef = useRef(0);
  const loadControllerRef = useRef(null);
  const actionControllerRef = useRef(null);
  const copyTimerRef = useRef(null);
  const normalizedOntologies = useMemo(
    () => normalizeMcpOntologies({ ontologies: sharedOntologies }),
    [sharedOntologies],
  );
  const ontologyListReady = !ontologiesLoading && !ontologiesError;
  const currentOntologies = useMemo(
    () => (ontologyListReady ? normalizedOntologies : []),
    [normalizedOntologies, ontologyListReady],
  );
  const [scopeState, setScopeState] = useState(() => createMcpScopeState(scopeKey));
  const [remoteState, setRemoteState] = useState({ scopeKey: '', status: null, tokens: [], concurrency: null });
  const [loadingScope, setLoadingScope] = useState('');
  const [actionState, setActionState] = useState({ scopeKey: '', kind: '', tokenId: '' });
  const [message, setMessage] = useState({ scopeKey: '', type: '', text: '' });

  scopeRef.current = scopeKey;
  if (scopeState.scopeKey !== scopeKey) setScopeState(createMcpScopeState(scopeKey));

  const currentRemote = remoteState.scopeKey === scopeKey
    ? remoteState
    : { status: null, tokens: [], concurrency: null };
  const currentMessage = message.scopeKey === scopeKey ? message : { type: '', text: '' };
  const loading = loadingScope === scopeKey;
  const acting = actionState.scopeKey === scopeKey ? actionState : { kind: '', tokenId: '' };
  const selfServiceAvailable = canUseMcpSelfService({ authenticated, capability: canManageTokens });
  const services = currentRemote.status?.services || [];
  const selectedService = services.find((service) => service.id === scopeState.selectedServiceId) || null;
  const revokeToken = currentRemote.tokens.find((token) => token.token_id === scopeState.revokeTokenId) || null;
  const configToken = scopeState.createdToken || '<MCP_TOKEN>';
  const selectedOntology = currentOntologies.find(
    (ontology) => ontology.id === scopeState.form.ontologyId,
  ) || null;
  const ontologyOptions = useMemo(
    () => currentOntologies.map((ontology) => ({
      ...ontology,
      eligibility: mcpOntologyEligibility(ontology, scopeState.form.scopes),
    })),
    [currentOntologies, scopeState.form.scopes],
  );
  const activeTokenCount = useMemo(
    () => currentRemote.tokens.filter((token) => token.status === 'active').length,
    [currentRemote.tokens],
  );
  const concurrency = normalizeMcpStreamConcurrency(currentRemote.concurrency || {});
  const [concurrencyDraft, setConcurrencyDraft] = useState(concurrency.value);
  const concurrencyReady = currentRemote.scopeKey === scopeKey && currentRemote.concurrency !== null;

  useEffect(() => {
    if (!concurrencyReady) setConcurrencyDraft(MCP_STREAM_USER_CONCURRENCY_DEFAULT);
  }, [concurrencyReady, scopeKey]);

  const loadData = useCallback(async ({ clearMessage = false } = {}) => {
    if (!workspaceId || !authenticated) return;
    loadControllerRef.current?.abort();
    const controller = new AbortController();
    const requestScope = scopeKey;
    const requestSequence = ++loadSequenceRef.current;
    loadControllerRef.current = controller;
    setLoadingScope(requestScope);
    if (clearMessage) setMessage({ scopeKey: requestScope, type: '', text: '' });
    const accepted = () => !controller.signal.aborted && acceptsMcpResponse({
      currentScope: scopeRef.current,
      requestScope,
      currentSequence: loadSequenceRef.current,
      requestSequence,
    });
    try {
      const [statusResponse, tokensResponse, concurrencyResponse] = await Promise.all([
        request('/agentic/mcp/runtime/status', { signal: controller.signal }),
        request('/agentic/mcp/tokens', { signal: controller.signal }),
        request('/agentic/mcp/runtime/concurrency', { signal: controller.signal }),
      ]);
      const [statusPayload, tokensPayload, concurrencyPayload] = await Promise.all([
        statusResponse.json().catch(() => ({})),
        tokensResponse.json().catch(() => ({})),
        concurrencyResponse.json().catch(() => ({})),
      ]);
      if (!statusResponse.ok) throw new Error(payloadMessage(statusPayload, '加载 MCP 服务状态失败'));
      if (!tokensResponse.ok) throw new Error(payloadMessage(tokensPayload, '加载 MCP Token 失败'));
      // Keep the status and token UI usable while an older backend is rolling out.
      if (!concurrencyResponse.ok && concurrencyResponse.status !== 404) {
        throw new Error(payloadMessage(concurrencyPayload, '加载 MCP 并发配置失败'));
      }
      const status = normalizeMcpStatus(statusPayload, workspaceId);
      if (!status.valid) throw new Error(status.error);
      if (!accepted()) return;
      const concurrencyConfig = concurrencyResponse.ok
        ? normalizeMcpStreamConcurrency({
          value: concurrencyPayload.stream_user_workspace_concurrency,
          min: concurrencyPayload.min,
          max: concurrencyPayload.max,
        })
        : null;
      setRemoteState({
        scopeKey: requestScope,
        status,
        tokens: normalizeMcpTokens(tokensPayload, workspaceId),
        concurrency: concurrencyConfig,
      });
      setConcurrencyDraft(
        concurrencyConfig?.value ?? MCP_STREAM_USER_CONCURRENCY_DEFAULT,
      );
    } catch (error) {
      if (accepted()) setMessage({ scopeKey: requestScope, type: 'error', text: error.message || '加载 MCP 配置失败' });
    } finally {
      if (accepted()) setLoadingScope('');
      if (loadControllerRef.current === controller) loadControllerRef.current = null;
    }
  }, [authenticated, request, scopeKey, workspaceId]);

  const saveConcurrency = () => performAction('concurrency', async (controller, requestScope) => {
    const normalized = normalizeMcpStreamConcurrency({ value: Number(concurrencyDraft) });
    if (normalized.value !== Number(concurrencyDraft)) {
      throw new Error(`并发数必须是 ${normalized.min}-${normalized.max} 的整数`);
    }
    const response = await request('/agentic/mcp/runtime/concurrency', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stream_user_workspace_concurrency: normalized.value }),
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payloadMessage(payload, '保存 MCP 并发配置失败'));
    if (controller.signal.aborted || scopeRef.current !== requestScope) return;
    setConcurrencyDraft(normalizeMcpStreamConcurrency({
      value: payload.stream_user_workspace_concurrency,
      min: payload.min,
      max: payload.max,
    }).value);
    setMessage({ scopeKey: requestScope, type: 'success', text: 'MCP 并发配置已保存。' });
    await loadData();
  }, { available: canManageConcurrency && concurrencyReady });

  const retryOntologyList = useCallback(
    () => Promise.resolve().then(() => onRetryOntologies?.()),
    [onRetryOntologies],
  );

  const refreshAll = useCallback(
    () => Promise.allSettled([
      loadData({ clearMessage: true }),
      retryOntologyList(),
    ]),
    [loadData, retryOntologyList],
  );

  useEffect(() => {
    if (!ontologyListReady) return;
    const ontologies = normalizedOntologies;
    setScopeState((current) => {
      if (current.scopeKey !== scopeKey) return current;
      return {
        ...current,
        form: {
          ...current.form,
          ontologyId: resolveMcpOntologySelection({
            ontologies,
            scopes: current.form.scopes,
            currentId: current.form.ontologyId,
          }),
        },
      };
    });
  }, [normalizedOntologies, ontologyListReady, scopeKey]);

  useEffect(() => {
    actionControllerRef.current?.abort();
    actionControllerRef.current = null;
    const timer = window.setTimeout(() => loadData({ clearMessage: true }), 0);
    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(copyTimerRef.current);
      loadControllerRef.current?.abort();
      actionControllerRef.current?.abort();
    };
  }, [loadData]);

  const updateScopeState = (patch) => setScopeState((current) => ({
    ...(current.scopeKey === scopeKey ? current : createMcpScopeState(scopeKey)),
    ...patch,
  }));

  const updateForm = (patch) => setScopeState((current) => {
    const safe = current.scopeKey === scopeKey ? current : createMcpScopeState(scopeKey);
    return {
      ...safe,
      createdToken: '',
      createdOntologyId: '',
      createdOntologyVersion: '',
      form: { ...safe.form, ...patch },
    };
  });

  const performAction = async (kind, callback, { available = selfServiceAvailable } = {}) => {
    if (!workspaceId || !available) return;
    actionControllerRef.current?.abort();
    const controller = new AbortController();
    const requestScope = scopeKey;
    actionControllerRef.current = controller;
    setActionState({ scopeKey: requestScope, kind, tokenId: scopeState.revokeTokenId });
    setMessage({ scopeKey: requestScope, type: '', text: '' });
    try {
      await callback(controller, requestScope);
    } catch (error) {
      if (!controller.signal.aborted && scopeRef.current === requestScope) {
        setMessage({ scopeKey: requestScope, type: 'error', text: error.message || 'MCP 操作失败' });
      }
    } finally {
      if (!controller.signal.aborted && scopeRef.current === requestScope) {
        setActionState({ scopeKey: requestScope, kind: '', tokenId: '' });
      }
      if (actionControllerRef.current === controller) actionControllerRef.current = null;
    }
  };

  const createToken = () => {
    if (!ontologyListReady) return;
    return performAction('create', async (controller, requestScope) => {
      const body = buildMcpTokenRequest(scopeState.form);
      const response = await request('/agentic/mcp/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payloadMessage(payload, '创建 MCP Token 失败'));
      if (controller.signal.aborted || scopeRef.current !== requestScope) return;
      const createdBinding = currentOntologies.find(
        (ontology) => ontology.id === scopeState.form.ontologyId,
      );
      const resetForm = createMcpScopeState(requestScope).form;
      updateScopeState({
        createdToken: createdMcpToken(payload),
        createdOntologyId: scopeState.form.ontologyId,
        createdOntologyVersion: createdBinding?.active_version || '',
        form: {
          ...resetForm,
          ontologyId: resolveMcpOntologySelection({
            ontologies: currentOntologies,
            scopes: resetForm.scopes,
          }),
        },
      });
      setMessage({ scopeKey: requestScope, type: 'success', text: 'Token 已创建。明文仅在当前工作空间的本次页面会话中展示，请立即复制。' });
      await loadData();
    });
  };

  const revokeTokenAction = () => performAction('revoke', async (controller, requestScope) => {
    const tokenId = scopeState.revokeTokenId;
    if (!tokenId) return;
    const response = await request(`/agentic/mcp/tokens/${encodeURIComponent(tokenId)}`, {
      method: 'DELETE', signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payloadMessage(payload, '吊销 MCP Token 失败'));
    if (controller.signal.aborted || scopeRef.current !== requestScope) return;
    updateScopeState({ revokeTokenId: '', createdToken: '' });
    setMessage({ scopeKey: requestScope, type: 'success', text: 'Token 已吊销。' });
    await loadData();
  });

  const copyValue = async (key, value) => {
    const result = await copyMcpText(value);
    if (scopeRef.current !== scopeKey) return;
    if (result.ok) {
      updateScopeState({ copiedKey: key });
      setMessage({ scopeKey, type: 'success', text: '已复制到剪贴板。' });
      window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => {
        if (scopeRef.current === scopeKey) updateScopeState({ copiedKey: '' });
      }, 1600);
    } else setMessage({ scopeKey, type: 'error', text: result.error });
  };

  const toggleScope = (value) => {
    const scopes = scopeState.form.scopes.includes(value)
      ? scopeState.form.scopes.filter((scope) => scope !== value)
      : [...scopeState.form.scopes, value];
    updateForm({
      scopes,
      ontologyId: resolveMcpOntologySelection({
        ontologies: currentOntologies,
        scopes,
        currentId: scopeState.form.ontologyId,
      }),
    });
  };

  if (!authenticated) return <div className="settings-mcp-empty">登录后可管理当前账号的 MCP Token。</div>;
  if (!workspaceId) return <div className="settings-mcp-empty">请选择工作空间后管理 MCP。</div>;

  return (
    <div className="settings-mcp-panel">
      <section className="card settings-mcp-overview">
        <div><h3>MCP 自服务接入</h3><p>{workspaceName || workspaceId} · Token 绑定当前账号、当前工作空间与所选知识包。</p></div>
        <button className="btn" type="button" disabled={loading || ontologiesLoading} onClick={refreshAll}>
          <RefreshCw size={14} />{loading || ontologiesLoading ? '刷新中…' : '刷新'}
        </button>
      </section>
      {!selfServiceAvailable && <div className="settings-mcp-notice">当前认证能力未开放 MCP Token 自服务。</div>}
      {currentMessage.text && <div className={`settings-mcp-notice ${currentMessage.type}`} role={currentMessage.type === 'error' ? 'alert' : 'status'}>{currentMessage.text}</div>}
      {loading && !currentRemote.status && <div className="settings-mcp-empty">加载 MCP 状态与 Token…</div>}
      {currentRemote.status && (
        <section className="settings-mcp-metrics">
          <div><span>服务状态</span><strong>{currentRemote.status.running ? '在线' : '离线'}</strong></div>
          <div><span>Transport</span><strong>{currentRemote.status.transport || '—'}</strong></div>
          <div><span>Public URL</span><strong>{currentRemote.status.publicBaseUrl || '—'}</strong></div>
          <div><span>Active Tokens</span><strong>{activeTokenCount}</strong></div>
        </section>
      )}
      <section className="settings-mcp-services">
        {services.map((service) => (
          <article className="card" key={service.id}>
            <div><h3>{service.title}</h3><p>{service.summary}</p></div>
            <code>{service.active_url || '暂无 endpoint'}</code>
            <div className="settings-mcp-actions">
              <button className="btn" type="button" disabled={!service.active_url}
                onClick={() => copyValue(`endpoint:${service.id}`, service.active_url)}>
                {scopeState.copiedKey === `endpoint:${service.id}` ? <Check size={14} /> : <Copy size={14} />}复制 endpoint
              </button>
              <button className="btn" type="button" onClick={() => updateScopeState({ selectedServiceId: service.id })}><Info size={14} />说明</button>
            </div>
          </article>
        ))}
        {!loading && !services.length && <div className="settings-mcp-empty">暂无 MCP 服务目录。</div>}
      </section>
      <section className="card settings-mcp-concurrency">
        <div><h3>流式 Chat 并发控制</h3><p>控制普通用户在当前工作空间可同时运行的 MCP Progress 流式 Chat 数量，默认 5，范围 1–32。</p></div>
        <label>普通用户并发数
          <input
            type="number"
            min={concurrency.min}
            max={concurrency.max}
            step="1"
            value={concurrencyDraft}
            disabled={!canManageConcurrency || !concurrencyReady || Boolean(acting.kind)}
            onChange={(event) => setConcurrencyDraft(event.target.value)}
          />
        </label>
        {!canManageConcurrency && <div className="settings-mcp-binding-hint">仅平台管理员可修改；当前值为 {concurrency.value}。</div>}
        {canManageConcurrency && <div className="settings-mcp-actions"><button className="btn btn-p" type="button" disabled={!concurrencyReady || Boolean(acting.kind)} onClick={saveConcurrency}><Save size={14} />{acting.kind === 'concurrency' ? '保存中…' : '保存并发配置'}</button></div>}
      </section>
      <section className="card settings-mcp-create">
        <h3>生成 Token</h3>
        <label>用途名称<input value={scopeState.form.name} disabled={!selfServiceAvailable || Boolean(acting.kind)}
          onChange={(event) => updateForm({ name: event.target.value })} placeholder="例如：本地 IDE" /></label>
        <label>有效期<select value={scopeState.form.expiresIn} disabled={!selfServiceAvailable || Boolean(acting.kind)}
          onChange={(event) => updateForm({ expiresIn: event.target.value })}>
          <option value="3600">1 小时</option><option value="86400">1 天</option>
          <option value="604800">7 天</option><option value="2592000">30 天</option><option value="never">永不过期</option>
        </select></label>
        <fieldset><legend>最小权限</legend>{MCP_SCOPES.map((scope) => (
          <label key={scope}><input type="checkbox" checked={scopeState.form.scopes.includes(scope)}
            disabled={!selfServiceAvailable || Boolean(acting.kind)} onChange={() => toggleScope(scope)} />{scope}</label>
        ))}</fieldset>
        <label>绑定知识包<select value={scopeState.form.ontologyId}
          disabled={!selfServiceAvailable || Boolean(acting.kind) || !ontologyListReady}
          onChange={(event) => updateForm({ ontologyId: event.target.value })}>
          <option value="">
            {ontologiesLoading ? '正在加载知识包…'
              : ontologiesError ? '知识包加载失败'
                : '请选择知识包'}
          </option>
          {ontologyOptions.map((ontology) => (
            <option key={ontology.id} value={ontology.id} disabled={!ontology.eligibility.eligible}>
              {ontology.name}（{ontology.active_version || '无激活版本'}）
              {ontology.eligibility.reason ? ` — ${ontology.eligibility.reason}` : ''}
            </option>
          ))}
        </select></label>
        {selectedOntology && <div className="settings-mcp-binding-hint">
          当前激活版本：<strong>{selectedOntology.active_version}</strong>。Token 只绑定知识包 ID，执行时按调用或会话规则解析版本。
        </div>}
        {ontologiesError && (
          <div className="settings-mcp-binding-hint error" role="alert">
            知识包列表加载失败：{ontologiesError}
            <button className="btn" type="button" onClick={retryOntologyList}>重新加载知识包</button>
          </div>
        )}
        {ontologyListReady && !ontologyOptions.some((item) => item.eligibility.eligible) && (
          <div className="settings-mcp-binding-hint error">当前权限组合下没有可绑定的知识包。</div>
        )}
        <button className="btn btn-p" type="button"
          disabled={!selfServiceAvailable || Boolean(acting.kind)
            || !ontologyListReady || !scopeState.form.ontologyId || !scopeState.form.scopes.length}
          onClick={createToken}>
          <Plus size={14} />{acting.kind === 'create' ? '生成中…' : '生成 Token'}
        </button>
        {scopeState.createdToken && (
          <div className="settings-mcp-secret" role="status">
            <strong><KeyRound size={15} />一次性 Token 明文</strong><code>{scopeState.createdToken}</code>
            <span>绑定知识包：{scopeState.createdOntologyId}；创建时激活版本：{scopeState.createdOntologyVersion || '—'}</span>
            <button className="btn" type="button" onClick={() => copyValue('created-token', scopeState.createdToken)}>
              {scopeState.copiedKey === 'created-token' ? <Check size={14} /> : <Copy size={14} />}复制
            </button>
          </div>
        )}
      </section>
      <section className="card settings-mcp-tokens">
        <h3>当前账号的 Token</h3>
        <div className="settings-table-scroll"><table className="dt"><thead><tr>
          <th>名称</th><th>Token</th><th>知识包</th><th>Scope</th><th>状态</th><th>到期</th><th>操作</th>
        </tr></thead><tbody>
          {currentRemote.tokens.map((token) => <tr key={token.token_id}>
            <td>{token.name || token.token_id}</td><td><code>{token.token_masked || '已隐藏'}</code></td>
            <td>{currentOntologies.find((item) => item.id === token.ontology_id)?.name || token.ontology_id || '工作空间级（旧 Token）'}</td>
            <td>{token.scopes.join('、') || '—'}</td><td>{token.status || '—'}</td><td>{formatDate(token.expires_at)}</td>
            <td><button className="btn btn-danger" type="button" disabled={!selfServiceAvailable || token.status !== 'active' || Boolean(acting.kind)}
              onClick={() => updateScopeState({ revokeTokenId: token.token_id })}><Trash2 size={14} />吊销</button></td>
          </tr>)}
          {!loading && !currentRemote.tokens.length && <tr><td colSpan="7" className="empty">暂无 MCP Token。</td></tr>}
        </tbody></table></div>
      </section>
      {services.length > 0 && <section className="card settings-mcp-config"><h3>客户端配置</h3>
        <p>切换工作空间或刷新页面后，配置自动恢复为占位符，不保留 Token 明文。</p>
        {services.map((service) => {
          const config = buildMcpClientConfig(service, configToken);
          return <div key={service.id}><strong>{service.title}</strong><pre>{config}</pre>
            <button className="btn" type="button" onClick={() => copyValue(`config:${service.id}`, config)}>
              {scopeState.copiedKey === `config:${service.id}` ? <Check size={14} /> : <Copy size={14} />}复制配置
            </button></div>;
        })}
      </section>}
      {selectedService && <Modal title={selectedService.title} description={selectedService.description || selectedService.summary}
        onClose={() => updateScopeState({ selectedServiceId: '' })} actions={<button className="btn btn-p" type="button" onClick={() => updateScopeState({ selectedServiceId: '' })}>完成</button>}>
        <div className="settings-mcp-tools">{selectedService.tools.map((tool) => <article key={tool.name}><code>{tool.name}</code><p>{tool.description}</p></article>)}</div>
      </Modal>}
      {revokeToken && <Modal title="吊销 MCP Token" description={`Token：${revokeToken.name || revokeToken.token_masked}`}
        onClose={() => { if (!acting.kind) updateScopeState({ revokeTokenId: '' }); }} actions={<>
          <button className="btn" type="button" disabled={Boolean(acting.kind)} onClick={() => updateScopeState({ revokeTokenId: '' })}>取消</button>
          <button className="btn btn-danger" type="button" disabled={Boolean(acting.kind)} onClick={revokeTokenAction}>{acting.kind === 'revoke' ? '吊销中…' : '确认吊销'}</button>
        </>}><p>吊销立即生效，使用该凭据的客户端会失去访问权限。</p></Modal>}
    </div>
  );
}
