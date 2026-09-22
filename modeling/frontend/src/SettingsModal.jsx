import React, { useEffect, useState } from 'react';
import { CircleHelp, Copy, KeyRound, RefreshCw, RotateCcw, Trash2 } from 'lucide-react';
import {
  createProjectMcpToken,
  getProjectMcpRuntimeStatus,
  getSettings,
  listProjectMcpTokens,
  revokeProjectMcpToken,
  saveSettings,
  syncDefaultLlm,
  testLlm,
  testConsumer,
} from './api.js';
import AdminLlmDefaults from './AdminLlmDefaults.jsx';
import LlmPurposeTabs from './LlmPurposeTabs.jsx';
import { prettyJsonText, previewJsonText } from './llmAdvancedConfig.js';
import { copyMcpText } from './mcpClipboard.js';
import { buildModelingMcpClientConfigs } from './mcpClientConfig.js';

const SYNC_DEFAULT_HELP = '清除当前用途的个人覆盖，改用 admin 统一配置的平台默认。';
const LLM_FIELD_SUFFIXES = [
  'BaseUrl', 'Model', 'ApiKey', 'ApiKeyClear', 'ProviderClear', 'MaxTokens', 'ContextWindow',
  'ThinkingMode', 'ReasoningEffort', 'ExtraBodyJson',
];
const DEFAULT_MCP_SCOPE_OPTIONS = [
  { scope: 'ontology.read', label: 'ontology.read', description: '读取 Object、Operation 和 Rule 列表及详情。' },
  { scope: 'ontology.change.validate', label: 'ontology.change.validate', description: '只做修改预校验，不写入正式 revision。' },
  { scope: 'ontology.change.apply', label: 'ontology.change.apply', description: '执行本体模型修改，并在校验通过后直接生成新的正式 revision。' },
];

function llmFormKey(purpose, suffix) {
  return `${purpose === 'main' ? 'llm' : 'taskLlm'}${suffix}`;
}

function normalizeSettingsTab(tab, projectId) {
  const normalized = String(tab || 'llm');
  return normalized === 'mcp' && !projectId ? 'llm' : normalized;
}

export function SettingsModalOverlay({ children }) {
  return <div className="modal-ov">{children}</div>;
}

// 配置中心：LLM（建模/判分用的大模型端点）· consumer（评测目标服务）· 评测（评测库与数据源默认值）。
// “我的覆盖”按当前登录用户隔离；admin 另有平台默认作用域。api key 保存后不回显；
// 测试失败展示服务端原始错误便于排障。
export default function SettingsModal({ onClose, user, projectId, tab: tabProp = 'llm' }) {
  const [tab, setTab] = useState(() => normalizeSettingsTab(tabProp, projectId));
  const [llmScope, setLlmScope] = useState(user?.role === 'admin' ? 'platform' : 'personal');
  const [llmPurpose, setLlmPurpose] = useState('main');
  // 操作引导逐 tab 讲解时经 props 切换
  useEffect(() => { setTab(normalizeSettingsTab(tabProp, projectId)); }, [tabProp, projectId]);
  const [s, setS] = useState(null);       // GET /api/settings 原始数据
  const [form, setForm] = useState({});    // 编辑缓冲
  const [dirty, setDirty] = useState({});  // 只提交用户实际编辑的字段，避免把默认值复制成个人覆盖
  const [msg, setMsg] = useState('');
  const [saving, setSaving] = useState(false);
  const [loadErr, setLoadErr] = useState('');

  const load = () => getSettings().then((d) => {
    setLoadErr('');
    setS(d);
    setForm({
      llmBaseUrl: d.llm.baseUrl, llmModel: d.llm.model, llmMaxTokens: String(d.llm.maxTokens),
      llmContextWindow: String(d.llm.contextWindow ?? ''), llmApiKey: '',
      llmThinkingMode: d.llm.thinkingMode ?? 'default',
      llmReasoningEffort: d.llm.reasoningEffort ?? '',
      llmExtraBodyJson: prettyJsonText(d.llm.extraBodyJson ?? '{}'),
      llmApiKeyClear: false, llmProviderClear: false,
      taskLlmBaseUrl: d.taskLlm.baseUrl, taskLlmModel: d.taskLlm.model,
      taskLlmMaxTokens: String(d.taskLlm.maxTokens),
      taskLlmContextWindow: String(d.taskLlm.contextWindow ?? ''), taskLlmApiKey: '',
      taskLlmThinkingMode: d.taskLlm.thinkingMode ?? 'default',
      taskLlmReasoningEffort: d.taskLlm.reasoningEffort ?? '',
      taskLlmExtraBodyJson: prettyJsonText(d.taskLlm.extraBodyJson ?? '{}'),
      taskLlmApiKeyClear: false, taskLlmProviderClear: false,
      consumerApiUrl: d.consumerApiUrl,
      evalDbUrl: d.evalDbUrl,
      evalBindingProvider: d.evalBinding.provider, evalBindingUrl: d.evalBinding.url,
      evalBindingToken: d.evalBinding.token, evalBindingScene: d.evalBinding.scene,
    });
    setDirty({});
  }).catch((e) => setLoadErr(e.message || '配置加载失败'));
  useEffect(() => { load(); }, []);

  // Esc 关闭弹窗
  useEffect(() => {
    const onKeyDown = (e) => { if (e.key === 'Escape' && !saving) onClose(); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [saving, onClose]);

  const setValue = (key, value) => {
    setForm((current) => ({ ...current, [key]: value }));
    setDirty((current) => ({ ...current, [key]: true }));
  };
  const set = (key) => (event) => setValue(key, event.target.value);
  const save = async () => {
    setSaving(true); setMsg('');
    try {
      const llmKeys = new Set(['main', 'task'].flatMap((purpose) => (
        LLM_FIELD_SUFFIXES.map((suffix) => llmFormKey(purpose, suffix))
      )));
      const patches = [];
      const generalPatch = Object.fromEntries(Object.keys(dirty)
        .filter((key) => !llmKeys.has(key))
        .map((key) => [key, form[key]]));
      if (Object.keys(generalPatch).length) patches.push(generalPatch);

      for (const purpose of ['main', 'task']) {
        const changedSuffixes = LLM_FIELD_SUFFIXES.filter((suffix) => dirty[llmFormKey(purpose, suffix)]);
        if (!changedSuffixes.length) continue;
        const patch = { llmPurpose: purpose };
        for (const suffix of changedSuffixes) {
          patch[`llm${suffix}`] = form[llmFormKey(purpose, suffix)];
        }
        const providerChanged = ['BaseUrl', 'Model', 'ApiKey', 'ApiKeyClear', 'ProviderClear']
          .some((suffix) => dirty[llmFormKey(purpose, suffix)]);
        const advancedChanged = ['ThinkingMode', 'ReasoningEffort', 'ExtraBodyJson']
          .some((suffix) => dirty[llmFormKey(purpose, suffix)]);
        // 每个用途的个人 provider 都是完整作用域；修改其中任一项时同时提交当前端点与模型。
        if (form[llmFormKey(purpose, 'ProviderClear')]) {
          delete patch.llmBaseUrl;
          delete patch.llmModel;
          delete patch.llmApiKey;
          delete patch.llmApiKeyClear;
        } else if (providerChanged) {
          patch.llmBaseUrl = form[llmFormKey(purpose, 'BaseUrl')];
          patch.llmModel = form[llmFormKey(purpose, 'Model')];
        }
        if (advancedChanged) {
          patch.llmThinkingMode = form[llmFormKey(purpose, 'ThinkingMode')];
          patch.llmReasoningEffort = form[llmFormKey(purpose, 'ReasoningEffort')];
          patch.llmExtraBodyJson = form[llmFormKey(purpose, 'ExtraBodyJson')];
        }
        patches.push(patch);
      }

      for (const patch of patches) await saveSettings(patch);
      setMsg('✓ 已保存');
      await load();
    }
    catch (e) { setMsg(`保存失败：${e.message}`); }
    finally { setSaving(false); }
  };
  const modalWidth = tab === 'mcp' ? 760 : 640;

  if (!s) {
    return (
      <SettingsModalOverlay>
        <div className="modal" style={{ width: modalWidth }}><h3>配置中心</h3>
          {loadErr
            ? <div className="empty">配置加载失败：{loadErr} <button type="button" className="btn" onClick={load}>重试</button></div>
            : <div className="empty">加载中…</div>}
          <div className="foot"><button type="button" className="btn" onClick={onClose}>关闭</button></div>
        </div>
      </SettingsModalOverlay>
    );
  }

  return (
    <SettingsModalOverlay>
      <div className="modal" style={{ width: modalWidth }}>
        <h3>配置中心</h3>
        <div className="cfg-tabs">
          <button className={tab === 'llm' ? 'on' : ''} onClick={() => setTab('llm')}>大模型（LLM）</button>
          <button className={tab === 'consumer' ? 'on' : ''} onClick={() => setTab('consumer')}>consumer 服务</button>
          <button className={tab === 'eval' ? 'on' : ''} onClick={() => setTab('eval')}>评测数据</button>
          {projectId && <button className={tab === 'mcp' ? 'on' : ''} onClick={() => setTab('mcp')}>MCP 接入</button>}
        </div>

        {tab === 'llm' && <div data-tour="cfg-llm"><LlmTab
          form={form} set={set} setValue={setValue}
          config={llmPurpose === 'main' ? s.llm : s.taskLlm}
          purpose={llmPurpose} onPurposeChange={setLlmPurpose} onDefaultSynced={load}
          isAdmin={user?.role === 'admin'} onPlatformChanged={load}
          scope={llmScope} onScopeChange={setLlmScope}
        /></div>}
        {tab === 'consumer' && <div data-tour="cfg-consumer"><ConsumerTab form={form} set={set} authIssuerUrl={s.authIssuerUrl} source={s.consumerApiUrlSource} /></div>}
        {tab === 'eval' && <div data-tour="cfg-eval"><EvalTab form={form} set={set} /></div>}
        {tab === 'mcp' && projectId && <div data-tour="cfg-mcp"><McpTab projectId={projectId} /></div>}

        {msg && <div style={{ fontSize: 12.5, color: msg.startsWith('✓') ? 'var(--good, #0e6b4a)' : 'var(--crit)', marginTop: 4 }}>{msg}</div>}
        <div className="foot">
          <button className="btn" onClick={onClose}>关闭</button>
          {tab !== 'mcp' && !(tab === 'llm' && user?.role === 'admin' && llmScope === 'platform') && (
            <button className="btn btn-p" disabled={saving || Object.keys(dirty).length === 0} onClick={save}>
              {saving ? '保存中…' : '保存修改'}
            </button>
          )}
        </div>
      </div>
    </SettingsModalOverlay>
  );
}

function LlmTab({
  form, set, setValue, config, purpose, onPurposeChange, onDefaultSynced,
  isAdmin, onPlatformChanged, scope, onScopeChange,
}) {
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [result, setResult] = useState(null);
  const [syncResult, setSyncResult] = useState(null);
  const field = (suffix) => form[llmFormKey(purpose, suffix)];
  const setField = (suffix) => set(llmFormKey(purpose, suffix));
  const setFieldValue = (suffix, value) => setValue(llmFormKey(purpose, suffix), value);

  useEffect(() => {
    setResult(null);
    setSyncResult(null);
  }, [purpose]);

  const test = async () => {
    setTesting(true); setResult(null);
    try {
      setResult(await testLlm({
        purpose,
        baseUrl: field('BaseUrl'),
        apiKey: field('ApiKey') || undefined,
        model: field('Model'),
        thinkingMode: field('ThinkingMode'),
        reasoningEffort: field('ReasoningEffort'),
        extraBodyJson: field('ExtraBodyJson'),
      }));
    } catch (e) { setResult({ ok: false, error: e.message }); }
    finally { setTesting(false); }
  };

  const syncDefault = async () => {
    setSyncing(true); setSyncResult(null); setResult(null);
    try {
      await syncDefaultLlm(purpose);
      await onDefaultSynced();
      setSyncResult({ ok: true, message: `已使用平台默认${purpose === 'main' ? '主模型' : '后台任务模型'}配置` });
    } catch (e) {
      setSyncResult({ ok: false, message: `同步失败：${e.message}` });
    } finally { setSyncing(false); }
  };

  return (
    <div>
      {isAdmin && (
        <div className="llm-scope-tabs" role="tablist" aria-label="LLM 配置作用域">
          <button type="button" className={scope === 'platform' ? 'on' : ''} onClick={() => onScopeChange('platform')}>平台默认</button>
          <button type="button" className={scope === 'personal' ? 'on' : ''} onClick={() => onScopeChange('personal')}>我的覆盖</button>
        </div>
      )}
      {(() => { if (isAdmin && scope === 'platform') return <AdminLlmDefaults onChanged={onPlatformChanged} />; return <>
      <LlmPurposeTabs purpose={purpose} onChange={onPurposeChange} ariaLabel="个人 LLM 用途" />
      <div className="field">
        <label>Base URL <span className="lbl-src">{{ user: '个人配置', platform_default: '平台默认', default_snapshot: '默认配置快照', main: '使用主模型', settings: '兼容设置', env: '部署配置' }[config.source]}</span></label>
        <input value={field('BaseUrl') ?? ''} onChange={(event) => {
          setField('BaseUrl')(event);
          setFieldValue('ProviderClear', false);
        }} placeholder="https://api.example.com/v1（填 faux 进入演示模式）" />
      </div>
      <div className="field">
        <label>模型名</label>
        <input value={field('Model') ?? ''} onChange={(event) => {
          setField('Model')(event);
          setFieldValue('ProviderClear', false);
        }} placeholder="例如 deepseek-chat" />
      </div>
      <div className="field">
        <label>API Key {(() => { if (config.apiKeySet) return <span className="tag tag-ok" style={{ marginLeft: 6 }}>已配置</span>; return <span className="tag tag-draft" style={{ marginLeft: 6 }}>未配置</span>; })()}</label>
        <input type="password" value={field('ApiKey') ?? ''} onChange={(event) => {
          setField('ApiKey')(event);
          setFieldValue('ApiKeyClear', false);
          setFieldValue('ProviderClear', false);
        }}
          placeholder={(() => { if (config.personalApiKeySet) return '留空 = 保持个人 Key 不变'; return ((() => { if (config.apiKeySet) return '已有 Key 不会复制到个人覆盖'; return '可选 API Key'; })()); })()} autoComplete="new-password" />
        {config.personalApiKeySet && (
          <button type="button" className="btn cfg-key-clear"
            disabled={field('ApiKeyClear') === true}
            onClick={() => { setFieldValue('ApiKey', ''); setFieldValue('ApiKeyClear', true); }}>
            {(() => { if (field('ApiKeyClear')) return '保存后清除个人 Key'; return '清除个人 Key'; })()}
          </button>
        )}
      </div>
      <div className="field">
        <label>单次输出上限</label>
        <input value={field('MaxTokens') ?? ''} onChange={setField('MaxTokens')} placeholder="128000" style={{ width: 220 }} />
      </div>
      <div className="field">
        <label>上下文窗口（输入 + 输出总 token）</label>
        <input inputMode="numeric" value={field('ContextWindow') ?? ''} onChange={setField('ContextWindow')} placeholder="按模型 API 实际上限填写，例如 1000000" style={{ width: 300 }} />
      </div>
      <div className="cfg-advanced-grid">
        <div className="field">
          <label>思考模式</label>
          <select value={field('ThinkingMode') ?? 'default'} onChange={setField('ThinkingMode')}>
            <option value="default">默认</option>
            <option value="enabled">开启</option>
            <option value="disabled">关闭</option>
          </select>
        </div>
        <div className="field">
          <label>思考强度</label>
          <select value={field('ReasoningEffort') ?? ''} disabled={field('ThinkingMode') !== 'enabled'} onChange={setField('ReasoningEffort')}>
            <option value="">默认</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
            <option value="max">max</option>
          </select>
        </div>
      </div>
      <div className="field">
        <label>自定义请求参数 JSON</label>
        <textarea className="cfg-json-input" value={field('ExtraBodyJson') ?? '{}'}
          onChange={setField('ExtraBodyJson')} spellCheck={false} rows={5}
          placeholder={'{"custom_param": true}'} />
      </div>
      <div className="field">
        <label>请求报文预览</label>
        <pre className="cfg-request-preview">{previewJsonText({
          model: field('Model'),
          thinkingMode: field('ThinkingMode'),
          reasoningEffort: field('ReasoningEffort'),
          extraBodyJson: field('ExtraBodyJson'),
        })}</pre>
      </div>
      <div className="cfg-llm-actions">
        <button className="btn" disabled={testing || syncing} onClick={test}>{(() => { if (testing) return '测试中…'; return '测试有效性'; })()}</button>
        <span className="cfg-sync-default-group">
          <button type="button" className="btn cfg-sync-default"
            disabled={!config.platformDefaultAvailable || syncing || testing}
            onClick={syncDefault}
            title={(() => { if (config.platformDefaultAvailable) return '使用对应的平台默认并清除当前用途的个人 LLM 覆盖'; return undefined; })()}>
            <RefreshCw size={14} aria-hidden="true" />
            {(() => { if (syncing) return '切换中…'; return '使用平台默认配置'; })()}
          </button>
          <span className="cfg-sync-help" tabIndex={0} aria-label={SYNC_DEFAULT_HELP} data-tooltip={SYNC_DEFAULT_HELP}>
            <CircleHelp size={16} aria-hidden="true" />
          </span>
        </span>
        {(config.source === 'user' || (purpose === 'task' && config.source === 'default_snapshot')) && (
          <button type="button" className="btn cfg-reset-personal" disabled={field('ProviderClear') === true}
            onClick={() => {
              setFieldValue('ProviderClear', true);
              setFieldValue('ApiKey', '');
              setFieldValue('ApiKeyClear', true);
              setFieldValue('MaxTokens', '');
              setFieldValue('ContextWindow', '');
              setFieldValue('ThinkingMode', 'default');
              setFieldValue('ReasoningEffort', '');
              setFieldValue('ExtraBodyJson', '{}');
            }}>
            <RotateCcw size={14} aria-hidden="true" />
            {(() => { if (field('ProviderClear')) return `保存后${(() => { if (purpose === 'task') return '使用主模型'; return '清除个人覆盖'; })()}`; return ((() => { if (purpose === 'task') return '清除并使用主模型'; return '清除个人覆盖'; })()); })()}
          </button>
        )}
      </div>
      {syncResult && (
        <div className={`cfg-test ${(() => { if (syncResult.ok) return 'ok'; return 'bad'; })()}`}>{syncResult.message}</div>
      )}
      {result && (
        <div className={`cfg-test ${(() => { if (result.ok) return 'ok'; return 'bad'; })()}`}>
          {(() => { if (result.ok) return <>✅ 有效{(() => { if (result.latencyMs != null) return `（${result.latencyMs}ms）`; return ''; })()}{(() => { if (result.detail) return ` · ${result.detail}`; return ''; })()}</>; return <>
                <div>❌ 无效{(() => { if (result.status) return `（HTTP ${result.status}）`; return ''; })()}{(() => { if (result.latencyMs != null) return ` · ${result.latencyMs}ms`; return ''; })()}</div>
                <pre className="cfg-raw">{result.error}</pre>
              </>; })()}
        </div>
      )}
      </>; })()}
    </div>
  );
}

function ConsumerTab({ form, set, authIssuerUrl, source }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);
  const test = async () => {
    setTesting(true); setResult(null);
    try { setResult(await testConsumer(form.consumerApiUrl)); }
    catch (e) { setResult({ detail: e.message }); }
    finally { setTesting(false); }
  };
  return (
    <div>
      <div className="field">
        <label>评测用 consumer API 地址（评测将导入知识包并运行用例到该服务）</label>
        <div style={{ display: 'flex', gap: 8 }}>
          <input value={form.consumerApiUrl ?? ''} onChange={set('consumerApiUrl')} placeholder="http://consumer-host:5055" style={{ flex: 1 }} />
          <button className="btn" disabled={testing || !form.consumerApiUrl} onClick={test}>{testing ? '测试中…' : '测试连接'}</button>
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 6 }}>
          当前来源：{{ settings: '页面配置', env: '环境变量' }[source]}
        </div>
      </div>
      {result && (
        <div className="cfg-test" style={{ marginBottom: 12 }}>
          <div>{result.reachable ? '✅ 服务可达（/health）' : '❌ 服务不可达'}</div>
          <div>{result.authOk ? '✅ 认证通过（当前账号在该 consumer 有效）' : '❌ 认证未通过'}</div>
          {result.modelReady != null && (
            <div>{result.modelReady ? '✅ 模型已就绪（可运行评测）' : '⚠️ 模型未就绪（评测将失败）'}</div>
          )}
          {result.detail && <div style={{ color: 'var(--muted)', marginTop: 4 }}>{result.detail}</div>}
        </div>
      )}
      <div className="field">
        <label>登录认证服务 auth-server（部署配置，只读——避免把自己锁出去）</label>
        <input value={authIssuerUrl} disabled style={{ background: '#f7f6f3', color: 'var(--muted)' }} />
      </div>
    </div>
  );
}

function EvalTab({ form, set }) {
  return (
    <div>
      <div className="field">
        <label>评测库 MySQL 地址</label>
        <input value={form.evalDbUrl ?? ''} onChange={set('evalDbUrl')} placeholder="mysql://user:pass@host:3306（Excel 数据自动落库；留空跳过）" />
      </div>
      <div style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--ink2)', margin: '14px 0 8px' }}>评测数据源默认值</div>
      <div className="field">
        <label>provider</label>
        <input value={form.evalBindingProvider ?? ''} onChange={set('evalBindingProvider')} placeholder="nl2sql_template" style={{ width: 240 }} />
      </div>
      <div className="field">
        <label>数据服务 URL</label>
        <input value={form.evalBindingUrl ?? ''} onChange={set('evalBindingUrl')} placeholder="http://localhost:5600" />
      </div>
      <div className="field">
        <label>token</label>
        <input value={form.evalBindingToken ?? ''} onChange={set('evalBindingToken')} />
      </div>
      <div className="field">
        <label>scene</label>
        <input value={form.evalBindingScene ?? ''} onChange={set('evalBindingScene')} placeholder="留空 = 本项目评测库 mw_proj_*" />
      </div>
    </div>
  );
}

function McpTab({ projectId }) {
  const [runtime, setRuntime] = useState(null);
  const [tokens, setTokens] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revokingId, setRevokingId] = useState('');
  const [loadErr, setLoadErr] = useState('');
  const [tokenLoadErr, setTokenLoadErr] = useState('');
  const [canManageTokens, setCanManageTokens] = useState(false);
  const [msg, setMsg] = useState('');
  const [form, setForm] = useState({
    name: '',
    expiresInSeconds: '',
    scopes: ['ontology.read'],
  });
  const [issued, setIssued] = useState(null);

  const load = async (mode = 'initial') => {
    if (mode === 'initial') setLoading(true);
    else setRefreshing(true);
    try {
      const [statusResult, tokenResult] = await Promise.allSettled([
        getProjectMcpRuntimeStatus(projectId),
        listProjectMcpTokens(projectId),
      ]);
      if (statusResult.status === 'rejected') throw statusResult.reason;
      setRuntime(statusResult.value);
      if (tokenResult.status === 'fulfilled') {
        setTokens(Array.isArray(tokenResult.value.tokens) ? tokenResult.value.tokens : []);
        setCanManageTokens(true);
        setTokenLoadErr('');
      } else {
        setTokens([]);
        setIssued(null);
        setCanManageTokens(false);
        setTokenLoadErr(tokenResult.reason?.message || '当前账号无权管理 MCP Token');
      }
      setLoadErr('');
    } catch (e) {
      setLoadErr(e.message || 'MCP 配置加载失败');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    setRuntime(null);
    setTokens([]);
    setIssued(null);
    setMsg('');
    setLoadErr('');
    setTokenLoadErr('');
    setCanManageTokens(false);
    setForm({
      name: '',
      expiresInSeconds: '',
      scopes: ['ontology.read'],
    });
    void load('initial');
  }, [projectId]);

  const scopeOptions = Array.isArray(runtime?.scope_catalog) && runtime.scope_catalog.length
    ? runtime.scope_catalog.map((item) => ({
        scope: item.scope,
        label: item.scope,
        description: item.description,
      }))
    : DEFAULT_MCP_SCOPE_OPTIONS;

  const toggleScope = (scope) => {
    setForm((current) => ({
      ...current,
      scopes: current.scopes.includes(scope)
        ? current.scopes.filter((item) => item !== scope)
        : [...current.scopes, scope],
    }));
  };

  const copyText = async (value, label) => {
    const result = await copyMcpText(value);
    if (result.ok) {
      setMsg(`✓ 已复制${label}`);
    } else {
      setMsg(`复制失败：${result.error}`);
    }
  };

  const clientConfigs = buildModelingMcpClientConfigs(
    runtime,
    issued?.token || '<MCP_TOKEN>',
    issued ? issued.token_summary?.scopes || [] : undefined,
  );

  const createToken = async () => {
    if (!canManageTokens) {
      setMsg('当前账号无权创建 MCP Token');
      return;
    }
    if (!form.name.trim()) {
      setMsg('token 名称必填');
      return;
    }
    if (!form.scopes.length) {
      setMsg('至少选择一个 scope');
      return;
    }
    setCreating(true);
    setMsg('');
    try {
      const draft = {
        name: form.name.trim(),
        scopes: form.scopes,
        ...(form.expiresInSeconds === 'never'
          ? { expires_in_seconds: null }
          : form.expiresInSeconds.trim()
            ? { expires_in_seconds: Number(form.expiresInSeconds) }
            : {}),
      };
      const created = await createProjectMcpToken(projectId, draft);
      setIssued(created);
      setTokens((current) => [created.token_summary, ...current.filter((item) => item.id !== created.token_summary.id)]);
      setForm((current) => ({ ...current, name: '' }));
      setMsg('✓ 已创建新 token，明文只会展示这一次');
    } catch (e) {
      setMsg(`创建失败：${e.message}`);
    } finally {
      setCreating(false);
    }
  };

  const revokeToken = async (tokenId, tokenName) => {
    if (!window.confirm(`确认吊销 token「${tokenName}」？吊销后外部 Agent 将无法继续调用。`)) return;
    setRevokingId(tokenId);
    setMsg('');
    try {
      await revokeProjectMcpToken(projectId, tokenId);
      setTokens((current) => current.filter((item) => item.id !== tokenId));
      setMsg('✓ token 已吊销');
    } catch (e) {
      setMsg(`吊销失败：${e.message}`);
    } finally {
      setRevokingId('');
    }
  };

  if (loading && !runtime) {
    return <div className="empty">MCP 配置加载中…</div>;
  }
  if (!runtime) {
    return (
      <div className="empty">
        MCP 配置加载失败：{loadErr || '未知错误'}
        <button type="button" className="btn" onClick={() => void load('initial')}>重试</button>
      </div>
    );
  }

  const loopbackBaseUrl = /:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::|\/|$)/u.test(String(runtime.base_url || ''));

  return (
    <div className="cfg-mcp">
      <div className="cfg-mcp-head">
        <div>
          <div className="cfg-mcp-title">项目级 MCP 接入</div>
          <div className="cfg-mcp-sub">当前项目独立 endpoint、token 与外部 Agent 接入信息。</div>
        </div>
        <button type="button" className="btn" disabled={refreshing || creating || Boolean(revokingId)} onClick={() => void load('refresh')}>
          {refreshing ? '刷新中…' : '刷新'}
        </button>
      </div>

      {loadErr && <div className="cfg-test bad">最近一次刷新失败：{loadErr}</div>}
      {tokenLoadErr && <div className="cfg-mcp-note">连接信息可正常查看；Token 仅对项目负责人或建模成员开放管理。</div>}
      {msg && <div className={`cfg-test ${msg.startsWith('✓') ? 'ok' : 'bad'}`}>{msg}</div>}
      {loopbackBaseUrl && (
        <div className="cfg-mcp-note">
          当前返回的 `base_url` 仍是本机回环地址。要让其他机器接入，需要把 `MODELING_MCP_PUBLIC_BASE_URL` 配成可访问域名或 IP。
        </div>
      )}

      <div className="cfg-mcp-section">
        <div className="cfg-mcp-summary">
          <span className={`tag ${runtime.running ? 'tag-ok' : 'tag-draft'}`}>
            {runtime.running ? '运行中' : '未运行'}
          </span>
          <span className="tag tag-lang">{runtime.transport}</span>
          <span className="tag tag-lang">{runtime.server_name}@{runtime.server_version}</span>
          <span className="tag tag-lang">{runtime.protocol_versions.join(' / ')}</span>
        </div>
        {!runtime.running && <div className="cfg-test bad">{runtime.detail || 'MCP 服务当前不可用'}</div>}
        <div className="field">
          <label>endpoint ID</label>
          <div className="cfg-mcp-copy-row">
            <input value={runtime.endpoint_id ?? ''} readOnly />
            <button type="button" className="btn" onClick={() => void copyText(runtime.endpoint_id, ' endpoint ID')}>
              <Copy size={14} aria-hidden="true" />
              复制
            </button>
          </div>
        </div>
        <div className="cfg-mcp-url-list">
          {runtime.urls.map((item) => {
            const toolset = runtime.toolsets.find((candidate) => candidate.name === item.toolset);
            return (
              <div className="cfg-mcp-url-item" key={`${item.toolset}:${item.transport}`}>
                <div className="cfg-mcp-url-head">
                  <strong>{item.toolset}</strong>
                  <span className="tag tag-lang">{item.transport}</span>
                </div>
                <div className="cfg-mcp-copy-row">
                  <input value={item.url} readOnly />
                  <button type="button" className="btn" onClick={() => void copyText(item.url, ` ${item.toolset} URL`)}>
                    <Copy size={14} aria-hidden="true" />
                    复制
                  </button>
                </div>
                <div className="cfg-mcp-meta">
                  {toolset?.tools?.length ? `工具：${toolset.tools.join('、')}` : '当前没有可用工具'}
                </div>
                <div className="cfg-mcp-meta">
                  该 toolset {toolset?.scope_requirement === 'any' ? '接受任一 scope' : '需要 scope'}：
                  {Array.isArray(toolset?.accepted_scopes) && toolset.accepted_scopes.length
                    ? toolset.accepted_scopes.join('、')
                    : Array.isArray(toolset?.required_scopes) && toolset.required_scopes.length
                      ? toolset.required_scopes.join('、')
                    : '无'}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {canManageTokens && <div className="cfg-mcp-section">
        <div className="cfg-mcp-title-row">
          <div className="cfg-mcp-title-sm">新建 Token</div>
          <div className="cfg-mcp-meta">
            默认过期 {runtime.token_policy?.default_ttl_seconds ?? 86400} 秒，限时 Token 最大 {runtime.token_policy?.max_ttl_seconds ?? 2592000} 秒；永不过期 Token 仍可随时吊销。
          </div>
        </div>
        <div className="field">
          <label>token 名称</label>
          <input value={form.name} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} placeholder="例如：业务 Agent 读写联调" />
        </div>
        <div className="field">
          <label>有效期</label>
          <select
            value={form.expiresInSeconds}
            onChange={(event) => setForm((current) => ({ ...current, expiresInSeconds: event.target.value }))}
            style={{ width: 220 }}
          >
            <option value="">默认有效期</option>
            <option value="3600">1 小时</option>
            <option value="86400">1 天</option>
            <option value="604800">7 天</option>
            <option value="2592000">30 天</option>
            {runtime.token_policy?.supports_non_expiring !== false && <option value="never">永不过期</option>}
          </select>
        </div>
        <div className="field">
          <label>scope</label>
          <div className="cfg-mcp-scopes">
            {scopeOptions.map((option) => (
              <label key={option.scope} className="cfg-mcp-scope-item">
                <input
                  type="checkbox"
                  checked={form.scopes.includes(option.scope)}
                  onChange={() => toggleScope(option.scope)}
                />
                <span>
                  <strong>{option.label}</strong>
                  <small>{option.description}</small>
                </span>
              </label>
            ))}
          </div>
        </div>
        <div className="cfg-llm-actions">
          <button type="button" className="btn btn-p" disabled={creating || !form.name.trim() || !form.scopes.length} onClick={createToken}>
            <KeyRound size={14} aria-hidden="true" />
            {creating ? '创建中…' : '创建 Token'}
          </button>
        </div>
      </div>}

      {canManageTokens && issued && (
        <div className="cfg-mcp-section">
          <div className="cfg-mcp-title-row">
            <div className="cfg-mcp-title-sm">刚创建的 Token</div>
            <div className="cfg-mcp-meta">明文只回显一次，关闭后无法再次查看。</div>
          </div>
          <div className="cfg-mcp-copy-row">
            <input value={issued.token} readOnly />
            <button type="button" className="btn" onClick={() => void copyText(issued.token, ' token')}>
              <Copy size={14} aria-hidden="true" />
              复制
            </button>
          </div>
          <div className="cfg-mcp-meta">token ID：{issued.token_summary?.id}</div>
        </div>
      )}

      {(clientConfigs.read || clientConfigs.change) && (
        <div className="cfg-mcp-section">
          <div className="cfg-mcp-title-row">
            <div className="cfg-mcp-title-sm">客户端 JSON 配置</div>
            <div className="cfg-mcp-meta">
              {issued
                ? '已按本次 Token 的 scope 生成可用配置；明文仅在当前页面会话中保留。'
                : 'Token 使用 <MCP_TOKEN> 占位符，请替换为具备对应 scope 的 Token。'}
            </div>
          </div>
          <div className="cfg-mcp-client-configs">
            {[
              ['read', 'Read 配置', clientConfigs.read],
              ['change', 'Change 配置', clientConfigs.change],
              ['combined', '完整配置', clientConfigs.combined],
            ].map(([key, label, value]) => value && (
              <div className="cfg-mcp-config-item" key={key}>
                <div className="cfg-mcp-url-head"><strong>{label}</strong></div>
                <pre>{value}</pre>
                <button type="button" className="btn" onClick={() => void copyText(value, ` ${label}`)}>
                  <Copy size={14} aria-hidden="true" />
                  复制{label}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {canManageTokens && <div className="cfg-mcp-section">
        <div className="cfg-mcp-title-row">
          <div className="cfg-mcp-title-sm">现有 Token</div>
          <div className="cfg-mcp-meta">仅展示未吊销 token；调用成功后会更新最近使用时间。</div>
        </div>
        {tokens.length ? (
          <div className="cfg-mcp-token-list">
            {tokens.map((token) => (
              <div className="cfg-mcp-token-row" key={token.id}>
                <div className="cfg-mcp-token-main">
                  <div className="cfg-mcp-token-name">{token.name}</div>
                  <div className="cfg-mcp-meta">ID：{token.id}</div>
                  <div className="cfg-mcp-meta">创建人：{token.subjectUsername} · 签发人：{token.issuedBy}</div>
                  <div className="cfg-mcp-meta">创建时间：{formatMcpTime(token.createdAt)} · 过期时间：{formatMcpTime(token.expiresAt)}</div>
                  <div className="cfg-mcp-meta">最近使用：{token.lastUsedAt ? formatMcpTime(token.lastUsedAt) : '尚未使用'}</div>
                  <div className="cfg-mcp-token-scopes">
                    {token.scopes.map((scope) => <span key={scope} className="tag tag-lang">{scope}</span>)}
                  </div>
                </div>
                <div className="cfg-mcp-token-actions">
                  <button
                    type="button"
                    className="btn"
                    disabled={creating || refreshing || revokingId === token.id}
                    onClick={() => void revokeToken(token.id, token.name)}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    {revokingId === token.id ? '吊销中…' : '吊销'}
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : <div className="empty">当前还没有可用 MCP token</div>}
      </div>}

      <div className="cfg-mcp-note">
        外部 Agent 连接顺序建议固定为：server/discover → tools/list → tools/call。旧客户端如果仍走 initialize，当前服务也兼容。
      </div>
    </div>
  );
}

function formatMcpTime(value) {
  const input = String(value || '').trim();
  if (!input) return '永不过期';
  const time = new Date(input);
  if (Number.isNaN(time.getTime())) return input;
  return time.toLocaleString('zh-CN', { hour12: false });
}
