import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { authFetch } from '../shared/api/auth';
import { useAuthContext } from './useAuthContext';
import {
  APPEARANCES,
  THEMES,
  applyAppearance,
  applyTheme,
  getAppearance,
  getTheme,
} from './theme';
import { FieldRow as Field, ScopeTabs, SectionCard } from './ui';

const ChangePasswordDialog = lazy(() => import('../features/auth/ChangePasswordDialog'));
const McpSettingsPanel = lazy(() => import('../features/settings/mcp/McpSettingsPanel'));
const SystemSettingsPanel = lazy(() => import('../features/settings/system/SystemSettingsPanel'));
const UserAdminPanel = lazy(() => import('../features/settings/admin/UserAdminPanel'));
const WorkspaceAdminPanel = lazy(() => import('../features/settings/admin/WorkspaceAdminPanel'));
const GlobalModelsPanel = lazy(() => import('../features/settings/models/ModelSettings')
  .then((module) => ({ default: module.GlobalModelsPanel })));
const WorkspaceModelsPanel = lazy(() => import('../features/settings/models/ModelSettings')
  .then((module) => ({ default: module.WorkspaceModelsPanel })));

// 配置中心：三层作用域（L1 全局 / L2 工作空间 / L3 个人）。
const SCOPES = {
  global: {
    lv: 'L1', badge: 'sb-l1', label: '全局配置',
    hint: '平台级配置，影响所有工作空间；仅平台管理员可修改，其他人可查看',
    sections: [
      { id: 'llm', name: '模型池' },
      { id: 'system', name: '系统配置' },
      { id: 'platform', name: '平台接入' },
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
      { id: 'wsobs', name: '空间观测' },
      { id: 'mcp', name: 'MCP 接入' },
    ],
  },
  personal: {
    lv: 'L3', badge: 'sb-l3', label: '个人设置',
    hint: '仅对你自己生效的个人偏好，随账号同步',
    sections: [
      { id: 'prefs', name: '偏好' },
      { id: 'memory', name: '长期记忆' },
      { id: 'account', name: '账号安全' },
    ],
  },
};

export default function ConfigCenter({ scope, section, onNavigate }) {
  const { workspaceId } = useAuthContext();
  const sc = SCOPES[scope] || SCOPES.global;
  const sections = sc.sections;
  const active = sections.find((s) => s.id === section) || sections[0];
  const workspaceScopedSection = scope === 'workspace' || (scope === 'personal' && active.id === 'memory');

  return (
    <div>
      <ScopeTabs
        value={scope}
        onChange={(key) => onNavigate({ scope: key, sec: SCOPES[key].sections[0].id })}
        items={Object.entries(SCOPES).map(([key, value]) => ({
          id: key,
          label: value.label,
          badge: value.lv,
          badgeClass: value.badge,
        }))}
      />
      <div className="scope-hint">{sc.hint}</div>
      <div className="cfg">
        <div className="subnav">
          {sections.map((s) => (
            <button key={s.id} className={s.id === active.id ? 'on' : ''}
              onClick={() => onNavigate({ scope, sec: s.id })}>{s.name}</button>
          ))}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <Suspense fallback={<SectionCard><div className="empty" role="status">正在加载配置模块…</div></SectionCard>}>
            <SectionBody
              key={workspaceScopedSection ? `${workspaceId}:${active.id}` : `${scope}:${active.id}`}
              scope={scope}
              section={active.id}
            />
          </Suspense>
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

function SectionBody({ scope, section }) {
  const {
    can,
    changePassword,
    reloadWorkspaces,
    switchWorkspace,
    user,
    workspaceId,
    workspaces,
  } = useAuthContext();
  const workspace = workspaces.find((item) => item.id === workspaceId);
  if (scope === 'global') {
    if (section === 'llm') {
      return <GlobalModelsPanel canEdit={can('system_config.write')} workspaces={workspaces} />;
    }
    if (section === 'system') {
      return <SystemSettingsPanel canEdit={can('system_config.write')} />;
    }
    if (section === 'wsmgmt') {
      return (
        <WorkspaceAdminPanel
          currentUser={user}
          currentWorkspaceId={workspaceId}
          onWorkspaceChange={switchWorkspace}
          onWorkspacesChanged={reloadWorkspaces}
        />
      );
    }
    if (section === 'users') return <UserAdminPanel currentUser={user} canManage={can('users.manage')} />;
    if (section === 'platform') return <PlatformAccess />;
  }
  if (scope === 'workspace') {
    if (section === 'info') return <WorkspaceInfo />;
    // key=workspaceId：切换空间时整体重挂，丢弃未保存的成员草稿，避免切回复活旧草稿覆盖他人变更
    if (section === 'members') return <WorkspaceMembers key={workspaceId} />;
    if (section === 'model') {
      return <WorkspaceModelsPanel workspaceId={workspaceId} canEdit={can('ws.config.write')} />;
    }
    if (section === 'conn') return <DataConnections />;
    if (section === 'bind') return <KnowledgeBinding />;
    if (section === 'runtime') return <WorkspaceRuntime />;
    if (section === 'wsobs') return <WorkspaceObservability />;
    if (section === 'mcp') {
      return (
        <McpSettingsPanel
          workspaceId={workspaceId}
          workspaceName={workspace?.name || workspaceId}
          authenticated={Boolean(user?.username)}
          canManageTokens={Boolean(user?.username && workspaceId)}
        />
      );
    }
  }
  if (scope === 'personal') {
    if (section === 'prefs') return <PersonalPrefs />;
    if (section === 'memory') return <PersonalMemory />;
    if (section === 'account') {
      return <AccountSecurity user={user} changePassword={changePassword} />;
    }
  }
  return null;
}

function PlatformAccess() {
  const { can } = useAuthContext();
  const canEdit = can('system_config.write');
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');
  const effective = { ...(config || {}), ...draft };
  const load = useCallback(async () => {
    const response = await authFetch('/api/platform-config');
    if (response.ok) { setConfig(await response.json()); setDraft({}); }
  }, []);
  useEffect(() => { const timer = setTimeout(load, 0); return () => clearTimeout(timer); }, [load]);
  const set = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const request = async (path) => {
    const payload = { ...draft };
    if (!payload.api_key) delete payload.api_key;
    const response = await authFetch(path, {
      method: path.endsWith('/test') ? 'POST' : 'PATCH',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    setMsg(response.ok ? (data.message || '已保存') : `操作失败：${data.detail || data.error || response.status}`);
    if (response.ok && !path.endsWith('/test')) await load();
  };
  if (!config) return <SectionCard><div className="empty">加载中…</div></SectionCard>;
  return (
    <SectionCard title="onto-platform 接入" description="统一配置运行实例注册、策略快照和 Trace 上报目标。">
      {!canEdit && <ReadOnlyBanner who="平台管理员" />}
      <Field label="启用平台接入"><Toggle on={Boolean(effective.enabled)} disabled={!canEdit} onChange={(value) => set('enabled', value)} /></Field>
      <Field label="平台地址"><input disabled={!canEdit} value={effective.base_url || ''} onChange={(event) => set('base_url', event.target.value)} /></Field>
      <Field label="环境标识"><input disabled={!canEdit} value={effective.environment_id || ''} onChange={(event) => set('environment_id', event.target.value)} /></Field>
      <Field label="实例标识" hint="留空时启用后自动生成"><input disabled={!canEdit} value={effective.instance_id || ''} onChange={(event) => set('instance_id', event.target.value)} /></Field>
      <Field label="组织标识"><input disabled={!canEdit} value={effective.organization_id || ''} onChange={(event) => set('organization_id', event.target.value)} /></Field>
      <Field label="环境 Key" hint={config.has_api_key ? '已配置，留空保留' : '尚未配置'}><input type="password" disabled={!canEdit} value={draft.api_key || ''} onChange={(event) => set('api_key', event.target.value)} /></Field>
      <Field label="批量上报条数"><input type="number" disabled={!canEdit} value={effective.batch_size || 20} onChange={(event) => set('batch_size', Number(event.target.value))} /></Field>
      <div className="foot"><span className="who">{msg}</span>{canEdit && <><button className="btn" onClick={() => request('/api/platform-config/test')}>测试连接</button><button className="btn btn-p" disabled={!Object.keys(draft).length} onClick={() => request('/api/platform-config')}>保存</button></>}</div>
    </SectionCard>
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
          {!members.length && <tr><td colSpan={canEdit ? 2 : 1} className="empty">暂无成员{ws?.owner ? '（仅空间负责人与管理员可访问）' : ''}</td></tr>}
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

function DataConnections() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.connections.write');
  const [conns, setConns] = useState([]);
  const [defaultId, setDefaultId] = useState('');
  const [msg, setMsg] = useState('');
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(null); // 正在编辑的连接（null=未编辑）

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
      <div className="cd">当前工作空间可用的业务数据库连接</div>
      {!canEdit && <ReadOnlyBanner who="空间管理员" />}
      {canEdit && !creating && !editing && (
        <div style={{ marginBottom: 12 }}>
          <button className="btn btn-p" onClick={() => setCreating(true)}>＋ 新建连接</button>
        </div>
      )}
      {(creating || editing) && (
        <ConnectionForm
          initial={editing}
          onDone={(text) => { setCreating(false); setEditing(null); if (text) setMsg(text); load(); }}
          onCancel={() => { setCreating(false); setEditing(null); }}
        />
      )}
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
                  <button className="btn" style={btnS} onClick={() => { setEditing(c); setCreating(false); }}>编辑</button>
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
      const res = await authFetch('/agentic/business-data/provider-schemas');
      if (res.ok) {
        const data = await res.json();
        const list = data.providers || [];
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
      {!isEdit && <Field label="设为默认连接"><Toggle on={asDefault} onChange={setAsDefault} /></Field>}
      {err && <div style={{ fontSize: 12.5, color: 'var(--crit)', margin: '8px 0' }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 10 }}>
        <button className="btn" disabled={busy} onClick={onCancel}>取消</button>
        <button className="btn btn-p" disabled={busy || !provider} onClick={submit}>{busy ? '保存中…' : (isEdit ? '保存修改' : '创建连接')}</button>
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
      <div className="cd">为每个知识包指定要查询的数据连接，让它能读到真实业务数据</div>
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

function WorkspaceObservability() {
  const { can, workspaceId } = useAuthContext();
  const canEdit = can('ws.observability.write');
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState({});
  const [msg, setMsg] = useState('');
  const effective = {
    ...(config || {}),
    ...draft,
    local: { ...(config?.local || {}), ...(draft.local || {}) },
    langfuse: { ...(config?.langfuse || {}), ...(draft.langfuse || {}) },
  };
  const load = useCallback(async () => {
    const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}/observability-config`);
    if (response.ok) { setConfig(await response.json()); setDraft({}); }
  }, [workspaceId]);
  useEffect(() => { const timer = setTimeout(load, 0); return () => clearTimeout(timer); }, [load]);
  const set = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const setNested = (section, key, value) => setDraft((current) => ({
    ...current,
    [section]: { ...(current[section] || {}), [key]: value },
  }));
  const submit = async (testOnly = false) => {
    const payload = { ...draft };
    if (payload.langfuse && !payload.langfuse.secret_key) {
      payload.langfuse = { ...payload.langfuse };
      delete payload.langfuse.secret_key;
    }
    const suffix = testOnly ? '/test' : '';
    const response = await authFetch(`/agentic/workspaces/${encodeURIComponent(workspaceId)}/observability-config${suffix}`, {
      method: testOnly ? 'POST' : 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    setMsg(response.ok ? (data.message || '已保存') : `操作失败：${data.detail || data.error || response.status}`);
    if (response.ok && !testOnly) await load();
  };
  if (!config) return <SectionCard><div className="empty">加载中…</div></SectionCard>;
  return (
    <SectionCard title="工作空间观测" description="为当前空间选择本地日志或独立 Langfuse 项目。">
      {!canEdit && <ReadOnlyBanner who="平台管理员" />}
      <Field label="启用观测"><Toggle on={Boolean(effective.enabled)} disabled={!canEdit} onChange={(value) => set('enabled', value)} /></Field>
      <Field label="观测模式"><select disabled={!canEdit} value={effective.mode || 'local'} onChange={(event) => set('mode', event.target.value)}><option value="local">本地日志</option><option value="langfuse">Langfuse</option></select></Field>
      {effective.mode === 'local' ? (
        <Field label="日志保留天数"><input type="number" disabled={!canEdit} value={effective.local.retention_days || 30} onChange={(event) => setNested('local', 'retention_days', Number(event.target.value))} /></Field>
      ) : (
        <>
          <Field label="Langfuse 地址"><input disabled={!canEdit} value={effective.langfuse.base_url || ''} onChange={(event) => setNested('langfuse', 'base_url', event.target.value)} /></Field>
          <Field label="Public Key"><input disabled={!canEdit} value={effective.langfuse.public_key || ''} onChange={(event) => setNested('langfuse', 'public_key', event.target.value)} /></Field>
          <Field label="Secret Key" hint={config.langfuse?.has_secret_key ? '已配置，留空保留' : '尚未配置'}><input type="password" disabled={!canEdit} value={draft.langfuse?.secret_key || ''} onChange={(event) => setNested('langfuse', 'secret_key', event.target.value)} /></Field>
        </>
      )}
      <div className="foot"><span className="who">{msg}</span>{canEdit && <><button className="btn" onClick={() => submit(true)}>测试</button><button className="btn btn-p" disabled={!Object.keys(draft).length} onClick={() => submit(false)}>保存</button></>}</div>
    </SectionCard>
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
            <button className="theme-choice" key={t.id} type="button" onClick={() => pickTheme(t.id)} title={t.label}
              aria-pressed={theme === t.id}
              style={{
                border: theme === t.id ? `2px solid ${t.color}` : '1px solid var(--hair)',
                color: theme === t.id ? t.color : 'var(--ink2)',
              }}>
              <span style={{ width: 14, height: 14, borderRadius: '50%', background: t.color, flex: 'none' }} />
              {t.label}
            </button>
          ))}
        </div>
      </Field>
      <Field label="外观模式" hint="跟随系统会响应操作系统的浅色或深色变化">
        <div className="appearance-options">
          {APPEARANCES.map((item) => (
            <button className={appearance === item.id ? 'on' : ''} key={item.id} type="button"
              aria-pressed={appearance === item.id} onClick={() => pickAppearance(item.id)}>
              {item.label}
            </button>
          ))}
        </div>
      </Field>
    </div>
  );
}

function PersonalMemory() {
  const { workspaceId } = useAuthContext();
  const [memories, setMemories] = useState([]);
  const [enabled, setEnabled] = useState(true);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(null);
  const [msg, setMsg] = useState('');
  const load = useCallback(async () => {
    const response = await authFetch('/agentic/memories?limit=200');
    const payload = await response.json().catch(() => ({}));
    if (response.ok) {
      setMemories(payload.memories || []);
      setEnabled(payload.memory_enabled !== false);
    } else setMsg(payload.error || `加载失败：${response.status}`);
  }, []);
  useEffect(() => { const timer = setTimeout(load, 0); return () => clearTimeout(timer); }, [load, workspaceId]);
  const save = async () => {
    const content = draft.trim();
    if (content.length < 8) { setMsg('记忆内容至少 8 个字'); return; }
    const response = await authFetch(editing ? `/agentic/memories/${editing.id}` : '/agentic/memories', {
      method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
    });
    const payload = await response.json().catch(() => ({}));
    setMsg(response.ok ? '记忆已保存' : `保存失败：${payload.error || response.status}`);
    if (response.ok) { setDraft(''); setEditing(null); await load(); }
  };
  const remove = async (memory) => {
    if (!window.confirm('删除这条长期记忆？')) return;
    const response = await authFetch(`/agentic/memories/${memory.id}`, { method: 'DELETE' });
    setMsg(response.ok ? '记忆已删除' : '删除失败');
    if (response.ok) await load();
  };
  return (
    <SectionCard title="长期记忆" description="查看、补充和修订当前账号的偏好与画像记忆。">
      {!enabled && <div className="ro-banner">当前运行环境未启用长期记忆。</div>}
      {enabled && (
        <div className="memory-editor">
          <textarea rows={3} value={draft} placeholder="例如：我偏好用中文输出，并把风险项放在结论前。" onChange={(event) => setDraft(event.target.value)} />
          <div><button className="btn" disabled={!editing} onClick={() => { setEditing(null); setDraft(''); }}>取消编辑</button><button className="btn btn-p" disabled={draft.trim().length < 8} onClick={save}>{editing ? '保存修改' : '新增记忆'}</button></div>
        </div>
      )}
      <table className="dt">
        <thead><tr><th>类型</th><th>记忆内容</th><th>置信度</th><th>操作</th></tr></thead>
        <tbody>
          {memories.map((memory) => (
            <tr key={memory.id}>
              <td><span className="tag tag-lang">{memory.memory_type || 'preference'}</span></td>
              <td>{memory.content}</td>
              <td>{memory.confidence !== undefined ? Number(memory.confidence).toFixed(2) : '—'}</td>
              <td><button className="btn" style={btnS} onClick={() => { setEditing(memory); setDraft(memory.content || ''); }}>编辑</button><button className="btn btn-danger" style={btnS} onClick={() => remove(memory)}>删除</button></td>
            </tr>
          ))}
          {!memories.length && <tr><td className="empty" colSpan={4}>暂无长期记忆</td></tr>}
        </tbody>
      </table>
      {msg && <div className="who" style={{ marginTop: 10 }}>{msg}</div>}
    </SectionCard>
  );
}

function AccountSecurity({ user, changePassword }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  return (
    <div className="card">
      <h3>账号安全</h3>
      <p className="muted">密码变更成功后会退出当前会话，需要使用新密码重新登录。</p>
      <Field label="用户名">
        <span style={{ fontSize: 13, color: 'var(--ink2)' }}>{user?.username}</span>
      </Field>
      <div className="foot">
        <button className="btn btn-p" type="button" onClick={() => setDialogOpen(true)}>修改密码</button>
      </div>
      {dialogOpen && (
        <ChangePasswordDialog
          currentUser={user}
          onChangePassword={changePassword}
          onClose={() => setDialogOpen(false)}
        />
      )}
    </div>
  );
}

/* ---------------- 小组件 ---------------- */

function Toggle({ on, disabled, onChange, ...accessibilityProps }) {
  return (
    <button type="button" disabled={disabled} aria-pressed={on} {...accessibilityProps}
      onClick={() => onChange(!on)} style={{
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
