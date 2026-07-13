import { useCallback, useEffect, useState } from 'react';
import { Monitor, Moon, Settings2, Sun } from 'lucide-react';
import { authFetch } from '../utils/auth';
import { friendlyRequestError } from '../utils/requestErrors';
import McpManagement from '../components/McpManagement';
import { useAuthContext } from './useAuthContext';
import { APPEARANCES, THEMES, applyAppearance, applyTheme, getAppearance, getTheme } from './theme';
import { ConfirmDialog, InputDialog, SkeletonState, ToastNotice } from './UiFeedback';

// 配置中心：三层作用域（L1 全局 / L2 工作空间 / L3 个人）。
const SCOPES = {
  global: {
    lv: 'L1', badge: 'sb-l1', label: '全局配置',
    hint: '平台级配置，影响所有工作空间；仅平台管理员可修改，其他人可查看',
    sections: [
      { id: 'llm', name: '模型池' },
      { id: 'web_search', name: '联网搜索' },
      { id: 'observability', name: '观测打点' },
      { id: 'agent', name: 'Agent 全局参数' },
      { id: 'wsmgmt', name: '工作空间管理' },
      { id: 'users', name: '用户与授权' },
    ],
  },
  workspace: {
    lv: 'L2', badge: 'sb-l2', label: '工作空间',
    hint: '当前工作空间的资源与运行设置；空间管理员可修改，成员可查看',
    sections: [
      { id: 'info', name: '空间信息' },
      { id: 'members', name: '成员' },
      { id: 'model', name: '模型选用' },
      { id: 'conn', name: '数据连接' },
      { id: 'bind', name: '知识与数据绑定' },
      { id: 'runtime', name: '空间运行参数' },
      { id: 'mcp', name: 'MCP 接入' },
    ],
  },
  personal: {
    lv: 'L3', badge: 'sb-l3', label: '个人设置',
    hint: '仅对你自己生效的个人偏好，随账号同步',
    sections: [
      { id: 'prefs', name: '偏好' },
      { id: 'account', name: '账号安全' },
    ],
  },
};

export default function ConfigCenter({ scope, section, onNavigate }) {
  const sc = SCOPES[scope] || SCOPES.global;
  const sections = sc.sections;
  const active = sections.find((s) => s.id === section) || sections[0];

  return (
    <div className="config-page">
      <div className="cfg-page-head">
        <div className="cfg-page-icon"><Settings2 size={20} /></div>
        <div>
          <div className="cfg-page-eyebrow">平台设置</div>
          <h1>配置中心</h1>
          <p>按全局、工作空间和个人三个层级管理平台能力与运行偏好。</p>
        </div>
      </div>
      <div className="scope-tabs">
        {Object.entries(SCOPES).map(([key, v]) => (
          <button key={key} className={key === scope ? 'on' : ''}
            onClick={() => onNavigate({ scope: key, sec: SCOPES[key].sections[0].id })}>
            <span className={`scope-badge ${v.badge}`}>{v.lv}</span>{v.label}
          </button>
        ))}
      </div>
      <div className="scope-hint">{sc.hint}</div>
      <div className="cfg">
        <div className="subnav">
          {sections.map((s) => (
            <button key={s.id} className={s.id === active.id ? 'on' : ''}
              onClick={() => onNavigate({ scope, sec: s.id })}>{s.name}</button>
          ))}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <SectionBody scope={scope} section={active.id} />
        </div>
      </div>
    </div>
  );
}

function ReadOnlyBanner({ who }) {
  return (
    <div className="ro-banner">🔒 <span>当前为<b>只读模式</b>——此层配置仅 <b>{who}</b> 可修改。你可以查看全部配置项，如需变更请联系管理员。</span></div>
  );
}

function Field({ label, hint, children }) {
  return (
    <div className="fieldrow">
      <div className="fl"><div className="n">{label}</div>{hint && <div className="h">{hint}</div>}</div>
      {children}
    </div>
  );
}

function SectionBody({ scope, section }) {
  const { workspaceId } = useAuthContext();
  if (scope === 'global') {
    if (section === 'wsmgmt') return <WorkspaceMgmt />;
    if (section === 'users') return <UserMgmt />;
    return <SystemConfigSection section={section} />;
  }
  if (scope === 'workspace') {
    if (section === 'info') return <WorkspaceInfo />;
    // key=workspaceId：切换空间时整体重挂，丢弃未保存的成员草稿，避免切回复活旧草稿覆盖他人变更
    if (section === 'members') return <WorkspaceMembers key={workspaceId} />;
    if (section === 'model') return <WorkspaceModel />;
    if (section === 'conn') return <DataConnections />;
    if (section === 'bind') return <KnowledgeBinding />;
    if (section === 'runtime') return <WorkspaceRuntime />;
    if (section === 'mcp') return <WorkspaceMcp />;
  }
  if (scope === 'personal') {
    if (section === 'prefs') return <PersonalPrefs />;
    if (section === 'account') return <AccountSecurity />;
  }
  return null;
}

/* ---------------- L1 · 系统配置各 section（/api/system-config） ---------------- */

const GLOBAL_SECTION_META = {
  llm: { title: '模型池', desc: '集中管理可用的大模型；密钥加密存储，各工作空间在“模型选用”里引用', wiz: 'cfg-llm' },
  web_search: { title: '联网搜索', desc: '联网搜索的域名白/黑名单，仅在此处统一设置' },
  observability: { title: '观测打点', desc: '运行链路追踪：本地日志或 Langfuse' },
  agent: { title: 'Agent 全局参数', desc: '智能体运行上限：对话轮次、子任务并发与深度、上下文长度' },
};

function SystemConfigSection({ section }) {
  const { can } = useAuthContext();
  const canEdit = can('system_config.write');
  const meta = GLOBAL_SECTION_META[section] || { title: section, desc: '' };
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [loadError, setLoadError] = useState('');

  const load = useCallback(async () => {
    setLoadError('');
    try {
      const res = await authFetch('/api/system-config');
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || data.error || `请求失败（${res.status}）`);
      }
      const data = await res.json();
      setConfig(data);
      setDraft({});
    } catch (error) {
      setLoadError(friendlyRequestError(error, '配置加载失败'));
    }
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);

  if (!config) return <LoadState error={loadError} onRetry={load} />;
  const secData = config[section === 'llm' ? 'llm_profiles' : section] || {};

  const save = async (data) => {
    setBusy(true);
    setMsg('');
    try {
      const res = await authFetch('/api/system-config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ section: section === 'llm' ? 'llm_profiles' : section, data }),
      });
      const payload = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已保存' : `保存失败：${payload.detail || payload.error || res.status}`);
      if (res.ok) await load();
      return res.ok;
    } catch {
      setMsg('保存失败：网络异常，请稍后重试');
      return false;
    } finally {
      setBusy(false);
    }
  };

  const set = (k, v) => setDraft((d) => ({ ...d, [k]: v }));
  const val = (k, fallback = '') => (draft[k] !== undefined ? draft[k] : (secData[k] ?? fallback));

  return (
    <div className="card" data-wiz={meta.wiz}>
      <h3>{meta.title}</h3>
      <div className="cd">{meta.desc}</div>
      {!canEdit && <ReadOnlyBanner who="平台管理员" />}
      {section === 'llm' && <LlmProfiles data={secData} canEdit={canEdit} onSave={save} busy={busy} />}
      {section === 'web_search' && (
        <>
          <Field label="启用联网搜索"><Toggle on={!!val('enabled', false)} disabled={!canEdit} onChange={(v) => set('enabled', v)} /></Field>
          <Field label="搜索深度">
            <select disabled={!canEdit} value={val('search_depth', 'basic')} onChange={(e) => set('search_depth', e.target.value)}>
              {['basic', 'fast', 'ultra-fast', 'advanced'].map((o) => <option key={o}>{o}</option>)}
            </select>
          </Field>
          <details className="config-advanced">
            <summary>高级域名策略</summary>
            <div className="config-advanced-body">
              <Field label="允许域名" hint="逗号分隔，空为不限"><input type="text" disabled={!canEdit} value={val('allowed_domains')} onChange={(e) => set('allowed_domains', e.target.value)} /></Field>
              <Field label="屏蔽域名" hint="逗号分隔"><input type="text" disabled={!canEdit} value={val('blocked_domains')} onChange={(e) => set('blocked_domains', e.target.value)} /></Field>
            </div>
          </details>
        </>
      )}
      {section === 'observability' && (
        <>
          <Field label="启用观测日志"><Toggle on={!!val('enabled', false)} disabled={!canEdit} onChange={(v) => set('enabled', v)} /></Field>
          <Field label="模式">
            <select disabled={!canEdit} value={val('mode', 'local')} onChange={(e) => set('mode', e.target.value)}>
              <option value="local">local（本地日志）</option>
              <option value="langfuse">langfuse</option>
            </select>
          </Field>
          <Field label="本地日志保留天数"><input type="number" disabled={!canEdit} value={val('local', {}).retention_days ?? ''} onChange={(e) => set('local', { retention_days: Number(e.target.value) })} /></Field>
        </>
      )}
      {section === 'agent' && (
        <>
          {[['main_agent_max_iterations', '主智能体最大轮次'], ['context_max_rounds', '上下文保留轮次'],
            ['llm_connection_max_retries', 'LLM 连接重试次数']].map(([k, label]) => (
              <Field key={k} label={label}>
                <input type="number" disabled={!canEdit} value={val(k, '')} onChange={(e) => set(k, Number(e.target.value))} />
              </Field>
            ))}
          <details className="config-advanced">
            <summary>高级多智能体参数</summary>
            <div className="config-advanced-body">
              {[['sub_agent_max_iterations', '子智能体最大轮次'], ['sub_agent_max_concurrency', '子智能体最大并发'],
                ['sub_agent_max_depth', '子智能体最大深度']].map(([k, label]) => (
                  <Field key={k} label={label}>
                    <input type="number" disabled={!canEdit} value={val(k, '')} onChange={(e) => set(k, Number(e.target.value))} />
                  </Field>
                ))}
            </div>
          </details>
        </>
      )}
      {section !== 'llm' && canEdit && (
        <div className="foot">
          <span className="who">{Object.keys(draft).length ? '有未保存的修改' : '配置已同步'}</span>
          <button className="btn" disabled={busy} onClick={() => setDraft({})}>重置</button>
          <button className="btn btn-p" disabled={busy || !Object.keys(draft).length} onClick={() => save(draft)}>保存修改</button>
        </div>
      )}
      <ToastNotice message={msg} />
    </div>
  );
}

function LlmProfiles({ data, canEdit, onSave, busy }) {
  const profiles = data.profiles || [];
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null); // 正在编辑的模型（null=未编辑）
  const colSpan = canEdit ? 7 : 6;
  return (
    <>
      <Field label="默认模型" hint="工作空间未单独指定时使用此模型">
        <select disabled={!canEdit || busy} value={data.default_profile_id || ''}
          onChange={(e) => onSave({ default_profile_id: e.target.value })}>
          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}（{p.model}）</option>)}
        </select>
      </Field>
      {canEdit && !creating && !editing && (
        <div style={{ margin: '12px 0' }}>
          <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新增模型</button>
        </div>
      )}
      {(creating || editing) && (
        <LlmProfileForm
          key={editing?.id || 'create'}
          initial={editing}
          existing={profiles}
          defaultProfileId={data.default_profile_id || ''}
          busy={busy}
          onSubmit={async (payload) => {
            if (await onSave(payload)) {
              setCreating(false);
              setEditing(null);
            }
          }}
          onCancel={() => { setCreating(false); setEditing(null); }}
        />
      )}
      <table className="dt" style={{ marginTop: 10 }}>
        <thead><tr><th>名称</th><th>类型</th><th>模型</th><th>接口地址</th><th>密钥</th><th>状态</th>{canEdit && <th style={{ width: 70 }}>操作</th>}</tr></thead>
        <tbody>
          {profiles.map((p) => (
            <tr key={p.id}>
              <td style={{ fontWeight: 600 }}>{p.name}{p.is_default ? ' ★' : ''}</td>
              <td>{p.provider}</td>
              <td>{p.model}</td>
              <td style={{ color: 'var(--ink2)', fontSize: 12 }}>{p.base_url}</td>
              <td>{p.has_api_key ? <span className="tag tag-ok">已配置</span> : <span className="tag tag-draft">未配置</span>}</td>
              <td>{p.enabled ? '启用' : '停用'}</td>
              {canEdit && (
                <td><button className="btn" style={btnS} disabled={busy || creating || !!editing}
                  onClick={() => { setEditing(p); setCreating(false); }}>编辑</button></td>
              )}
            </tr>
          ))}
          {!profiles.length && <tr><td colSpan={colSpan} className="empty">尚未配置模型</td></tr>}
        </tbody>
      </table>
    </>
  );
}

// 模型表单（新增 / 编辑两用）：提交完整模型数组——未改动的项只带 {id}（后端按 id 合并、
// 保留含密钥的全部旧值），新增或被编辑的项带全字段；密钥留空则保留旧值。
const PROVIDER_PRESETS = {
  glm: { label: 'GLM（智谱）', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  deepseek: { label: 'DeepSeek', base_url: 'https://api.deepseek.com' },
  openai_compatible: { label: 'OpenAI 兼容', base_url: '' },
};

function LlmProfileForm({ initial, existing, defaultProfileId, busy, onSubmit, onCancel }) {
  const isEdit = !!initial;
  const [form, setForm] = useState(() => ({
    id: initial?.id || '', name: initial?.name || '',
    provider: initial?.provider || 'openai_compatible', model: initial?.model || '',
    base_url: initial?.base_url || '', api_key: '',
    timeout: initial?.timeout ?? 60, max_tokens: initial?.max_tokens ?? 4096,
    temperature: initial?.temperature ?? 0.7, enabled: initial ? initial.enabled !== false : true,
  }));
  const [asDefault, setAsDefault] = useState(isEdit && initial.id === defaultProfileId);
  const [err, setErr] = useState('');
  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const pickProvider = (p) => {
    setForm((f) => ({
      ...f,
      provider: p,
      base_url: !f.base_url || Object.values(PROVIDER_PRESETS).some((x) => x.base_url === f.base_url)
        ? (PROVIDER_PRESETS[p]?.base_url || '')
        : f.base_url,
    }));
  };

  const submit = async () => {
    const id = isEdit ? initial.id : (form.id.trim() || `profile-${Date.now().toString(36)}`);
    if (!form.name.trim()) { setErr('请填写显示名称'); return; }
    if (!form.model.trim()) { setErr('请填写模型名称'); return; }
    if (!isEdit) {
      if (/\s/.test(id) || id.length > 80) { setErr('标识不能含空格且不超过 80 字符'); return; }
      if (existing.some((p) => p.id === id)) { setErr(`标识「${id}」已存在`); return; }
    }
    setErr('');
    const edited = {
      id, name: form.name.trim(), provider: form.provider, model: form.model.trim(),
      base_url: form.base_url.trim(), timeout: Number(form.timeout) || 60,
      max_tokens: Number(form.max_tokens) || 4096, temperature: Number(form.temperature),
      enabled: form.enabled,
    };
    if (form.api_key) edited.api_key = form.api_key; // 留空则后端保留旧密钥
    // 未改动的项只带 {id}（后端保留旧值），被编辑/新增的项带全字段
    const profiles = isEdit
      ? existing.map((p) => (p.id === id ? edited : { id: p.id }))
      : [...existing.map((p) => ({ id: p.id })), edited];
    const payload = { profiles };
    if (asDefault) payload.default_profile_id = id;
    await onSubmit(payload);
  };

  return (
    <div style={{ border: '1px solid var(--hair)', borderRadius: 12, padding: '6px 18px 14px', margin: '12px 0', background: '#fbfbf9' }}>
      <div style={{ fontWeight: 650, fontSize: 13, margin: '8px 0 4px' }}>{isEdit ? `编辑模型 · ${initial.name}` : '新增模型'}</div>
      <Field label="显示名称 *" hint="展示用，如 GLM-4.7"><input type="text" value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
      {!isEdit && (
        <Field label="标识" hint="可选，留空自动生成；创建后不可改">
          <input type="text" value={form.id} onChange={(e) => set('id', e.target.value)} /></Field>
      )}
      <Field label="类型">
        <select value={form.provider} onChange={(e) => pickProvider(e.target.value)}>
          {Object.entries(PROVIDER_PRESETS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
      </Field>
      <Field label="模型名称 *" hint="模型的调用名，如 glm-4.7 / deepseek-chat"><input type="text" value={form.model} onChange={(e) => set('model', e.target.value)} /></Field>
      <Field label="接口地址"><input type="text" value={form.base_url} onChange={(e) => set('base_url', e.target.value)} /></Field>
      <Field label="API 密钥" hint={isEdit && initial.has_api_key ? '已配置，留空则保留原密钥' : '加密存储，不会回显；留空可稍后补配'}>
        <input type="password" value={form.api_key} placeholder={isEdit && initial.has_api_key ? '••••••（留空保留）' : ''}
          onChange={(e) => set('api_key', e.target.value)} autoComplete="new-password" /></Field>
      <Field label="超时（秒）" hint="单次请求最长等待时间"><input type="number" value={form.timeout} onChange={(e) => set('timeout', e.target.value)} /></Field>
      <Field label="最大输出长度" hint="单次回复的最多字数上限"><input type="number" value={form.max_tokens} onChange={(e) => set('max_tokens', e.target.value)} /></Field>
      <Field label="回答随机性" hint="0 更稳定确定，越高越发散（一般 0.7）"><input type="number" step="0.1" value={form.temperature} onChange={(e) => set('temperature', e.target.value)} /></Field>
      <Field label="启用" hint="停用后该模型不可被选用"><Toggle on={form.enabled} onChange={(v) => set('enabled', v)} /></Field>
      <Field label="设为全局默认"><Toggle on={asDefault} onChange={setAsDefault} /></Field>
      {err && <div style={{ fontSize: 12.5, color: 'var(--crit)', margin: '8px 0' }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
        <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
        <button className="btn btn-p" disabled={busy} onClick={submit}>{busy ? '保存中…' : '保存模型'}</button>
      </div>
    </div>
  );
}

/* ---------------- L1 · 工作空间管理 / 用户与授权 ---------------- */

function WorkspaceMgmt() {
  const { can, workspaces, defaultWorkspaceId, reloadWorkspaces } = useAuthContext();
  const canManage = can('workspace.manage');
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ id: '', name: '', description: '' });
  const [msg, setMsg] = useState('');
  const [deleteId, setDeleteId] = useState('');
  const [busyAction, setBusyAction] = useState('');

  const create = async () => {
    if (!form.name) { setMsg('名称必填'); return; }
    setBusyAction('create');
    try {
      const res = await authFetch('/agentic/workspaces', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
      });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已创建' : `创建失败：${data.detail || res.status}`);
      if (res.ok) { setCreating(false); setForm({ id: '', name: '', description: '' }); reloadWorkspaces(); }
    } catch (error) {
      setMsg(friendlyRequestError(error, '创建失败'));
    } finally { setBusyAction(''); }
  };
  const setDefault = async (id) => {
    setBusyAction(`default:${id}`);
    try {
      const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(id)}/default`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已设为默认工作空间' : `设置失败：${data.detail || data.error || res.status}`);
      if (res.ok) reloadWorkspaces();
    } catch (error) { setMsg(friendlyRequestError(error, '设置失败')); }
    finally { setBusyAction(''); }
  };
  const remove = async (id) => {
    setBusyAction(`delete:${id}`);
    try {
      const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!res.ok) { const d = await res.json().catch(() => ({})); setMsg(`删除失败：${d.detail || res.status}`); }
      else setMsg('工作空间已删除');
      setDeleteId('');
      reloadWorkspaces();
    } catch (error) { setMsg(friendlyRequestError(error, '删除失败')); }
    finally { setBusyAction(''); }
  };

  return (
    <div className="card">
      <h3>工作空间管理</h3>
      <div className="cd">创建 / 删除 / 设默认——管理空间集合，属全局操作</div>
      {!canManage && <ReadOnlyBanner who="平台管理员" />}
      {canManage && (
        <div style={{ marginBottom: 12 }}>
          {!creating
            ? <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新建工作空间</button>
            : (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                <input type="text" placeholder="ID（可选）" style={inputS(120)} value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} />
                <input type="text" placeholder="名称（必填）" style={inputS(160)} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                <input type="text" placeholder="描述" style={inputS(220)} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
                <button className="btn btn-p" disabled={!!busyAction} onClick={create}>{busyAction === 'create' ? '创建中…' : '创建'}</button>
                <button className="btn" onClick={() => setCreating(false)}>取消</button>
              </div>
            )}
        </div>
      )}
      <table className="dt">
        <thead><tr><th>空间</th><th>描述</th><th>默认</th>{canManage && <th style={{ width: 160 }}>操作</th>}</tr></thead>
        <tbody>
          {workspaces.map((w) => (
            <tr key={w.id}>
              <td style={{ fontWeight: 600 }}>{w.name} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>({w.id})</span></td>
              <td style={{ color: 'var(--ink2)' }}>{w.description || '—'}</td>
              <td>{(w.is_default || w.id === defaultWorkspaceId) ? '★ 默认' : ''}</td>
              {canManage && (
                <td>
                  {!(w.is_default || w.id === defaultWorkspaceId) && (
                    <>
                      <button className="btn" disabled={!!busyAction} style={{ padding: '3px 10px', fontSize: 12, marginRight: 6 }} onClick={() => setDefault(w.id)}>设为默认</button>
                      <button className="btn" disabled={!!busyAction} style={{ padding: '3px 10px', fontSize: 12 }} onClick={() => setDeleteId(w.id)}>删除</button>
                    </>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <ConfirmDialog open={Boolean(deleteId)} title="删除工作空间" description={`将删除工作空间「${deleteId}」及其运行数据，此操作无法撤销。`}
        confirmLabel="删除工作空间" danger busy={busyAction === `delete:${deleteId}`} onConfirm={() => remove(deleteId)} onCancel={() => setDeleteId('')} />
      <ToastNotice message={msg} />
    </div>
  );
}

function UserMgmt() {
  const { can, user } = useAuthContext();
  const canManage = can('users.manage');
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ username: '', password: '' });
  const [msg, setMsg] = useState('');
  const [deleteUsername, setDeleteUsername] = useState('');
  const [resetUsername, setResetUsername] = useState('');
  const [resetPassword, setResetPassword] = useState('');
  const [loadError, setLoadError] = useState('');
  const [busyAction, setBusyAction] = useState('');

  const load = useCallback(async () => {
    if (!canManage) return;
    setLoadError('');
    try {
      const res = await authFetch('/auth/users');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `用户列表加载失败（${res.status}）`);
      setUsers(data.users || []);
    } catch (error) { setLoadError(friendlyRequestError(error, '用户列表加载失败')); }
  }, [canManage]);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);

  if (!canManage) {
    return (
      <div className="card">
        <h3>用户与授权</h3>
        <ReadOnlyBanner who="平台管理员" />
        <div className="empty">用户管理仅平台管理员可见</div>
      </div>
    );
  }

  const create = async () => {
    setBusyAction('create');
    try {
      const res = await authFetch('/auth/users', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
      });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? `已创建 ${form.username}` : `失败：${data.error || res.status}`);
      if (res.ok) { setForm({ username: '', password: '' }); load(); }
    } catch (error) { setMsg(friendlyRequestError(error, '创建失败')); }
    finally { setBusyAction(''); }
  };
  const resetPw = async () => {
    if (!resetUsername || !resetPassword) return;
    setBusyAction(`reset:${resetUsername}`);
    try {
      const res = await authFetch(`/auth/users/${encodeURIComponent(resetUsername)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: resetPassword }),
      });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已重置' : `失败：${data.error || res.status}`);
      if (res.ok) { setResetUsername(''); setResetPassword(''); }
    } catch (error) { setMsg(friendlyRequestError(error, '重置失败')); }
    finally { setBusyAction(''); }
  };
  const remove = async (username) => {
    setBusyAction(`delete:${username}`);
    try {
      const res = await authFetch(`/auth/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已删除' : `失败：${data.error || res.status}`);
      setDeleteUsername('');
      load();
    } catch (error) { setMsg(friendlyRequestError(error, '删除失败')); }
    finally { setBusyAction(''); }
  };

  return (
    <div className="card">
      <h3>用户与授权</h3>
      <div className="cd">新建用户默认为普通用户；空间级角色授予为后续规划能力</div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <input type="text" placeholder="用户名" style={inputS(140)} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input type="password" placeholder="初始密码" style={inputS(160)} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <button className="btn btn-p" disabled={!form.username || !form.password || !!busyAction} onClick={create}>{busyAction === 'create' ? '创建中…' : '＋ 新建用户'}</button>
      </div>
      {loadError && <div className="inline-load-error"><span>{loadError}</span><button className="btn" type="button" onClick={load}>重试</button></div>}
      <table className="dt">
        <thead><tr><th>用户名</th><th>角色</th><th>状态</th><th style={{ width: 180 }}>操作</th></tr></thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.username}>
              <td style={{ fontWeight: 600 }}>{u.username}{u.username === user?.username ? '（当前账号）' : ''}</td>
              <td>{u.role === 'admin' ? <span className="tag tag-ok">平台管理员</span> : <span className="tag tag-lang">普通用户</span>}</td>
              <td style={{ color: 'var(--ink2)' }}>{u.password_change_required ? '待改密' : '正常'}</td>
              <td>
                {u.username !== user?.username && (
                  <>
                    <button className="btn" style={{ padding: '3px 10px', fontSize: 12, marginRight: 6 }} onClick={() => { setResetUsername(u.username); setResetPassword(''); }}>重置密码</button>
                    {u.role !== 'admin' && <button className="btn" style={{ padding: '3px 10px', fontSize: 12 }} onClick={() => setDeleteUsername(u.username)}>删除</button>}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ConfirmDialog open={Boolean(deleteUsername)} title="删除用户" description={`删除用户「${deleteUsername}」后，该账号将无法登录。`}
        confirmLabel="删除用户" danger busy={busyAction === `delete:${deleteUsername}`} onConfirm={() => remove(deleteUsername)} onCancel={() => setDeleteUsername('')} />
      <InputDialog open={Boolean(resetUsername)} title="重置用户密码" description={`为用户「${resetUsername}」设置新的初始密码。`}
        value={resetPassword} onChange={setResetPassword} placeholder="输入符合安全策略的新密码" confirmLabel="重置密码" busy={busyAction === `reset:${resetUsername}`}
        onConfirm={resetPw} onCancel={() => { setResetUsername(''); setResetPassword(''); }} />
      <ToastNotice message={msg} />
    </div>
  );
}

/* ---------------- L2 · 工作空间各面板 ---------------- */

function WorkspaceInfo() {
  const { can, workspaceId, workspaces, reloadWorkspaces } = useAuthContext();
  const canEdit = can('ws.info.write');
  const ws = workspaces.find((w) => w.id === workspaceId);
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState('');
  const current = draft ?? { name: ws?.name || '', description: ws?.description || '' };

  const save = async () => {
    const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(current),
    });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已保存' : `失败：${data.detail || res.status}`);
    if (res.ok) { setDraft(null); reloadWorkspaces(); }
  };

  return (
    <div className="card">
      <h3>空间信息</h3>
      <div className="cd">修改空间名称与描述；新建 / 删除空间请到 全局配置 → 工作空间管理</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      <Field label="ID"><span style={{ fontSize: 13, color: 'var(--ink2)' }}>{workspaceId || '—'}</span></Field>
      <Field label="名称"><input type="text" disabled={!canEdit} value={current.name} onChange={(e) => setDraft({ ...current, name: e.target.value })} /></Field>
      <Field label="描述"><input type="text" disabled={!canEdit} value={current.description} onChange={(e) => setDraft({ ...current, description: e.target.value })} /></Field>
      {canEdit && (
        <div className="foot">
          <span className="who">{msg}</span>
          <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>重置</button>
          <button className="btn btn-p" disabled={!draft} onClick={save}>保存修改</button>
        </div>
      )}
    </div>
  );
}

function WorkspaceMembers() {
  const { can, workspaceId, workspaces, reloadWorkspaces } = useAuthContext();
  const canEdit = can('ws.members.write');
  const [fetched, setFetched] = useState(null);
  const ws = workspaces.find((w) => w.id === workspaceId) || fetched;
  // draft 记录所属空间：切换空间后自动失效回退到服务端数据
  const [draftState, setDraftState] = useState(null);
  const draft = draftState && draftState.wsId === workspaceId ? draftState.members : null;
  const setDraft = (members) => setDraftState(members === null ? null : { wsId: workspaceId, members });
  const [newName, setNewName] = useState('');
  const [msg, setMsg] = useState('');
  const members = draft ?? (ws?.members || []);

  // 优先复用 AuthContext 已拉取的条目；找不到当前空间时兜底重拉一次列表
  useEffect(() => {
    if (workspaces.some((w) => w.id === workspaceId)) return undefined;
    const t = setTimeout(async () => {
      const res = await authFetch('/agentic/workspaces');
      if (!res.ok) return;
      const data = await res.json().catch(() => ({}));
      setFetched((data.workspaces || []).find((w) => w.id === workspaceId) || null);
    }, 0);
    return () => clearTimeout(t);
  }, [workspaces, workspaceId]);

  const add = () => {
    const name = newName.trim();
    if (!name) return;
    if (members.includes(name)) { setMsg(`「${name}」已在成员列表`); return; }
    setDraft([...members, name]);
    setNewName('');
    setMsg('');
  };
  const removeMember = (name) => setDraft(members.filter((m) => m !== name));

  const save = async () => {
    const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}/members`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ members }),
    });
    const data = await res.json().catch(() => ({}));
    // 后端 403 用 error 字段、400 校验错用 detail 字段，两者都取
    setMsg(res.ok ? '已保存' : `失败：${data.error || data.detail || res.status}`);
    if (res.ok) { setDraft(null); reloadWorkspaces(); }
  };

  return (
    <div className="card">
      <h3>成员</h3>
      <div className="cd">管理本空间的成员；成员可访问本空间的数据与知识。变更空间负责人需平台管理员操作</div>
      {!canEdit && <ReadOnlyBanner who="平台管理员或空间负责人" />}
      <Field label="空间负责人" hint="可管理本空间的成员与设置">
        <span style={{ fontSize: 13, color: 'var(--ink2)' }}>{ws?.owner || '—（公共空间，所有登录用户可访问）'}</span>
      </Field>
      {canEdit && (
        <div style={{ display: 'flex', gap: 8, margin: '12px 0', alignItems: 'center' }}>
          <input type="text" placeholder="用户名" style={inputS(160)} value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
          <button className="btn btn-p" disabled={!newName.trim()} onClick={add}>＋ 添加成员</button>
        </div>
      )}
      <table className="dt">
        <thead><tr><th>成员</th>{canEdit && <th style={{ width: 100 }}>操作</th>}</tr></thead>
        <tbody>
          {members.map((m) => (
            <tr key={m}>
              <td style={{ fontWeight: 600 }}>{m}</td>
              {canEdit && (
                <td>
                  <button className="btn" style={btnS} onClick={() => removeMember(m)}>移除</button>
                </td>
              )}
            </tr>
          ))}
          {!members.length && <tr><td colSpan={canEdit ? 2 : 1} className="empty">暂无成员{ws?.owner ? '（仅空间负责人与管理员可访问）' : ''}</td></tr>}
        </tbody>
      </table>
      <ToastNotice message={msg} />
      {canEdit && (
        <div className="foot">
          <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>重置</button>
          <button className="btn btn-p" disabled={!draft} onClick={save}>保存修改</button>
        </div>
      )}
    </div>
  );
}

function WorkspaceModel() {
  const { workspaceId } = useAuthContext();
  const [profiles, setProfiles] = useState([]);
  const [defaultId, setDefaultId] = useState('');
  const [loadError, setLoadError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const storageKey = `mw_ws_model_${workspaceId}`;
  const [selected, setSelected] = useState(() => localStorage.getItem(storageKey) || '');

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      setLoadError('');
      try {
        const res = await authFetch('/agentic/model-profiles');
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || data.error || `模型列表加载失败（${res.status}）`);
        if (!cancelled) {
          setProfiles((data.profiles || []).filter((p) => p.enabled));
          setDefaultId(data.default_profile_id || '');
        }
      } catch (error) {
        if (!cancelled) setLoadError(friendlyRequestError(error, '模型列表加载失败'));
      }
    }, 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [reloadKey]);
  useEffect(() => {
    const t = setTimeout(() => setSelected(localStorage.getItem(`mw_ws_model_${workspaceId}`) || ''), 0);
    return () => clearTimeout(t);
  }, [workspaceId]);

  const pick = (v) => {
    setSelected(v);
    if (v) localStorage.setItem(storageKey, v); else localStorage.removeItem(storageKey);
  };

  return (
    <div className="card">
      <h3>模型选用</h3>
      <div className="cd">选择本空间对话默认使用的模型，可在会话中临时切换。</div>
      {loadError && <div className="inline-load-error"><span>{loadError}</span><button className="btn" type="button" onClick={() => setReloadKey((value) => value + 1)}>重试</button></div>}
      <Field label="本空间默认模型" hint="留空则跟随全局默认">
        <select value={selected} onChange={(e) => pick(e.target.value)}>
          <option value="">跟随全局{defaultId ? `（${profiles.find((p) => p.id === defaultId)?.name || defaultId}）` : ''}</option>
          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}（{p.model}）</option>)}
        </select>
      </Field>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
        该选择保存在当前浏览器，新建会话时生效。
      </div>
    </div>
  );
}

function DataConnections() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.connections.write');
  const [conns, setConns] = useState([]);
  const [defaultId, setDefaultId] = useState('');
  const [msg, setMsg] = useState('');
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null); // 正在编辑的连接（null=未编辑）
  const [deleteId, setDeleteId] = useState('');
  const [loadError, setLoadError] = useState('');
  const [busyAction, setBusyAction] = useState('');

  const load = useCallback(async () => {
    setLoadError('');
    try {
      const res = await authFetch('/agentic/data-connections');
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || `数据连接加载失败（${res.status}）`);
      setConns(data.connections || []);
      setDefaultId(data.default_connection_id || '');
    } catch (error) { setLoadError(friendlyRequestError(error, '数据连接加载失败')); }
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, workspaceId]);

  const test = async (id) => {
    setBusyAction(`test:${id}`);
    setMsg(`测试 ${id}…`);
    try {
      const res = await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}/test`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? `${id}: ${data.success ? '✓ 可用' : '✗ 不可用'}${data.message ? ` · ${data.message}` : ''}` : `测试失败：${data.detail || res.status}`);
    } catch (error) { setMsg(friendlyRequestError(error, '测试失败')); }
    finally { setBusyAction(''); }
  };
  const setDefault = async (id) => {
    setBusyAction(`default:${id}`);
    try {
      const res = await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}/default`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? `已将 ${id} 设为默认连接` : `设置失败：${data.detail || data.error || res.status}`);
      if (res.ok) load();
    } catch (error) { setMsg(friendlyRequestError(error, '设置失败')); }
    finally { setBusyAction(''); }
  };
  const remove = async (id) => {
    setBusyAction(`delete:${id}`);
    try {
      const res = await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) setMsg(data.bound_ontologies ? `无法删除：已被知识包绑定（${data.bound_ontologies.join(', ')}）` : `删除失败：${data.detail || res.status}`);
      else setMsg('数据连接已删除');
      setDeleteId('');
      load();
    } catch (error) { setMsg(friendlyRequestError(error, '删除失败')); }
    finally { setBusyAction(''); }
  };

  return (
    <div className="card" data-wiz="cfg-conn">
      <h3>数据连接</h3>
      <div className="cd">当前工作空间可用的业务数据库连接</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      {canEdit && !creating && !editing && (
        <div style={{ marginBottom: 12 }}>
          <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新建连接</button>
        </div>
      )}
      {(creating || editing) && (
        <ConnectionForm
          key={editing?.id || 'create'}
          initial={editing}
          onDone={(text) => { setCreating(false); setEditing(null); if (text) setMsg(text); load(); }}
          onCancel={() => { setCreating(false); setEditing(null); }}
        />
      )}
      {loadError && <div className="inline-load-error"><span>{loadError}</span><button className="btn" type="button" onClick={load}>重试</button></div>}
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
      <table className="dt">
        <thead><tr><th>连接</th><th>类型</th><th>默认</th>{canEdit && <th style={{ width: 260 }}>操作</th>}</tr></thead>
        <tbody>
          {conns.map((c) => (
            <tr key={c.id}>
              <td style={{ fontWeight: 600 }}>{c.name} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>({c.id})</span></td>
              <td>{c.provider}</td>
              <td>{c.id === defaultId ? '★ 默认' : ''}</td>
              {canEdit && (
                <td>
                  <button className="btn" style={btnS} disabled={creating || !!editing || !!busyAction}
                    onClick={() => { setEditing(c); setCreating(false); }}>编辑</button>
                  <button className="btn" disabled={!!busyAction} style={btnS} onClick={() => test(c.id)}>测试</button>
                  {c.id !== defaultId && <button className="btn" disabled={!!busyAction} style={btnS} onClick={() => setDefault(c.id)}>设默认</button>}
                  <button className="btn" disabled={!!busyAction} style={btnS} onClick={() => setDeleteId(c.id)}>删除</button>
                </td>
              )}
            </tr>
          ))}
          {!conns.length && <tr><td colSpan={4} className="empty">当前空间暂无数据连接</td></tr>}
        </tbody>
      </table>
      <ConfirmDialog open={Boolean(deleteId)} title="删除数据连接" description={`将删除连接「${deleteId}」。已绑定知识包的连接不会被删除。`}
        confirmLabel="删除连接" danger busy={busyAction === `delete:${deleteId}`} onConfirm={() => remove(deleteId)} onCancel={() => setDeleteId('')} />
      <ToastNotice message={msg} />
    </div>
  );
}

// 连接表单（新建 / 编辑两用）：连接类型的字段由后端下发，按其定义动态渲染。
// 编辑时密钥字段留空则保留原值（类型不变时后端自动保留）。
function ConnectionForm({ initial, onDone, onCancel }) {
  const isEdit = !!initial;
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState(initial?.provider || '');
  const [name, setName] = useState(initial?.name || '');
  const [connId, setConnId] = useState(initial?.id || '');
  const [asDefault, setAsDefault] = useState(false);
  const [config, setConfig] = useState({});
  const [err, setErr] = useState('');
  const [schemaError, setSchemaError] = useState('');
  const [busy, setBusy] = useState(false);

  // 从连接的已有配置回填非密钥字段（密钥不回显，留空即保留）
  const configFromInitial = (fields) => {
    const next = {};
    (fields || []).forEach((f) => {
      if (!f.secret && initial?.config?.[f.name] !== undefined) next[f.name] = initial.config[f.name];
    });
    return next;
  };

  useEffect(() => {
    const t = setTimeout(async () => {
      setSchemaError('');
      try {
        const res = await authFetch('/agentic/business-data/provider-schemas');
        if (!res.ok) throw new Error(`连接类型加载失败（${res.status}）`);
        const data = await res.json();
        const list = data.providers || [];
        if (!list.length) throw new Error('没有可用的数据连接类型');
        setProviders(list);
        const initName = isEdit ? initial.provider : (list[0]?.name || '');
        setProvider((prev) => prev || initName);
        const initSchema = list.find((p) => p.name === initName);
        if (isEdit) {
          setConfig(configFromInitial(initSchema?.fields));
        } else {
          const defaults = {};
          (initSchema?.fields || []).forEach((f) => { if (f.default !== undefined && f.default !== null) defaults[f.name] = f.default; });
          setConfig(defaults);
        }
      } catch (error) {
        setSchemaError(friendlyRequestError(error, '连接类型加载失败'));
      }
    }, 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const schema = providers.find((p) => p.name === provider);
  // 编辑时若未换类型，已配置的密钥可留空保留
  const secretKept = (f) => isEdit && provider === initial.provider && initial?.config?.[`has_${f.name}`];

  const pickProvider = (nextName) => {
    setProvider(nextName);
    const next = providers.find((p) => p.name === nextName);
    if (isEdit && nextName === initial.provider) {
      setConfig(configFromInitial(next?.fields));
      return;
    }
    const defaults = {};
    (next?.fields || []).forEach((f) => { if (f.default !== undefined && f.default !== null) defaults[f.name] = f.default; });
    setConfig(defaults);
  };

  const submit = async () => {
    if (!name.trim()) { setErr('请填写名称'); return; }
    // 已保留的密钥字段视为已满足
    const missing = (schema?.fields || []).filter((f) => f.required && !secretKept(f) && (config[f.name] === undefined || config[f.name] === ''));
    if (missing.length) { setErr(`请填写：${missing.map((f) => f.label || f.name).join('、')}`); return; }
    setBusy(true);
    setErr('');
    try {
      const body = { name: name.trim(), provider, config };
      const res = isEdit
        ? await authFetch(`/agentic/data-connections/${encodeURIComponent(initial.id)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          })
        : await authFetch('/agentic/data-connections', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...body, ...(connId.trim() ? { id: connId.trim() } : {}), ...(asDefault ? { set_as_default: true } : {}) }),
          });
      const data = await res.json().catch(() => ({}));
      if (res.ok) onDone(isEdit ? `已更新连接 ${data.connection?.name || name}` : `已创建连接 ${data.connection?.name || name}`);
      else setErr(`${isEdit ? '更新' : '创建'}失败：${data.detail || data.error || res.status}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ border: '1px solid var(--hair)', borderRadius: 12, padding: '6px 18px 14px', marginBottom: 14, background: '#fbfbf9' }}>
      <div style={{ fontWeight: 650, fontSize: 13, margin: '8px 0 4px' }}>{isEdit ? `编辑连接 · ${initial.name}` : '新建连接'}</div>
      <Field label="名称" hint="展示用"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="连接标识" hint={isEdit ? '创建后不可修改' : '可选，留空自动生成；创建后不可改'}>
        <input type="text" value={connId} disabled={isEdit} onChange={(e) => setConnId(e.target.value)} /></Field>
      <Field label="类型" hint={schema?.description || ''}>
        <select value={provider} onChange={(e) => pickProvider(e.target.value)}>
          {providers.map((p) => <option key={p.name} value={p.name}>{p.label || p.name}</option>)}
        </select>
      </Field>
      {(schema?.fields || []).map((f) => (
        <Field key={f.name} label={`${f.label || f.name}${f.required ? ' *' : ''}`}
          hint={f.secret ? (secretKept(f) ? '已配置，留空则保留原值' : '加密存储，不会回显') : ''}>
          {f.type === 'bool' ? (
            <Toggle on={!!config[f.name]} onChange={(v) => setConfig((c) => ({ ...c, [f.name]: v }))} />
          ) : (
            <input
              type={f.secret ? 'password' : f.type === 'int' ? 'number' : 'text'}
              value={config[f.name] ?? ''}
              placeholder={secretKept(f) ? '••••••（留空保留）' : ''}
              onChange={(e) => setConfig((c) => ({ ...c, [f.name]: f.type === 'int' ? Number(e.target.value) : e.target.value }))}
            />
          )}
        </Field>
      ))}
      {schemaError && <div className="lerr" style={{ marginTop: 10 }}>{schemaError}</div>}
      {!isEdit && <Field label="设为默认连接"><Toggle on={asDefault} onChange={setAsDefault} /></Field>}
      {err && <div style={{ fontSize: 12.5, color: 'var(--crit)', margin: '8px 0' }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
        <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
        <button className="btn btn-p" disabled={busy || !provider || !!schemaError} onClick={submit}>{busy ? '保存中…' : (isEdit ? '保存修改' : '创建连接')}</button>
      </div>
    </div>
  );
}

function KnowledgeBinding() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.kb.write');
  const [pkgs, setPkgs] = useState([]);
  const [conns, setConns] = useState([]);
  const [msg, setMsg] = useState('');
  const [loadError, setLoadError] = useState('');
  const [bindingId, setBindingId] = useState('');

  const load = useCallback(async () => {
    setLoadError('');
    try {
      const [or, cr] = await Promise.all([
        authFetch('/agentic/ontologies'),
        authFetch('/agentic/data-connections'),
      ]);
      const [ontologyData, connectionData] = await Promise.all([
        or.json().catch(() => ({})), cr.json().catch(() => ({})),
      ]);
      if (!or.ok || !cr.ok) throw new Error(ontologyData.detail || ontologyData.error || connectionData.detail || connectionData.error || '绑定数据加载失败');
      setPkgs(ontologyData.ontologies || []);
      setConns(connectionData.connections || []);
    } catch (error) { setLoadError(friendlyRequestError(error, '绑定数据加载失败')); }
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, workspaceId]);

  const bind = async (ontologyId, connectionId) => {
    setBindingId(ontologyId);
    const url = `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-binding`;
    try {
      const res = connectionId
        ? await authFetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ connection_id: connectionId }) })
        : await authFetch(url, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      setMsg(res.ok ? '已更新绑定' : `失败：${data.detail || data.error || res.status}`);
      if (res.ok) load();
    } catch (error) { setMsg(friendlyRequestError(error, '绑定失败')); }
    finally { setBindingId(''); }
  };

  return (
    <div className="card" data-wiz="cfg-bind">
      <h3>知识与数据绑定</h3>
      <div className="cd">为每个知识包指定要查询的数据连接，让它能读到真实业务数据</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      {loadError && <div className="inline-load-error"><span>{loadError}</span><button className="btn" type="button" onClick={load}>重试</button></div>}
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
      {pkgs.map((p) => (
        <Field key={p.id} label={p.name || p.id} hint={p.id}>
          <select disabled={!canEdit || bindingId === p.id} value={p.business_data?.connection_id || ''} onChange={(e) => bind(p.id, e.target.value)}>
            <option value="">（不绑定）</option>
            {conns.map((c) => <option key={c.id} value={c.id}>{c.name}（{c.provider}）</option>)}
          </select>
        </Field>
      ))}
      {!pkgs.length && <div className="empty">暂无知识包，先到知识包工作台导入</div>}
    </div>
  );
}

function WorkspaceRuntime() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.config.write');
  const [cfgState, setCfgState] = useState(null);
  const cfg = cfgState?.wsId === workspaceId ? cfgState.data : null;
  const [draftState, setDraftState] = useState(null);
  const draft = draftState?.wsId === workspaceId ? draftState.data : null;
  const setDraft = (data) => setDraftState(data === null ? null : { wsId: workspaceId, data });
  const [msg, setMsg] = useState('');
  const [loadErrorState, setLoadErrorState] = useState(null);
  const loadError = loadErrorState?.wsId === workspaceId ? loadErrorState.message : '';
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!workspaceId) return undefined;
    let cancelled = false;
    const timer = setTimeout(async () => {
      setLoadErrorState({ wsId: workspaceId, message: '' });
      try {
        const res = await authFetch('/agentic/workspaces/current/runtime-config');
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || data.error || `请求失败（${res.status}）`);
        if (!cancelled) setCfgState({ wsId: workspaceId, data });
      } catch (error) {
        if (!cancelled) setLoadErrorState({ wsId: workspaceId, message: friendlyRequestError(error, '空间运行参数加载失败') });
      }
    }, 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [workspaceId, reloadKey]);

  if (!workspaceId) return <div className="card"><div className="empty">请先选择工作空间</div></div>;
  if (!cfg) return <LoadState error={loadError} onRetry={() => setReloadKey((key) => key + 1)} />;
  const current = draft ?? {
    lang: { enabled: !!cfg.lang?.enabled, default: cfg.lang?.default || 'zh' },
    acp_suppress_script_patterns: Array.isArray(cfg.acp_suppress_script_patterns)
      ? cfg.acp_suppress_script_patterns.join(', ')
      : (cfg.acp_suppress_script_patterns || ''),
  };

  const save = async () => {
    const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}/runtime-config`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lang: current.lang, acp_suppress_script_patterns: current.acp_suppress_script_patterns }),
    });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已保存' : `失败：${data.detail || res.status}`);
    if (res.ok) { setCfgState({ wsId: workspaceId, data }); setDraft(null); }
  };

  return (
    <div className="card">
      <h3>空间运行参数</h3>
      <div className="cd">控制回复语言，以及哪些工具输出对用户隐藏</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      <Field label="启用语言控制">
        <Toggle on={current.lang.enabled} disabled={!canEdit} onChange={(v) => setDraft({ ...current, lang: { ...current.lang, enabled: v } })} />
      </Field>
      <Field label="默认回复语言" hint="当请求和个人偏好都没指定语言时使用">
        <select disabled={!canEdit} value={current.lang.default} onChange={(e) => setDraft({ ...current, lang: { ...current.lang, default: e.target.value } })}>
          <option value="zh">zh</option><option value="en">en</option>
        </select>
      </Field>
      <Field label="隐藏工具输出的关键词" hint="多个关键词用逗号分隔；命中的工具输出不展示给用户">
        <input type="text" disabled={!canEdit} value={current.acp_suppress_script_patterns}
          onChange={(e) => setDraft({ ...current, acp_suppress_script_patterns: e.target.value })} />
      </Field>
      {canEdit && (
        <div className="foot">
          <span className="who">{msg}</span>
          <button className="btn" disabled={!draft} onClick={() => setDraft(null)}>重置</button>
          <button className="btn btn-p" disabled={!draft} onClick={save}>保存修改</button>
        </div>
      )}
    </div>
  );
}

function WorkspaceMcp() {
  const { user, workspaceId, workspaces } = useAuthContext();
  const ws = workspaces.find((w) => w.id === workspaceId);
  return (
    <McpManagement
      currentWorkspaceId={workspaceId}
      currentWorkspaceName={ws?.name || workspaceId}
      canEdit={Boolean(user?.username && workspaceId)}
    />
  );
}

/* ---------------- L3 · 个人设置 ---------------- */

function PersonalPrefs() {
  const { workspaces, workspaceId, switchWorkspace } = useAuthContext();
  const [lang, setLang] = useState(() => localStorage.getItem('mw_pref_reply_lang') || '');
  const [theme, setTheme] = useState(() => getTheme());
  const [appearance, setAppearance] = useState(() => getAppearance());

  const pickTheme = (id) => setTheme(applyTheme(id));
  const pickAppearance = (id) => setAppearance(applyAppearance(id));
  const appearanceIcons = { system: Monitor, light: Sun, dark: Moon };

  return (
    <div className="card" data-wiz="cfg-prefs">
      <h3>偏好</h3>
      <div className="cd">以下偏好当前保存在你的浏览器</div>
      <Field label="当前工作空间" hint="切换后全站生效">
        <select value={workspaceId || ''} onChange={(e) => switchWorkspace(e.target.value)}>
          {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}（{w.id}）</option>)}
        </select>
      </Field>
      <Field label="回复语言偏好" hint="优先于工作空间的语言设置">
        <select value={lang} onChange={(e) => { setLang(e.target.value); localStorage.setItem('mw_pref_reply_lang', e.target.value); }}>
          <option value="">跟随空间</option><option value="zh">中文</option><option value="en">English</option>
        </select>
      </Field>
      <Field label="主题配色" hint="即时生效，全站应用">
        <div className="theme-options">
          {THEMES.map((t) => (
            <button key={t.id} type="button" onClick={() => pickTheme(t.id)} title={t.label}
              className={`theme-choice ${theme === t.id ? 'on' : ''}`}
              style={{ '--choice-color': t.color }}>
              <span style={{ width: 14, height: 14, borderRadius: '50%', background: t.color, flex: 'none' }} />
              {t.label}
            </button>
          ))}
        </div>
      </Field>
      <Field label="明暗外观" hint="深色模式适合低光环境；跟随系统会自动切换">
        <div className="appearance-options">
          {APPEARANCES.map((item) => {
            const Icon = appearanceIcons[item.id];
            return <button key={item.id} type="button" className={appearance === item.id ? 'on' : ''} onClick={() => pickAppearance(item.id)}><Icon size={14} />{item.label}</button>;
          })}
        </div>
      </Field>
    </div>
  );
}

function AccountSecurity() {
  const { user, changePassword } = useAuthContext();
  const [oldPw, setOldPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    const result = await changePassword(oldPw, newPw);
    setBusy(false);
    if (!result.success) {
      setMsg([result.error, ...(result.violations || [])].filter(Boolean).join('；'));
    }
    // 成功时 useAuth 会自动登出并回到登录页
  };

  return (
    <div className="card">
      <h3>账号安全</h3>
      <Field label="用户名"><span style={{ fontSize: 13, color: 'var(--ink2)' }}>{user?.username}</span></Field>
      <Field label="当前密码"><input type="password" value={oldPw} onChange={(e) => setOldPw(e.target.value)} /></Field>
      <Field label="新密码" hint="修改成功后需要重新登录"><input type="password" value={newPw} onChange={(e) => setNewPw(e.target.value)} /></Field>
      {msg && <div style={{ fontSize: 12.5, color: 'var(--crit)', margin: '8px 0' }}>{msg}</div>}
      <div className="foot">
        <button className="btn btn-p" disabled={busy || !oldPw || !newPw} onClick={submit}>修改密码</button>
      </div>
    </div>
  );
}

/* ---------------- 小组件 ---------------- */

function Toggle({ on, disabled, onChange }) {
  return (
    <button type="button" disabled={disabled} onClick={() => onChange(!on)} style={{
      width: 38, height: 22, borderRadius: 99, border: 'none', position: 'relative', flex: 'none',
      background: on ? 'var(--blue)' : '#d8d7d0', opacity: disabled ? 0.55 : 1,
      cursor: disabled ? 'not-allowed' : 'pointer', transition: 'background .15s',
    }}>
      <span style={{
        position: 'absolute', top: 2.5, left: on ? 18 : 3, width: 17, height: 17,
        borderRadius: '50%', background: '#fff', transition: 'left .15s',
      }} />
    </button>
  );
}

function LoadState({ error, onRetry }) {
  if (!error) return <div className="card"><SkeletonState rows={6} label="正在加载配置" /></div>;
  return (
    <div className="card">
      <div className="empty">
        <div>{error}</div>
        <button className="btn" style={{ marginTop: 12 }} onClick={onRetry}>重试</button>
      </div>
    </div>
  );
}

const inputS = (w) => ({ border: '1px solid var(--hair)', borderRadius: 8, padding: '7px 11px', width: w, background: '#fff' });
const btnS = { padding: '3px 10px', fontSize: 12, marginRight: 6 };
