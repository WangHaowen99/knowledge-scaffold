import { useCallback, useEffect, useState } from 'react';
import { Loader2, RefreshCw, Save } from 'lucide-react';

import { authFetch } from '../shared/api/auth';

const DEFAULT_CONFIG = {
  lang: {
    enabled: false,
    default: 'zh',
  },
  acp_suppress_script_patterns: '',
};

function normalizePatterns(value) {
  if (Array.isArray(value)) {
    return value.filter(Boolean).join(', ');
  }
  return value ?? '';
}

function WorkspaceRuntimeConfig({
  currentWorkspaceId,
  currentWorkspaceName,
  canEdit = false,
}) {
  const [runtimeLangEnabled, setRuntimeLangEnabled] = useState(false);
  const [runtimeLangDefault, setRuntimeLangDefault] = useState('zh');
  const [runtimeAcpPatterns, setRuntimeAcpPatterns] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState({ type: '', text: '' });

  const isReadOnly = !canEdit;
  const isBusy = loading || saving;
  const inputDisabled = isBusy || isReadOnly || !currentWorkspaceId;

  const applyConfig = useCallback((data = DEFAULT_CONFIG) => {
    setRuntimeLangEnabled(data?.lang?.enabled ?? false);
    setRuntimeLangDefault(data?.lang?.default ?? 'zh');
    setRuntimeAcpPatterns(normalizePatterns(data?.acp_suppress_script_patterns));
  }, []);

  const loadConfig = useCallback(async ({ clearMessage = false } = {}) => {
    if (!currentWorkspaceId) {
      applyConfig(DEFAULT_CONFIG);
      return;
    }
    setLoading(true);
    if (clearMessage) {
      setMessage({ type: '', text: '' });
    }
    try {
      const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(currentWorkspaceId)}/runtime-config`);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '加载运行参数失败');
      }
      applyConfig(data);
    } catch (err) {
      setMessage({ type: 'error', text: err?.message || '加载运行参数失败' });
    } finally {
      setLoading(false);
    }
  }, [applyConfig, currentWorkspaceId]);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleSave = async () => {
    if (!currentWorkspaceId) {
      setMessage({ type: 'error', text: '请先选择工作空间' });
      return;
    }

    setSaving(true);
    setMessage({ type: '', text: '' });
    try {
      const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(currentWorkspaceId)}/runtime-config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          lang: {
            enabled: runtimeLangEnabled,
            default: runtimeLangDefault,
          },
          acp_suppress_script_patterns: runtimeAcpPatterns,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data?.detail || '保存运行参数失败');
      }
      applyConfig(data);
      setMessage({ type: 'success', text: '运行参数已保存' });
    } catch (err) {
      setMessage({ type: 'error', text: err?.message || '保存运行参数失败' });
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="configuration-workspace-panel workspace-runtime-config-panel settings-panel-runtime">
      <div className="configuration-section-title configuration-section-title-actions">
        <div>
          <h3>运行参数</h3>
          <p>{currentWorkspaceName || currentWorkspaceId || '当前工作空间'} 的回复语言与 ACP 工具结果展示规则。</p>
        </div>
        <div className="workspace-management-actions">
          <button
            className="btn btn-secondary workspace-management-refresh"
            onClick={() => loadConfig({ clearMessage: true })}
            disabled={isBusy || !currentWorkspaceId}
            type="button"
          >
            {loading ? <Loader2 size={16} className="spin" /> : <RefreshCw size={16} />}
            <span>刷新</span>
          </button>
          <button
            className="btn btn-primary"
            onClick={handleSave}
            disabled={inputDisabled}
            type="button"
          >
            {saving ? <Loader2 size={16} className="spin" /> : <Save size={15} />}
            <span>保存</span>
          </button>
        </div>
      </div>

      {isReadOnly && (
        <div className="workspace-message">当前用户只能查看运行参数。</div>
      )}
      {message.text && (
        <div className={`workspace-message ${message.type}`}>{message.text}</div>
      )}

      <div className="settings-field-groups workspace-runtime-field-groups">
        <div className="settings-field-group tone-terminology">
          <div className="settings-field-group-head workspace-runtime-group-head">
            <h4 className="settings-field-group-title">回复语言</h4>
          </div>
          <div className="settings-field-group-body">
            <div className="settings-fields-grid is-single">
              <div className="workspace-runtime-inline-controls">
                <p className="field-hint workspace-runtime-group-hint">控制是否向 Agent 注入明确的输出语言指令。</p>
                <label className="workspace-checkbox workspace-runtime-toggle">
                  <input
                    type="checkbox"
                    checked={runtimeLangEnabled}
                    onChange={(event) => setRuntimeLangEnabled(event.target.checked)}
                    disabled={inputDisabled}
                  />
                  <span>启用语言控制</span>
                </label>
                <p className="field-hint">
                  {runtimeLangEnabled
                    ? '开启后优先使用外部请求传入的 lang，例如 ACP 的 lang 字段；未传入时自动识别用户问题语言，识别不到时使用兜底语言。'
                    : '关闭后不注入额外语言指令，通常按用户问题语言回复；外部请求传入的 lang 不生效。'}
                </p>

                {runtimeLangEnabled && (
                  <label className="workspace-field workspace-runtime-language-select">
                    <span>兜底语言</span>
                    <select
                      value={runtimeLangDefault}
                      onChange={(event) => setRuntimeLangDefault(event.target.value)}
                      disabled={inputDisabled}
                    >
                      <option value="zh">中文</option>
                      <option value="en">English</option>
                    </select>
                  </label>
                )}
              </div>
            </div>
          </div>
        </div>

        <div className="settings-field-group tone-highlight">
          <div className="settings-field-group-head workspace-runtime-group-head">
            <h4 className="settings-field-group-title">隐藏工具结果</h4>
          </div>
          <div className="settings-field-group-body">
            <div className="settings-fields-grid is-single">
              <p className="field-hint workspace-runtime-group-hint">仅影响 ACP 流式响应：命中工具名或脚本操作名时，不向调用方返回该工具结果。</p>
              <label className="workspace-field workspace-runtime-acp-field">
                <span>匹配关键词</span>
                <input
                  type="text"
                  value={runtimeAcpPatterns}
                  onChange={(event) => setRuntimeAcpPatterns(event.target.value)}
                  disabled={inputDisabled}
                  placeholder="business_data_search"
                />
              </label>
              <p className="field-hint">
                多个关键词用英文逗号分隔，例如 business_data_search。
              </p>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export default WorkspaceRuntimeConfig;
