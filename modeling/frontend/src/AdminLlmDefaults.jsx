import React, { useEffect, useState } from 'react';
import { PlugZap, RotateCcw, Save, Trash2 } from 'lucide-react';
import {
  getAdminLlmDefaults,
  saveAdminLlmDefault,
  testAdminLlmDefault,
} from './api.js';
import LlmPurposeTabs from './LlmPurposeTabs.jsx';

const EMPTY_DRAFT = {
  baseUrl: '', model: '', apiKey: '', apiKeyClear: false, maxTokens: '128000', contextWindow: '1000000',
};

function draftFrom(value) {
  return value ? {
    baseUrl: value.baseUrl,
    model: value.model,
    apiKey: '',
    apiKeyClear: false,
    maxTokens: String(value.maxTokens),
    contextWindow: String(value.contextWindow ?? 1000000),
  } : { ...EMPTY_DRAFT };
}

export default function AdminLlmDefaults({ onChanged }) {
  const [purpose, setPurpose] = useState('main');
  const [data, setData] = useState(null);
  const [drafts, setDrafts] = useState({ main: { ...EMPTY_DRAFT }, task: { ...EMPTY_DRAFT } });
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');

  const load = async () => {
    try {
      const response = await getAdminLlmDefaults();
      setData(response);
      setDrafts({
        main: draftFrom(response.defaults.main),
        task: draftFrom(response.defaults.task),
      });
      setMessage('');
    } catch (error) {
      setMessage(`加载失败：${error.message}`);
    }
  };

  useEffect(() => { void load(); }, []);

  const saved = data?.defaults?.[purpose] ?? null;
  const draft = drafts[purpose];
  const setField = (key, value) => setDrafts((current) => ({
    ...current,
    [purpose]: { ...current[purpose], [key]: value },
  }));
  const payload = () => ({
    purpose,
    baseUrl: draft.baseUrl,
    model: draft.model,
    apiKey: draft.apiKey || undefined,
    apiKeyClear: draft.apiKeyClear,
    maxTokens: draft.maxTokens,
    contextWindow: draft.contextWindow,
    expectedRevision: saved?.revision ?? 0,
  });

  const save = async () => {
    setBusy('save'); setMessage('');
    try {
      await saveAdminLlmDefault(payload());
      await load();
      setMessage('✓ 平台默认已保存');
      await onChanged?.();
    } catch (error) {
      setMessage(`保存失败：${error.message}`);
    } finally { setBusy(''); }
  };

  const test = async () => {
    setBusy('test'); setMessage('');
    try {
      const result = await testAdminLlmDefault(payload());
      setMessage(result.ok
        ? `✓ 连接有效${result.latencyMs != null ? `（${result.latencyMs}ms）` : ''}`
        : `连接失败${result.status ? `（HTTP ${result.status}）` : ''}：${result.error ?? '未知错误'}`);
    } catch (error) {
      setMessage(`测试失败：${error.message}`);
    } finally { setBusy(''); }
  };

  const reset = async () => {
    if (!saved || !window.confirm(`确认清除平台${purpose === 'main' ? '主模型默认' : '后台任务模型默认'}？`)) return;
    setBusy('reset'); setMessage('');
    try {
      await saveAdminLlmDefault({
        purpose,
        enabled: false,
        expectedRevision: saved.revision,
      });
      await load();
      setMessage('✓ 平台默认已清除');
      await onChanged?.();
    } catch (error) {
      setMessage(`清除失败：${error.message}`);
    } finally { setBusy(''); }
  };

  if (!data) return <div className="empty">{message || '加载中…'}</div>;

  return (
    <div className="admin-llm-defaults">
      <LlmPurposeTabs
        purpose={purpose}
        onChange={setPurpose}
        ariaLabel="平台 LLM 用途"
        className="admin-llm-purpose"
      />

      <div className="admin-llm-status">
        <span className={`tag ${saved ? 'tag-ok' : 'tag-draft'}`}>{saved ? `已启用 · r${saved.revision}` : '未设置'}</span>
        {saved && <span>最后由 {saved.updatedBy} 更新</span>}
        {!saved && <span>{purpose === 'task' ? '新建快照时跟随主模型' : '新用户不写入默认快照'}</span>}
      </div>

      <div className="admin-llm-policy">
        保存只更新默认模板，不会修改已有用户；仅新注册用户和主动同步的用户会采用当前值。
      </div>

      {data.encryptionKeyMode === 'managed' && (
        <div className="admin-llm-warning">当前由 DATA_DIR 自动管理加密密钥；请将自动密钥文件与 modeling.db 一同持久化和备份。</div>
      )}

      <div className="admin-llm-grid">
        <div className="field admin-llm-wide">
          <label>Base URL</label>
          <input value={draft.baseUrl} onChange={(event) => setField('baseUrl', event.target.value)} placeholder="https://api.example.com/v1 或 faux" />
        </div>
        <div className="field">
          <label>模型名</label>
          <input value={draft.model} onChange={(event) => setField('model', event.target.value)} placeholder="model-name" />
        </div>
        <div className="field">
          <label>单次输出上限</label>
          <input inputMode="numeric" value={draft.maxTokens} onChange={(event) => setField('maxTokens', event.target.value)} />
        </div>
        <div className="field">
          <label>上下文窗口（输入 + 输出总 token）</label>
          <input inputMode="numeric" value={draft.contextWindow} onChange={(event) => setField('contextWindow', event.target.value)} />
        </div>
        <div className="field admin-llm-wide">
          <label>API Key {saved?.apiKeySet && <span className="tag tag-ok">已加密保存</span>}</label>
          <input type="password" autoComplete="new-password" value={draft.apiKey}
            onChange={(event) => {
              setField('apiKey', event.target.value);
              setField('apiKeyClear', false);
            }}
            placeholder={saved?.apiKeySet ? '留空保留当前 Key' : '可选'} />
          {saved?.apiKeySet && (
            <button type="button" className="btn cfg-key-clear" disabled={draft.apiKeyClear}
              onClick={() => {
                setField('apiKey', '');
                setField('apiKeyClear', true);
              }}>
              <Trash2 size={14} aria-hidden="true" />
              {draft.apiKeyClear ? '保存后清除 Key' : '清除 Key'}
            </button>
          )}
        </div>
      </div>

      <div className="admin-llm-actions">
        {saved && <button type="button" className="btn" disabled={Boolean(busy)} onClick={reset} title="清除平台默认">
          <RotateCcw size={15} aria-hidden="true" /> 清除默认
        </button>}
        <div className="admin-llm-primary-actions">
          <button type="button" className="btn" disabled={Boolean(busy)} onClick={test}>
            <PlugZap size={15} aria-hidden="true" /> {busy === 'test' ? '测试中…' : '测试连接'}
          </button>
          <button type="button" className="btn btn-p" disabled={Boolean(busy)} onClick={save}>
            <Save size={15} aria-hidden="true" /> {busy === 'save' ? '保存中…' : '保存默认'}
          </button>
        </div>
      </div>

      {message && <div className={`cfg-test ${message.startsWith('✓') ? 'ok' : 'bad'}`}>{message}</div>}
    </div>
  );
}
