import { useEffect, useState } from 'react';
import { getSettings, saveSettings, testLlm, testConsumer } from './api.js';

// 配置中心：LLM（建模/判分用的大模型端点）· consumer（评测目标服务）· 评测（评测库与数据连接默认值）。
// 优先级统一为：页面配置 > 环境变量。api key 保存后不回显；测试失败展示服务端原始错误便于排障。
export default function SettingsModal({ onClose }) {
  const [tab, setTab] = useState('llm');
  const [s, setS] = useState(null);       // GET /api/settings 原始数据
  const [form, setForm] = useState({});    // 编辑缓冲
  const [msg, setMsg] = useState('');
  const [saving, setSaving] = useState(false);

  const load = () => getSettings().then((d) => {
    setS(d);
    setForm({
      llmBaseUrl: d.llm.baseUrl, llmModel: d.llm.model, llmMaxTokens: String(d.llm.maxTokens), llmApiKey: '',
      consumerApiUrl: d.consumerApiUrl,
      evalDbUrl: d.evalDbUrl,
      evalBindingProvider: d.evalBinding.provider, evalBindingUrl: d.evalBinding.url,
      evalBindingToken: d.evalBinding.token, evalBindingScene: d.evalBinding.scene,
    });
  });
  useEffect(() => { load(); }, []);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const save = async () => {
    setSaving(true); setMsg('');
    try { await saveSettings(form); setMsg('✓ 已保存'); await load(); }
    catch (e) { setMsg(`保存失败：${e.message}`); }
    finally { setSaving(false); }
  };

  if (!s) {
    return (
      <div className="modal-ov" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
        <div className="modal" style={{ width: 640 }}><h3>配置中心</h3><div className="empty">加载中…</div></div>
      </div>
    );
  }

  return (
    <div className="modal-ov" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={{ width: 640 }}>
        <h3>配置中心</h3>
        <div className="cfg-tabs">
          <button className={tab === 'llm' ? 'on' : ''} onClick={() => setTab('llm')}>大模型（LLM）</button>
          <button className={tab === 'consumer' ? 'on' : ''} onClick={() => setTab('consumer')}>consumer 服务</button>
          <button className={tab === 'eval' ? 'on' : ''} onClick={() => setTab('eval')}>评测数据</button>
        </div>

        {tab === 'llm' && <LlmTab form={form} set={set} apiKeySet={s.llm.apiKeySet} source={s.llm.source} />}
        {tab === 'consumer' && <ConsumerTab form={form} set={set} authIssuerUrl={s.authIssuerUrl} source={s.consumerApiUrlSource} />}
        {tab === 'eval' && <EvalTab form={form} set={set} />}

        {msg && <div style={{ fontSize: 12.5, color: msg.startsWith('✓') ? 'var(--good, #0e6b4a)' : 'var(--crit)', marginTop: 4 }}>{msg}</div>}
        <div className="foot">
          <button className="btn" onClick={onClose}>关闭</button>
          <button className="btn btn-p" disabled={saving} onClick={save}>{saving ? '保存中…' : '保存全部'}</button>
        </div>
      </div>
    </div>
  );
}

function LlmTab({ form, set, apiKeySet, source }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);

  const test = async () => {
    setTesting(true); setResult(null);
    try {
      setResult(await testLlm({ baseUrl: form.llmBaseUrl, apiKey: form.llmApiKey || undefined, model: form.llmModel }));
    } catch (e) { setResult({ ok: false, error: e.message }); }
    finally { setTesting(false); }
  };

  return (
    <div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
        建模 agent、评测 Judge 与优化器共用此配置。当前来源：{source === 'settings' ? '页面配置' : '环境变量'}。
        填 <code>faux</code> 可进入演示模式（无需 key）。
      </div>
      <div className="field">
        <label>Base URL（OpenAI-compatible，一般以 /v1 结尾）</label>
        <input value={form.llmBaseUrl ?? ''} onChange={set('llmBaseUrl')} placeholder="https://api.example.com/v1" />
      </div>
      <div className="field">
        <label>模型名</label>
        <input value={form.llmModel ?? ''} onChange={set('llmModel')} placeholder="例如 deepseek-chat" />
      </div>
      <div className="field">
        <label>API Key {apiKeySet ? <span className="tag tag-ok" style={{ marginLeft: 6 }}>已配置</span> : <span className="tag tag-draft" style={{ marginLeft: 6 }}>未配置</span>}</label>
        <input type="password" value={form.llmApiKey ?? ''} onChange={set('llmApiKey')}
          placeholder={apiKeySet ? '留空 = 保持现有 key 不变' : '粘贴 API Key'} autoComplete="new-password" />
      </div>
      <div className="field">
        <label>单次输出上限（maxTokens，规格大时建议 ≥16384）</label>
        <input value={form.llmMaxTokens ?? ''} onChange={set('llmMaxTokens')} placeholder="8192" style={{ width: 160 }} />
      </div>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 10 }}>
        <button className="btn" disabled={testing} onClick={test}>{testing ? '测试中…' : '测试有效性'}</button>
        <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>发送一次最小请求验证 url / key / 模型名</span>
      </div>
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
          当前来源：{{ settings: '页面配置', env: '环境变量', issuer: '默认（登录 issuer）' }[source]}
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
        <label>登录认证 issuer（部署配置，只读——避免把自己锁出去）</label>
        <input value={authIssuerUrl} disabled style={{ background: '#f7f6f3', color: 'var(--muted)' }} />
      </div>
    </div>
  );
}

function EvalTab({ form, set }) {
  return (
    <div>
      <div className="field">
        <label>评测库 MySQL 地址（上传的 Excel 全量行会自动落库到 mw_proj_* 供数据服务查询；留空则跳过落库）</label>
        <input value={form.evalDbUrl ?? ''} onChange={set('evalDbUrl')} placeholder="mysql://user:pass@host:3306" />
      </div>
      <div style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--ink2)', margin: '14px 0 8px' }}>评测数据连接默认值（评测运行面板自动预填）</div>
      <div className="field">
        <label>provider</label>
        <input value={form.evalBindingProvider ?? ''} onChange={set('evalBindingProvider')} placeholder="nl2sql_template" style={{ width: 240 }} />
      </div>
      <div className="field">
        <label>数据服务 URL（如本地 NL2SQL 桥接服务）</label>
        <input value={form.evalBindingUrl ?? ''} onChange={set('evalBindingUrl')} placeholder="http://localhost:5600" />
      </div>
      <div className="field">
        <label>token</label>
        <input value={form.evalBindingToken ?? ''} onChange={set('evalBindingToken')} placeholder="数据服务的访问 token" />
      </div>
      <div className="field">
        <label>scene（数据源名；留空则评测时默认用本项目落库 schema mw_proj_*）</label>
        <input value={form.evalBindingScene ?? ''} onChange={set('evalBindingScene')} placeholder="留空 = 项目评测库" />
      </div>
    </div>
  );
}
