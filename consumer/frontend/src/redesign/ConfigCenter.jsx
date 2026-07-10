import { useCallback, useEffect, useState } from 'react';
import { authFetch } from '../utils/auth';
import { useAuthContext } from './useAuthContext';
import { THEMES, applyTheme, getTheme } from './theme';

// 配置中心：三层作用域（L1 全局 / L2 工作空间 / L3 个人）。
// 全局写 = platform_admin；空间写 = admin（为 workspace_admin 预留）；个人 = 本人。
const SCOPES = {
  global: {
    lv: 'L1', badge: 'sb-l1', label: '全局配置',
    hint: '平台运行时能力，影响所有空间 · 写 platform_admin · 读 全员（secret 脱敏为 has_*）',
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
    hint: '当前空间的资源与运行行为 · 写 管理员（规划：workspace_admin 限所辖空间）· 读 空间成员',
    sections: [
      { id: 'info', name: '空间信息' },
      { id: 'members', name: '成员' },
      { id: 'model', name: '模型选用' },
      { id: 'conn', name: '数据连接' },
      { id: 'bind', name: '知识与数据绑定' },
      { id: 'runtime', name: '空间运行参数' },
    ],
  },
  personal: {
    lv: 'L3', badge: 'sb-l3', label: '个人设置',
    hint: '个人偏好，随账号走 · 仅本人可改，任何角色都可写',
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
    <div>
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
  }
  if (scope === 'personal') {
    if (section === 'prefs') return <PersonalPrefs />;
    if (section === 'account') return <AccountSecurity />;
  }
  return null;
}

/* ---------------- L1 · 系统配置各 section（/api/system-config） ---------------- */

const GLOBAL_SECTION_META = {
  llm: { title: '模型池', desc: '全局定义模型 profile，密钥仅存本层；空间通过“模型选用”引用', wiz: 'cfg-llm' },
  web_search: { title: '联网搜索', desc: '域名黑白名单为合规项，独占全局、不可覆盖' },
  observability: { title: '观测打点', desc: 'local / langfuse 双模式' },
  agent: { title: 'Agent 全局参数', desc: '全局硬顶：轮次 / 并发 / 深度 / 上下文' },
};

function SystemConfigSection({ section }) {
  const { can } = useAuthContext();
  const canEdit = can('system_config.write');
  const meta = GLOBAL_SECTION_META[section] || { title: section, desc: '' };
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(async () => {
    const res = await authFetch('/api/system-config');
    if (res.ok) {
      const data = await res.json();
      setConfig(data);
      setDraft({});
    }
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load]);

  if (!config) return <div className="card"><div className="empty">加载中…</div></div>;
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
          <Field label="允许域名" hint="逗号分隔，空为不限"><input type="text" disabled={!canEdit} value={val('allowed_domains')} onChange={(e) => set('allowed_domains', e.target.value)} /></Field>
          <Field label="屏蔽域名" hint="逗号分隔"><input type="text" disabled={!canEdit} value={val('blocked_domains')} onChange={(e) => set('blocked_domains', e.target.value)} /></Field>
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
          {[['main_agent_max_iterations', '主智能体最大轮次'], ['sub_agent_max_iterations', '子智能体最大轮次'],
            ['sub_agent_max_concurrency', '子智能体最大并发'], ['sub_agent_max_depth', '子智能体最大深度'],
            ['context_max_rounds', '上下文保留轮次'], ['llm_connection_max_retries', 'LLM 连接重试次数']].map(([k, label]) => (
              <Field key={k} label={label}>
                <input type="number" disabled={!canEdit} value={val(k, '')} onChange={(e) => set(k, Number(e.target.value))} />
              </Field>
            ))}
        </>
      )}
      {section !== 'llm' && canEdit && (
        <div className="foot">
          <span className="who">{msg}</span>
          <button className="btn" disabled={busy} onClick={() => setDraft({})}>重置</button>
          <button className="btn btn-p" disabled={busy || !Object.keys(draft).length} onClick={() => save(draft)}>保存修改</button>
        </div>
      )}
      {section === 'llm' && msg && <div className="foot"><span className="who">{msg}</span></div>}
    </div>
  );
}

function LlmProfiles({ data, canEdit, onSave, busy }) {
  const profiles = data.profiles || [];
  const [creating, setCreating] = useState(false);
  return (
    <>
      <Field label="默认模型" hint="可被空间级覆盖（规划能力）">
        <select disabled={!canEdit || busy} value={data.default_profile_id || ''}
          onChange={(e) => onSave({ default_profile_id: e.target.value })}>
          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}（{p.model}）</option>)}
        </select>
      </Field>
      {canEdit && !creating && (
        <div style={{ margin: '12px 0' }}>
          <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新增模型</button>
        </div>
      )}
      {creating && (
        <LlmProfileCreateForm
          existing={profiles}
          busy={busy}
          onSubmit={async (payload) => { await onSave(payload); setCreating(false); }}
          onCancel={() => setCreating(false)}
        />
      )}
      <table className="dt" style={{ marginTop: 10 }}>
        <thead><tr><th>名称</th><th>Provider</th><th>模型</th><th>Base URL</th><th>密钥</th><th>状态</th></tr></thead>
        <tbody>
          {profiles.map((p) => (
            <tr key={p.id}>
              <td style={{ fontWeight: 600 }}>{p.name}{p.is_default ? ' ★' : ''}</td>
              <td>{p.provider}</td>
              <td>{p.model}</td>
              <td style={{ color: 'var(--ink2)', fontSize: 12 }}>{p.base_url}</td>
              <td>{p.has_api_key ? <span className="tag tag-ok">已配置</span> : <span className="tag tag-draft">未配置</span>}</td>
              <td>{p.enabled ? '启用' : '停用'}</td>
            </tr>
          ))}
          {!profiles.length && <tr><td colSpan={6} className="empty">尚未配置模型 profile</td></tr>}
        </tbody>
      </table>
    </>
  );
}

// 新增模型 profile：提交完整 profiles 数组——已有项只带 {id}（后端按 id 合并、保留含密钥的全部旧值），
// 新增项带全字段；可选“设为默认”一并提交 default_profile_id。
const PROVIDER_PRESETS = {
  glm: { label: 'GLM（智谱）', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  deepseek: { label: 'DeepSeek', base_url: 'https://api.deepseek.com' },
  openai_compatible: { label: 'OpenAI 兼容', base_url: '' },
};

function LlmProfileCreateForm({ existing, busy, onSubmit, onCancel }) {
  const [form, setForm] = useState({
    id: '', name: '', provider: 'openai_compatible', model: '', base_url: '',
    api_key: '', timeout: 60, max_tokens: 4096, temperature: 0.7,
  });
  const [asDefault, setAsDefault] = useState(false);
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
    const id = form.id.trim() || `profile-${Date.now().toString(36)}`;
    if (!form.name.trim()) { setErr('显示名称必填'); return; }
    if (!form.model.trim()) { setErr('模型 ID 必填'); return; }
    if (/\s/.test(id) || id.length > 80) { setErr('Profile ID 不能含空白字符且不超过 80 字符'); return; }
    if (existing.some((p) => p.id === id)) { setErr(`Profile ID「${id}」已存在`); return; }
    setErr('');
    const newProfile = {
      id, name: form.name.trim(), provider: form.provider, model: form.model.trim(),
      base_url: form.base_url.trim(), timeout: Number(form.timeout) || 60,
      max_tokens: Number(form.max_tokens) || 4096, temperature: Number(form.temperature), enabled: true,
    };
    if (form.api_key) newProfile.api_key = form.api_key;
    const payload = { profiles: [...existing.map((p) => ({ id: p.id })), newProfile] };
    if (asDefault) payload.default_profile_id = id;
    await onSubmit(payload);
  };

  return (
    <div style={{ border: '1px solid var(--hair)', borderRadius: 12, padding: '6px 18px 14px', margin: '12px 0', background: '#fbfbf9' }}>
      <Field label="显示名称 *" hint="如 GLM-4.7"><input type="text" value={form.name} onChange={(e) => set('name', e.target.value)} /></Field>
      <Field label="Profile ID" hint="可选，留空自动生成；不能含空白字符">
        <input type="text" value={form.id} onChange={(e) => set('id', e.target.value)} /></Field>
      <Field label="Provider">
        <select value={form.provider} onChange={(e) => pickProvider(e.target.value)}>
          {Object.entries(PROVIDER_PRESETS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </select>
      </Field>
      <Field label="模型 ID *" hint="如 glm-4.7 / deepseek-chat"><input type="text" value={form.model} onChange={(e) => set('model', e.target.value)} /></Field>
      <Field label="Base URL"><input type="text" value={form.base_url} onChange={(e) => set('base_url', e.target.value)} /></Field>
      <Field label="API Key" hint="加密存储，不会回显；留空可稍后补配">
        <input type="password" value={form.api_key} onChange={(e) => set('api_key', e.target.value)} autoComplete="new-password" /></Field>
      <Field label="超时（秒）"><input type="number" value={form.timeout} onChange={(e) => set('timeout', e.target.value)} /></Field>
      <Field label="max_tokens"><input type="number" value={form.max_tokens} onChange={(e) => set('max_tokens', e.target.value)} /></Field>
      <Field label="temperature"><input type="number" step="0.1" value={form.temperature} onChange={(e) => set('temperature', e.target.value)} /></Field>
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

  const create = async () => {
    if (!form.name) { setMsg('名称必填'); return; }
    const res = await authFetch('/agentic/workspaces', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
    });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已创建' : `创建失败：${data.detail || res.status}`);
    if (res.ok) { setCreating(false); setForm({ id: '', name: '', description: '' }); reloadWorkspaces(); }
  };
  const setDefault = async (id) => {
    await authFetch(`/agentic/workspaces/${encodeURIComponent(id)}/default`, { method: 'POST' });
    reloadWorkspaces();
  };
  const remove = async (id) => {
    if (!window.confirm(`确定删除工作空间「${id}」？此操作不可撤销。`)) return;
    const res = await authFetch(`/agentic/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!res.ok) { const d = await res.json().catch(() => ({})); setMsg(`删除失败：${d.detail || res.status}`); }
    reloadWorkspaces();
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
                <button className="btn btn-p" onClick={create}>创建</button>
                <button className="btn" onClick={() => setCreating(false)}>取消</button>
              </div>
            )}
        </div>
      )}
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
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
                      <button className="btn" style={{ padding: '3px 10px', fontSize: 12, marginRight: 6 }} onClick={() => setDefault(w.id)}>设为默认</button>
                      <button className="btn" style={{ padding: '3px 10px', fontSize: 12 }} onClick={() => remove(w.id)}>删除</button>
                    </>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function UserMgmt() {
  const { can, user } = useAuthContext();
  const canManage = can('users.manage');
  const [users, setUsers] = useState([]);
  const [form, setForm] = useState({ username: '', password: '' });
  const [msg, setMsg] = useState('');

  const load = useCallback(async () => {
    if (!canManage) return;
    const res = await authFetch('/auth/users');
    if (res.ok) setUsers((await res.json()).users || []);
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
    const res = await authFetch('/auth/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(form),
    });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? `已创建 ${form.username}` : `失败：${data.error || res.status}`);
    if (res.ok) { setForm({ username: '', password: '' }); load(); }
  };
  const resetPw = async (username) => {
    const pw = window.prompt(`为 ${username} 设置新密码：`);
    if (!pw) return;
    const res = await authFetch(`/auth/users/${encodeURIComponent(username)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw }),
    });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已重置' : `失败：${data.error || res.status}`);
  };
  const remove = async (username) => {
    if (!window.confirm(`确定删除用户 ${username}？`)) return;
    const res = await authFetch(`/auth/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已删除' : `失败：${data.error || res.status}`);
    load();
  };

  return (
    <div className="card">
      <h3>用户与授权</h3>
      <div className="cd">新建用户默认为普通用户；角色分配与 workspace 角色授予为规划能力（对应权限重构 Phase 2/3）</div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <input type="text" placeholder="用户名" style={inputS(140)} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        <input type="password" placeholder="初始密码" style={inputS(160)} value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        <button className="btn btn-p" disabled={!form.username || !form.password} onClick={create}>＋ 新建用户</button>
        <span style={{ fontSize: 12.5, color: 'var(--ink2)' }}>{msg}</span>
      </div>
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
                    <button className="btn" style={{ padding: '3px 10px', fontSize: 12, marginRight: 6 }} onClick={() => resetPw(u.username)}>重置密码</button>
                    {u.role !== 'admin' && <button className="btn" style={{ padding: '3px 10px', fontSize: 12 }} onClick={() => remove(u.username)}>删除</button>}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
      <div className="cd">仅名称与描述；空间的增删 / 设默认在 全局配置 → 工作空间管理</div>
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
      <div className="cd">空间归属与成员名单；owner 变更仅平台管理员可操作（在后端 PATCH 中）</div>
      {!canEdit && <ReadOnlyBanner who="平台管理员 / 空间 owner" />}
      <Field label="Owner" hint="空间负责人，自动获得空间管理权限">
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
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
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
          {!members.length && <tr><td colSpan={canEdit ? 2 : 1} className="empty">暂无成员{ws?.owner ? '（仅 owner 与管理员可访问）' : ''}</td></tr>}
        </tbody>
      </table>
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
  const storageKey = `mw_ws_model_${workspaceId}`;
  const [selected, setSelected] = useState(() => localStorage.getItem(storageKey) || '');

  useEffect(() => {
    (async () => {
      const res = await authFetch('/agentic/model-profiles');
      if (res.ok) {
        const data = await res.json();
        setProfiles((data.profiles || []).filter((p) => p.enabled));
        setDefaultId(data.default_profile_id || '');
      }
    })();
  }, []);
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
      <div className="cd">从全局模型池引用本空间默认模型，不接触密钥。对话默认使用此模型，可在会话内临时切换。</div>
      <Field label="本空间默认模型" hint="留空则跟随全局默认">
        <select value={selected} onChange={(e) => pick(e.target.value)}>
          <option value="">跟随全局{defaultId ? `（${profiles.find((p) => p.id === defaultId)?.name || defaultId}）` : ''}</option>
          {profiles.map((p) => <option key={p.id} value={p.id}>{p.name}（{p.model}）</option>)}
        </select>
      </Field>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
        说明：空间级默认模型的服务端存储对应权限重构 Phase 2 的 config registry；当前版本先存于浏览器本地并在新建会话时生效。
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

  const load = useCallback(async () => {
    const res = await authFetch('/agentic/data-connections');
    if (res.ok) {
      const data = await res.json();
      setConns(data.connections || []);
      setDefaultId(data.default_connection_id || '');
    }
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, workspaceId]);

  const test = async (id) => {
    setMsg(`测试 ${id}…`);
    const res = await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}/test`, { method: 'POST' });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? `${id}: ${data.success ? '✓ 可用' : '✗ 不可用'}${data.message ? ` · ${data.message}` : ''}` : `测试失败：${data.detail || res.status}`);
  };
  const setDefault = async (id) => {
    await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}/default`, { method: 'POST' });
    load();
  };
  const remove = async (id) => {
    if (!window.confirm(`确定删除连接「${id}」？`)) return;
    const res = await authFetch(`/agentic/data-connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) setMsg(data.bound_ontologies ? `无法删除：已被知识包绑定（${data.bound_ontologies.join(', ')}）` : `删除失败：${data.detail || res.status}`);
    load();
  };

  return (
    <div className="card" data-wiz="cfg-conn">
      <h3>数据连接</h3>
      <div className="cd">当前工作空间的业务数据库连接；测试连接为写侧权限</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      {canEdit && !creating && (
        <div style={{ marginBottom: 12 }}>
          <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新建连接</button>
        </div>
      )}
      {creating && (
        <ConnectionCreateForm
          onDone={(text) => { setCreating(false); if (text) setMsg(text); load(); }}
          onCancel={() => setCreating(false)}
        />
      )}
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
      <table className="dt">
        <thead><tr><th>连接</th><th>Provider</th><th>默认</th>{canEdit && <th style={{ width: 220 }}>操作</th>}</tr></thead>
        <tbody>
          {conns.map((c) => (
            <tr key={c.id}>
              <td style={{ fontWeight: 600 }}>{c.name} <span style={{ color: 'var(--muted)', fontWeight: 400 }}>({c.id})</span></td>
              <td>{c.provider}</td>
              <td>{c.id === defaultId ? '★ 默认' : ''}</td>
              {canEdit && (
                <td>
                  <button className="btn" style={btnS} onClick={() => test(c.id)}>测试</button>
                  {c.id !== defaultId && <button className="btn" style={btnS} onClick={() => setDefault(c.id)}>设默认</button>}
                  <button className="btn" style={btnS} onClick={() => remove(c.id)}>删除</button>
                </td>
              )}
            </tr>
          ))}
          {!conns.length && <tr><td colSpan={4} className="empty">当前空间暂无数据连接</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

// 新建连接表单：provider 字段由后端 provider-schemas 下发，按 schema 动态渲染
function ConnectionCreateForm({ onDone, onCancel }) {
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState('');
  const [name, setName] = useState('');
  const [connId, setConnId] = useState('');
  const [asDefault, setAsDefault] = useState(false);
  const [config, setConfig] = useState({});
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const t = setTimeout(async () => {
      const res = await authFetch('/agentic/business-data/provider-schemas');
      if (res.ok) {
        const data = await res.json();
        const list = data.providers || [];
        setProviders(list);
        if (list.length) setProvider((prev) => prev || list[0].name);
      }
    }, 0);
    return () => clearTimeout(t);
  }, []);

  const schema = providers.find((p) => p.name === provider);

  const pickProvider = (nextName) => {
    setProvider(nextName);
    const next = providers.find((p) => p.name === nextName);
    const defaults = {};
    (next?.fields || []).forEach((f) => { if (f.default !== undefined && f.default !== null) defaults[f.name] = f.default; });
    setConfig(defaults);
  };

  const submit = async () => {
    if (!name.trim()) { setErr('名称必填'); return; }
    const missing = (schema?.fields || []).filter((f) => f.required && (config[f.name] === undefined || config[f.name] === ''));
    if (missing.length) { setErr(`缺少必填字段：${missing.map((f) => f.label || f.name).join('、')}`); return; }
    setBusy(true);
    setErr('');
    try {
      const body = { name: name.trim(), provider, config };
      if (connId.trim()) body.id = connId.trim();
      if (asDefault) body.set_as_default = true;
      const res = await authFetch('/agentic/data-connections', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok) onDone(`已创建连接 ${data.connection?.name || name}`);
      else setErr(`创建失败：${data.detail || data.error || res.status}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ border: '1px solid var(--hair)', borderRadius: 12, padding: '6px 18px 14px', marginBottom: 14, background: '#fbfbf9' }}>
      <Field label="名称" hint="必填，展示用"><input type="text" value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <Field label="连接 ID" hint="可选，留空自动生成；创建后不可改"><input type="text" value={connId} onChange={(e) => setConnId(e.target.value)} /></Field>
      <Field label="Provider" hint={schema?.description || ''}>
        <select value={provider} onChange={(e) => pickProvider(e.target.value)}>
          {providers.map((p) => <option key={p.name} value={p.name}>{p.label || p.name}</option>)}
        </select>
      </Field>
      {(schema?.fields || []).map((f) => (
        <Field key={f.name} label={`${f.label || f.name}${f.required ? ' *' : ''}`} hint={f.secret ? '加密存储，不会回显' : ''}>
          {f.type === 'bool' ? (
            <Toggle on={!!config[f.name]} onChange={(v) => setConfig((c) => ({ ...c, [f.name]: v }))} />
          ) : (
            <input
              type={f.secret ? 'password' : f.type === 'int' ? 'number' : 'text'}
              value={config[f.name] ?? ''}
              onChange={(e) => setConfig((c) => ({ ...c, [f.name]: f.type === 'int' ? Number(e.target.value) : e.target.value }))}
            />
          )}
        </Field>
      ))}
      <Field label="设为默认连接"><Toggle on={asDefault} onChange={setAsDefault} /></Field>
      {err && <div style={{ fontSize: 12.5, color: 'var(--crit)', margin: '8px 0' }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
        <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
        <button className="btn btn-p" disabled={busy || !provider} onClick={submit}>{busy ? '创建中…' : '创建连接'}</button>
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

  const load = useCallback(async () => {
    const [or, cr] = await Promise.all([
      authFetch('/agentic/ontologies'),
      authFetch('/agentic/data-connections'),
    ]);
    if (or.ok) setPkgs((await or.json()).ontologies || []);
    if (cr.ok) setConns((await cr.json()).connections || []);
  }, []);
  useEffect(() => { const t = setTimeout(load, 0); return () => clearTimeout(t); }, [load, workspaceId]);

  const bind = async (ontologyId, connectionId) => {
    const url = `/agentic/ontologies/${encodeURIComponent(ontologyId)}/data-binding`;
    const res = connectionId
      ? await authFetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ connection_id: connectionId }) })
      : await authFetch(url, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    setMsg(res.ok ? '已更新绑定' : `失败：${data.detail || data.error || res.status}`);
    load();
  };

  return (
    <div className="card" data-wiz="cfg-bind">
      <h3>知识与数据绑定</h3>
      <div className="cd">知识包 ↔ 数据连接映射：知识包中的业务对象经此映射查询真实业务数据</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      {msg && <div style={{ fontSize: 12.5, color: 'var(--ink2)', marginBottom: 8 }}>{msg}</div>}
      {pkgs.map((p) => (
        <Field key={p.id} label={p.name || p.id} hint={p.id}>
          <select disabled={!canEdit} value={p.business_data?.connection_id || ''} onChange={(e) => bind(p.id, e.target.value)}>
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
  const [cfg, setCfg] = useState(null);
  const [draft, setDraft] = useState(null);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    (async () => {
      const res = await authFetch('/agentic/workspaces/current/runtime-config');
      if (res.ok) setCfg(await res.json());
    })();
  }, [workspaceId]);

  if (!cfg) return <div className="card"><div className="empty">加载中…</div></div>;
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
    if (res.ok) { setCfg(data); setDraft(null); }
  };

  return (
    <div className="card">
      <h3>空间运行参数</h3>
      <div className="cd">回复语言兜底与 ACP 工具结果隐藏</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      <Field label="启用语言控制">
        <Toggle on={current.lang.enabled} disabled={!canEdit} onChange={(v) => setDraft({ ...current, lang: { ...current.lang, enabled: v } })} />
      </Field>
      <Field label="兜底语言" hint="优先级：请求 › 用户偏好 › 此兜底">
        <select disabled={!canEdit} value={current.lang.default} onChange={(e) => setDraft({ ...current, lang: { ...current.lang, default: e.target.value } })}>
          <option value="zh">zh</option><option value="en">en</option>
        </select>
      </Field>
      <Field label="隐藏工具结果关键词" hint="逗号分隔，仅影响 ACP 流式响应">
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

/* ---------------- L3 · 个人设置 ---------------- */

function PersonalPrefs() {
  const { workspaces, workspaceId, switchWorkspace } = useAuthContext();
  const [lang, setLang] = useState(() => localStorage.getItem('mw_pref_reply_lang') || '');
  const [theme, setTheme] = useState(() => getTheme());

  const pickTheme = (id) => setTheme(applyTheme(id));

  return (
    <div className="card" data-wiz="cfg-prefs">
      <h3>偏好</h3>
      <div className="cd">个人偏好随账号走；服务端存储（/auth/me/preferences）对应权限重构 Phase 2，当前先存于浏览器本地</div>
      <Field label="当前工作空间" hint="切换后全站生效">
        <select value={workspaceId || ''} onChange={(e) => switchWorkspace(e.target.value)}>
          {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}（{w.id}）</option>)}
        </select>
      </Field>
      <Field label="回复语言偏好" hint="覆盖空间兜底语言">
        <select value={lang} onChange={(e) => { setLang(e.target.value); localStorage.setItem('mw_pref_reply_lang', e.target.value); }}>
          <option value="">跟随空间</option><option value="zh">中文</option><option value="en">English</option>
        </select>
      </Field>
      <Field label="主题配色" hint="即时生效，全站应用">
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {THEMES.map((t) => (
            <button key={t.id} type="button" onClick={() => pickTheme(t.id)} title={t.label}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '6px 13px', borderRadius: 99,
                border: theme === t.id ? `2px solid ${t.color}` : '1px solid var(--hair)',
                background: '#fff', fontSize: 12.5, fontWeight: 600,
                color: theme === t.id ? t.color : 'var(--ink2)',
              }}>
              <span style={{ width: 14, height: 14, borderRadius: '50%', background: t.color, flex: 'none' }} />
              {t.label}
            </button>
          ))}
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

const inputS = (w) => ({ border: '1px solid var(--hair)', borderRadius: 8, padding: '7px 11px', width: w, background: '#fff' });
const btnS = { padding: '3px 10px', fontSize: 12, marginRight: 6 };
