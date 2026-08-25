import React, { useEffect, useState } from 'react';
import { CircleHelp, RefreshCw, RotateCcw } from 'lucide-react';
import { getSettings, saveSettings, syncDefaultLlm, testLlm, testConsumer } from './api.js';
import AdminLlmDefaults from './AdminLlmDefaults.jsx';
import LlmPurposeTabs from './LlmPurposeTabs.jsx';

const SYNC_DEFAULT_HELP = '只有 admin 账号设置了默认 LLM 配置后，才可使用。';
const LLM_FIELD_SUFFIXES = [
  'BaseUrl', 'Model', 'ApiKey', 'ApiKeyClear', 'ProviderClear', 'MaxTokens', 'ContextWindow',
];

function llmFormKey(purpose, suffix) {
  return `${purpose === 'main' ? 'llm' : 'taskLlm'}${suffix}`;
}

export function SettingsModalOverlay({ children }) {
  return <div className="modal-ov">{children}</div>;
}

// 配置中心：LLM（建模/判分用的大模型端点）· consumer（评测目标服务）· 评测（评测库与数据源默认值）。
// “我的覆盖”按当前登录用户隔离；admin 另有平台默认作用域。api key 保存后不回显；
// 测试失败展示服务端原始错误便于排障。
export default function SettingsModal({ onClose, user, tab: tabProp = 'llm' }) {
  const [tab, setTab] = useState(tabProp);
  const [llmScope, setLlmScope] = useState(user?.role === 'admin' ? 'platform' : 'personal');
  const [llmPurpose, setLlmPurpose] = useState('main');
  // 操作引导逐 tab 讲解时经 props 切换
  useEffect(() => { setTab(tabProp); }, [tabProp]);
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
      llmApiKeyClear: false, llmProviderClear: false,
      taskLlmBaseUrl: d.taskLlm.baseUrl, taskLlmModel: d.taskLlm.model,
      taskLlmMaxTokens: String(d.taskLlm.maxTokens),
      taskLlmContextWindow: String(d.taskLlm.contextWindow ?? ''), taskLlmApiKey: '',
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
        patches.push(patch);
      }

      for (const patch of patches) await saveSettings(patch);
      setMsg('✓ 已保存');
      await load();
    }
    catch (e) { setMsg(`保存失败：${e.message}`); }
    finally { setSaving(false); }
  };

  if (!s) {
    return (
      <SettingsModalOverlay>
        <div className="modal" style={{ width: 640 }}><h3>配置中心</h3>
          {loadErr
            ? <div className="empty">配置加载失败：{loadErr} <button type="button" className="btn" onClick={load}>重试</button></div>
            : <div className="empty">加载中…</div>}
        </div>
      </SettingsModalOverlay>
    );
  }

  return (
    <SettingsModalOverlay>
      <div className="modal" style={{ width: 640 }}>
        <h3>配置中心</h3>
        <div className="cfg-tabs">
          <button className={tab === 'llm' ? 'on' : ''} onClick={() => setTab('llm')}>大模型（LLM）</button>
          <button className={tab === 'consumer' ? 'on' : ''} onClick={() => setTab('consumer')}>consumer 服务</button>
          <button className={tab === 'eval' ? 'on' : ''} onClick={() => setTab('eval')}>评测数据</button>
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

        {msg && <div style={{ fontSize: 12.5, color: msg.startsWith('✓') ? 'var(--good, #0e6b4a)' : 'var(--crit)', marginTop: 4 }}>{msg}</div>}
        <div className="foot">
          <button className="btn" onClick={onClose}>关闭</button>
          {!(tab === 'llm' && user?.role === 'admin' && llmScope === 'platform') && (
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
      }));
    } catch (e) { setResult({ ok: false, error: e.message }); }
    finally { setTesting(false); }
  };

  const syncDefault = async () => {
    setSyncing(true); setSyncResult(null); setResult(null);
    try {
      await syncDefaultLlm(purpose);
      await onDefaultSynced();
      setSyncResult({ ok: true, message: `已同步平台默认${purpose === 'main' ? '主模型' : '后台任务模型'}配置` });
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
      {isAdmin && scope === 'platform'
        ? <AdminLlmDefaults onChanged={onPlatformChanged} />
        : <>
      <LlmPurposeTabs purpose={purpose} onChange={onPurposeChange} ariaLabel="个人 LLM 用途" />
      <div className="field">
        <label>Base URL <span className="lbl-src">{{ user: '个人配置', default_snapshot: '默认配置快照', main: '使用主模型', settings: '兼容设置', env: '部署配置' }[config.source]}</span></label>
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
        <label>API Key {config.apiKeySet ? <span className="tag tag-ok" style={{ marginLeft: 6 }}>已配置</span> : <span className="tag tag-draft" style={{ marginLeft: 6 }}>未配置</span>}</label>
        <input type="password" value={field('ApiKey') ?? ''} onChange={(event) => {
          setField('ApiKey')(event);
          setFieldValue('ApiKeyClear', false);
          setFieldValue('ProviderClear', false);
        }}
          placeholder={config.personalApiKeySet
            ? '留空 = 保持个人 Key 不变'
            : (config.apiKeySet ? '已有 Key 不会复制到个人覆盖' : '可选 API Key')} autoComplete="new-password" />
        {config.personalApiKeySet && (
          <button type="button" className="btn cfg-key-clear"
            disabled={field('ApiKeyClear') === true}
            onClick={() => { setFieldValue('ApiKey', ''); setFieldValue('ApiKeyClear', true); }}>
            {field('ApiKeyClear') ? '保存后清除个人 Key' : '清除个人 Key'}
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
      <div className="cfg-llm-actions">
        <button className="btn" disabled={testing || syncing} onClick={test}>{testing ? '测试中…' : '测试有效性'}</button>
        <span className="cfg-sync-default-group">
          <button type="button" className="btn cfg-sync-default"
            disabled={!config.platformDefaultAvailable || syncing || testing}
            onClick={syncDefault}
            title={config.platformDefaultAvailable ? '使用对应的平台默认并清除当前用途的个人 LLM 覆盖' : undefined}>
            <RefreshCw size={14} aria-hidden="true" />
            {syncing ? '同步中…' : '同步默认 LLM 配置'}
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
            }}>
            <RotateCcw size={14} aria-hidden="true" />
            {field('ProviderClear')
              ? `保存后${purpose === 'task' ? '使用主模型' : '清除个人覆盖'}`
              : (purpose === 'task' ? '清除并使用主模型' : '清除个人覆盖')}
          </button>
        )}
      </div>
      {syncResult && (
        <div className={`cfg-test ${syncResult.ok ? 'ok' : 'bad'}`}>{syncResult.message}</div>
      )}
      {result && (
        <div className={`cfg-test ${result.ok ? 'ok' : 'bad'}`}>
          {result.ok
            ? <>✅ 有效{result.latencyMs != null ? `（${result.latencyMs}ms）` : ''}{result.detail ? ` · ${result.detail}` : ''}</>
            : <>
                <div>❌ 无效{result.status ? `（HTTP ${result.status}）` : ''}{result.latencyMs != null ? ` · ${result.latencyMs}ms` : ''}</div>
                <pre className="cfg-raw">{result.error}</pre>
              </>}
        </div>
      )}
      </>}
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
